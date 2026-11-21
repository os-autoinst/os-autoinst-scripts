# Benchmark records

- [gemma4_verdict_regression.md](gemma4_verdict_regression.md) and [llm_verdict_dataset_audit.md](llm_verdict_dataset_audit.md): the verdict quality of `openqa-llm-investigate` on the 50-job dataset, and why its verdicts collapse to one class.
- [gemma4_mtp_qat.md](gemma4_mtp_qat.md) and [gemma4_speculative_qat.md](gemma4_speculative_qat.md): the llama.cpp MTP draft-head and QAT-weight evaluation for the same model, with its dossier.

The benchmark harness is in `scripts/benchmarking/`, and the 50-job dataset is `tests/data/eval_dataset.json`.
Deployment-side records (host facts, deployment parameters, rejected candidates) live in the o3-config repository.
