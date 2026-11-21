# Benchmark Report: gemma-4-26B-A4B verdict re-baseline and fix validation

- **Date**: 2026-11-19/20
- **Test Host**: the benchmark host — AMD EPYC 7773X, 128 threads, 512 GiB, Milan-X AVX2
- **Harness**: `ghcr.io/ggml-org/llama.cpp:server-b8740` (build `8740`, digest
  `sha256:6c02b386613f2a5d64326c28ad37b43270671674550e2e948534cd75b71510e5`), 64 threads,
  `--ctx-size 65536`, benchmark port 8082
- **Weights**: `unsloth/gemma-4-26B-A4B-it-GGUF@gemma-4-26B-A4B-it-UD-Q4_K_M.gguf`
- **Tool**: `openqa-llm-investigate` at `22575395` (unfixed) for the re-baseline;
  `6cd1d1dc` (fixed) for the validation. `22575395`
  and its base `93a1dcc6` differ only in a docstring, one local variable name and `_GATES`
  built from `_CANONICAL`, i.e. the classification path is identical
- **Runner**: `scripts/benchmarking/run_investigate_eval.py` (resumable, `--start-index`)
- **Raw results**: `/tmp/gemma_qat/reb8740_run{1,2}.json`, `/tmp/gemma_qat/fix_*.json`

## Re-baseline

Two complete 50-job runs of the incumbent, one container restart between them, same image tag and
weights, tool unfixed:

| Model | Run | Answered | Correct (all 50) | Correct (answered) | Median | p90 | Max | Timeouts | Errors |
|---|---|---|---|---|---|---|---|---|---|
| gemma-4-26B-A4B-it | 1 | 50 | 31/50 (62.0%) | 62.0% | 201.5 s | 368.5 s | 476.3 s | 0 | 0 |
| gemma-4-26B-A4B-it | 2 | 50 | 31/50 (62.0%) | 62.0% | 202.6 s | 343.9 s | 780.2 s | 0 | 0 |

Per-class, identical in both runs (the runner's own matrix):

| Class | TP | FP | FN | Precision | Recall |
|---|---|---|---|---|---|
| Product | 31 | 18 | 1 | 63.3% | 96.9% |
| Test | 0 | 1 | 18 | 0.0% | 0.0% |
| Infrastructure | 0 | 0 | 0 | — | — |

Per dataset half (the imbalance this document records: 22 product / 3 test, then 10 product / 15 test):

| Half | Correct | All-`product` ceiling | Latency median |
|---|---|---|---|
| entries 0-24 | 22/25 (88.0%) | 22/25 (88.0%) | 203.1 / 204.5 s |
| entries 25-49 | 9/25 (36.0%) | 10/25 (40.0%) | 191.4 / 202.1 s |

Latency medians are given as run 1 / run 2.

The model is not degenerate in the strict sense: it answers `Test issue` once, on `6317891`
(`package-dependency@aarch64`), which is labelled product. Every one of the 18 test-labelled jobs
answers `Product bug`, including the four the dataset's stored `llm_verdict` field records as `test`
(`6317910`, `6317892`, `6315059`, `6315020`). Latency is above the 158 s reference but well inside
the 600 s LLM timeout; the two run-2 jobs above it (690.8 s, 780.2 s) still completed.

Reproduction (`deploy_candidate.sh` is in the o3-config repository):

```sh
deploy_candidate.sh stop --worker <worker> 8082
deploy_candidate.sh start --worker <worker> \
  --image ghcr.io/ggml-org/llama.cpp:server-b8740 \
  "unsloth/gemma-4-26B-A4B-it-GGUF@gemma-4-26B-A4B-it-UD-Q4_K_M.gguf" 8082 64 65536
python3 -u scripts/benchmarking/run_investigate_eval.py --dataset eval_dataset.json \
  --llm-url http://127.0.0.1:8082/v1/chat/completions --llm-model gemma-4-26B-A4B-it \
  --llm-timeout 600 --retries 1 --output /tmp/gemma_qat/reb8740_run2.json
```

## Run-to-run variance

| Measure | Value |
|---|---|
| Verdict agreement, run 1 vs run 2 | **50/50 (100%)** |
| Disagreeing jobs | none |
| Accuracy difference | 0.0 points |
| Median latency difference | +1.1 s |
| p90 difference | −24.7 s |

Two full runs of one configuration therefore differ by zero verdicts, far below the 20%
kill criterion. The verdict at `TEMPERATURE` (1.0) is stable for this job set, so no arm comparison
in this study is confounded by sampling noise, and the collapse is not an instability artefact.

## Ceiling

The dataset labels 32 product and 18 test, so a model that always answers `Product bug` scores
**32/50 = 64.0%** (88.0% on the first half, 40.0% on the second). The measured 62.0% is *below* that
ceiling by exactly the single product-labelled job the model called `Test issue`.

Consequences:

- **78.0% (39/50) is not reproducible** on this image, weights and tool. By the H5 criterion
  the figure is withdrawn as a reference: the same build the record names now scores 62.0%, and a
  model with no usable `Test` column cannot reach 39/50 on these labels at all.
- The record itself does not describe this configuration, and the dataset says so: its stored
  per-entry `llm_verdict` values score **22/50 (44.0%)**, not 39/50, with 28 product, 18 test and 4
  infrastructure answers. They also match today's production comments on all 50 jobs, so they are a
  faithful snapshot of what the tool answered when the dataset was built (2026-11-16, tool revision
  `5a9450bf` or earlier), and they differ from this benchmark's verdict on 21 of 50 jobs (see
  "Harness versus production"). No observable configuration reproduces 39/50, so the figure records a
  measurement that no longer exists; the ceiling argument below holds independently of which one it
  was.
- The honest number to deploy against is **62.0% (31/50)** with zero `Test` recall, which is worse
  than the 64.0% a constant `Product bug` answer would reach. The single-class collapse is confirmed
  at tool revision `22575395`/`93a1dcc6` on build 8740, in addition to the MTP/QAT measurements in [gemma4_mtp_qat.md](gemma4_mtp_qat.md) on
  build 12464 with QAT weights.

## Held-out validation

The fixed tool (`6cd1d1dc`: the verdict rule calls a panic a symptom and points at the "Test commits
since the last good job" section) on the 16 held-out entries (every third entry, fixed
before any prompt change), fresh container, same image and weights:

| Split | Correct | Test predicted | Test correct | Median | p90 |
|---|---|---|---|---|---|
| held-out 16 | 10/16 (62.5%) | 1 | 0 | 198.5 s | 251.1 s |

All five test-labelled held-out jobs answer `Product bug`. The single `Test issue` is `6317891`
(labelled product), which the unfixed tool also answers `test`, so **the held-out verdicts are
identical to the re-baseline's, 16/16**: the rule change moves nothing on jobs it was not derived from.

The reason is measurable in the prompts, not in the model. `--print-prompt` with the fixed tool over
all 18 test-labelled jobs (`/tmp/gemma_qat/evidence_scan.txt`) shows the section the new rule points
at carries decisive content for only a third of them:

| Evidence in prompt 1 | Jobs | Count |
|---|---|---|
| marked commit (`[stack] 91db3b29a7`, `[failed module] 5d72ad7531`) | 6317910, 6317892, 6318100, 6317972, 6315007, 6315008 | 6 |
| bare count, nothing touching the failing module | 6315059, 6315006, 6315011, 6315012, 6315014, 6315015, 6315016, 6315017, 6314991 | 9 |
| no "Test commits" section at all | 6315010, 6315020, 6314993 | 3 |

Twelve of the eighteen test labels need knowledge the tool never has (the defect lives in a module
other than the failing one, e.g. `bootloader_setup.pm` behind the `jeos-ltp-*` reboots), so the
prompt-side ceiling for this fix is 31 + 6 = 37/50 = 74%, and all five held-out test jobs sit in the
count-only or no-section rows. A zero `Test` column there refutes the rule as the *sufficient* cause,
which is what this run shows.

## All 50

Same fixed tool, all 50 entries; the 16 held-out records are the run above (resumed report), the
other 34 ran on the same container instance:

| Tool | Correct | Accuracy | Test predicted | Test correct | Median | p90 | Max | Errors |
|---|---|---|---|---|---|---|---|---|
| unfixed `22575395` (mean of run 1/2) | 31/50 | 62.0% | 1 | 0 | 202.1 s | 356.2 s | — | 0 |
| fixed `6cd1d1dc` | 28/50 | **56.0%** | 6 | 1 | 247.4 s | 449.7 s | 731.9 s | 0 |

Five verdicts differ from the re-baseline (45/50 agreement): `6317910` product → test, which is
correct, and `6339285`, `6317999`, `6318414`, `6317946` product → test, which are wrong. The rule
therefore converts **1 of 6** jobs that carry a marked test commit and buys that single hit with four
false `Test issue` answers. Accuracy falls three points below the unfixed tool and eight below the
64.0% constant-`Product bug` ceiling. The success bar (≥38/50) is not met, and per the decision
criteria this is outcome 3: the ceiling is information absence, not the rule. Latency rises by ~45 s
median, consistent with longer test-branch replies; it stays inside the 600 s LLM timeout.

## Per-class

| Tool | Class | TP | FP | FN | Precision | Recall |
|---|---|---|---|---|---|---|
| unfixed | Product | 31 | 18 | 1 | 63.3% | 96.9% |
| unfixed | Test | 0 | 1 | 18 | 0.0% | 0.0% |
| fixed | Product | 27 | 17 | 5 | 61.4% | 84.4% |
| fixed | Test | 1 | 5 | 17 | 16.7% | 5.6% |

The fixed tool's `Test` column is non-zero, so it is no longer a single-class classifier — but at 16.7%
precision and 5.6% recall the column is not usable for triage, and the product column loses 3.2 points
of precision. The `Infrastructure` column stays empty in both, matching the dataset, which contains no
infrastructure labels.

## Verdict

**Cause**: the collapse is a prompt-information defect, not a model defect and not instability. The
labels record the *cause* of a failure (a defect in the test code), the prompt shows only its
*symptom* (a kernel panic, a missing needle), and for 12 of the 18 test labels the cause lives in a
module other than the failing one, so no prompt text can carry it. Two independent measurements pin
this down: verdicts are reproducible (50/50 between two full runs, 5/5 on repeated single jobs, so
`TEMPERATURE` is not the culprit), and a rule that names the cause explicitly changes 5 of 50 verdicts
— one correct, four wrong.

**Fix**: rejected and reverted. `6cd1d1dc` scored 28/50 against the unfixed 31/50, with `Test`
precision 16.7% and recall 5.6%; `3431f19c` drops it. The remaining test-code detection needs evidence
the tool does not fetch — a known-issues/ticket lookup or the culprit module from the ticket — not
another rule, which is where a follow-up study should start.

**Reference numbers** for `gemma-4-26B-A4B-it:UD-Q4_K_M` (build 8740, digest `sha256:6c02b386…`,
tool `22575395`, 64 threads, 65536 ctx, 50 jobs, 0 errors):

| Measure | Value |
|---|---|
| Accuracy, all jobs | **62.0% (31/50)** |
| Accuracy, answered jobs | 62.0% (50/50 answered) |
| `Test` recall / precision | 0.0% / 0.0% |
| Constant-`Product bug` ceiling | 64.0% (32/50) |
| Prompt-side ceiling with the marked evidence | 74.0% (37/50) |
| Median / p90 per investigation | 202 s / 356 s |

The 78.0% (39/50) and 158 s figures are withdrawn: neither the benchmark's 62.0% nor the dataset's
own stored 44.0% reproduces them, and two runs of the benchmark agree verdict for verdict, so the old
number describes a configuration that no longer exists. Every accuracy quoted in this repository now
carries its image build, tool revision and, where it matters, its context window.

**Against the previous model, not against this one** (measured 2026-11-20): the
dataset's stored `llm_verdict` column answers 28 product / 18 test / 4 infrastructure and scores
22/50 (44.0%) — it is the output of `gemma-4-E4B-it:UD-IQ2_M`, which production ran until the quadlet
changed on 2026-11-18 12:37, two days after the dataset was built. So "44.0% then, 62.0% now" is two
models *and* two harnesses, not one model regressing. Attribution on a 20-job subset rules out the
tool revision (`5a9450bf` under this harness: 19 product / 1 test) and the 4096-token gateway window
(0 test in 19 answers), leaving the model switch as the explanation
([llm_verdict_dataset_audit.md](llm_verdict_dataset_audit.md), "Attribution"). The incumbent has posted only 8
labelled comments since 2026-11-18 (5 product / 3 test), so its production accuracy is unmeasured;
the 62.0% above is a harness number for 26B-A4B and remains the only reproducible figure for it.

**Deployment statement**: make no change — keep the incumbent
`gemma-4-26B-A4B-it:UD-Q4_K_M` and the deployed tool; no QAT variant, no MTP draft head and no prompt
change was shown to help, and the only credible accuracy numbers in circulation for this setup are
the dataset's 44.0% (production comments) and the benchmark's 62.0% (harness), against a 64.0%
constant-`Product bug` ceiling. Which of the two describes production today is an open question, not a deployment decision.

## Parameter comparison at four slots

Because the verdict column cannot rank models (every arm answers one class on these jobs),
this compares throughput and footprint at the shape production runs: build `12464` (the only pinned
build that loads the MTP draft head), 64 threads, 65536 ctx, the server's 4 slots as reported by
`/props` on both the production and the benchmark container, same six dataset jobs per arm — two
investigations one at a time, then four launched concurrently.

| Arm | Single-request (2 jobs) | Concurrent, per job (4 jobs) | Batch wall | Footprint |
|---|---|---|---|---|
| `UD-Q4_K_M` (incumbent) | 217 s, 142 s | 453, 417, 393, 357 s | 454 s | 15.78 GiB |
| `qat-UD-Q4_K_XL` | 156 s, 116 s | 442, 350, 457, 404 s | 457 s | 13.27 GiB |
| `qat-UD-Q4_K_XL` + MTP n-max 3 | 136 s, 761 s | 383, 344, 889, 309 s | 889 s | 13.50 GiB |

Reading:

- **QAT is ~1.3x faster and 2.5 GiB smaller per single request** (median 136 s vs 180 s) but gains
  nothing under four concurrent investigations: batch wall 457 s vs 454 s, per-job means 415 s vs
  405 s. The load is thread-bound either way, so the smaller weights buy memory, not throughput at
  the production shape.
- **MTP is not faster under load**: batch wall 889 s, twice the other arms, and it produced the run's
  only outlier on a single request (761 s). Its speculative acceptance collapses when four slots
  compete for the same 64 threads, which is consistent with the draft head adding work per token
  rather than saving it. It also still fails the output-identity criterion of the MTP/QAT evaluation ([gemma4_mtp_qat.md](gemma4_mtp_qat.md)).
- **No arm produced a different verdict class**: 18 of 18 investigations answered `Product bug`, so
  concurrency changes nothing about the collapse and no quality statement can be made from this run.
- `podman stats` reported `0B / 540.4GB` for both containers on this host, so RAM is quoted from the
  model file sizes rather than measured; the CPU figure (12-13x of one core, i.e. the 64
  threads of the benchmark container) is the only usable line and it is identical across arms.

**Recommendation**: keep the incumbent `UD-Q4_K_M`. QAT is a memory win (2.5 GiB) with no throughput
win at four slots and no measurable quality difference on a job set where every arm answers one
class; MTP costs twice the wall time under load. Neither should be deployed before a harness exists
in which the incumbent produces more than one class on a representative job sample.

## Production measurement

Measurement of the incumbent `gemma-4-26B-A4B-it:UD-Q4_K_M` as deployed behind the
production gateway (the first and the second worker, port 8080) on the stratified 20-job subset
(`eval_attrib20.json`), evaluated using `openqa-llm-investigate` with `--dry --force` via SSH tunnel
and local proxy (no comments posted, 0 errors, 0 timeouts).

Per-job verdicts and latencies:

| Job ID | Ground Truth | Stored E4B (posted) | Benchmark 8740 | Production Gateway (26B-A4B) | Latency |
|---|---|---|---|---|---|
| 6311672 | Product | Infra | Product | Product | 255.9 s |
| 6311673 | Product | Infra | Product | Product | 398.7 s |
| 6311674 | Product | Infra | Product | Product | 229.5 s |
| 6319607 | Product | Product | Product | Product | 275.3 s |
| 6317868 | Product | Test | Product | Product | 667.2 s |
| 6317874 | Product | Test | Product | Product | 289.1 s |
| 6317910 | Test | Test | Product | Product | 287.9 s |
| 6317892 | Test | Test | Product | Product | 229.6 s |
| 6315059 | Test | Test | Product | Test | 306.9 s |
| 6341261 | Product | Test | Product | Product | 156.7 s |
| 6341516 | Product | Test | Product | Product | 156.2 s |
| 6339285 | Product | Test | Product | Product | 309.7 s |
| 6330152 | Product | Product | Product | Product | 250.4 s |
| 6317928 | Product | Infra | Product | Product | 524.2 s |
| 6319613 | Product | Product | Product | Product | 165.6 s |
| 6319618 | Product | Product | Product | Product | 749.4 s |
| 6317946 | Product | Product | Product | Product | 230.4 s |
| 6315074 | Product | Product | Product | Product | 272.2 s |
| 6315010 | Test | Product | Product | Product | 137.3 s |
| 6315015 | Test | Product | Product | Product | 264.1 s |

Distribution and performance comparison:

| Measure | Stored E4B (posted) | Benchmark 8740 | Production Gateway (26B-A4B) |
|---|---|---|---|
| Verdict distribution | 8 product / 8 test / 4 infra | 20 product / 0 test / 0 infra | 19 product / 1 test / 0 infra |
| Correct vs Ground Truth | 9/20 (45.0%) | 15/20 (75.0%) | 16/20 (80.0%) |
| Product precision / recall | 42.1% / 53.3% | 75.0% / 100.0% | 78.9% / 100.0% |
| Test precision / recall | 37.5% / 60.0% | — / 0.0% | 100.0% / 20.0% |
| Agreement with stored E4B | 20/20 (100%) | 8/20 (40.0%) | 9/20 (45.0%) |
| Agreement with benchmark 8740 | 8/20 (40.0%) | 20/20 (100%) | 19/20 (95.0%) |
| Latency median / p90 | — | 202 s / 356 s (full set) | 268.2 s / 652.9 s |

Reading:

- **Production behaves almost identically to the benchmark server**: 19 of 20 verdicts agree
  (95.0%), and the gateway collapses to 95.0% `Product bug` on this subset. The 44.0% baseline in
  `eval_dataset.json` was an artefact of the predecessor model (`gemma-4-E4B-it`), not a property of
  the deployed `26B-A4B` model.
- **Not strictly 100% collapsed**: job `6315059` (`bootloader_setup.pm:562`, ground truth `test`,
  stored `test`) was correctly identified as `Test issue` in 306.9 s (where the benchmark harness
  called it `product`), reaching 100% precision and 20.0% recall on the 5 test jobs.
- **Latency is higher through the production gateway**: median 268.2 s and p90 652.9 s (vs 202 s /
  356 s on the dedicated benchmark container), reflecting gateway proxy routing and shared production
  host inference.

