# Benchmark records

- [gemma4_verdict_regression.md](gemma4_verdict_regression.md) and [llm_verdict_dataset_audit.md](llm_verdict_dataset_audit.md): the verdict quality of `openqa-llm-investigate` on the 50-job dataset, and why its verdicts collapse to one class.
- [gemma4_mtp_qat.md](gemma4_mtp_qat.md) and [gemma4_speculative_qat.md](gemma4_speculative_qat.md): the llama.cpp MTP draft-head and QAT-weight evaluation for the same model, with its dossier.

The benchmark harness is in `scripts/benchmarking/`, and the 50-job dataset is `tests/data/eval_dataset.json`.
Deployment-side records (host facts, deployment parameters, rejected candidates) live in the o3-config repository.

## Tool revisions

The verdict-quality records name tool revisions by what they changed rather than by commit hash,
because those changes were evaluated before they were upstream:

| Name used in the records | What it changes |
|---|---|
| pre-normalisation revision | reads the verdict without normalising it first |
| normalisation revision | normalises the verdict before reading it |
| base revision | the normalisation revision plus formatting only; the classification path is identical to the current revision |
| current revision | spells the verdict lines out and moves the classification into the summary header |
| rule-based fix | adds a rule that names the cause explicitly; measured and reverted |
| revert | drops the rule-based fix |
