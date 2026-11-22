"""Offline tests for multi-provider routing and circuit-breaker behavior."""
import unittest

from sovereign_core.model_router import (
    ModelCandidate, ModelInfo, ModelRouter, ModelRoutingError, is_capacity_error,
)


class FakeBound:
    def __init__(self, outcome):
        self.outcome = outcome

    def invoke(self, messages):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class FakeClient:
    def __init__(self, outcome):
        self.outcome = outcome

    def bind_tools(self, tools):
        return FakeBound(self.outcome)



class FakeStreamBound:
    def __init__(self, outcome):
        self.outcome = outcome

    def stream(self, messages):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        for item in self.outcome:
            if isinstance(item, Exception):
                raise item
            yield item


class FakeStreamClient:
    def __init__(self, outcome):
        self.outcome = outcome

    def bind(self, **kwargs):
        return FakeStreamBound(self.outcome)

    def stream(self, messages):
        return FakeStreamBound(self.outcome).stream(messages)


class FakeError(Exception):
    status_code = 429


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def candidate(provider, model, outcome):
    return ModelCandidate(ModelInfo(provider, model, "cloud"), FakeClient(outcome))


class ModelRouterTests(unittest.TestCase):
    def test_quota_errors_are_classified(self):
        self.assertTrue(is_capacity_error(FakeError("quota")))
        self.assertTrue(is_capacity_error(RuntimeError("rate limit exceeded")))
        self.assertTrue(is_capacity_error(RuntimeError("maximum context length exceeded")))
        self.assertFalse(is_capacity_error(ValueError("invalid prompt structure")))

    def test_falls_back_after_rate_limit(self):
        router = ModelRouter(environ={})
        router.candidates = [candidate("Provider A", "model-a", FakeError("rate limit")),
                             candidate("Provider B", "model-b", "reply from fallback")]
        result, info, index = router.invoke([], tools=[])
        self.assertEqual(result, "reply from fallback")
        self.assertEqual(info.provider, "Provider B")
        self.assertEqual(index, 1)

    def test_provider_cooldown_is_remembered_across_requests(self):
        clock = FakeClock()
        router = ModelRouter(environ={}, clock=clock)
        router.candidates = [candidate("Provider A", "model-a", FakeError("429")),
                             candidate("Provider B", "model-b", "fallback")]
        router.invoke([], tools=[])
        router.candidates[1] = candidate("Provider B", "model-b", "fallback again")
        result, info, index = router.invoke([], tools=[])
        self.assertEqual(info.provider, "Provider B")
        self.assertEqual(index, 1)
        self.assertEqual(result, "fallback again")
        self.assertEqual(router.health_snapshot()[0]["status"], "cooldown")

    def test_provider_returns_after_cooldown(self):
        clock = FakeClock()
        router = ModelRouter(environ={}, clock=clock)
        router.candidates = [candidate("Provider A", "model-a", FakeError("429")),
                             candidate("Provider B", "model-b", "fallback")]
        router.invoke([], tools=[])
        clock.advance(61)
        router.candidates[0] = candidate("Provider A", "model-a", "primary recovered")
        result, info, index = router.invoke([], tools=[])
        self.assertEqual(result, "primary recovered")
        self.assertEqual(info.provider, "Provider A")
        self.assertEqual(index, 0)

    def test_start_index_does_not_retry_exhausted_primary(self):
        router = ModelRouter(environ={})
        router.candidates = [candidate("A", "a", "wrong"),
                             ModelCandidate(ModelInfo("B", "b", "local"), FakeClient("local reply"))]
        result, info, index = router.invoke([], tools=[], start_index=1)
        self.assertEqual(result, "local reply")
        self.assertEqual(info.runtime, "local")
        self.assertEqual(index, 1)

    def test_all_quota_failures_report_no_paid_fallback(self):
        router = ModelRouter(environ={})
        router.candidates = [candidate("A", "a", FakeError("429")),
                             candidate("B", "b", FakeError("429"))]
        with self.assertRaises(ModelRoutingError) as caught:
            router.invoke([], tools=[])
        self.assertTrue(caught.exception.premium_required)
        self.assertIn("did not make a paid request", str(caught.exception))

    def test_compatible_provider_configuration_is_opt_in(self):
        router = ModelRouter(environ={
            "MISTRAL_API_KEY": "test-key",
            "SOVEREIGN_MODEL_ORDER": "mistral",
        })
        self.assertEqual(router.candidates, [])
        self.assertTrue(any("disabled" in warning for warning in router.configuration_warnings))



    def test_candidate_allowlist_prevents_qwen_fallback(self):
        router = ModelRouter(environ={})
        router.candidates = [
            candidate("Groq", "qwen/qwen3.8-27b", "must not run"),
            candidate("Provider B", "safe-model", "approved candidate"),
            candidate("Provider C", "other-model", "must not run either"),
        ]
        result, info, index = router.invoke([], tools=[], candidate_indices=[1])
        self.assertEqual(result, "approved candidate")
        self.assertEqual(info.model, "safe-model")
        self.assertEqual(index, 1)

    def test_empty_candidate_allowlist_fails_closed(self):
        router = ModelRouter(environ={})
        router.candidates = [candidate("Groq", "qwen/qwen3.8-27b", "must not run")]
        with self.assertRaises(ModelRoutingError):
            router.invoke([], candidate_indices=[])


    def test_stream_yields_incremental_chunks_from_configured_candidate(self):
        router = ModelRouter(environ={})
        router.candidates = [
            ModelCandidate(ModelInfo("Provider A", "model-a", "cloud"),
                           FakeStreamClient(["Hel", "lo"])),
        ]
        chunks = list(router.stream([], max_tokens=64))
        self.assertEqual([item[0] for item in chunks], ["Hel", "lo"])
        self.assertTrue(all(item[1].provider == "Provider A" and item[2] == 0
                            for item in chunks))
        self.assertEqual(router.health_snapshot()[0]["successes"], 1)

    def test_stream_fails_over_only_before_first_chunk(self):
        router = ModelRouter(environ={})
        router.candidates = [
            ModelCandidate(ModelInfo("Provider A", "model-a", "cloud"),
                           FakeStreamClient(FakeError("rate limit"))),
            ModelCandidate(ModelInfo("Provider B", "model-b", "local"),
                           FakeStreamClient(["fallback"])),
        ]
        chunks = list(router.stream([]))
        self.assertEqual([item[0] for item in chunks], ["fallback"])
        self.assertEqual(chunks[0][1].provider, "Provider B")

    def test_stream_never_switches_provider_after_output_starts(self):
        router = ModelRouter(environ={})
        router.candidates = [
            ModelCandidate(ModelInfo("Provider A", "model-a", "cloud"),
                           FakeStreamClient(["partial", FakeError("rate limit")])),
            ModelCandidate(ModelInfo("Provider B", "model-b", "local"),
                           FakeStreamClient(["must not be mixed in"])),
        ]
        stream = router.stream([])
        first = next(stream)
        self.assertEqual(first[0], "partial")
        with self.assertRaises(FakeError):
            next(stream)
        self.assertEqual(router.health_snapshot()[1]["successes"], 0)

if __name__ == "__main__":
    unittest.main()
