"""Pure request planning, shared by connector and CPU contract tests."""

from dataclasses import dataclass


@dataclass
class RequestPlan:
    request_id: str
    incarnation: int
    sequence: int
    blocks: list[int]


def prefix_tokens(prompt_length: int, block_size: int) -> int:
    if prompt_length < 1 or block_size < 1:
        raise ValueError("positive prompt length and block size required")
    # Decode recomputes the tail (including at least the last prompt token),
    # then generates the first output token itself. Prefill output is discarded.
    return ((prompt_length - 1) // block_size) * block_size


def request_plan(request_id, params, prompt_length, block_size, blocks):
    if not params or "kvtransfer" not in params:
        return None
    key = params["kvtransfer"]
    incarnation, sequence = key["incarnation"], key["sequence"]
    if any(type(x) is not int or not 0 < x < 2**64 for x in (incarnation, sequence)):
        raise ValueError("transfer ID must contain positive uint64 values")
    count = prefix_tokens(prompt_length, block_size) // block_size
    if not count:
        return None
    selected = list(blocks[:count])
    if len(selected) != count or len(set(selected)) != count or any(x < 0 for x in selected):
        raise ValueError("incomplete or invalid block allocation")
    return RequestPlan(request_id, incarnation, sequence, selected)
