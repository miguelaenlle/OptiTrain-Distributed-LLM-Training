import pytest
from kvtransfer.planning import prefix_tokens, request_plan


@pytest.mark.parametrize(
    "length,expected",
    [(1, 0), (15, 0), (16, 0), (17, 16), (31, 16), (32, 16), (33, 32), (8192, 8176)],
)
def test_decode_keeps_tail_for_first_token(length, expected):
    assert prefix_tokens(length, 16) == expected


def test_plan_maps_only_allocated_full_prefix():
    params = {"kvtransfer": {"incarnation": 42, "sequence": 7}}
    plan = request_plan("request", params, 33, 16, [8, 3, 1])
    assert plan.blocks == [8, 3]
    assert (plan.incarnation, plan.sequence) == (42, 7)
    with pytest.raises(ValueError):
        request_plan("request", params, 33, 16, [8])
    with pytest.raises(ValueError):
        request_plan("request", params, 33, 16, [8, 8])
    assert request_plan("request", None, 33, 16, [8, 3]) is None
