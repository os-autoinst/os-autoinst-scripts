# Candidate dossier: Ornith-1.5-9B GSQ-RCO (poo#210528)

Research date: 2026-11-18. Candidate for the `openqa-llm-investigate` backend on `openqaworker26`/`27`,
evaluated against `gemma-4-26B-A4B-it:UD-Q4_K_M` (78.0% accuracy, median 158 s per 3-turn
investigation, `docs/benchmarks/model_variations_benchmark.md`). This file records facts and
pre-registered criteria; the measurements and the verdict belong into `docs/benchmarks/ornith_9b_gsq_rco.md`.

Correction (2026-11-20): the incumbent reference used in this file is withdrawn. Re-measuring
`gemma-4-26B-A4B-it:UD-Q4_K_M` on build 8740 at tool revision `22575395` gives **62.0% (31/50)**
and 202 s median / 356 s p90, with 0% `Test` recall against a 64.0% constant-`Product` ceiling
(the verdict re-baseline in os-autoinst-scripts (`docs/benchmarks/gemma4_verdict_regression.md`)). Every 78.0% and 158 s figure below is history,
not a bar to beat; the ratios it inspired stay valid only where both arms were measured at once.

## TL;DR

`abenzerps/Ornith-1.5-9B-GSQ-RCO-GGUF` is a 9B dense hybrid (`qwen35` arch, Gated Delta Net + attention)
in 4.2 GB at the highest quant offered (IQ3_M, 3.75 bpw). It is 4x smaller than the current winner and
4x smaller than Swift-1.5-Qwen3.8-27B, whose GDN recurrence capped this host at 18.7 tok/s prefill and
8.19 tok/s decode. The open questions are therefore prefill latency on 5k-11k line logs and whether
3.75 bpw holds classification accuracy. Both are answered in the benchmark report, not here.

## Facts

Verified 2026-11-18 against the HF API, the model cards, and a read-only probe of `openqaworker26`.

### Model and repository

| Fact | Value |
|---|---|
| Base model | `ornith-ai/Ornith-1.5-9B`, MIT, dense 9B, 262144 native ctx |
| GGUF architecture | `qwen35` (hybrid: Gated Delta Net recurrence + attention), 8,953,803,264 tensors bytes total |
| Chat template | OpenAI-style with `<|im_start|>` markers, vision/video placeholders, `reasoning_content` thinking channel |
| Quant files | IQ2_XS 2.61 GiB (2.50 bpw), IQ2_S 2.87, IQ3_XXS 3.13, IQ3_XS 3.39, IQ3_S 3.65, IQ3_M 3.91 GiB (3.75 bpw, recommended by the card) |
| `-mtp` siblings | one Multi-Token-Prediction head per quant (layer 32), speculative-decoding draft, 3.37-4.15 GiB |
| GSQ-RCO claim | 48 critical SSM tensors kept in BF16 plus per-layer precision from Hessian sensitivity calibration, because stock low-bit quantization degrades hybrid SSM-Transformer models (card: stock IQ3_M perplexity ~75.2) |
| Upstream control | `ornith-ai/Ornith-1.5-9B-GGUF`: Q4_K_M 5.38 GiB, Q5_K_M 6.19, Q6_K 7.04, Q8_0 9.11, BF16 17.14, plus `mmproj` 0.86 GiB |

### Host and harness (probe of `openqaworker26`, 2026-11-18)

| Fact | Value |
|---|---|
| CPU / RAM | AMD EPYC 7773X, 128 threads, 512 GiB, single NUMA node, AVX2 (no AVX-512) |
| Disk | `/opt` 476 GiB total, 426 GiB free; `/opt/llm/models` holds 40 GiB of cached GGUFs |
| Port 8080 | occupied on w26; benchmark port 8082 free |
| Container | `ghcr.io/ggml-org/llama.cpp:server`, version 8740 (`e34f04215`), contains `curl` only (no `strings`, no `python3`) |
| `qwen35` support | proven on this build by the Swift-1.5-Qwen3.8-27B run, which loaded `qwen35.ssm` tensors on the same image |
| HF cache layout | `/opt/llm/models/hub/models--<owner>--<repo>` (llama.cpp `/root/.cache/huggingface` mapped there by `deploy_candidate.sh`) |
| Production parity flags | `deploy_candidate.sh`: `--threads 64 --ctx-size 65536 --cache-ram 65536 -ctk q8_0 -ctv q8_0 -fa on --no-mmproj --temp 1.0 --top-p 0.95 --top-k 64` |

## Hypotheses

H1 (throughput, decode). Decode is bandwidth-bound at one weight read per token: Swift-1.5 needed
10.95 GiB/token for 8.19 tok/s, so 3.91 GiB/token should give ~23 tok/s. Falsified if measured
decode < 12 tok/s (would mean the recurrence, not bandwidth, dominates).

H2 (throughput, prefill). Per-token compute drops 3x versus Swift-1.5 (26.9B → 9B) while GDN
recurrence latency stays, so expect 30-60 tok/s prefill and 85-170 s for a 5k-token log, 185-370 s
for 11k. Falsified if prefill stays below 25 tok/s, which would put the third prompt of a large job
past the 600 s request budget.

H3 (quantization). At 3.75 bpw with BF16 SSM tensors the model should land between the Gemma-E4B
Q4_K_M (52.0%) and Gemma-26B-A4B UD-Q4_K_M (78.0%) tiers; the upstream Q4_K_M control of the same
base model isolates quant damage from architecture limits. Falsified if IQ3_M trails the Q4_K_M
control by more than 8 percentage points.

H4 (harness). The chat template emits thinking into `reasoning_content`, so llama.cpp must be told
how to render it (`--jinja`, `--reasoning-format none`) or the three-prompt flow loses its verdict
header. Falsified if `run_investigate_eval.py` parses verdicts at the first attempt with
production-parity flags.

H5 (MTP). The `-mtp` draft head is a separate GGUF, so it can only be used with `-md` after the base
file is pinned; loading it as the main model fails, exactly like the Swift-1.5 `-mtp` case. Falsified
if `-md` yields a reproducible decode gain.

## Measurement matrix

| Axis | Values | Purpose |
|---|---|---|
| Quant | GSQ-RCO IQ2_XS, IQ3_M; upstream Q4_K_M control | H3: quant damage vs architecture limit |
| Threads | 64 (production parity), 32 | GDN recurrence scaling on 64C/128T |
| Chat template | production parity (no `--jinja`), `--jinja --reasoning-format none` | H4: verdict header survivability |
| Prompt size | ~1k, ~5k, ~11k tokens of log-shaped text | H2: request-budget headroom |
| MTP | base only, then base + `-md` draft | H5: speculative decode gain |
| Accuracy | all 50 `eval_dataset.json` entries | classification vs human ground truth |

Every endpoint run uses `scripts/benchmarking/bench_throughput.py` for timings and
`scripts/benchmarking/run_investigate_eval.py --llm-timeout 600 --retries 1` for accuracy, both on
isolated port 8082.

## Decision criteria

Adopt `Ornith-1.5-9B GSQ-RCO` over `gemma-4-26B-A4B-it:UD-Q4_K_M` only if all three hold:

1. accuracy ≥ 80.0% on the 50-job dataset (strictly above the incumbent's 78.0%), and
2. median per-job investigation ≤ 158 s and p90 ≤ 300 s, and
3. zero jobs hitting the 600 s request timeout in the 50-job replay.

Secondary preference (not sufficient alone): RAM footprint below 10 GiB and decode above 20 tok/s,
which would allow a second production slot per worker. If Ornith wins, plan 002 Task 3.1 deploys it;
if it loses, plan 002 Task 3.1 deploys Gemma-4-26B-A4B unchanged and this candidate is recorded as
rejected with evidence.

## Kill criteria

Stop the evaluation and report instead of tuning if any of these fire:

- 11k-token prefill over 400 s on the production-parity flag set (third prompt would exceed 600 s).
- p90 per-job investigation over 300 s, or any job hitting the 600 s request timeout.
- accuracy below 70.0%, i.e. more than 8 points under the incumbent.
- `llama.cpp` rejects the `qwen35`/RCO tensors (`failed to load tensor`, unknown type) on build 8740
  and no newer tag loads them without changing production behaviour.
- `run_investigate_eval.py` parses zero verdicts for both chat-template variants; the defect then
  lies in `openqa-llm-investigate` (separate checkout, separate plan), not in the model.

## Open questions

- Does the `qwen35` speculative draft actually engage for this GGUF header, or is `-mtp` a dead file
  for llama.cpp 8740?
- Is IQ2_XS usable at all on hybrid SSM (the card's own motivation for GSQ-RCO suggests low-bit
  damage is severe), or does the sweep collapse below IQ3_M immediately?
- Would the upstream Q8_0 control (9.11 GiB, still 2x smaller than Gemma-26B) beat IQ3_M on accuracy
  at acceptable speed? Only worth a run if IQ3_M lands within 2 points of the incumbent.
