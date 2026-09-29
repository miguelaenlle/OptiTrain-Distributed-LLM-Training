"""Run a fixed 200-prompt exact-token gate against live vLLM endpoints.

This is correctness tooling, not a load/goodput benchmark. Endpoints must already
be running with the model and connector settings in docs/integration.md.
"""

import argparse
import json
import secrets
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

MODEL = "Qwen/Qwen2.5-0.5B-Instruct"
REVISION = "7ae557604adf67be50417f59c2c2f167def9a775"


def corpus(tokenizer):
    lengths = [1, 15, 16, 17, 31, 32, 33, 128, 512, 1024, 4096, 8192]
    topics = [
        "Explain how a bicycle works.",
        "Describe a tree in winter.",
        "Compare sorting algorithms.",
        "Write a short story about a river.",
    ]
    for i in range(200):
        length = lengths[i % len(lengths)]
        seed = tokenizer.encode(
            f"Request {i}. {topics[i % len(topics)]} ", add_special_tokens=False
        )
        tokens = (seed * ((length + len(seed) - 1) // len(seed)))[:length]
        yield {"index": i, "prompt": tokens, "max_tokens": [8, 16, 32][i % 3]}


def complete(url, body):
    req = urllib.request.Request(
        url.rstrip("/") + "/v1/completions",
        json.dumps(body).encode(),
        {"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=180) as response:
        data = json.load(response)
    choice = data["choices"][0]
    ids = []
    for token in choice["logprobs"]["tokens"]:
        if not token.startswith("token_id:"):
            raise ValueError("server did not return token IDs; refusing text-only parity")
        ids.append(int(token.removeprefix("token_id:")))
    return {"token_ids": ids, "finish_reason": choice["finish_reason"]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--prefill", required=True)
    parser.add_argument("--decode", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--mode",
        choices=["whole-request", "streaming"],
        required=True,
        help="Label matching how the serving pair was launched",
    )
    args = parser.parse_args()
    from transformers import AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(MODEL, revision=REVISION)
    incarnation = secrets.randbits(63) or 1
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x") as output, ThreadPoolExecutor(2) as pool:
        for item in corpus(tokenizer):
            body = {
                "model": MODEL,
                "prompt": item["prompt"],
                "max_tokens": item["max_tokens"],
                "temperature": 0,
                "seed": 0,
                "logprobs": 1,
                "return_tokens_as_token_ids": True,
                "stream": False,
            }
            baseline = complete(args.baseline, body)
            repeated = complete(args.baseline, body)
            if baseline != repeated:
                raise RuntimeError(f"colocated output unstable at prompt {item['index']}")
            params = {"kvtransfer": {"incarnation": incarnation, "sequence": item["index"] + 1}}
            producer = pool.submit(
                complete, args.prefill, {**body, "max_tokens": 1, "kv_transfer_params": params}
            )
            consumer = pool.submit(complete, args.decode, {**body, "kv_transfer_params": params})
            try:
                producer.result()
                actual = consumer.result()
            except Exception as error:
                output.write(json.dumps({**item, "mode": args.mode, "error": str(error)}) + "\n")
                output.flush()
                raise
            matched = actual == baseline
            output.write(
                json.dumps(
                    {
                        **item,
                        "mode": args.mode,
                        "model_revision": REVISION,
                        "baseline": baseline,
                        "actual": actual,
                        "matched": matched,
                    }
                )
                + "\n"
            )
            output.flush()
            if not matched:
                raise RuntimeError(f"token parity failed at prompt {item['index']}; see {path}")
    print(f"200/200 exact token sequences matched; results: {path}")


if __name__ == "__main__":
    main()
