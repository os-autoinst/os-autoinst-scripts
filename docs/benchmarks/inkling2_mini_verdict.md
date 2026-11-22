# Benchmark Report: Inkling-2-Mini review quality, with a same-build control (poo#210528)

- **Date**: 2026-11-22
- **Candidate**: `unsloth/Inkling-2-Mini-GGUF` `Mainline_Support` `UD-Q4_K_M` on
  `ghcr.io/ggml-org/llama.cpp:server-b12627` (build 12627, commit `225a5345b`), w26, port 8082
- **Control**: `unsloth/gemma-4-26B-A4B-it-GGUF@gemma-4-26B-A4B-it-UD-Q4_K_M.gguf` on the **same**
  pinned build, same host, same port, so model and build drift are separable
- **Tool**: `openqa-llm-investigate` at revision `22575395` (the production revision), `--dry --force`,
  nothing posted
- **Runner**: `scripts/benchmarking/run_investigate_eval.py` (resumable via `--start-index`),
  `--llm-timeout 600 --retries 1`, executed on the o3 host against `http://w26:8082`
- **Effort**: candidate runs through `scripts/benchmarking/params_proxy.py` (o3, 8089) with
  `reasoning_effort=minimal` (0.1); the control runs direct, so it sends no effort and the tool's own
  `TEMPERATURE 0.3` applies to both
- **Dataset**: `tests/data/eval_dataset.json`, 50 entries, 32 `product` / 18 `test`
- **Raw results**: `/tmp/inkling/cand50_run1.json`, `/tmp/inkling/cand50_run2.json`,
  `/tmp/inkling/control50.json`

## Full-50 replay

Candidate, two complete runs, one container, same flags and effort:

| Run | Window | Answered | Correct (all 50) | Median | p90 | Max | Timeouts | Errors |
|---|---|---|---|---|---|---|---|---|
| run 1 | 08:57-11:47 | 50/50 | **24/50 (48.0%)** | 190.9 s | 324.7 s | 469.2 s | 0 | 0 |
| run 2 | 11:50-13:11 | 50/50 | **25/50 (50.0%)** | 82.6 s | 145.3 s | 233.1 s | 0 | 0 |

Both runs completed with the runner's resume mechanism (`--start-index` chunks of 12-24 jobs, same
`--output` file), so no job was run twice inside a run and no job was lost. Latency is the co-tenant
load: run 1 ran while w26 was loaded (load 14.71 at the start of the phase), run 2 and the control ran on
a quieter host (load 2.11 at the control deploy). The median difference is 108 s on the same
configuration, so latency comparisons inside this report use the control measured on the same day, and
the accuracy difference between run 1 and run 2 (1 job) is the stability number below.

Verdict parsing never failed: 50/50 headers parsed in both runs, so the jinja template and the tool's
`**LLM Investigation summary:**` header work for this model.

## Incumbent control

The incumbent on the same pinned build, one complete run, 13:13-17:16:

| Arm | Correct (all 50) | Median | p90 | Max | Timeouts | Errors |
|---|---|---|---|---|---|---|
| gemma-4-26B-A4B-it, build 12627 | **31/50 (62.0%)** | 192.9 s | 326.1 s | 481.3 s | 0 | 0 |
| gemma-4-26B-A4B-it, build 8740 (recorded, plan 006) | 31/50 (62.0%) | 202 s | 356 s | 780 s | 0 | 0 |

The control reproduces the recorded reference exactly on accuracy and on the per-class matrix
(`Product` 31 TP / 18 FP / 1 FN, `Test` 0 TP / 1 FP / 18 FN), so build drift 8740 → 12627 moves latency
by ~10 s median and nothing on classification. The candidate is **12-14 points below** the same-build
control, and 16-18 points below the constant-`Product` ceiling of 64.0%.

## Per-class

| Arm | Class | TP | FP | FN | Precision | Recall |
|---|---|---|---|---|---|---|
| candidate run 1 | Product | 21 | 15 | 11 | 58.3% | 65.6% |
| candidate run 1 | Test | 3 | 11 | 15 | 21.4% | **16.7%** |
| candidate run 2 | Product | 20 | 13 | 12 | 60.6% | 62.5% |
| candidate run 2 | Test | 5 | 12 | 13 | **29.4%** | **27.8%** |
| control (b12627) | Product | 31 | 18 | 1 | 63.3% | 96.9% |
| control (b12627) | Test | 0 | 1 | 18 | 0.0% | 0.0% |

The candidate moves `Test` recall off zero (the incumbent's defining failure) but pays for it: 11-12
`Product` jobs are answered `Test`, so `Product` recall falls from 96.9% to 62.5-65.6%. `Test` precision
stays under the bar (21-29% against the required 50%). The answer distribution is not degenerate
(both classes are produced), so the single-class collapse is a model-capable-vs-model-reliable
difference, not a formatting defect.

## Run-to-run agreement

43 of 50 verdicts agree between the two candidate runs: **86.0%**, under the ≥ 90% criterion. The seven
disagreements:

| Job | Ground truth | run 1 | run 2 |
|---|---|---|---|
| 6317868 | product | test | product |
| 6317910 | test | product | test |
| 6317972 | test | product | test |
| 6319603 | product | product | test |
| 6319606 | product | product | test |
| 6319608 | product | test | product |
| 6319615 | product | product | test |

Six of the seven sit on `product`-labelled jobs and flip in both directions, so the drift is not a
one-sided bias. The control arm reproduces the recorded reference, so drift is the candidate's, not the
harness's. Candidate run 1 agrees with the control on 37 of 50 jobs.

## Test-labelled jobs

Of the 18 `test`-labelled jobs, plan 006's evidence scan established that prompt 1 carries a marked test
commit for 6 of them (`6317910`, `6317892`, `6318100`, `6317972`, `6315007`, `6315008`), a bare commit
count for 9, and no `Test commits` section for 3. The candidate's behaviour on those six, per run, with
whether its analysis text quotes a commit hash:

| Job | run 1 verdict | run 1 hash | run 2 verdict | run 2 hash | control verdict |
|---|---|---|---|---|---|
| 6317910 | product | quoted | **test** | not quoted | product |
| 6317892 | **test** | quoted | **test** | quoted | product |
| 6318100 | **test** | quoted | **test** | quoted | product |
| 6317972 | product | quoted | **test** | quoted | product |
| 6315007 | product | not quoted | product | quoted | product |
| 6315008 | product | not quoted | product | quoted | product |

Marked-commit jobs answered correctly: run 1 **2 of 6**, run 2 **4 of 6**, control **0 of 6**. Analysis
texts quoting a commit hash anywhere: run 1 9 of 18, run 2 12 of 18, control 7 of 18. So the candidate
does quote the marked evidence more often than the incumbent, and it converts that evidence into the
correct class for 4 of the 6 marked jobs at effort 0.1, where the incumbent converts it for 0 of 6.
For the 12 test labels whose prompt carries no marked commit, both arms answer `Product`: the ceiling
plan 006 recorded (74.0%) is information absence, and Inkling does not break it.

## Bar check

| Criterion (plan 021) | Requirement | Measured | Result |
|---|---|---|---|
| 1 | full-50 accuracy ≥ 68.0% | 48.0% / 50.0% | **fail** |
| 2 | `Test` recall ≥ 25% **and** precision ≥ 50% | 16.7%/21.4%, 27.8%/29.4% | **fail** |
| 3 | median ≤ control, p90 ≤ 600 s, zero jobs at 600 s | 190.9 and 82.6 ≤ 192.9, p90 324.7/145.3, 0 | pass |
| 4 | 13.8k prefill ≤ 400 s | 202.3 s (report in o3-config) | pass |
| 5 | two runs agree ≥ 90% | 86.0% | **fail** |
| 6 | pinned upstream image, committed flags | `server-b12627`, `--model-path` + `--server-args` | pass |
| 7 | accuracy ≤ 74.0% | 48.0% / 50.0% | pass |

Kill criteria that fired: `p90` per job 324.7 s exceeds the 300 s threshold in run 1, and the
run-to-run agreement criterion. No job reached the 600 s budget, no verdict failed to parse, the loader
accepted the layout, and perplexity was sane. Tuning was not attempted: the accuracy gap is 12-14 points
against a same-build control on the same host, and effort, quant and drafter are the tuning axes the plan
made conditional on a pass.

The conditional units are therefore not triggered: Task 3.3 (production shape) requires Task 3.2 to clear
the bar, Task 4.1 (drafter) requires quality to pass, Task 4.2 (quant escalation) requires `Q4_K_M` within
3 points of the bar, and Task 4.3 (tool flag) requires latency above the gateway budget at the selected
level, which did not happen (max 469.2 s, 0 jobs at 600 s).

## Commands

```sh
python3 -u scripts/benchmarking/run_investigate_eval.py --dataset tests/data/eval_dataset.json \
  --script-path ~/local/os-autoinst/os-autoinst-scripts/openqa-llm-investigate \
  --llm-url http://127.0.0.1:8089/v1/chat/completions --llm-model Inkling-2-Mini \
  --llm-timeout 600 --retries 1 --output /tmp/inkling/cand50_run1.json
python3 -u scripts/benchmarking/run_investigate_eval.py --dataset tests/data/eval_dataset.json \
  --script-path ~/local/os-autoinst/os-autoinst-scripts/openqa-llm-investigate \
  --llm-url http://w26:8082/v1/chat/completions --llm-model gemma-4-26B-A4B-it \
  --llm-timeout 600 --retries 1 --output /tmp/inkling/control50.json
```

The candidate ran through `params_proxy.py` with `--params reasoning_effort=minimal`; the control ran
direct against `http://w26:8082/v1/chat/completions`, so it sent no effort, as the tool does in
production.
