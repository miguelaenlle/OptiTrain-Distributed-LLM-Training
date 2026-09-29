from concurrent.futures import ThreadPoolExecutor

import _native as native


def test_python_round_trip_and_gil_release():
    config = native.Config("test@fixed", 2, 2, 8, 4, "float16")
    config.chunk_bytes = 37
    config.timeout_ms = 2000
    listener = native.Listener("127.0.0.1", 0)
    with ThreadPoolExecutor(1) as threads:
        pending = threads.submit(listener.accept, config)
        sender = native.Engine.connect("127.0.0.1", listener.port, config)
        receiver = pending.result(timeout=3)
    identifier = native.TransferId(1, 1)
    source = native.HostLayer(3, 256)
    source.assign(bytes(i % 251 for i in range(3 * 256)))
    targets = [native.HostLayer(3, 256), native.HostLayer(3, 256)]
    sender.begin_send(identifier, [2, 0])
    receiver.begin_recv(identifier, [0, 2], targets)
    for index in range(2):
        sender.send_layer(identifier, index, source)
    sender.wait_all(2000)
    receiver.wait_all(2000)
    original = source.snapshot()
    for target in targets:
        actual = target.snapshot()
        assert actual[:256] == original[512:]
        assert actual[256:512] == bytes(256)
        assert actual[512:] == original[:256]
    assert sender.poll_finished()[0].success
    assert receiver.poll_finished()[0].success
    assert not sender.poll_finished()


def test_native_rejects_unsupported_dtype():
    try:
        native.Config("test", 1, 1, 1, 1, "float32")
    except native.TransferError:
        pass
    else:
        raise AssertionError("unsupported dtype accepted")
