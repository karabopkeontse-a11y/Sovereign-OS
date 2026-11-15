# SovereignOS multi-provider routing and model health

## Included

- Provider circuit breakers: quota, authentication, billing, transient outage and model-specific cooldowns are tracked across chat turns for the lifetime of the router process.
- Retry-After response headers are honored (bounded to 24 hours); sensible defaults apply when the provider omits the header.
- Models returning 404/model-not-found are cooled down individually instead of disabling every model from that provider.
- A credential-free `health_snapshot()` reports configured model readiness, cooldown reason and success/failure counts.
- OpenAI-compatible adapters: Cloudflare Workers AI, Z.AI, SambaNova Cloud, NVIDIA NIM, Mistral API, plus existing OpenRouter and Ollama.
- Existing Groq multi-model routing remains.
- Paid Mistral API is disabled unless both `MISTRAL_API_KEY` and `SOVEREIGN_ENABLE_PAID_MISTRAL=true` are configured. It is not in the default provider order.
- No provider key is created or stored by the application. No paid provider is called by default.

## Provider configuration

All keys are optional. Add only the providers you have registered with and reviewed. Do not commit keys to Git.

```dotenv
# Existing
GROQ_FALLBACK_MODELS=llama-3.3-70b-versatile
OPENROUTER_API_KEY=
SOVEREIGN_MODEL_ORDER=groq,openrouter,cloudflare,zai,sambanova,nvidia,ollama

# Cloudflare Workers AI (account ID + API token)
CLOUDFLARE_ACCOUNT_ID=
CLOUDFLARE_API_TOKEN=
CLOUDFLARE_MODEL=@cf/zai-org/glm-4.7-flash

# Z.AI (confirm current model access and account limits in its console)
ZAI_API_KEY=
ZAI_MODEL=glm-4.7-flash
ZAI_BASE_URL=https://api.z.ai/api/paas/v4

# SambaNova (confirm current account plan, limits and billing status)
SAMBANOVA_API_KEY=
SAMBANOVA_MODEL=DeepSeek-V3.1
SAMBANOVA_BASE_URL=https://api.sambanova.ai/v1

# NVIDIA NIM (model availability and free endpoint limits vary)
NVIDIA_API_KEY=
NVIDIA_MODEL=meta/llama-3.3-70b-instruct
NVIDIA_BASE_URL=https://integrate.api.nvidia.com/v1

# Mistral API is paid/usage-billed; off unless explicitly enabled
MISTRAL_API_KEY=
MISTRAL_MODEL=mistral-small-latest
SOVEREIGN_ENABLE_PAID_MISTRAL=false
```

Install the OpenAI-compatible adapter dependency if not already installed:

```bat
py -3.11 -m pip install langchain-openai
```

## Important quota and pricing facts

- Cloudflare Workers AI currently documents 10,000 free Neurons/day on Workers Free, shared at account level. Some models require Workers Paid; the default configured model should be checked against the current free-plan catalogue before use. Exceeding the free allocation can require a paid plan. Do not enable paid billing without explicit approval.
- Z.AI, SambaNova and NVIDIA free/developer availability, eligible models and rate limits can change by account, region and model. Check the provider console before relying on them. This adapter does not assert unlimited or guaranteed free access.
- Mistral's Free mode is not the same as free API inference. API usage requires billing activation/pay-as-you-go, so its adapter is explicitly gated and excluded from the default route.
- OpenRouter's `openrouter/free` is a shared free-model router, not a guaranteed capacity reservation.
- Groq fallback models still share the Groq account's rate and token limits; additional model IDs are not independent provider capacity.

## Circuit-breaker behavior

- 429: provider cooldown uses Retry-After or 60 seconds.
- 402: provider cooldown defaults to one hour.
- 401/403: provider cooldown defaults to 30 minutes.
- 5xx/network/timeout: provider cooldown defaults to 45 seconds.
- 404/model-not-found: only that model is cooled down for 24 hours.
- Cooldowns are process-local, not persisted across application restarts.
- Failures are not retried against the same exhausted provider in the same turn. Other configured eligible providers are attempted in order.

## Health data

Call `MODEL_ROUTER.health_snapshot()` from a diagnostics view to show provider/model, ready/cooldown state, reason, successes and failures. The snapshot contains no API keys.

## Privacy and security

Each configured provider receives the prompt, selected chat history, relevant memory context and tool-call content sent for that model request. Do not configure a provider for sensitive work unless its retention, training, data location and contractual terms are acceptable. Keep API keys in a private local environment file; never commit them to Git or bundle them in a public desktop installer.
