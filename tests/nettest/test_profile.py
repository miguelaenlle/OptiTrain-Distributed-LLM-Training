import math

import pytest

from orchestrator.nettest.profile import schedule


class Engine:
    def predict_prefill_latency(self, batch, length, prefix):
        assert batch == 1 and prefix == 0
        return length / 10

    def decode_step_latency(self, batch, context, outputs):
        assert batch == 1 and outputs == 2
        return context / 100


def test_export_counts_first_output_once_and_grows_decode_context():
    sample = schedule(Engine(), 100, 3)
    assert sample["first_output_ms"] == 10
    assert sample["gaps_ms"] == [1, 1.01]


@pytest.mark.parametrize("invalid", [0, -1, math.nan, math.inf])
def test_bad_estimator_values_fail_without_fallback(invalid):
    class BadEngine(Engine):
        def predict_prefill_latency(self, *args):
            return invalid

    with pytest.raises(ValueError):
        schedule(BadEngine(), 100, 3)
