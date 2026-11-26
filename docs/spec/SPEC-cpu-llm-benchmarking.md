# Spec: CPU-Only LLM Benchmarking & Evaluation Pipeline for o3

## Objective

Evaluate, benchmark, and deploy the optimal CPU-only model and inference harness for automated openQA job-failure investigation (`openqa-llm-investigate`) on dedicated openQA worker hosts `openqaworker26` (w26) and `openqaworker27` (w27), replacing the current baseline `gemma-4-E4B-it-GGUF:UD-IQ2_M`.

Selection is strictly empirical: candidates are ranked by:
1. **Task Quality**: Classification accuracy against human review on o3 historical jobs (cross-checking `comment=LLM` jobs where subsequent human action set a Bugzilla bug ⇒ Product Bug, progress ticket ⇒ Test Issue, or infra ticket ⇒ Infrastructure).
2. **Inference Latency & Throughput**: Prompt evaluation and token generation times measured on live worker hardware (median and p90 seconds per job, staying well within the 600s proxy timeout).
3. **External Generic Benchmarks**: Reference scores from independent evaluations (e.g., artificialanalysis.ai, GPQA, MMLU-Pro).

---

## Live Hardware Baseline (w26 & w27)

Measured via live probe on 2026-11-15:
- **Processor**: Single-socket AMD EPYC 7773X (Milan-X, Zen 3)
  - 64 physical cores, 128 logical threads (SMT enabled)
  - 768 MiB L3 cache (8 × 96 MiB 3D V-Cache slices)
  - SIMD: AVX2, FMA, BMI2, VAES, VPCLMULQDQ (no AVX-512)
  - Memory Bandwidth: 8-channel DDR4-3200 (~204.8 GB/s theoretical peak)
- **Memory**: 512 GiB DDR4 (503 GiB usable, >480 GiB free)
- **NUMA**: Single unified node (NPS1, node 0 has 515 GB RAM and all 128 CPUs)
- **Disk / Scratch**: 460 GiB available on `/` (ext4/xfs on `/dev/md0p2`), 252 GiB available in `/dev/shm` (tmpfs)
- **Current State**: Dedicated (all `openqa-worker-auto-restart@*.service` units masked/dead; load avg ~0.1)
- **Current Container**: `ghcr.io/ggml-org/llama.cpp:server` (build `b8740-e34f04215`), `--ctx-size 65536`, 4 slots

---

## Benchmark Candidate Matrix

Given 512 GiB RAM and 204 GB/s memory bandwidth, RAM is not a constraint. Architecture (MoE active parameters vs dense weights) governs decode speed.

| Candidate | Architecture | Total / Active Params | Quantization | Est. RAM Footprint | Target Harness |
|---|---|---|---|---|---|
| **Baseline: Gemma-4-E4B** | Dense (embedding layers) | 8B / 4.5B | UD-IQ2_M | ~3.5 GB | llama.cpp server |
| **Gemma-4-26B-A4B-it** | MoE (medium) | 25.9B / 3.8B | UD-Q4_K_M | ~17 GB | llama.cpp / modchip |
| **Swift-1.5-Qwen3.8-27B** | Hybrid Gated Delta Net + Attn | 26.9B / 26.9B (dense) | IQ3_S (GSQ-RCO) | ~11 GB | llama.cpp server |
| **gpt-oss-120b** | MoE | 117B / 5.1B | MXFP4_MOE | ~65 GB | llama.cpp server |
| **Nemotron-3.5-30B-A3B** | Hybrid Mamba-2 + MoE | 30B / 3.0B | Q4_K_M / Q4_0 | ~19 GB | llama.cpp / modchip |
| **Qwen3-30B-A3B-Instruct** | MoE | 30.5B / 3.3B | UD-Q4_K_M | ~19 GB | llama.cpp server |
| **gpt-oss-20b** | MoE | 20B / 3.6B | MXFP4_MOE | ~12 GB | llama.cpp server |

### Harnesses in Scope
1. **Upstream `llama.cpp:server`** (podman quadlet): Baseline production engine, supports GGUF, MoE cache (`-ncmoe`), prompt caching, FlashAttention (`-fa`).
2. **`llama-modchip`** (synchopate/llama-modchip): Evaluated on w26 for AVX2 kernel performance, prefix-cache (`HARP`), and speculative drafter claims.
3. **`Strata`**: Evaluated for CPU fallback viability; excluded if strictly GPU-dependent.

---

## Commands

### 1. Hardware & Runtime Telemetry Probe
```bash
# Executed via o3 bastion to w26/w27
ssh -o BatchMode=yes o3 'ssh -o BatchMode=yes w26 "lscpu; free -h; cat /proc/loadavg"'
```

### 2. Candidate Container Deployment (Isolated Port 8082 for Benchmarking)
```bash
# Run candidate on w26 without disrupting production port 8080
sudo -u llama podman run --name llama-bench-candidate -d \
  -v /opt/llm/models:/models -p 8082:8080 \
  ghcr.io/ggml-org/llama.cpp:server \
  -hf <model_hf_repo:quant> \
  --threads 32 --ctx-size 65536 --temp 1.0 --top-p 0.95 --top-k 64 --no-mmproj
```

### 3. Eval Dataset Extraction (o3 Historical Jobs)
```bash
# Query jobs with LLM comments from o3 API
curl -s "https://openqa.opensuse.org/api/v1/jobs?comment=LLM" | jq '.' > eval_jobs.json
```

### 4. Replay & Quality Scoring Execution
```bash
# Replay evaluation jobs against candidate endpoint
openqa-llm-investigate --dry --force --llm-url http://w26:8082/v1/chat/completions <job_id>
```

---

## Evaluation Pipeline & Dataset Design

### Evaluation Dataset Construction
The eval set is extracted from historical o3 jobs containing `openqa-llm-investigate` comments:
1. **Target Pool**: All jobs with an LLM investigation comment (`comment=LLM`) in groups 1, 3, 38, and 139.
2. **Ground Truth Identification**:
   - **Product Bug**: A subsequent comment or linked issue contains a Bugzilla bug reference (`bsc#...`, `boo#...`).
   - **Test Issue**: A subsequent comment or linked issue contains a Progress/Redmine test ticket (`poo#...` in openQA Tests).
   - **Infrastructure**: A subsequent comment or linked issue references an openQA Infrastructure ticket.
   - **Human Override**: Explicit human comment approving or overriding the LLM classification.
3. **Stratified Split**:
   - Primary Golden Set: ~50-100 historical jobs with unambiguous human ground truth.
   - Contrastive Set: Jobs where baseline `gemma-4-E4B` produced an incorrect or inconclusive verdict.

### Metrics & Scoring
- **Classification Accuracy (%)**: Match rate between model's output class and ground-truth human category.
- **Root-Cause Relevance**: Subjective 1-5 rubric on sample cases evaluating evidence quotation and plausibility.
- **Latency**:
  - `prompt eval time` (seconds / N tokens)
  - `eval time` (tokens/second during generation)
  - Total end-to-end request time per job (seconds).

---

## Project Structure

```
docs/
├── intent/
│   └── cpu-only-llm-models-o3.md         → Confirmed Statement of Intent
├── research/
│   └── cpu-only-llm-inference.md         → Primary source research findings
└── spec/
    └── SPEC-cpu-llm-benchmarking.md      → This specification document

scripts/benchmarking/
├── fetch_eval_dataset.py                 → Queries o3 API for comment=LLM + human ground truth
└── deploy_candidate.sh                   → Starts/stops a benchmark container on port 8082
```

The evaluation harness (`bench_throughput.py`, `run_investigate_eval.py`,
`compare_spec_outputs.py`, `score_verdicts.py`), the 50-job dataset and the
analysis records live in `os-autoinst-scripts` next to `openqa-llm-investigate`,
under `scripts/benchmarking/` and `docs/benchmarks/`.

---

## Code Style & Implementation Guidelines

- **Language**: Python 3.11+ for scripts, Bash for quadlet/podman orchestration.
- **Typing & Linting**: Strictly type-hinted Python (`mypy` / `ty`), formatted with `ruff`.
- **API Interactions**: Use `urllib.request` or `requests` with explicit timeouts (max 600s); handle single-slot 503s with exponential backoff.
- **Error Handling**: Graceful fallback when a prompt exceeds context; log exact token counts and server timings.

---

## Boundaries

### Always Do
- Run benchmark instances on dedicated non-production ports (e.g. `8082` or temporary socket), keeping production `8080` intact until replacement is verified.
- Run remote commands with `BatchMode=yes` and explicit timeouts.
- Capture raw server timings (`prompt eval time`, `eval time`) directly from container logs.

### Ask First
- Changing production `/etc/nginx/vhosts.d/llm-proxy.conf` on `o3`.
- Replacing the active production quadlet container configuration on `w26` or `w27`.
- Downloading models larger than 100 GB to `/opt/llm/models`.

### Never Do
- Execute any destructive command (`rm`, `wipe`, `reboot`, `systemctl stop`) on production openQA workers without explicit authorization.
- Use `sudo` locally or on unapproved actors.
- Version-control secret tokens or credentials.

---

## Success Criteria

1. **Dataset**: A validated golden set of ≥50 o3 jobs with human-verified ground truth is constructed.
2. **Benchmark Execution**: At least 3 candidate models (including `gemma-4-26B-A4B-it` and `gpt-oss-120b` or `Nemotron-3.5`) are benchmarked on `w26` against the baseline.
3. **Quality Gain**: A selected candidate achieves a statistically significant improvement in classification accuracy over `gemma-4-E4B-it` baseline.
4. **Latency Verification**: Winning candidate delivers median job processing time < 180s on `w26` (well under the 600s timeout).
5. **Production Deployment Plan**: Verified quadlet definition and nginx proxy configuration ready for rollout.

---

## Open Questions

1. Does `llama-modchip` provide a measurable throughput benefit on AMD EPYC 7773X (Zen 3 AVX2) over upstream `llama.cpp`'s AVX2 kernels?
2. Does speculative decoding with Gemma-4's MTP (Medusa/Draft) module provide sufficient speedup to justify the additional RAM/KV slot?
3. Should `openqa-llm-investigate`'s prompt truncation (currently ~4096 tokens) be expanded to leverage the 64k/128k context windows supported by MoE candidates?
