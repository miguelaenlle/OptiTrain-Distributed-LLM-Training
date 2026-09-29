"""Two-process, real-TCP fake-KV test. Optional shaping affects this container only."""

import argparse
import json
import selectors
import subprocess


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", default="build/fake_kv")
    parser.add_argument("--requests", type=int, default=100)
    parser.add_argument("--netem", action="store_true")
    args = parser.parse_args()
    shaped = False
    receiver = None
    try:
        if args.netem:
            subprocess.run(
                [
                    "tc",
                    "qdisc",
                    "add",
                    "dev",
                    "lo",
                    "root",
                    "netem",
                    "delay",
                    "1ms",
                    "rate",
                    "10gbit",
                ],
                check=True,
            )
            shaped = True
        receiver = subprocess.Popen(
            [args.binary, "recv", "127.0.0.1", "19091", str(args.requests)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        with selectors.DefaultSelector() as selector:
            selector.register(receiver.stderr, selectors.EVENT_READ)
            if not selector.select(10):
                raise RuntimeError("receiver did not become ready")
        if receiver.stderr.readline().strip() != "listening":
            raise RuntimeError("receiver startup failed")
        sender = subprocess.run(
            [args.binary, "send", "127.0.0.1", "19091", str(args.requests)],
            capture_output=True,
            text=True,
            timeout=140,
        )
        output, error = receiver.communicate(timeout=140)
        if sender.returncode or receiver.returncode:
            raise RuntimeError(f"sender: {sender.stderr}; receiver: {error}")
        for label, raw in [("send", sender.stdout), ("recv", output)]:
            record = json.loads(raw)
            assert record["completed"] == args.requests
            print(json.dumps({"mode": label, "netem": args.netem, **record}))
    finally:
        if receiver is not None and receiver.poll() is None:
            receiver.kill()
            receiver.communicate()
        if shaped:
            subprocess.run(["tc", "qdisc", "del", "dev", "lo", "root"], check=True)


if __name__ == "__main__":
    main()
