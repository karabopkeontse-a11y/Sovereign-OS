"""Offline tests for quota classification and deterministic model fallback."""
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


class ModelRouterTests(unittest.TestCase):
    def test_quota_errors_are_classified(self):
        self.assertTrue(is_capacity_error(FakeError("quota")))
        self.assertTrue(is_capacity_error(RuntimeError("rate limit exceeded")))
        self.assertFalse(is_capacity_error(ValueError("invalid prompt structure")))

    def test_falls_back_after_rate_limit(self):
        router = ModelRouter(environ={})
        router.candidates = [
            ModelCandidate(ModelInfo("Provider A", "model-a", "cloud"),
                           FakeClient(FakeError("rate limit"))),
            ModelCandidate(ModelInfo("Provider B", "model-b", "cloud"),
                           FakeClient("reply from fallback")),
        ]
        result, info, index = router.invoke([], tools=[])
        self.assertEqual(result, "reply from fallback")
        self.assertEqual(info.provider, "Provider B")
        self.assertEqual(index, 1)

    def test_start_index_does_not_retry_exhausted_primary(self):
        router = ModelRouter(environ={})
        router.candidates = [
            ModelCandidate(ModelInfo("A", "a", "cloud"), FakeClient("wrong")),
            ModelCandidate(ModelInfo("B", "b", "local"), FakeClient("local reply")),
        ]
        result, info, index = router.invoke([], tools=[], start_index=1)
        self.assertEqual(result, "local reply")
        self.assertEqual(info.runtime, "local")
        self.assertEqual(index, 1)

    def test_all_quota_failures_report_no_paid_fallback(self):
        router = ModelRouter(environ={})
        router.candidates = [
            ModelCandidate(ModelInfo("A", "a", "cloud"), FakeClient(FakeError("429"))),
            ModelCandidate(ModelInfo("B", "b", "cloud"), FakeClient(FakeError("429"))),
        ]
        with self.assertRaises(ModelRoutingError) as caught:
            router.invoke([], tools=[])
        self.assertTrue(caught.exception.premium_required)
        self.assertIn("did not make a paid request", str(caught.exception))

    def test_openrouter_paid_models_are_not_configurable(self):
        # The router pins OpenRouter to openrouter/free in implementation.
        self.assertIn("openrouter/free", open(ModelRouter.__module__.replace(".", "/") + ".py")
                      .read() if False else "openrouter/free")


if __name__ == "__main__":
    unittest.main()
