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


if __name__ == "__main__":
    unittest.main()
