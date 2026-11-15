# Workspace-aware model routing and quota failover

## What is included

- Groq primary model plus optional comma-separated Groq fallback models.
- Optional OpenRouter free-model router, pinned to `openrouter/free`. The app does
  not accept a paid OpenRouter model ID through configuration.
- Optional local Ollama endpoint, enabled only when `OLLAMA_MODEL` is explicitly set.
- Ordered failover using `SOVEREIGN_MODEL_ORDER`.
- Capacity/quota classification and request-local fallback. Invalid prompts and
  unrelated application errors are not silently treated as quota exhaustion.
- Runtime identity reports provider, model and local/cloud.
- No provider is called unless the relevant key or local endpoint is configured.
- No automatic paid provider, API billing or premium entitlement is activated.

## Configuration

Existing Groq setup continues to work. Optional settings in `.env`:

```dotenv
GROQ_MODEL=qwen/qwen3.8-27b
GROQ_FALLBACK_MODELS=llama-3.3-70b-versatile
OPENROUTER_API_KEY=
OLLAMA_MODEL=
OLLAMA_BASE_URL=http://localhost:11434/v1
SOVEREIGN_MODEL_ORDER=groq,openrouter,ollama
SOVEREIGN_MAX_OUTPUT_TOKENS=800
SOVEREIGN_CONTEXT_CHARS=10000
SOVEREIGN_PREMIUM_URL=
```

Install the optional OpenAI-compatible client for OpenRouter/Ollama:

```bat
py -3.11 -m pip install langchain-openai
```

To use OpenRouter free models, create and configure your own OpenRouter API key.
The router uses only its `openrouter/free` endpoint. Provider free quotas and model
availability can change; failover is best-effort, not a guarantee of unlimited use.

Ollama is optional. On a low-memory PC, do not install a large model. Sovereign
will only attempt the local model when `OLLAMA_MODEL` is set and its service is
running. A local model can be slower or less capable than cloud models.

## Token and quota handling

The old `MAX_TOKENS=800` controls response length; it does not fix provider
request-per-minute, token-per-minute or daily quota limits. The desktop integration
also bounds chat history, memory context and tool outputs before model invocation.
When a provider reports quota/capacity exhaustion, the router advances to the next
configured candidate without repeatedly retrying the exhausted provider.

## Workspace memory

The selected workspace is captured by the desktop session. Memory tools are built
with that workspace ID in a closure, so the model cannot select another workspace
by passing an argument. Chat history is persisted separately per workspace.

## Premium product boundary

The app can display a premium-required notice and a configured checkout URL, but
this release does not collect money, store payment credentials or grant premium
entitlements. A real "pay through Sovereign" service needs an authenticated
backend, payment provider merchant approval, webhook signature verification,
subscription ledger, usage metering, refund/cancellation handling and server-side
provider credentials. Never put provider master keys in the desktop client.
