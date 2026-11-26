# Candidate dossier: gemma-4-26B-A4B tuning — MTP draft heads and QAT weights (poo#210528)

Research date: 2026-11-18. Tuning dossier for the incumbent `unsloth/gemma-4-26B-A4B-it-GGUF:UD-Q4_K_M`
(78.0% accuracy, 158 s median per 3-turn investigation, `model_variations_benchmark.md` in the o3-config repository).
This file records facts and pre-registered criteria; measurements and the verdict belong into
[gemma4_mtp_qat.md](gemma4_mtp_qat.md).

Correction (2026-11-20): the incumbent reference used in this file is withdrawn. Re-measuring
`gemma-4-26B-A4B-it:UD-Q4_K_M` on build 8740 at the current revision gives **62.0% (31/50)**
and 202 s median / 356 s p90, with 0% `Test` recall against a 64.0% constant-`Product` ceiling
([gemma4_verdict_regression.md](gemma4_verdict_regression.md)). Every 78.0% and 158 s figure below is history,
not a bar to beat; the ratios it inspired stay valid only where both arms were measured at once.

## TL;DR

Four base-model candidates failed on quality, so the remaining lever is a *quality-neutral* one:
speculative decoding verifies every drafted token with the target model, and QAT weights trade
footprint for accuracy instead of hunting a better base model. Both are cheap to test (draft heads
are 0.23-0.80 GiB).

The single hard dependency is the runner: `draft-mtp` needs `NextN` tensors and a
`gemma4-assistant` architecture that build 8740 (`e34f04215`, the build recorded in
`model_variations_benchmark.md` in the o3-config repository) does **not** have. It exists on llama.cpp master,
i.e. in a newer `:server` image than the recorded one — see `Corrections` below. The deployment step must pin an
image tag and record the build string it actually ran.

## Facts

Verified 2026-11-18 against the Hugging Face API, the model cards, and the pinned `llama.cpp` sources
(`e34f04215`, master) fetched read-only from GitHub.

### Repositories and files

| Fact | Value |
|---|---|
| Incumbent weights | `unsloth/gemma-4-26B-A4B-it-GGUF`, `gemma-4-26B-A4B-it-UD-Q4_K_M.gguf` **15.78 GiB**, arch `Gemma4ForConditionalGeneration`, Apache-2.0 |
| Incumbent's own MTP head | same repo, `mtp-gemma-4-26B-A4B-it.gguf` **0.43 GiB** (Q8_0) at the repo root, plus `MTP/` F16/BF16 0.80 GiB and `MTP/mtp-…-Q8_0.gguf` 0.43 GiB |
| QAT weights | `unsloth/gemma-4-26B-A4B-it-qat-GGUF`, `gemma-4-26B-A4B-it-qat-UD-Q4_K_XL.gguf` **13.27 GiB** (QAT of `google/gemma-4-26B-A4B-it-qat-q4_0-unquantized`) |
| QAT MTP head | same repo, `mtp-gemma-4-26B-A4B-it.gguf` **0.23 GiB** (Q4_0) at the root, `MTP/` variants Q4_0 0.23, Q8_0 0.43, F16/BF16 0.80 GiB |
| Official drafter | `google/gemma-4-26B-A4B-it-assistant`, `model.safetensors` **0.78 GiB**, arch `Gemma4AssistantForCausalLM`, Apache-2.0 |
| Official drafter, GGUF | `AtomicChat/gemma-4-26B-A4B-it-assistant-GGUF`: F16 **0.80 GiB**, Q8_0 0.43, Q5_K_M 0.32, Q4_K_M **0.30**, Q4_K_S 0.30 GiB |
| EAGLE-3 speculator | `RedHatAI/gemma-4-26B-A4B-it-speculator.eagle3`, `model.safetensors` **1.73 GiB**, arch `Eagle3DraftModel`, Apache-2.0 — safetensors only, a GGUF must be converted first |
| Google QAT GGUF | `google/gemma-4-26B-A4B-it-qat-q4_0-gguf`, file `gemma-4-26B_q4_0-it.gguf` **13.45 GiB** (note the file name drops `A4B`) |

Weight of evidence: the incumbent's own repo already ships a matching MTP head, so the draft arm does
not need the QAT pull to start, and the drafter always comes from the same quant family as the target.

### Runner capability — two different builds

Flags below are read from `common/arg.cpp`, `common/common.h`, `common/speculative.cpp` and
`src/llama-arch.cpp` at the two commits, not from prose docs (`docs/speculative.md` at `e34f04215`
still documents only the n-gram types, because it predates the draft-model rework).

| Capability | Build 8740 (`e34f04215`, recorded production) | master (current `:server`, probed on the worker) |
|---|---|---|
| `--spec-type` values | `none\|ngram-cache\|ngram-simple\|ngram-map-k\|ngram-map-k4v\|ngram-mod` only | comma-separated list incl. `draft-simple,draft-eagle3,draft-mtp,draft-dflash,draft-dspark,dflash2,lilicorr` + the n-gram types |
| Draft model flag | `-md/--model-draft` (path only) | `--spec-draft-model/-md/--model-draft`, sets `path` *and* `hf_file` |
| Draft repo flag | `-hfd/-hfrd/--hf-repo-draft` (no `--hf-file-draft`) | `--spec-draft-hf/-hfd/-hfrd/--hf-repo-draft` |
| Draft length | `--draft/--draft-n/--draft-max`, default 16 | `--spec-draft-n-max`, default **3** (plus `--spec-draft-n-min`, `-n-ubatch`) |
| `--spec-type draft-mtp` | rejected: `unknown speculative decoding type without draft model` | accepted |
| `gemma4_assistant` arch | absent, `LLM_ARCH_GEMMA4` only | `LLM_ARCH_GEMMA4_ASSISTANT` / `"gemma4-assistant"` |
| NextN/MTP tensors | `// NextN/MTP tensors are currently ignored (reserved for future MTP support)` | `blk.%d.nextn.*` tensors supported |

Consequence: on build 8740 **no** MTP or EAGLE-3 variant can engage — not for lack of weights, but
because the flags and the tensor/arch support do not exist. The card invocation targets a newer build.

### Card invocations (the exact commands to reproduce)

`unsloth/gemma-4-26B-A4B-it-qat-GGUF` README, section "Run with MTP (speculative decoding)":

```bash
./build/bin/llama-server \
  -hf unsloth/gemma-4-26B-A4B-it-qat-GGUF:UD-Q4_K_XL \
  --spec-type draft-mtp --spec-draft-n-max 4 \
  -ngl 999 -fa on
```

The card states the MTP drafter at the repo root is auto-discovered from `-hf` ("you do not pass
`--model-draft`"), that it shares the target's KV cache, and that the target verifies every drafted
token so the output does not change.

### Mechanism map (drafter → flag combination)

| Mechanism | Weights | Server flags | Notes |
|---|---|---|---|
| (a) unsloth MTP head | `mtp-gemma-4-26B-A4B-it.gguf` (0.43 GiB, base repo) or 0.23 GiB (QAT repo) | `--spec-type draft-mtp` + auto-discovery from `-hf`, or explicit `--spec-draft-hf <repo> --spec-draft-model <file>` | same architecture as the assistant drafter: `gemma4-assistant`, NextN head |
| (b) official assistant | `AtomicChat/…-assistant-GGUF` F16 0.80 GiB, or Q4_K_M 0.30 GiB | `--spec-type draft-mtp --spec-draft-hf AtomicChat/… --spec-draft-model gemma-4-26B-A4B-it-assistant.F16.gguf` | *not* `draft-simple`: `common/speculative.cpp` routes gemma-4 assistants through the `draft-mtp` implementation and shares the target KV (all draft tokens use `pos0`) |
| (c) EAGLE-3 | `RedHatAI/…-speculator.eagle3` (1.73 GiB safetensors, needs `convert_hf_to_gguf.py --target-model-dir`) | `--spec-type draft-eagle3 --spec-draft-model <converted.gguf>` | fallback only, conditional on the MTP result |
| none (baseline) | `UD-Q4_K_M` 15.78 GiB | today's production flag set | reference for every comparison |

`--spec-draft-n-max` is not clamped for gemma-4 drafters: `common/speculative.cpp` only clamps it for
chained multi-head drafters (`chain_heads`, e.g. step35), and `is_mem_shared` gemma-4 holds all draft
tokens at one position with the shared KV. A 3-vs-6 sweep is therefore meaningful here.

### Production harness facts (unchanged from earlier reports)

| Fact | Value |
|---|---|
| Hosts | the first worker and the second worker: EPYC 7773X, 128 threads, 512 GiB, AVX2, single NUMA node |
| Production flags | `--threads 64 --ctx-size 65536 --cache-ram 65536 -ctk q8_0 -ctv q8_0 -fa on --no-mmproj --temp 1.0 --top-p 0.95 --top-k 64` |
| Ports | production `8080` (4 slots, quadlet), benchmark `8082`; `8083` free for the equality A/B |
| Co-tenant load | production server keeps ~19 of 128 threads busy, `load average` ~125 during benchmarks |
| Accuracy precedent | `UD-IQ2_M` 64.0% vs `UD-Q4_K_M` 78.0% vs `UD-Q8_0` 78.0% (all 50 jobs) — quant choice is not cosmetic, QAT needs measurement, MTP does not |
| Job shape | 3 requests per job, 1000-1500 reasoning tokens per turn; decode is 60-90 s of the 158 s median |
| Checkpoint trap | long-lived hybrid servers log `erased invalidated context checkpoint` and re-prefill; restart per configuration (earlier Ornith run, recorded in the o3-config repository) |

## Hypotheses

H1 (loadability). The unsloth MTP head loads as a `draft-mtp` context on a pinned newer `:server`
image and the logs show `statistics draft: #gen tokens > 0`. Falsified by `unknown model architecture`,
a rejected `--spec-type`, `failed to load draft model`, or a drafter that never generates a token
(`#gen drafts = 0` after a real prompt).

H2 (speed). Draft-token verification amortises the target forward pass, so decode at ~8.1k context
improves by 1.5-3x with acceptance above 50% and no prefill regression. Falsified if the best variant
stays under 1.2x decode or acceptance under 30%.

H3 (quality neutrality). Greedy (`--temperature 0`) outputs from baseline and speculative server are
token-for-token identical, and the 25-job replay keeps ≥19/25 correct. Falsified by any divergence
beyond a one-token sampling artefact, or by losing more than 2 correct answers.

H4 (QAT). QAT weights (`qat-UD-Q4_K_XL` 13.27 GiB) give the same accuracy as `UD-Q4_K_M` at equal or
better throughput and 16% less footprint, because quantization error is trained in instead of
recovered afterwards. Falsified by accuracy below 19/25, or a decode/prefill regression against
`UD-Q4_K_M` on the same host and thread count.

## Measurement matrix

| Axis | Values | Purpose |
|---|---|---|
| Draft head | none, unsloth MTP (`draft-mtp`), official assistant F16 (`draft-mtp`) | H1/H2: acceptance and speed per mechanism |
| `--spec-draft-n-max` | 3 (default), 6 | H2: acceptance vs verification cost |
| Weights | `UD-Q4_K_M` (15.78 GiB), `qat-UD-Q4_K_XL` (13.27 GiB) | H4: QAT quality and footprint |
| Prompt size | ~1.6k, ~8.1k tokens | turn 1 vs turn 2/3 prefill shapes |
| Output | greedy (temp 0) token-for-token comparison, two containers on 8082/8083 | H3: proof of neutrality |
| Accuracy set | `eval_dataset.json` jobs `6319605…6315007` (25), extend to 50 only on a pass | comparability with the Ornith numbers in the o3-config repository |
| Threads | 64 (production parity) | keep one variable |
| Runner | one pinned image tag per arm, build string recorded from `/props` | the flag set differs between builds |

## Decision criteria

Adopt the draft head only if all hold:

1. greedy outputs are token-for-token identical to the baseline for every probe prompt,
2. decode throughput at ~8.1k context improves by ≥1.5x with acceptance above 50%,
3. the 25-job replay keeps accuracy (≥19/25 correct) and produces no new timeout,
4. additional memory is ≤1 GiB and startup stays inside the 300 s health window.

Adopt the QAT weights only if all hold:

1. 25-job accuracy ≥19/25 correct (the incumbent's 78.0% on 50 jobs allows 16/25 to be a regression),
2. decode and prefill throughput are at least equal to `UD-Q4_K_M` on the same host,
3. weight footprint drops as advertised (~13.3 GiB vs 15.8 GiB).

Each component stands alone: a bad QAT result must not block a good draft head, and vice versa.
A combined adoption (QAT weights + winning draft head) is preferred and gets its own replay.

## Kill criteria

Stop and report instead of tuning further if any of these fire:

- greedy outputs diverge between baseline and speculative server beyond a fixed tokenization artefact
  (the draft path is then not quality-neutral on this build);
- no draft variant reaches 1.2x decode at ~8.1k, or acceptance stays below 30%;
- the draft GGUF is rejected by the pinned image (`unknown model architecture`, missing tensor) and no
  conversion in the same repo loads;
- the 25-job replay loses more than 2 correct answers versus the incumbent's rate;
- accuracy or latency degrades as a function of run order (cache/checkpoint instability), i.e. the same
  job answers differently between two runs of the same configuration — restart per run and rerun.

## Corrections to the planning notes' fact table

The planning notes state: "Production runner … build 8740 (`e34f04215`) — already supports
`--spec-type draft-mtp,draft-eagle3`, `-md/--spec-draft-model`, `--spec-draft-n-max`". That is
**not true of commit `e34f04215`** and the related wording in the planning notes needs two further fixes:

| Claim in the planning notes | Verified fact | Source |
|---|---|---|
| build 8740 supports `--spec-type draft-mtp`/`draft-eagle3` | 8740 rejects both; `--spec-type` is n-gram only. The live `--help` probe that produced the claim must have hit a newer image, because `ghcr.io/ggml-org/llama.cpp:server` is a floating tag | `common/arg.cpp` L3502-3523 @ `e34f04215` |
| 8740 supports `-md/--spec-draft-model` | 8740 has `-md/--model-draft`; the `--spec-draft-model` alias only exists on master | `common/arg.cpp` L3488 @ `e34f04215`, L4321 @ master |
| 8740 supports `--spec-draft-n-max` | 8740 has `--draft/--draft-n/--draft-max` (default 16); master uses `--spec-draft-n-max` (default 3) | `common/common.h` L317 @ `e34f04215`, L345 @ master |
| MTP head via `-md` + `--hf-file` | `--hf-file` applies to the *main* model; the draft file is pinned with `--spec-draft-hf <repo> --spec-draft-model <file>` (master) or `-hfd <repo> -md <file>` (8740 path form) | `common/arg.cpp` L2607/L4030/L4321 |
| assistant drafter runs as `draft-simple` | a `gemma4-assistant` drafter is an MTP head sharing the target KV and must run as `draft-mtp`; `draft-simple` expects a standalone draft context | `common/speculative.cpp` L1808, L2244 @ master |
| MTP is unavailable for gemma-4 | true on 8740 (`// NextN/MTP tensors are currently ignored`), false on master (`blk.%d.nextn.*` supported) | `src/llama-arch.cpp` L757 @ `e34f04215`, L1246 @ master |

Planning consequence: the deployment step pins a specific newer image tag via `deploy_candidate.sh` (in the o3-config repository) `--image …`,
records the build string from `/props`, and treats "no such flag in this build" as the H1 falsifier
rather than as a model-quality signal. The harness flags are implemented against the
master spelling (`--spec-draft-hf`, `--spec-draft-model`, `--spec-type`, `--spec-draft-n-max`).

## Open questions

- Which published `:server` tag first ships the `draft-mtp` path, and does its CPU backend beat 8740
  on the same job set? If a newer image is needed anyway, its own prefill/decode deltas must be
  measured before the draft arms so the speedup is attributed to speculation, not to the rebuild.
- Does `-hf <repo> --hf-file <file>` still auto-discover the MTP sidecar (the cards only show the
  plain `-hf repo:quant` form)? If not, the explicit `--spec-draft-hf`/`--spec-draft-model` pair is
  mandatory and the harness must always emit it.
- Acceptance on reasoning text: drafts are cheap but the target writes 1000-1500 tokens of free-form
  analysis, where acceptance can be far lower than on code rewriting. This is the number that decides
  H2, not the 3x from the card.
- Does a 4-slot production server keep the shared-KV MTP gain, or does per-slot draft state bound the
  aggregate throughput? This is measured only if a draft head is adopted.
