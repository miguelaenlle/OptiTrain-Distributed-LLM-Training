# Replay profiles

- `h100-llama31-8b.json` (default): **model-derived**, generated on CPU using
  NVIDIA AIConfigurator 0.12.0 for H100 SXM / Llama 3.1 8B / vLLM 0.24.0,
  batch=TP=PP=1, 512 input / 16 output tokens. Performance-data hashes and SDK
  prediction provenance are embedded. It does not contain measured per-token
  timestamps, model weights or a model of continuous batching.
- `synthetic.json`: **synthetic**, 100ms first output and fifteen 20ms gaps.
  Functional fixture only; not GPU data.

Regenerate in a separate CPU preparation environment:

```sh
python -m pip install aiconfigurator==0.12.0
PYTHONPATH=src python -m orchestrator.nettest.profile \
  --model meta-llama/Meta-Llama-3.1-8B --system h100_sxm \
  --backend vllm --backend-version 0.24.0 \
  --input-tokens 512 --output-tokens 16 --output /tmp/h100-8b.json
```

The exporter refuses to overwrite an existing output or substitute synthetic
values after an estimator failure. `None` in AIC's per-prediction provenance
means no non-silicon transfer tier was reported, not measured serving accuracy.
See [NVIDIA AIConfigurator](https://github.com/ai-dynamo/aiconfigurator) and its
[license](https://github.com/ai-dynamo/aiconfigurator/blob/main/LICENSE).
