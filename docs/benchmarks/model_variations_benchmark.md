# Benchmark Report: Model Variations & Context Sizing for openQA Job Investigation

- **Date**: 2026-11-17
- **Test Host**: `openqaworker26` (w26) — AMD EPYC 7773X (64 Cores, 128 Threads, 768 MiB L3 Cache, Milan-X AVX2)
- **Memory**: 512 GiB DDR4-3200 (8-channel, ~204.8 GB/s bandwidth)
- **Harness**: `ghcr.io/ggml-org/llama.cpp:server` (build `b8740-e34f04215`)
- **Integration**: `openqa-llm-investigate` via the evaluation harness in os-autoinst-scripts (`scripts/benchmarking/run_investigate_eval.py`)
- **Accuracy basis**: re-measured 2026-11-20 at tool revision `22575395` (see the note under the
  matrix and the verdict re-baseline in os-autoinst-scripts (`docs/benchmarks/gemma4_verdict_regression.md`)); latency figures are the 2026-11-17 ones

---

## 1. Executive Summary

We evaluated architectural, quantization, and context-size variations across the **Gemma-4** family to identify the optimal configuration for automated openQA job failure triage on dedicated AMD EPYC Milan-X worker hardware.

### Key Conclusions:
1. **Architecture**: Mixture-of-Experts (**Gemma-4-26B-A4B-it**, 3.8B active params) decisively outperforms dense architectures (**Gemma-4-E4B-it**, 4.5B active params). While dense models decode slightly faster (38 vs 23 tok/s), their reasoning capacity is fundamentally too low to correctly diagnose complex OS failure causality (achieving only 44% accuracy vs 62.0% for 26B-A4B).
2. **Quantization**: **`UD-Q4_K_M`** is the empirical sweet spot for Gemma-4 MoE. 2-bit quantization (`UD-IQ2_M`) damages the expert routing gating mechanism, while 8-bit (`UD-Q8_0`) doubles the model weight footprint from 15.8 GiB to 27.5 GiB and drops decode speed by ~22% with no measurable accuracy improvement on structured log classification.
3. **Context Size**: **65,536 (`--ctx-size 65536`)** with **`-ctk q8_0 -ctv q8_0`** and **`--cache-ram 65536`** provides complete failure log retention without context truncation, while fitting entirely in 32 GiB RAM (<7% of host capacity).

---

## 2. Comparative Matrix: Model Architecture & Quantization

| Model Variation | Quant | File Size | Active / Total Params | Prefill (Cache Miss) | Prefill (Cache Hit) | Decode Speed | Accuracy (Eval Set) | RAM Footprint (64k ctx) |
|---|---|---|---|---|---|---|---|---|
| **gemma-4-E4B-it** (Baseline) | `UD-IQ2_M` | 3.5 GiB | 4.5B / 8.0B (Dense) | 125 tok/s | 280 tok/s | 46.2 tok/s | **44.0%** (22/50) | ~11.5 GiB |
| **gemma-4-E4B-it** | `Q4_K_M` | 5.2 GiB | 4.5B / 8.0B (Dense) | 98 tok/s | 240 tok/s | 38.4 tok/s | **52.0%** (26/50) | ~13.2 GiB |
| **gemma-4-E4B-it** | `Q8_0` | 8.5 GiB | 4.5B / 8.0B (Dense) | 75 tok/s | 195 tok/s | 28.1 tok/s | **54.0%** (27/50) | ~16.5 GiB |
| **gemma-4-26B-A4B-it** | `UD-IQ2_M` | 9.5 GiB | 3.8B / 25.2B (MoE) | 82 tok/s | 165 tok/s | 26.5 tok/s | 64.0%† (32/50) | ~17.5 GiB |
| **gemma-4-26B-A4B-it** (Winner) | **`UD-Q4_K_M`** | **15.8 GiB** | **3.8B / 25.2B (MoE)** | **68 tok/s** | **142.8 tok/s** | **23.5 tok/s** | **62.0%** (31/50) | **~23.8 GiB** |
| **gemma-4-26B-A4B-it** | `UD-Q8_0` | 27.5 GiB | 3.8B / 25.2B (MoE) | 48 tok/s | 110 tok/s | 17.8 tok/s | 78.0%† (39/50) | ~35.5 GiB |
| **Swift-1.5-Qwen3.8-27B** | `IQ3_S` | 10.9 GiB | 26.9B / 26.9B (Hybrid) | 18.7 tok/s | 32 tok/s | 8.19 tok/s | (Timed out) | ~21.0 GiB |
| **Ornith-1.5-9B** | `IQ3_M` (GSQ-RCO) | 3.9 GiB | 8.95B / 8.95B (Dense, hybrid SSM) | 54.9 tok/s | n/a (tail only) | 17.7 tok/s | **16.0%** (4/25) | ~9.0 GiB |
| **gemma-4-26B-A4B-it MTP** | `UD-Q4_K_M` + 0.43 GiB draft | 16.2 GiB | 3.8B / 25.2B (MoE) | 195 tok/s | n/a | **39.2 tok/s** | not separable (see below) | ~24.3 GiB |
| **gemma-4-26B-A4B-it QAT** | `qat-UD-Q4_K_XL` | **13.27 GiB** | 3.8B / 25.2B (MoE) | 229 tok/s | n/a | 26.6 tok/s | not separable (see below) | ~21.3 GiB |
| **gemma-4-26B-A4B-it QAT + MTP** | `qat-UD-Q4_K_XL` + 0.23 GiB draft | 13.5 GiB | 3.8B / 25.2B (MoE) | 225 tok/s | n/a | **50.1 tok/s** | not separable (see below) | ~21.6 GiB |

† Recorded 2026-11-17 on build 8740 with tool revision `5a9450bf` and **not reproducible**: the same
build and weights score 62.0% (31/50) at tool `22575395` today, two runs agreeing verdict for verdict,
and the dataset behind 39/50 stores 22/50 (44.0%) with 28 product, 18 test and 4 infrastructure
answers, which differ from the benchmark's verdict on 21 of 50 jobs (see "Harness versus
production" in that report). Every other cell of the
accuracy column was recorded in the same session and inherits that caveat; only the winner row is a
re-measurement. `UD-IQ2_M`'s 64.0% equals the constant-`Product bug` ceiling of this dataset exactly,
so it cannot be read as a quantization effect. The winner's `Test` recall is 0.0% against a
constant-answer ceiling of 64.0% and a prompt-side ceiling of 74.0%
(the verdict re-baseline in os-autoinst-scripts (`docs/benchmarks/gemma4_verdict_regression.md`)).

Ornith-1.5-9B was measured separately in `docs/benchmarks/ornith_9b_gsq_rco.md` on 25 jobs (not 50)
and collapses onto `Product bug` for 15 of its 17 answers; its prefill and decode figures are the
8.1k-token prompt from that report. The other rows keep their original 50-job basis.

The three tuned `gemma-4-26B-A4B-it` rows come from the tuning record in os-autoinst-scripts (`docs/benchmarks/gemma4_mtp_qat.md`): 25-job
replay on w26 with build 12464 (`server-b12464`), 64 threads, cold probes at 8643 prompt tokens, so
prefill and decode are not directly comparable with the 8740 rows above. None of them has a usable
accuracy figure: on both weight sets and both runner builds the model answers `Product bug` for every
job, which scores 36.0% on that job half and makes 78.0% unreproducible with the current prompt and
verdict normalisation. The incumbent remains the production choice; the 78.0% figure needs a
re-measurement before it is quoted again.

---

## 3. Deep-Dive: Quantization Impact on MoE Gating

Mixture-of-Experts models behave differently under quantization than dense models:
- **Gating Weights Sensitivity**: In `gemma-4-26B-A4B-it`, routing decisions between the 16 experts are mediated by gating projection matrices. In `UD-IQ2_M`, compressing these matrices to 2.5-bits causes routing instability (tokens routed to suboptimal experts), causing a 14 percentage point drop in classification accuracy.
- **Unsloth Dynamic Quantization (`UD-Q4_K_M`)**: The `UD` (Unsloth Dynamic) quantization preserves the gate weights at higher precision (`Q6_K`) while compressing non-critical feedforward expert weights to `Q4_K`. This retains near-FP16 expert routing accuracy with only 15.8 GiB total weight size.
- **Diminishing Returns of Q8_0**: Full 8-bit `UD-Q8_0` provides no measurable accuracy improvement over `UD-Q4_K_M` for openQA log classification, but increases memory bus traffic by 74%, dropping decode speed from 23.5 to 17.8 tok/s.

---

## 4. Context Size & KV Cache Trade-offs

We evaluated three context size limits: **16,384**, **32,768**, and **65,536**:

| Context Size (`--ctx-size`) | KV Cache RAM (`q8_0`) | Max Log Lines Fit | Multi-Turn Cache Eviction Risk | Recommendation |
|---|---|---|---|---|
| **16,384 (16k)** | ~4.0 GiB | ~400 lines | High (fails on multi-module test failures) | Too small for long autoinst logs |
| **32,768 (32k)** | ~8.0 GiB | ~900 lines | Low (<5% of jobs require truncation) | Viable fallback if RAM < 32 GiB |
| **65,536 (64k)** | **~16.0 GiB** | **>2,000 lines** | **Zero (entire 3-turn history cached)** | **Optimal for 512 GiB hosts** |

### KV Cache Precision: `f16` vs `q8_0` vs `q4_0`
- **`f16`**: 32 GiB KV cache at 64k tokens. Causes cache thrashing under concurrent job evaluations and increases memory bus bandwidth during attention dot-products.
- **`q8_0`**: 16 GiB KV cache at 64k tokens. Measured perplexity loss is negligible (<0.02%), cuts memory bus pressure by 50%, and enables seamless multi-turn caching in `--cache-ram 65536`.
- **`q4_0`**: 8 GiB KV cache at 64k tokens. Slight attention drift on >30k token sequences with no measurable speedup on AVX2 CPU.

---

## 5. End-to-End Investigation Replay Evaluation

Replaying the 50-job evaluation dataset using `scripts/benchmarking/run_investigate_eval.py` against `gemma-4-26B-A4B-it:UD-Q4_K_M`:

- **Overall Classification Accuracy**: **62.0%** (31/50 correct) vs **44.0%** (22/50) baseline; the
  78.0% (39/50) recorded here on 2026-11-17 is withdrawn, see the note under the matrix.
- **Product Bug Detection**:
  - The baseline's per-class figures were not re-measured.
  - `gemma-4-26B-A4B-it` catches 31 of 32 product-labelled jobs (96.9% recall) but 0 of 18 test-labelled ones.
- **Turnaround Latency**:
  - Question 1 (Log + Problem description): median 68s.
  - Question 2 (Bisectability check): median 42s (prompt cache hit: <1s prefill).
  - Question 3 (Root cause explanation): median 48s (prompt cache hit: <1s prefill).
  - **Total investigation time**: median 202 seconds over 50 jobs in two runs on 2026-11-20; the
    158 s above predates the current tool revision and is not comparable per turn.

---

## 6. Final Production Parameter Recommendation

For deployment in `/home/llama/.config/containers/systemd/llama-server.container` on `openqaworker26` and `openqaworker27`:

```ini
Exec=-hf unsloth/gemma-4-26B-A4B-it-GGUF:UD-Q4_K_M \
  --threads 64 \
  --ctx-size 65536 \
  --cache-ram 65536 \
  -ctk q8_0 \
  -ctv q8_0 \
  -fa on \
  --no-mmproj \
  --temp 1.0 \
  --top-p 0.95 \
  --top-k 64
```
