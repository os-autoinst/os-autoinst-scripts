# Benchmark Report: Ornith-1.5-9B GSQ-RCO (IQ3_M) on AMD EPYC 7773X

- **Date**: 2026-11-18
- **Candidate**: `abenzerps/Ornith-1.5-9B-GSQ-RCO-GGUF` (file `Ornith-1.5-9B-GSQ-RCO-IQ3_M.gguf`, 3.91 GiB, 3.75 bpw)
- **Architecture**: `qwen35` hybrid, 32 layers (Gated Delta Net recurrence + attention), 8.95B dense parameters, 262144 native ctx
- **Test Host**: `openqaworker26` (w26) — AMD EPYC 7773X 64-Core (128 threads), 512 GiB RAM, Milan-X AVX2
- **Harness**: `ghcr.io/ggml-org/llama.cpp:server` build 8740 (`e34f04215`), isolated port 8082
- **Dossier**: `docs/research/ornith_1_5_9b_gsq_rco.md` (hypotheses H1-H5, adopt and kill criteria)

Correction (2026-11-20): the incumbent reference used in this file is withdrawn. Re-measuring
`gemma-4-26B-A4B-it:UD-Q4_K_M` on build 8740 at tool revision `22575395` gives **62.0% (31/50)**
and 202 s median / 356 s p90, with 0% `Test` recall against a 64.0% constant-`Product` ceiling
(the verdict re-baseline in os-autoinst-scripts (`docs/benchmarks/gemma4_verdict_regression.md`)). Every 78.0% and 158 s figure below is history,
not a bar to beat; the ratios it inspired stay valid only where both arms were measured at once.

---

## Deployment configuration

Pre-pull into the shared HF cache first, because a 3.9 GiB download does not fit the 300 s
health window of `deploy_candidate.sh`:

```sh
podman run -d --name ornith-prefetch -v /opt/llm/models:/root/.cache/huggingface:z \
  ghcr.io/ggml-org/llama.cpp:server \
  -hf abenzerps/Ornith-1.5-9B-GSQ-RCO-GGUF \
  --hf-file Ornith-1.5-9B-GSQ-RCO-IQ3_M.gguf -c 256 --no-mmproj --port 18099
```

Then benchmark with the production flag set on the isolated port:

```sh
./scripts/benchmarking/deploy_candidate.sh start \
  "abenzerps/Ornith-1.5-9B-GSQ-RCO-GGUF@Ornith-1.5-9B-GSQ-RCO-IQ3_M.gguf" 8082 64 65536
python3 scripts/benchmarking/bench_throughput.py \
  --llm-url http://127.0.0.1:8082/v1/completions \
  --llm-model abenzerps/Ornith-1.5-9B-GSQ-RCO-GGUF \
  --token-target 1000 --token-target 5000 --token-target 8500
```

The GGUF loads on build 8740 without tensor errors; `print_info` reports `arch = qwen35`,
`n_layer = 32`, `model params = 8.95 B`, `ssm_d_state = 128`, `ssm_d_inner = 4096`. RCO keeps the
48 SSM tensors in BF16, which the loader accepts.

## Throughput

Cold probes (prefix cache disabled, so the prefill rate covers the whole prompt) with the
production flag set on w26:

| Model | Quant | Weights | Prompt tokens | Cold wall (s) | Prefill tok/s | Decode tok/s | Threads |
|---|---|---|---|---|---|---|---|
| **Ornith-1.5-9B** | IQ3_M | 3.91 GiB | 1661 | 30.23 | **57.87** | **23.13** | 64 |
| **Ornith-1.5-9B** | IQ3_M | 3.91 GiB | 8136 | 150.02 | **54.90** | **17.72** | 64 |
| **Ornith-1.5-9B** | IQ3_M | 3.91 GiB | 13812 | 299.05 | **46.53** | **14.54** | 64 |
| Ornith-1.5-9B | IQ3_M | 3.91 GiB | 8136 | 226.22 | 36.29 | 16.40 | 32 |

Warm probes (prefix cache hit, only the tail re-evaluated) take 1.5-2.3 s per turn, so turns 2 and 3
of an investigation cost almost nothing as long as the prefix survives.

H1 (decode ≈ 23 tok/s) holds for short contexts and degrades to 14.5 tok/s at 13.8k tokens because
each generated token re-reads a longer KV cache. H2 (prefill 30-60 tok/s) holds: the hybrid
Gated Delta Net recurrence is clearly cheaper here than on Swift-1.5 (18.7 tok/s at 26.9B), but the
scaling is sub-linear: 32 threads reach 66% of the 64-thread prefill rate, not 50%, so the
recurrence does bound the parallel gain.

## Timeout budget

`openqa-llm-investigate` sends three requests per job: job details plus log analysis, the summary
question, and the verdict question. The nginx `llm-proxy` grants 600 s per request, not per job.

| Log size | Turn 1 prefill | Turn 2-3 prefill | Decode per turn (1000-1500 tokens) | Turn 1 total |
|---|---|---|---|---|
| 1.6k tokens | 30 s | <3 s | 45-65 s | ~80 s |
| 8.1k tokens | 150 s | <3 s | 60-85 s | ~220 s |
| 13.8k tokens | 299 s | <3 s | 70-105 s | ~370 s |

A single request therefore stays inside the 600 s proxy timeout even for the largest logs, but the
whole investigation costs 300-600 s per job on this host, because the model writes 1000-1500 tokens
of reasoning per turn at 9-18 tok/s. Measured server-side decode rates during the live replay ranged
from 8.2 to 17.9 tok/s depending on prompt-cache state, co-tenant load, and reply length.

**Co-tenant load caveat**: the benchmark container shares the host with the production
`llama-server` on port 8080, which kept ~19 of the 128 hardware threads busy during the
measurements (`load average` ~125). The numbers above are therefore production-realistic (the
eventual rollout replaces the port 8080 server rather than adding a second one) but they are not
idle-host numbers.

## MTP probe

`-mtp` is **unsupported** on the pinned `llama.cpp` build 8740 (`e34f04215`), in both roles:

- as main model (`--hf-file Ornith-1.5-9B-GSQ-RCO-IQ3_M-mtp.gguf`):
  `llama_model_load: error loading model: missing tensor 'blk.32.ssm_conv1d.weight'`
- as draft model (`-md …/Ornith-1.5-9B-GSQ-RCO-IQ3_M-mtp.gguf` next to the base file):
  `srv load_model: failed to load draft model` with the same missing-tensor error

No speculative decoding is therefore available for this candidate; H5 is falsified in the sense
that the head cannot be engaged at all. Pinning the base file stays mandatory, because omitting
`--hf-file` makes the server pick the `-mtp` sibling first and fail the same way.

## Accuracy

Method: `scripts/benchmarking/run_investigate_eval.py` replays `eval_dataset.json` through
`openqa-llm-investigate --dry --force` and compares the printed verdict header with the human
ground truth. Run on w27 against the isolated port 8082, 64 threads, production flag set,
`--llm-timeout 600 --retries 1`, dataset half `6319605…6315007` (25 jobs). The runner was executed
on the o3 host because an SSH port forward left long requests blocked with all server slots idle;
the model never saw that difference.

| Configuration | Jobs | Correct | Accuracy (all jobs) | Accuracy (answered) | Baseline (same jobs) |
|---|---|---|---|---|---|
| Ornith-1.5-9B IQ3_M | 25 | 4 | **16.0%** | 23.5% (4/17) | 32.0% |
| gemma-4-26B-A4B-it UD-Q4_K_M (incumbent, all 50) | 50 | 39 | **78.0%** | — | — |
| gemma-4-E4B-it UD-IQ2_M (previous production, all 50) | 50 | 22 | **44.0%** | — | — |

The failure is not noise, it is a collapse onto one class: of the 17 answered jobs the model says
`Product bug` 15 times, including 11 of the 15 test-code failures, and only twice deviates. The
confusion matrix is `Product→Product 4, Product→Test 2, Test→Product 11`, plus 8 timeouts.

Two limits of this measurement, stated explicitly:

- **n=25, not 50.** The first dataset half was stopped after 16 of 25 jobs when the pre-registered
  kill criteria had already fired (accuracy far below 70%, timeout rate above 15%); the runner
  persists results only on completion, so those 16 partial runs are not part of the numbers above.
- **The upstream Q4_K_M control cannot run on this build.** `ornith-ai/Ornith-1.5-9B-GGUF`
  `Q4_K_M` (5.38 GiB) fails to load with the same
  `missing tensor 'blk.32.ssm_conv1d.weight'` error as the `-mtp` head, so whether the 3.75 bpw GSQ
  quantization or the model itself causes the collapse stays undecided. It does not change the
  recommendation: only the GSQ-RCO files load on the pinned server, and those are the ones on offer.

## Latency

| Metric | Ornith-1.5-9B IQ3_M (n=25) | Criterion |
|---|---|---|
| Median per job | **322.8 s** | ≤ 158 s (incumbent) |
| p90 per job | **960.1 s** | ≤ 300 s |
| Jobs hitting the 900 s runner timeout | **8 of 25 (32%)** | 0 |
| Answering jobs only, median | 213.0 s (max 518 s) | — |

The three-turn flow costs 2-6 minutes even when it answers, because the model writes 1000-1500
tokens of reasoning per turn at 8-18 tok/s. Timeouts cluster on the large aarch64 logs
(`gnome@aarch64_lse_smp4`, `upgrade_Leap_15.x_gnome@aarch64`), which matches the throughput table:
prefill alone is 150-300 s before the first token of reasoning. Every single request stayed below
the 600 s `proxy_read_timeout`, so the production proxy would not cut a turn — the investigation as
a whole is simply too slow, and the timeouts above are the runner's 900 s per-job budget.

## Per-job results

| Job | Test | Ground truth | Baseline verdict | Candidate verdict | Seconds | Error |
|---|---|---|---|---|---|---|
| 6319605 | desktopapps-documentation-wayland@aarch64 | product | test | test | 358 |  |
| 6319617 | extra_tests_misc@aarch64 | product | product | product | 326 |  |
| 6317992 | gnome@aarch64_lse_smp4 | product | test | - | 960 | timed out after 900s |
| 6317946 | extra_tests_gnome@aarch64 | product | product | - | 960 | timed out after 900s |
| 6319602 | toolkits@aarch64 | product | test | product | 343 |  |
| 6319603 | openqa_bootstrap@aarch64 | product | test | - | 960 | timed out after 900s |
| 6318043 | tomcat@aarch64 | product | product | product | 518 |  |
| 6317891 | package-dependency@aarch64 | product | test | test | 323 |  |
| 6318100 | upgrade_Leap_15.3_gnome@aarch64 | test | test | - | 960 | timed out after 900s |
| 6317972 | upgrade_Leap_15.2_gnome@aarch64 | test | test | - | 960 | timed out after 900s |
| 6315104 | fips-container-host@aarch64 | product | product | - | 960 | timed out after 900s |
| 6315074 | fips-container-host@aarch64-4G-HD40G | product | product | product | 238 |  |
| 6314991 | jeos-extra@aarch64-HD24G | test | product | product | 238 |  |
| 6314993 | jeos-fips-container_host@aarch64-HD20G | test | product | product | 211 |  |
| 6315020 | jeos-selinux@aarch64-HD20G | test | test | product | 135 |  |
| 6315010 | jeos-fips@aarch64-HD20G | test | product | product | 131 |  |
| 6315006 | jeos-apparmor@aarch64-HD20G | test | product | product | 161 |  |
| 6315016 | jeos-ltp-syscalls@aarch64-HD20G | test | product | - | 960 | timed out after 900s |
| 6315015 | jeos-ltp-ima@aarch64-HD20G | test | product | product | 147 |  |
| 6315012 | jeos-ltp-containers@aarch64-HD20G | test | product | product | 177 |  |
| 6315017 | jeos-ltp-syscalls-ipc@aarch64-HD20G | test | product | product | 199 |  |
| 6315011 | jeos-ltp-commands@aarch64-HD20G | test | product | - | 960 | timed out after 900s |
| 6315014 | jeos-ltp-dio@aarch64-HD20G | test | product | product | 200 |  |
| 6315008 | jeos-container_image@aarch64-HD20G | test | product | product | 213 |  |
| 6315007 | jeos-container_host@aarch64-HD20G | test | product | product | 254 |  |

## Hypothesis outcomes

Closure of the pre-registered hypotheses from `docs/research/ornith_1_5_9b_gsq_rco.md`:

| Hypothesis | Prediction | Outcome |
|---|---|---|
| H1 decode | ~23 tok/s, bandwidth-bound at 3.91 GiB/token | **confirmed for short contexts** (23.1 tok/s at 1.6k) and degrades to 14.5 tok/s at 13.8k, so the KV re-read, not the weights, becomes the limit |
| H2 prefill | 30-60 tok/s, 85-170 s for a 5k log | **confirmed and better than Swift-1.5**: 46.5-57.9 tok/s, 150 s for 8.1k tokens; 32 threads reach 66% of the 64-thread rate, so the recurrence bounds but does not dominate the scaling |
| H3 quantization | between the E4B (52.0%) and gemma-26B (78.0%) tiers | **falsified, far below**: 16.0%, worse than the 44.0% baseline; the upstream Q4_K_M control needed to separate quant damage from model behaviour cannot load on build 8740 |
| H4 harness | `reasoning_content` breaks the verdict header | **falsified**: verdicts parse on the first attempt with production parity flags, all three turns included; thinking text arrives inside the normal message content |
| H5 MTP | draft head usable with `-md` after pinning the base file | **falsified**: the `-mtp` GGUF cannot be loaded in either role (`missing tensor 'blk.32.ssm_conv1d.weight'`) |

## Observed server behaviour

Two host-level effects shaped the measurements and are worth knowing before the next candidate run:

- **Prompt-cache checkpoint invalidation on w26.** Logs of the long-running benchmark container
  repeat `erased invalidated context checkpoint (pos_min = …, n_tokens = …)` and then
  `n_tokens = 0, memory_seq_rm [0, end)` followed by `prompt processing progress … progress = 0.32`,
  i.e. the server throws away its prefix checkpoints and re-evaluates the prompt from scratch. This
  fits the 900 s jobs on the 4-slot, `--cache-ram 65536` server that had been serving for hours,
  while a freshly started server on w27 answered the same jobs in 135-520 s. Hybrid SSM state cannot
  be partially restored, so a stale checkpoint costs the whole prefill again.
- **Co-tenant CPU and memory bandwidth.** The production `llama-server` on port 8080 kept ~19 of
  128 hardware threads busy on both workers (`load average` ~125), so the benchmark container never
  had the host to itself; decode rates during the replay ranged from 8.2 to 17.9 tok/s.

## Verdict

**Do not adopt `abenzerps/Ornith-1.5-9B-GSQ-RCO-GGUF`.** All three adopt criteria fail; two of them
fail by a wide margin:

| Criterion | Required | Measured | Decided |
|---|---|---|---|
| Accuracy on the dataset | ≥ 80.0% (incumbent 78.0%) | 16.0% (4/25) | fail |
| Median per job | ≤ 158 s | 322.8 s | fail |
| p90 per job | ≤ 300 s | 960.1 s | fail |
| Jobs at the request timeout | 0 | 8/25 at the 900 s runner budget | fail |

The speed is not the problem — 46-58 tok/s prefill and 14.5-23 tok/s decode are perfectly usable on
this host, and a single turn stays under the 600 s proxy limit. The problem is quality: the model
answers `Product bug` to 15 of 17 failures, including 11 of 12 test-code failures, which makes its
comments worse than the gemma-4-E4B baseline it would replace (44.0%). A model that needs 5 minutes
per job and still mislabels the majority of failures has no role in the o3 investigation flow.

**Consequence for plan 002 Task 3.1**: deploy `gemma-4-26B-A4B-it:UD-Q4_K_M` on w26/w27 unchanged.
This candidate is recorded as rejected, with the pre-registered kill criteria as the reason.

**What would reopen it**: a loadable higher-bit quant of the same base model on a newer `llama.cpp`
(both the upstream `Q4_K_M` and every `-mtp` file fail on build 8740 with
`missing tensor 'blk.32.ssm_conv1d.weight'`), plus a measurement that the single-class collapse is
quantization damage rather than the model's own behaviour. Until then the GSQ-RCO files are the only
loadable variant and that variant is rejected.

**Operational notes for future replays**:

- Run `run_investigate_eval.py` on the o3 host against `http://w2x:8082` rather than through an SSH
  port forward — forwarded requests can hang with all server slots idle, which the runner records as
  a model timeout.
- Pre-pull each GGUF into `/opt/llm/models` first; a 4-5 GiB download does not fit the 300 s health
  window of `deploy_candidate.sh`.
- `deploy_candidate.sh` hardcodes `WORKER=w26`, so `start`/`stop 8082` always acts on w26 even when
  the log message suggests otherwise; stopping w27's container needs an explicit command. This cost
  one aborted replay half during this evaluation and is not yet fixed in the script.
- Restart the benchmark container between long replays instead of reusing it for hours, see the
  checkpoint invalidation above.
