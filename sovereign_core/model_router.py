"""Resilient, provider-neutral model routing for SovereignOS.

Only configured providers are eligible. OpenRouter is pinned to its free-model
router; this module never silently routes to a paid OpenRouter model. Ollama is
optional and only used when the user explicitly configures a local model.
"""
from __future__ import annotations

from dataclasses import dataclass
import os


@dataclass(frozen=True)
class ModelInfo:
    provider: str
    model: str
    runtime: str  # "cloud" or "local"

    @property
    def label(self) -> str:
        return f"{self.runtime.title()} · {self.provider} · {self.model}"


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


def is_capacity_error(exc: Exception) -> bool:
    """Return true only for provider/model capacity or availability failures."""
    status = getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    if status is None and response is not None:
        status = getattr(response, "status_code", None)
    if status in {401, 402, 403, 404, 408, 409, 425, 429}:
        return True
    if isinstance(status, int) and status >= 500:
        return True
    name = type(exc).__name__.casefold()
    message = str(exc).casefold()
    if any(word in name for word in ("timeout", "connection", "ratelimit")):
        return True
    markers = (
        "rate limit", "rate_limit", "too many requests", "quota exceeded",
        "insufficient_quota", "resource_exhausted", "tokens per minute",
        "requests per minute", "capacity", "overloaded", "temporarily unavailable",
        "service unavailable", "model_not_found", "model not found",
        "decommissioned", "maximum context length", "context_length_exceeded",
        "context window", "unauthorized", "invalid api key", "authentication",
        "timed out", "connection refused", "connection reset",
    )
    return any(marker in message for marker in markers)


class ModelRouter:
    """Ordered model candidates with request-local failover and transparent status."""

    def __init__(self, max_tokens: int = 800, temperature: float = 0.7,
                 environ=None):
        self.env = os.environ if environ is None else environ
        self.max_tokens = int(max_tokens)
        self.temperature = float(temperature)
        self.checkout_url = self.env.get("SOVEREIGN_PREMIUM_URL", "").strip()
        self.candidates = self._build_candidates()

    def _build_candidates(self) -> list[ModelCandidate]:
        groups: dict[str, list[ModelCandidate]] = {"groq": [], "openrouter": [], "ollama": []}
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
                                  max_tokens=self.max_tokens, temperature=self.temperature)
                    if "qwen3.8" in model or "qwen3.6" in model:
                        kwargs.update(reasoning_effort="none", reasoning_format="hidden")
                    groups["groq"].append(ModelCandidate(
                        ModelInfo("Groq", model, "cloud"), ChatGroq(**kwargs)
                    ))
            except ImportError:
                pass

        openrouter_key = self.env.get("OPENROUTER_API_KEY", "").strip()
        if openrouter_key:
            try:
                from langchain_openai import ChatOpenAI
                # Deliberately fixed: never accept an arbitrary paid model via env.
                groups["openrouter"].append(ModelCandidate(
                    ModelInfo("OpenRouter Free", "openrouter/free", "cloud"),
                    ChatOpenAI(
                        model="openrouter/free", api_key=openrouter_key,
                        base_url="https://openrouter.ai/api/v1",
                        max_tokens=self.max_tokens, temperature=self.temperature,
                        default_headers={"X-Title": "SovereignOS"},
                    ),
                ))
            except ImportError:
                pass

        ollama_model = self.env.get("OLLAMA_MODEL", "").strip()
        if ollama_model:
            try:
                from langchain_openai import ChatOpenAI
                base_url = self.env.get("OLLAMA_BASE_URL", "http://localhost:11434/v1").strip()
                groups["ollama"].append(ModelCandidate(
                    ModelInfo("Ollama", ollama_model, "local"),
                    ChatOpenAI(
                        model=ollama_model, api_key="ollama",
                        base_url=base_url, max_tokens=self.max_tokens,
                        temperature=self.temperature,
                    ),
                ))
            except ImportError:
                pass

        order = [part.strip().casefold() for part in self.env.get(
            "SOVEREIGN_MODEL_ORDER", "groq,openrouter,ollama"
        ).split(",") if part.strip()]
        result: list[ModelCandidate] = []
        for provider in order:
            result.extend(groups.get(provider, []))
        return result

    def invoke(self, messages, tools=None, start_index: int = 0):
        """Invoke candidates in order. Returns (AIMessage, ModelInfo, candidate_index)."""
        failures: list[str] = []
        if not self.candidates:
            raise ModelRoutingError(
                "No model provider is configured. Add a Groq key, an OpenRouter key "
                "for free models, or configure an existing local Ollama model.",
                checkout_url=self.checkout_url,
            )
        for index in range(max(0, start_index), len(self.candidates)):
            candidate = self.candidates[index]
            try:
                model = candidate.client.bind_tools(tools) if tools else candidate.client
                result = model.invoke(messages)
                return result, candidate.info, index
            except Exception as exc:
                failures.append(f"{candidate.info.label}: {type(exc).__name__}: {exc}")
                if not is_capacity_error(exc):
                    raise
        detail = "\n".join(failures[-6:])
        raise ModelRoutingError(
            "All configured model options are currently unavailable or out of quota. "
            "Sovereign did not make a paid request. Add another free provider key, "
            "wait for quota reset, or configure a local model. Premium access is "
            "not activated until an official checkout and entitlement service is connected."
            + (f"\nProvider details:\n{detail}" if detail else ""),
            failures=failures, checkout_url=self.checkout_url,
        )
