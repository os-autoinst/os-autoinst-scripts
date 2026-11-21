# Benchmark Report: gemma-4-26B-A4B MTP draft heads and QAT weights

- **Date**: 2026-11-18/19
- **Test Host**: the benchmark host — AMD EPYC 7773X, 128 threads, 512 GiB, Milan-X AVX2
- **Harness**: `ghcr.io/ggml-org/llama.cpp:server-b12464` (build `12464`, commit `d3c3e242c`,
  digest `sha256:9f11c1a27d5bdadef2ebf8c629706bf0ed48173f0fea1a78c4dd666918134a8e`)
- **Incumbent reference**: `gemma-4-26B-A4B-it:UD-Q4_K_M` — **62.0% (31/50)**, 202 s median, p90 356 s,
  re-measured 2026-11-20 on build 8740 at the current revision
  ([gemma4_verdict_regression.md](gemma4_verdict_regression.md)); the 78.0% (39/50) and 158 s quoted below are withdrawn
- **Dossier**: [gemma4_speculative_qat.md](gemma4_speculative_qat.md)

## Deployment configuration

The recorded production build 8740 predates the draft-model rework: its `--spec-type` accepts only
the n-gram types and `src/llama-arch.cpp` marks NextN/MTP tensors as "reserved for future MTP
support". The `:server` tag floats, so this run pins a specific tag and records the build string. The deploy step is `deploy_candidate.sh`, which lives in the o3-config repository:

```sh
deploy_candidate.sh start --worker <worker> \
  --image ghcr.io/ggml-org/llama.cpp:server-b12464 \
  --draft "unsloth/gemma-4-26B-A4B-it-GGUF@mtp-gemma-4-26B-A4B-it.gguf" \
  --spec-type draft-mtp --spec-draft-n-max 3 \
  "unsloth/gemma-4-26B-A4B-it-GGUF@gemma-4-26B-A4B-it-UD-Q4_K_M.gguf" 8083 64 65536
```

Build 12464 advertises `--spec-type none,draft-simple,draft-eagle3,draft-mtp,draft-dflash,…` and
`--spec-draft-n-max` (default 3), and the MTP head loads as a gemma4 assistant drafter:

```
common_speculative_init_result: loading draft model '…/mtp-gemma-4-26B-A4B-it.gguf'
llama_kv_cache: layer 3: sharing with layer 29. k = 0x…, v = 0x…
```

Two deployment facts cost a restart each and are worth recording:

- `--hf-file UD-Q4_K_M` (the quant name) no longer resolves on this build:
  `llama_model_load_from_file_impl: exactly one out metadata, path_model, and file must be defined`.
  The full file name (`gemma-4-26B-A4B-it-UD-Q4_K_M.gguf`) is required.
- The MTP head cannot be loaded as a standalone model ("Gemma4Assistant requires ctx_other to be
  set"), so a prefetch container must pass it as a draft (`--spec-draft-hf`/`--spec-draft-model`) or
  accept the expected failure after the download.

Flags are the production set (`--threads 64 --ctx-size 65536 --cache-ram 65536 -ctk q8_0 -ctv q8_0
-fa on --no-mmproj --temp 1.0 --top-p 0.95 --top-k 64`) plus the speculative flags above. Only 8082
and 8083 were used; production 8080, quadlets and nginx stayed untouched. The co-tenant
`llama-server` on 8080 kept running; `load average` on the worker ranged from 0.03 to 106 during the runs,
so every arm is reported next to its own baseline probe and ratios are preferred over absolutes.

## Speculative throughput

Cold probes (prefix cache disabled, so prefill covers the whole prompt) via
`scripts/benchmarking/bench_throughput.py --token-target 1000 --token-target 5000 --repeat 2`, which
produces 1764 and 8643 real prompt tokens:

| Configuration | Weights | Prompt tokens | Prefill tok/s | Decode tok/s | Load |
|---|---|---|---|---|---|
| gemma-4-26B-A4B-it baseline | UD-Q4_K_M | 1764 | 266.8 | 24.71 | idle |
| gemma-4-26B-A4B-it baseline | UD-Q4_K_M | 8643 | 202.7 | 21.68 | idle |
| gemma-4-26B-A4B-it + MTP n-max 3 | UD-Q4_K_M | 1764 | 267.2 | 37.96 | ~50 |
| gemma-4-26B-A4B-it + MTP n-max 3 | UD-Q4_K_M | 8643 | 195.4 | 39.24 | ~50 |
| gemma-4-26B-A4B-it + MTP n-max 6 | UD-Q4_K_M | 1764 | 265.8 | 27.38 | ~50 |
| gemma-4-26B-A4B-it + MTP n-max 6 | UD-Q4_K_M | 8643 | 192.0 | 31.03 | ~50 |
| gemma-4-26B-A4B-it baseline | UD-Q4_K_M | 1764 | 265.4 | 25.19 | ~55 |
| gemma-4-26B-A4B-it baseline | UD-Q4_K_M | 8643 | 198.2 | 21.03 | ~55 |

Ratios against the bracketing baseline probes: n-max 3 gives **1.54x decode at 1.8k** and
**1.81x at 8.6k** (warm probes: 1.73x and 1.81x), with prefill unchanged (267.2 vs 266.8, 195.4 vs
202.7 — within noise). n-max 6 is worse than the default 3 on both prompt sizes (27.4/31.0 tok/s):
longer drafts cost more verification than they recover.

H2 (1.5-3x decode, no prefill regression) holds for n-max 3. An earlier baseline probe inside the
same window reported 15.21/21.79 tok/s at `load average` 106, which is the transient that makes
back-to-back bracketing necessary; the two later baseline probes at load ~0 and ~55 agree within 3%.

## Accept rate

Server-side statistics from the container logs (`draft acceptance = … (accepted / generated)`):

| Configuration | Prompt 0 | Prompt 1 | Prompt 2 | Mean draft length |
|---|---|---|---|---|
| UD-Q4_K_M + MTP, n-max 3 | 0.729 (175/240) | 0.719 (174/242) | 0.320 (47/147) | 3.19 / 3.15 / 1.96 |
| UD-Q4_K_M + MTP, n-max 6 | 0.341 (42/123) | 0.474 (46/97) | 0.598 (49/82) | 3.00 / 3.71 / 4.50 |
| qat-UD-Q4_K_XL + MTP, n-max 3 | 0.920 (46/50) | 0.849 (45/53) | 0.800 (44/55) | 3.71 / 3.50 / 3.32 |

n-max 3 accepts 63% of the drafted tokens on the Q4_K_M weights, above the 50% threshold, and the
one hard prompt (free-form continuation, 0.32) is what limits the gain. The QAT pair (QAT weights
with the QAT-trained head) reaches 80-92%: the head matches its own quant family, which is the
cheapest acceptance win measured here.

## Output equality

`scripts/benchmarking/compare_spec_outputs.py` sends the same three greedy prompts (`temperature 0`,
`seed 42`, prefix cache disabled, 256 max tokens) to two endpoints and compares the generated text
token by token. Baseline on 8082, speculative on 8083:

| Comparison | Verdict | Common token prefixes | Note |
|---|---|---|---|
| baseline 8082 vs itself | identical (3/3) | 25/25, 1/1, 92/92 | the baseline server is deterministic |
| MTP 8083 vs itself | identical (3/3) | 11/11, 7/7, 69/69 | the speculative server is deterministic |
| baseline vs MTP n-max 3 | **DIVERGENT** | 2, 0, 8 | same divergence on a second run: 11/7/69 tokens again |
| baseline vs `--spec-type ngram-simple` (no draft model) | **DIVERGENT** | 1, 1, 1 | only prompt 0 diverges, but it does so without any draft model |

Conclusion: on this build speculative verification is **not** bit-exact, and the cause is the
verification path itself, not the MTP head — a draftless self-speculative configuration diverges as
well. Verification batches the drafted tokens, so the CPU kernels accumulate in a different order and
near-tie logits flip the argmax; each server is internally deterministic, which is why this shows up
only in the cross comparison.

That kills adopt criterion 1 ("greedy outputs are token-for-token identical") for every speculative
configuration available on this build, MTP included. The finding is a property of the harness, not of
the candidate weights: no draft head could pass it, and EAGLE-3 would be measured on the
same path.
## QAT throughput

Cold probes on an idle host, same flag set, only the weight file changed:

| Configuration | Weights | Footprint | Prompt tokens | Prefill tok/s | Decode tok/s | Decode vs baseline |
|---|---|---|---|---|---|---|
| gemma-4-26B-A4B-it baseline | `UD-Q4_K_M` | 15.78 GiB | 1764 | 266.8 | 24.71 | 1.00x |
| gemma-4-26B-A4B-it baseline | `UD-Q4_K_M` | 15.78 GiB | 8643 | 202.7 | 21.68 | 1.00x |
| gemma-4-26B-A4B-it QAT | `qat-UD-Q4_K_XL` | **13.27 GiB** | 1764 | 305.3 | **28.31** | **1.15x** |
| gemma-4-26B-A4B-it QAT | `qat-UD-Q4_K_XL` | **13.27 GiB** | 8643 | 228.6 | **26.60** | **1.23x** |

Prefill also improves (305.3 vs 266.8, 228.6 vs 202.7) and the footprint drops by 16% (13.27 vs
15.78 GiB), so H4's throughput expectation (1.1-1.2x decode at smaller size) holds. QAT is a free
win on speed and RAM; the accuracy question below is separate.

## QAT accuracy

Method: `scripts/benchmarking/run_investigate_eval.py` replays `eval_dataset.json` jobs
`6319605…6315007` (the second half, 25 jobs, prepared as a sliced dataset because the runner has no
`--start-index` yet) through `openqa-llm-investigate --dry --force`, run locally
against the benchmark container through an SSH tunnel, `--llm-timeout 600 --retries 1`. The same 25
jobs were replayed against the incumbent configuration on the same image as the control.

| Configuration | Jobs | Answered | Correct | Accuracy (all) | Accuracy (answered) | Median | p90 | Footprint |
|---|---|---|---|---|---|---|---|---|
| gemma-4-26B-A4B-it QAT + MTP n-max 3 | 25 | 24 | 9 | **36.0%** | 37.5% | 139.9 s | 278.8 s | 13.27 GiB + 0.23 GiB |
| gemma-4-26B-A4B-it incumbent control (no draft) | 25 | 24 | 9 | 36.0% | 37.5% | ~200 s | ~410 s | 15.78 GiB |
| Incumbent re-measured (build 8740, 50 jobs, 2026-11-20) | 50 | 50 | 31 | **62.0%** | 62.0% | 201.5 / 202.6 s | 368.5 / 343.9 s | 15.78 GiB |
| Incumbent reference as recorded 2026-11-17 (**withdrawn**) | 50 | 50 | 39 | 78.0% | — | 158 s | — | 15.78 GiB |

Confusion matrix of the QAT+MTP run: `Product→Product 9`, `Test→Product 15`, one timeout
(`6319617`), zero `Test` verdicts. Verdict equality with the control: **24/24 answered jobs carry
the identical verdict**, so QAT and MTP are verdict-neutral *relative to the incumbent on the same
jobs*.

That equality is degenerate, and the run exposed a regression that is older than this evaluation: the
model answers `Product bug` for **every** job, the control does the same, and nine additional probes
outside this half (three `test`-labelled jobs from the first half, plus the six-job triage) also came
back `product`. The same probes repeated on build **8740** return `product` as well, so neither QAT,
nor MTP, nor the newer runner build causes it:

| Probe | Build 12464 | Build 8740 |
|---|---|---|
| 6317910, 6317892, 6315059 (first half, ground truth `test`) | product, product, product | not run |
| 6319605, 6317992, 6318100, 6317972, 6314991, 6315020 (mixed) | product ×6 | product ×6 |
| 6315020 single run, verbose output | "Kernel panic … VFS: Unable to mount root fs" → `Classification: Product bug` | same |

The single-class collapse makes the 25-job half unusable as an accuracy oracle: the half is 15 `test`
/ 10 `product`, an all-`product` answer therefore scores exactly 40% (10/25, 36.0% here because one
job timed out), and the incumbent's 78.0% on 50 jobs cannot be reproduced by a model that never
answers `Test issue`. Both the current checkout of `openqa-llm-investigate` and the pre-2026-11-18
revision (the pre-normalisation revision, extracted with `git archive`) produce `Product bug` on the same job, so the
verdict-normalisation change is not the cause either.

## Combined configuration

`qat-UD-Q4_K_XL` plus its own QAT-trained MTP head, n-max 3, on the same host and flag set:

| Configuration | Prompt tokens | Prefill tok/s | Decode tok/s | Decode vs baseline | Accept rate |
|---|---|---|---|---|---|
| gemma-4-26B-A4B-it QAT + MTP n-max 3 | 1764 | 300.1 | **64.75** | **2.62x** | 0.92 / 0.85 / 0.80 |
| gemma-4-26B-A4B-it QAT + MTP n-max 3 | 8643 | 225.1 | **50.09** | **2.31x** | 0.85 typical |

The two gains multiply as expected (1.23x QAT × 1.88x MTP at 8.6k) and prefill does not regress.
The combination is the fastest configuration measured in this evaluation and the only one whose draft
acceptance exceeds 80%, because the head was trained with the weights.

## Per-job results

`qat-UD-Q4_K_XL` + MTP n-max 3, jobs `6319605…6315007`. "Baseline" is the historical verdict stored
in `eval_dataset.json` (gemma-4-E4B), not the 26B incumbent; the 26B control answered `product` on
all 24 answered jobs, identical to the candidate column.

| Job | Test | Ground truth | Baseline | Candidate | Seconds | Error |
|---|---|---|---|---|---|---|
| 6319605 | desktopapps-documentation-wayland@aarch64 | product | test | product | 122 |  |
| 6319617 | extra_tests_misc@aarch64 | product | product | - | 960 | timed out after 900s |
| 6317992 | gnome@aarch64_lse_smp4 | product | test | product | 281 |  |
| 6317946 | extra_tests_gnome@aarch64 | product | product | product | 187 |  |
| 6319602 | toolkits@aarch64 | product | test | product | 123 |  |
| 6319603 | openqa_bootstrap@aarch64 | product | test | product | 116 |  |
| 6318043 | tomcat@aarch64 | product | product | product | 144 |  |
| 6317891 | package-dependency@aarch64 | product | test | product | 209 |  |
| 6318100 | upgrade_Leap_15.3_gnome@aarch64 | test | test | product | 134 |  |
| 6317972 | upgrade_Leap_15.2_gnome@aarch64 | test | test | product | 145 |  |
| 6315104 | fips-container-host@aarch64 | product | product | product | 192 |  |
| 6315074 | fips-container-host@aarch64-4G-HD40G | product | product | product | 181 |  |
| 6314991 | jeos-extra@aarch64-HD24G | test | product | product | 178 |  |
| 6314993 | jeos-fips-container_host@aarch64-HD20G | test | product | product | 84 |  |
| 6315020 | jeos-selinux@aarch64-HD20G | test | test | product | 122 |  |
| 6315010 | jeos-fips@aarch64-HD20G | test | product | product | 84 |  |
| 6315006 | jeos-apparmor@aarch64-HD20G | test | product | product | 136 |  |
| 6315016 | jeos-ltp-syscalls@aarch64-HD20G | test | product | product | 105 |  |
| 6315015 | jeos-ltp-ima@aarch64-HD20G | test | product | product | 277 |  |
| 6315012 | jeos-ltp-containers@aarch64-HD20G | test | product | product | 249 |  |
| 6315017 | jeos-ltp-syscalls-ipc@aarch64-HD20G | test | product | product | 92 |  |
| 6315011 | jeos-ltp-commands@aarch64-HD20G | test | product | product | 183 |  |
| 6315014 | jeos-ltp-dio@aarch64-HD20G | test | product | product | 853 |  |
| 6315008 | jeos-container_image@aarch64-HD20G | test | product | product | 114 |  |
| 6315007 | jeos-container_host@aarch64-HD20G | test | product | product | 109 |  |

Operational notes for the next replay:

- The runner persists its JSON only after the last job, so the control run (killed at 24/25) left
  verdicts in the log but no file; its latencies above are therefore approximate and the numbers are
  quoted as ~values. The fix (writing results per job) was still open at the time of this run.
- Run the runner against the tunnel with a `ServerAliveInterval`, and pass `--llm-url` explicitly:
  without it the tool falls back to the production gateway URL, which does not resolve off the
  production host (`httpx.ConnectError: Connection refused`).
- Restart the container between arms. The 853 s job and the 960 s timeout both occur on large
  aarch64 logs and reproduce with the incumbent configuration, so they are prompt-size effects, not
  speculative-decoding artefacts.

## Verdict

**Neither component is adopted for production by this evaluation.**

| Component | Criterion | Requirement | Measured | Decided |
|---|---|---|---|---|
| MTP draft head | greedy outputs identical | every probe prompt | divergent, for MTP *and* for a draftless `ngram-simple` control | **fail (blocking)** |
| MTP draft head | decode at ~8.1k | ≥ 1.5x | 1.81x (n-max 3), 2.31x combined | pass |
| MTP draft head | accept rate | > 50% | 63% (Q4_K_M head), 80-92% (QAT head) | pass |
| MTP draft head | 25-job replay | ≥ 19/25 correct | 9/25, equal to the incumbent control | not separable |
| QAT weights | 25-job accuracy | ≥ 19/25 correct | 9/25, equal to the incumbent control on the same jobs | **fail (literal)** |
| QAT weights | decode/prefill not worse | ≥ 1.0x | 1.15-1.23x decode, 1.13-1.14x prefill | pass |
| QAT weights | footprint | drops ~16% | 13.27 vs 15.78 GiB | pass |

Deciding measurements, per component:

- **Draft head: no.** The speed target is met with margin (1.54x at 1.8k, 1.81x at 8.6k, 2.31x on QAT
  weights) and acceptance is well above 50%, but the quality-neutrality criterion cannot be met on
  this build by any draft model: `ngram-simple` — which drafts nothing from a model at all —
  diverges from the baseline at temperature 0 as well. Adopting a mechanism whose only quality
  argument is bit-exactness, on a harness where bit-exactness does not hold, would trade a known
  quality baseline for an unquantified one. The conditional EAGLE-3 follow-up does not trigger, because
  the pre-registered condition was poor MTP acceptance (<1.5x decode), which is not the case.
- **QAT weights: not yet.** Throughput and footprint both improve and the replay is verdict-identical
  to the incumbent on the same 25 jobs, but the absolute accuracy (36.0%) is a pre-existing
  single-class collapse: the model, on both weight sets and both runner builds, answers `Product bug`
  for all 24 answered jobs, including nine `test`-labelled probes outside this half. Adopting smaller
  and faster weights on top of a model that is misclassifying everything would harden a regression
  into production. The collapse needs its own investigation (prompt, thinking-channel handling,
  log truncation, ground truth) before any weight change is defensible.

Conditional follow-ups, neither triggered: the first (its gate is a 25-job replay that keeps ≥19/25, which
fails for the incumbent as well), and the second (no throughput cliff observed; the 4-slot
production interaction was not measured because the draft head is not adopted).

**Production delta: none.** Keep
`unsloth/gemma-4-26B-A4B-it-GGUF:UD-Q4_K_M` and do not add `--spec-draft-*`, `--spec-type` or
`--spec-draft-n-max` to the quadlet. This report is the only record of that decision.

**The numbers to beat have changed.** The reference is now the 2026-11-20 re-measurement of the same
build and weights: 62.0% (31/50), median 202 s, p90 356 s, against a constant-`Product bug` ceiling of
64.0% ([gemma4_verdict_regression.md](gemma4_verdict_regression.md)). The 78.0% (39/50) and 158 s recorded here are
withdrawn: two full re-runs agree verdict for verdict, so the old figure came from a configuration that
does not reproduce — the dataset stores 22/50 (44.0%) and the benchmark 31/50 (62.0%), and the two
disagree on 21 of 50 jobs — so the gap is at least partly the harness, not the weights. Surviving
requirement for further model work: the runner image must be pinned to a tag rather than the floating
`ghcr.io/ggml-org/llama.cpp:server`, which now resolves to a build newer than the 8740 recorded here.

**What would reopen this evaluation:** a harness build in which speculative verification is bit-exact at
temperature 0 (then the 1.8x decode gain becomes adoptable without a quality argument), or a QAT
replay on a job set where the incumbent answers a mix of classes (then QAT's verdict equality
becomes evidence instead of a degenerate agreement).
