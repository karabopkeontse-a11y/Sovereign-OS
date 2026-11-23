"""Resilient multi-provider model routing for SovereignOS.

Providers are opt-in through environment variables. The router never selects an
unconfigured provider and never enables a paid provider implicitly. Provider
cooldowns are process-local and survive individual chat turns while this router
singleton remains alive.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
import re
import time
import random
from email.utils import parsedate_to_datetime


@dataclass(frozen=True)
class ModelInfo:
    provider: str
    model: str
    runtime: str  # "cloud" or "local"
    fallback: bool = False

    @property
    def label(self) -> str:
        suffix = " · fallback after quota" if self.fallback else ""
        return f"{self.runtime.title()} · {self.provider} · {self.model}{suffix}"


@dataclass(frozen=True)
class ModelCandidate:
    info: ModelInfo
    client: object


class ModelRoutingError(RuntimeError):
    def __init__(self, message: str, failures: list[str] | None = None,
                 checkout_url: str = ""):
        super().__init__(message)
        self.failures = failures or []
        self.checkout_url = checkout_url
        self.premium_required = True


def _status_code(exc: Exception):
    status = getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    if status is None and response is not None:
        status = getattr(response, "status_code", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def is_capacity_error(exc: Exception) -> bool:
    """Return whether another configured model/provider is a sensible fallback.

    Authentication and malformed-request errors are configuration/input faults,
    not capacity faults. They must surface immediately instead of causing a
    misleading provider cooldown or repeated failover on every request.
    """
    status = _status_code(exc)
    name, message = type(exc).__name__.casefold(), str(exc).casefold()

    # Transient transport/provider failures and explicit quota exhaustion.
    if status in {402, 408, 409, 425, 429}:
        return True
    if status is not None and status >= 500:
        return True
    if any(word in name for word in ("timeout", "connection", "ratelimit")):
        return True

    # Model-specific incompatibilities may be resolved by another candidate.
    model_markers = (
        "model_not_found", "model not found", "unknown model",
        "decommissioned", "no endpoints found",
        "maximum context length", "context_length_exceeded",
        "context window", "tool calling is not supported",
        "does not support tool calling", "tool use is not supported",
        "unsupported tool", "vision is not supported",
        "image input is not supported", "unsupported image",
    )
    if any(marker in message for marker in model_markers):
        return True

    # Some providers report quota/rate limiting in the body without a useful
    # status code. Do not include generic "capacity" words from auth errors.
    quota_markers = (
        "rate limit", "rate_limit", "too many requests", "quota exceeded",
        "insufficient_quota", "resource_exhausted", "tokens per minute",
        "requests per minute", "temporarily unavailable",
        "service unavailable", "timed out", "connection refused",
        "connection reset", "overloaded",
    )
    if any(marker in message for marker in quota_markers):
        return True

    # A 404 is fallback-eligible only when the provider explicitly says the
    # requested model ID is unavailable. Generic route/endpoint/path 404s are
    # usually URL or deployment configuration faults and must surface directly.
    if status == 404 and any(marker in message for marker in (
        "model_not_found", "model not found", "unknown model",
        "model does not exist", "no such model",
    )):
        return True
    return False


def _retry_after_seconds(exc: Exception) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", {}) or {}
    value = headers.get("retry-after") or headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(1.0, min(float(value), 86400.0))
    except (TypeError, ValueError):
        try:
            when = parsedate_to_datetime(str(value))
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            return max(1.0, min((when - datetime.now(timezone.utc)).total_seconds(), 86400.0))
        except Exception:
            return None


def _supports_vision(candidate: ModelCandidate) -> bool:
    """Conservative model-ID capability check for image-input routing."""
    model = candidate.info.model.casefold()
    markers = (
        "vision", "-vl", ":vl", "gpt-4o", "gpt-4.1", "gpt-4.5",
        "gemini", "claude-3", "claude-sonnet-4", "pixtral", "llava",
        "internvl", "qwen2-vl", "qwen2.5-vl", "qwen-vl", "minicpm-v",
        "molmo", "idefics", "phi-3.5-vision", "phi-4-multimodal",
    )
    return any(marker in model for marker in markers)


class ModelRouter:
    """Ordered model candidates with provider-level circuit breakers."""

    def __init__(self, max_tokens: int = 800, temperature: float = 0.7,
                 environ=None, clock=None, sleeper=None, random_source=None):
        self.env = os.environ if environ is None else environ
        self.max_tokens = int(max_tokens)
        self.temperature = float(temperature)
        try:
            self.request_timeout = max(1.0, float(
                self.env.get("SOVEREIGN_MODEL_TIMEOUT_SECONDS", "45")
            ))
        except (TypeError, ValueError):
            self.request_timeout = 45.0
        self.checkout_url = self.env.get("SOVEREIGN_PREMIUM_URL", "").strip()
        self.configuration_warnings: list[str] = []
        self._clock = clock or time.monotonic
        self._sleep = sleeper or time.sleep
        self._random = random_source or random.random
        try:
            self.max_attempts = max(1, min(5, int(
                self.env.get("SOVEREIGN_MODEL_MAX_ATTEMPTS", "2")
            )))
        except (TypeError, ValueError):
            self.max_attempts = 2
        try:
            self.retry_base_seconds = max(0.05, float(
                self.env.get("SOVEREIGN_MODEL_RETRY_BASE_SECONDS", "0.5")
            ))
        except (TypeError, ValueError):
            self.retry_base_seconds = 0.5
        try:
            self.retry_max_seconds = max(self.retry_base_seconds, float(
                self.env.get("SOVEREIGN_MODEL_RETRY_MAX_SECONDS", "8")
            ))
        except (TypeError, ValueError):
            self.retry_max_seconds = 8.0
        try:
            self.total_deadline_seconds = max(1.0, float(
                self.env.get("SOVEREIGN_MODEL_TOTAL_DEADLINE_SECONDS", "60")
            ))
        except (TypeError, ValueError):
            self.total_deadline_seconds = 60.0
        self.request_timeout = min(self.request_timeout, self.total_deadline_seconds)
        self._provider_cooldowns: dict[str, tuple[float, str]] = {}
        self._candidate_cooldowns: dict[str, tuple[float, str]] = {}
        self._provider_failures: dict[str, int] = {}
        self._provider_successes: dict[str, int] = {}
        self.candidates = self._build_candidates()
        self.active_index = 0

    def select_model(self, provider: str, model: str) -> dict:
        """Select an already configured candidate; never creates or bills a provider."""
        for index, candidate in enumerate(self.candidates):
            if (candidate.info.provider.casefold() == provider.casefold()
                    and candidate.info.model.casefold() == model.casefold()):
                eligible, reason = self._eligible(candidate)
                if not eligible:
                    raise ValueError(f"Model is cooling down: {reason}")
                self.active_index = index
                return {"provider": candidate.info.provider, "model": candidate.info.model,
                        "runtime": candidate.info.runtime, "active": True}
        raise ValueError("That model is not configured on this SovereignOS API instance.")

    def _openai_compatible(self, provider: str, key: str, model: str,
                           base_url: str, runtime: str = "cloud",
                           extra_headers: dict | None = None):
        try:
            from langchain_openai import ChatOpenAI
        except ImportError:
            self.configuration_warnings.append(
                f"{provider} requires langchain-openai; install with "
                "'py -3.11 -m pip install langchain-openai'."
            )
            return None
        try:
            return ChatOpenAI(
                model=model, api_key=key, base_url=base_url,
                max_tokens=self.max_tokens, temperature=self.temperature,
                timeout=self.request_timeout, max_retries=0,
                default_headers=extra_headers or {},
            )
        except Exception as exc:
            self.configuration_warnings.append(
                f"{provider} client could not initialize: {type(exc).__name__}: {exc}"
            )
            return None

    def _add_compatible(self, groups, provider, env_key, model_key,
                        default_model, base_url, runtime="cloud",
                        headers=None, require_opt_in=None):
        key = self.env.get(env_key, "").strip()
        if not key:
            return
        if require_opt_in and self.env.get(require_opt_in, "").casefold() not in {"1", "true", "yes", "on"}:
            self.configuration_warnings.append(
                f"{provider} key is present but disabled; set {require_opt_in}=true only if you accept its billing terms."
            )
            return
        model = self.env.get(model_key, default_model).strip()
        if not model:
            self.configuration_warnings.append(f"{provider} key is set but its model name is empty.")
            return
        client = self._openai_compatible(provider, key, model, base_url, runtime, headers)
        if client is not None:
            provider_key = {
                "openrouter free": "openrouter",
                "cloudflare workers ai": "cloudflare",
                "z.ai": "zai",
                "nvidia nim": "nvidia",
                "mistral api (paid opt-in)": "mistral",
            }.get(provider.casefold(), provider.casefold())
            groups[provider_key].append(
                ModelCandidate(ModelInfo(provider, model, runtime), client)
            )

    def _build_candidates(self) -> list[ModelCandidate]:
        groups = {name: [] for name in (
            "groq", "openrouter", "cloudflare", "zai", "sambanova",
            "nvidia", "ollama", "mistral"
        )}
        groq_key = self.env.get("GROQ_API_KEY", "").strip()
        if groq_key:
            try:
                from langchain_groq import ChatGroq
                primary = self.env.get("GROQ_MODEL", "qwen/qwen3.8-27b").strip()
                models = [primary] + [
                    value.strip() for value in self.env.get(
                        "GROQ_FALLBACK_MODELS", "llama-3.3-70b-versatile"
                    ).split(",") if value.strip() and value.strip() != primary
                ]
                for model in models:
                    kwargs = dict(api_key=groq_key, model=model,
                                  max_tokens=self.max_tokens, temperature=self.temperature,
                                  timeout=self.request_timeout, max_retries=0)
                    if "qwen3.8" in model or "qwen3.6" in model:
                        kwargs.update(reasoning_effort="none", reasoning_format="hidden")
                    try:
                        client = ChatGroq(**kwargs)
                    except TypeError:
                        kwargs.pop("reasoning_effort", None)
                        kwargs.pop("reasoning_format", None)
                        try:
                            client = ChatGroq(**kwargs)
                        except Exception as exc:
                            self.configuration_warnings.append(f"Groq model {model} failed to initialize: {exc}")
                            continue
                    except Exception as exc:
                        self.configuration_warnings.append(f"Groq model {model} failed to initialize: {exc}")
                        continue
                    groups["groq"].append(ModelCandidate(ModelInfo("Groq", model, "cloud"), client))
            except ImportError:
                self.configuration_warnings.append("Groq client dependency is missing; install langchain-groq.")
            if not groups["groq"] and not any("Groq client dependency" in w for w in self.configuration_warnings):
                self.configuration_warnings.append("Groq key found, but no Groq model client initialized. Check model IDs and dependencies.")

        openrouter_models = [
            self.env.get("OPENROUTER_MODEL", "openrouter/free").strip(),
            *[value.strip() for value in self.env.get(
                "OPENROUTER_FALLBACK_MODELS",
                "nvidia/nemotron-3.5-super-vl:free,nvidia/nemotron-3-ultra-550b-a55b:free,cohere/north-mini-code:free"
            ).split(",") if value.strip()],
        ]
        seen_openrouter = set()
        for index, model in enumerate(openrouter_models):
            if not model or model.casefold() in seen_openrouter:
                continue
            seen_openrouter.add(model.casefold())
            if index == 0:
                self._add_compatible(groups, "OpenRouter Free", "OPENROUTER_API_KEY",
                    "OPENROUTER_MODEL", "openrouter/free", "https://openrouter.ai/api/v1",
                    headers={"X-Title": "SovereignOS"})
            else:
                key = self.env.get("OPENROUTER_API_KEY", "").strip()
                if key:
                    client = self._openai_compatible("OpenRouter Free", key, model,
                        "https://openrouter.ai/api/v1", extra_headers={"X-Title": "SovereignOS"})
                    if client is not None:
                        groups["openrouter"].append(
                            ModelCandidate(ModelInfo("OpenRouter Free", model, "cloud"), client)
                        )
        # Cloudflare's free daily allocation is shared across its account. Choose
        # a model that Cloudflare currently lists as Free-plan eligible by default.
        account = self.env.get("CLOUDFLARE_ACCOUNT_ID", "").strip()
        cf_key = self.env.get("CLOUDFLARE_API_TOKEN", "").strip()
        if cf_key and account:
            self._add_compatible(groups, "Cloudflare Workers AI", "CLOUDFLARE_API_TOKEN",
                "CLOUDFLARE_MODEL", "@cf/zai-org/glm-4.7-flash",
                f"https://api.cloudflare.com/client/v4/accounts/{account}/ai/v1")
        elif cf_key or account:
            self.configuration_warnings.append("Cloudflare requires both CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN.")

        self._add_compatible(groups, "Z.AI", "ZAI_API_KEY", "ZAI_MODEL",
            "glm-4.7-flash", self.env.get("ZAI_BASE_URL", "https://api.z.ai/api/paas/v4"))
        self._add_compatible(groups, "SambaNova", "SAMBANOVA_API_KEY", "SAMBANOVA_MODEL",
            "DeepSeek-V3.1", self.env.get("SAMBANOVA_BASE_URL", "https://api.sambanova.ai/v1"))
        self._add_compatible(groups, "NVIDIA NIM", "NVIDIA_API_KEY", "NVIDIA_MODEL",
            "meta/llama-3.3-70b-instruct", self.env.get("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1"))
        # Mistral API is not treated as a free tier. It is impossible to activate
        # accidentally: key plus an explicit opt-in are both required.
        self._add_compatible(groups, "Mistral API (paid opt-in)", "MISTRAL_API_KEY",
            "MISTRAL_MODEL", "mistral-small-latest",
            "https://api.mistral.ai/v1", require_opt_in="SOVEREIGN_ENABLE_PAID_MISTRAL")

        ollama_model = self.env.get("OLLAMA_MODEL", "").strip()
        if ollama_model:
            base_url = self.env.get("OLLAMA_BASE_URL", "http://localhost:11434/v1").strip()
            client = self._openai_compatible("Ollama", "ollama", ollama_model, base_url, "local")
            if client is not None:
                groups["ollama"].append(ModelCandidate(ModelInfo("Ollama", ollama_model, "local"), client))

        order = [part.strip().casefold() for part in self.env.get(
            "SOVEREIGN_MODEL_ORDER",
            "groq,openrouter,cloudflare,zai,sambanova,nvidia,ollama"
        ).split(",") if part.strip()]
        result = []
        for provider in order:
            result.extend(groups.get(provider, []))
        # Paid Mistral is deliberately absent from the default order.
        if "mistral" in order and groups["mistral"]:
            result.extend(groups["mistral"])
        if not result and not self.configuration_warnings:
            self.configuration_warnings.append(
                "No provider credentials were found in the API process. Add GROQ_API_KEY or OPENROUTER_API_KEY to the project .env file, then restart the API."
            )
        return result

    def _cooldown_key(self, candidate: ModelCandidate) -> str:
        return f"{candidate.info.provider.casefold()}::{candidate.info.model.casefold()}"

    def _cooldown_duration(self, exc: Exception) -> float:
        status = _status_code(exc)
        retry = _retry_after_seconds(exc)
        if retry is not None:
            return retry
        if status == 429:
            return 60.0
        if status == 402:
            return 3600.0
        if status in {401, 403}:
            return 1800.0
        if status == 404:
            return 86400.0
        if status and status >= 500:
            return 45.0
        return 30.0

    def _record_failure(self, candidate: ModelCandidate, exc: Exception):
        provider = candidate.info.provider.casefold()
        status = _status_code(exc)
        duration = self._cooldown_duration(exc)
        self._provider_failures[provider] = self._provider_failures.get(provider, 0) + 1
        # A bad model ID/tool capability should not disable other models at the
        # same provider; account/auth/quota/network failures should.
        message = str(exc).casefold()
        candidate_fault = (
            status == 404 or "model not found" in message
            or "unknown model" in message or "unsupported tool" in message
            or "does not support tool" in message
            or "context_length_exceeded" in message
            or "maximum context length" in message
            or "vision is not supported" in message
            or "unsupported image" in message
        )
        if candidate_fault:
            self._candidate_cooldowns[self._cooldown_key(candidate)] = (
                self._clock() + duration, str(exc)[:240]
            )
        else:
            self._provider_cooldowns[provider] = (
                self._clock() + duration, str(exc)[:240]
            )

    @staticmethod
    def _is_retryable_error(exc: Exception) -> bool:
        """Only retry transient transport/rate-limit/server failures.

        Model incompatibilities, quota/billing responses (402), auth faults and
        malformed requests should move to a different candidate or fail fast.
        """
        status = _status_code(exc)
        if status in {408, 409, 425, 429} or (status is not None and status >= 500):
            return True
        name = type(exc).__name__.casefold()
        message = str(exc).casefold()
        if any(word in name for word in ("timeout", "connection", "ratelimit")):
            return True
        return any(marker in message for marker in (
            "rate limit", "rate_limit", "too many requests",
            "temporarily unavailable", "service unavailable",
            "timed out", "connection refused", "connection reset",
            "connection aborted", "overloaded",
        ))

    def _retry_wait(self, exc: Exception, retry_number: int,
                    deadline: float) -> bool:
        """Wait within the shared request deadline; return False if it cannot fit."""
        remaining = deadline - self._clock()
        if remaining <= 0:
            return False
        retry_after = _retry_after_seconds(exc)
        if retry_after is not None:
            delay = retry_after
        else:
            ceiling = min(
                self.retry_max_seconds,
                self.retry_base_seconds * (2 ** max(0, retry_number - 1)),
            )
            # Full jitter in [0, ceiling] prevents synchronized retry bursts.
            delay = max(0.0, min(1.0, float(self._random()))) * ceiling
        if delay >= remaining:
            return False
        if delay > 0:
            self._sleep(delay)
        return self._clock() < deadline

    def _eligible(self, candidate: ModelCandidate) -> tuple[bool, str]:
        now = self._clock()
        key = self._cooldown_key(candidate)
        candidate_state = self._candidate_cooldowns.get(key)
        if candidate_state and candidate_state[0] > now:
            return False, f"model cooldown ({int(candidate_state[0] - now)}s): {candidate_state[1]}"
        provider_state = self._provider_cooldowns.get(candidate.info.provider.casefold())
        if provider_state and provider_state[0] > now:
            return False, f"provider cooldown ({int(provider_state[0] - now)}s): {provider_state[1]}"
        return True, ""

    def health_snapshot(self) -> list[dict]:
        """Return safe, credential-free health details for a UI or diagnostics panel."""
        rows = []
        now = self._clock()
        for candidate in self.candidates:
            provider = candidate.info.provider
            eligible, reason = self._eligible(candidate)
            rows.append({
                "provider": provider, "model": candidate.info.model,
                "runtime": candidate.info.runtime,
                "status": ("cooldown" if not eligible else ("ready" if self._provider_successes.get(provider.casefold(), 0) else "connected")),
                "active": self.candidates.index(candidate) == self.active_index,
                "cooldown_reason": reason,
                "failures": self._provider_failures.get(provider.casefold(), 0),
                "successes": self._provider_successes.get(provider.casefold(), 0),
            })
        return rows

    def stream(self, messages, max_tokens: int | None = None,
               start_index: int | None = None,
               candidate_indices: list[int] | None = None):
        """Yield stream chunks; retry only before the first visible chunk."""
        if start_index is None:
            start_index = self.active_index
        failures: list[str] = []
        if not self.candidates:
            details = " ".join(self.configuration_warnings)
            raise ModelRoutingError(
                "No model provider is configured. " + details,
                checkout_url=self.checkout_url,
            )
        start_index = max(0, min(int(start_index), len(self.candidates) - 1))
        order = list(range(start_index, len(self.candidates))) + list(range(0, start_index))
        if candidate_indices is not None:
            allowed = {int(index) for index in candidate_indices
                       if isinstance(index, int) and 0 <= index < len(self.candidates)}
            order = [index for index in order if index in allowed]
            if not order:
                raise ModelRoutingError("No permitted model candidate is available for this request.")
        deadline = self._clock() + self.total_deadline_seconds
        for index in order:
            if self._clock() >= deadline:
                break
            candidate = self.candidates[index]
            eligible, reason = self._eligible(candidate)
            if not eligible:
                failures.append(f"{candidate.info.label}: skipped: {reason}")
                continue
            emitted = False
            for attempt in range(1, self.max_attempts + 1):
                if self._clock() >= deadline:
                    break
                try:
                    model = candidate.client
                    if max_tokens is not None:
                        model = model.bind(max_tokens=int(max_tokens))
                    stream_method = getattr(model, "stream", None)
                    if not callable(stream_method):
                        raise TypeError(f"{candidate.info.label} does not support streaming.")
                    for chunk in stream_method(messages):
                        emitted = True
                        yield chunk, candidate.info, index
                    provider = candidate.info.provider.casefold()
                    self._provider_successes[provider] = self._provider_successes.get(provider, 0) + 1
                    return
                except GeneratorExit:
                    raise
                except Exception as exc:
                    if emitted or not is_capacity_error(exc):
                        raise
                    failures.append(
                        f"{candidate.info.label} attempt {attempt}/{self.max_attempts}: "
                        f"{type(exc).__name__}: {exc}"
                    )
                    if (attempt < self.max_attempts
                            and self._is_retryable_error(exc)
                            and self._retry_wait(exc, attempt, deadline)):
                        continue
                    self._record_failure(candidate, exc)
                    break
                if self._clock() >= deadline:
                    break
        detail = "\\n".join(failures[-10:])
        raise ModelRoutingError(
            "All configured model options are currently unavailable, cooling down, "
            "or out of quota. Sovereign did not make a paid request. "
            "Paid providers remain disabled unless explicitly opted in.\\nProvider details:\\n" + detail,
            failures=failures, checkout_url=self.checkout_url,
        )

    def invoke(self, messages, tools=None, start_index: int | None = None,
               max_tokens: int | None = None, require_vision: bool = False,
               candidate_indices: list[int] | None = None):
        """Invoke an eligible model with bounded transient retries and failover."""
        if start_index is None:
            start_index = self.active_index
        failures: list[str] = []
        if not self.candidates:
            details = " ".join(self.configuration_warnings)
            raise ModelRoutingError(
                "No model provider is configured. Configure Groq, OpenRouter Free, "
                "Cloudflare Workers AI, Z.AI, SambaNova, NVIDIA NIM, or an explicitly "
                "configured local Ollama model. " + details,
                checkout_url=self.checkout_url,
            )
        start_index = max(0, min(int(start_index), len(self.candidates) - 1))
        order = list(range(start_index, len(self.candidates))) + list(range(0, start_index))
        if candidate_indices is not None:
            allowed = {int(index) for index in candidate_indices
                       if isinstance(index, int) and 0 <= index < len(self.candidates)}
            order = [index for index in order if index in allowed]
            if not order:
                raise ModelRoutingError("No permitted model candidate is available for this request.")
        if require_vision:
            order = [index for index in order if _supports_vision(self.candidates[index])]
            if not order:
                raise ModelRoutingError(
                    "No configured model is identified as vision-capable. "
                    "Configure an enabled vision model in your local provider settings; "
                    "text-only models will not receive image data."
                )
        deadline = self._clock() + self.total_deadline_seconds
        for index in order:
            candidate = self.candidates[index]
            eligible, reason = self._eligible(candidate)
            if not eligible:
                failures.append(f"{candidate.info.label}: skipped: {reason}")
                continue
            for attempt in range(1, self.max_attempts + 1):
                try:
                    model = candidate.client
                    if tools is not None:
                        model = model.bind_tools(tools)
                    if max_tokens is not None:
                        model = model.bind(max_tokens=int(max_tokens))
                    result = model.invoke(messages)
                    provider = candidate.info.provider.casefold()
                    self._provider_successes[provider] = self._provider_successes.get(provider, 0) + 1
                    return result, candidate.info, index
                except Exception as exc:
                    failures.append(
                        f"{candidate.info.label} attempt {attempt}/{self.max_attempts}: "
                        f"{type(exc).__name__}: {exc}"
                    )
                    if not is_capacity_error(exc):
                        raise
                    message = str(exc).casefold()
                    status = _status_code(exc)
                    capability_rejection = status in {400, 409, 422} and any(
                        marker in message for marker in ("image", "vision", "multimodal", "content type")
                    )
                    if capability_rejection:
                        self._candidate_cooldowns[self._cooldown_key(candidate)] = (
                            self._clock() + 300.0, str(exc)[:240]
                        )
                        break
                    if (attempt < self.max_attempts
                            and self._is_retryable_error(exc)
                            and self._retry_wait(exc, attempt, deadline)):
                        continue
                    self._record_failure(candidate, exc)
                    break
                if self._clock() >= deadline:
                    break
        detail = "\\n".join(failures[-10:])
        raise ModelRoutingError(
            "All configured model options are currently unavailable, cooling down, "
            "or out of quota. Sovereign did not make a paid request. Wait for a "
            "provider reset or configure another eligible provider. Paid providers "
            "remain disabled unless explicitly opted in.\\nProvider details:\\n" + detail,
            failures=failures, checkout_url=self.checkout_url,
        )
