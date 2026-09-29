"""Hardware acceptance test; requires an installed CUDA-enabled wheel and torch."""

from concurrent.futures import ThreadPoolExecutor

import pytest

torch = pytest.importorskip("torch")
native = pytest.importorskip("kvtransfer._native")

pytestmark = pytest.mark.skipif(
    not torch.cuda.is_available() or not native.cuda_enabled,
    reason="requires CUDA device and CUDA-enabled engine",
)


def test_cuda_tcp_round_trip_partial_chunks_and_lifetimes():
    config = native.Config("gpu-test@fixed", 2, 2, 8, 4, "float16")
    config.chunk_bytes = 113  # Deliberately crosses both element and K/V boundaries.
    listener = native.Listener("127.0.0.1", 0)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(listener.accept, config)
        sender = native.Engine.connect("127.0.0.1", listener.port, config)
        receiver = future.result(5)
    context = native.CudaStaging(torch.cuda.current_device())
    stream = torch.cuda.Stream()
    with torch.cuda.stream(stream):
        source = torch.arange(2 * 9 * 4 * 2 * 8, device="cuda", dtype=torch.float16).reshape(
            2, 9, 4, 2, 8
        )
        targets = [
            torch.full((2, 11, 4, 2, 8), -1, device="cuda", dtype=torch.float16) for _ in range(2)
        ]
        source_view = native.cuda_layer(context, source, stream.cuda_stream)
        destination_views = [
            native.cuda_layer(context, target, stream.cuda_stream) for target in targets
        ]
    identifier = native.TransferId(77, 1)
    sender.begin_send(identifier, [8, 1, 5])
    receiver.begin_recv(identifier, [2, 9, 4], destination_views)
    for layer in range(2):
        sender.send_layer(identifier, layer, source_view)
    del source_view, destination_views
    sender.wait_all(10000)
    receiver.wait_all(10000)
    for target in targets:
        assert torch.equal(target[:, [2, 9, 4]], source[:, [8, 1, 5]])
        assert torch.all(target[:, [0, 1, 3, 5, 6, 7, 8, 10]] == -1)
