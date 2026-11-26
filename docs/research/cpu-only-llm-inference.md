# CPU-only LLM inference for openQA job-failure investigation

Research date: 2026-11-13. Goal: best model + harness that runs inference **without any GPU accelerator** on the o3 worker fleet (x86_64 + aarch64), for long openQA log analysis and root-cause reasoning (the `openqa-llm-investigate` workload).

## TL;DR

Current o3 deployment runs `llama-server` in podman behind the nginx `llm-proxy` (workers 26/27/28:8080, least_conn, 600 s read timeout) with `gemma-4-E4B` at `UD-IQ2_M` (~3.5 GB). That is the **weakest** model of the Gemma 4 family. The single biggest quality lever that keeps the existing harness is upgrading the worker model to the MoE tier:

| Rank | Model (weights) | GGUF size | Total/active params | Native ctx | KV cache @ 128K (f16) | Reasoning/long-context evals (official cards) | CPU decode (est., 45 GB/s machine) |
|---|---|---|---|---|---|---|---|
| 1 | `google/gemma-4-26B-A4B-it` UD-Q4_K_M / MXFP4_MOE | 16.9 / 16.6 GB | 25.9B / 3.8B | 256K (sliding window 1024) | 32.2 GB raw, sliding layers cap it | MMLU Pro 82.6, GPQA Diamond 82.3, AIME 2026 88.3, MRCR v2 128k 44.1 | ~20 tok/s ceiling, 10-15 real |
| 2 | `openai/gpt-oss-120b` MXFP4_MOE | 63.4 GB | 117B / 5.1B | 128K (sliding 128) | 9.7 GB | top open tier at high reasoning: card reports it nearly matches OpenAI o3 on HealthBench, beats GPT-4o/o1/o3-mini/o4-mini; AIME/GPQA/MMLU/HLE/Codeforces/tau-Bench in model-card Table 3 | ~15 tok/s ceiling; needs >=96 GB RAM |
| 3 | `nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B` ggml-org Q4_0 / unsloth UD-Q4_K_M | 18.9 / 25.3 GB | 30B / 3B | 1M card, 256K deployment | 7.0 GB + constant Mamba-2 state | GPQA 75.4, MMLU Pro 81.9, AA-LCR long ctx 52.0 | ~25 tok/s ceiling |
| 4 | `Qwen/Qwen3-30B-A3B-Instruct-2507` UD-Q4_K_M | 18.6 GB | 30.5B / 3.3B | 262,144 native | 12.9 GB | strong Qwen3 tier (see card) | ~23 tok/s ceiling |
| 5 | `openai/gpt-oss-20b` MXFP4_MOE | 12.1 GB | 20B / 3.6B | 128K (sliding 128) | 6.4 GB | card table; runs "within 16GB of memory" (card) | ~25 tok/s ceiling |

Excluded as CPU-infeasible (interactive): dense 70B-class models (`Llama-3.3-70B`: 70B active params → ~1-2 tok/s at Q4) and `Llama-4-Scout` (17B active, 109B total; MMLU Pro 68.9 / GPQA 50.5 — worse than the MoE tiers above at far higher decode cost). `Phi-4` (14B dense, MMLU 84.8, GPQA 56.1) is fast (~8.4 GB Q4) but capped at 16K context — unusable for whole-log analysis.

**Strata is not CPU-only.** Strata's own docs require a GPU: "Run 125B+ MoE Models on 12GB VRAM GPUs", min 11.5 GB VRAM + 52 GB system RAM for Qwen3.8-Flash-Next; supported hardware is NVIDIA RTX 20-50, AMD Radeon (HIP), AMD Ryzen AI Max "Strix Halo" (iGPU), NVIDIA DGX Spark. It streams dormant experts through 64 GB DDR5 into a 12 GB VRAM cache — i.e. it is a GPU-accelerated MoE offloader, not a CPU engine.

**llama-modchip is a real project, not vaporware, but is not upstream.** See the credibility section.

## Method and sources

Primary sources used (no third-party blogs):
- Model cards / configs on huggingface.co: `google/gemma-4-26B-A4B-it` (raw README + config.json), `openai/gpt-oss-20b`/`gpt-oss-120b`, `Qwen/Qwen3-30B-A3B-Instruct-2507`, `Qwen/Qwen3-Next-80B-A3B-Instruct`, `microsoft/Phi-4`, `deepseek-ai/DeepSeek-R1`, `deepseek-ai/DeepSeek-V4-Flash`, `nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-BF16`.
- Official vendor docs: `llama.com/docs/model-cards-and-prompt-formats/llama3_3/` and `/llama4/`; `meta-llama/llama-models` MODEL_CARD.md; `ai.google.dev` Gemma docs.
- llama.cpp repo docs (ggml-org master): `docs/build.md` (KleidiAI AArch64, ZenDNN AMD EPYC), `docs/moe-cache.md` (`-cmoe/-ncmoe`, MoE cache), `docs/speculative.md`, `docs/models.md`, `tools/quantize/README.md`, `tools/server/README.md` (`-c`, `-fa`, `-ctk/-ctv` KV quant types, `-ncmoe`, `-np`).
- GGUF file sizes from the HF API (`/api/models/{repo}?blobs=true`) for `unsloth/gemma-4-26B-A4B-it-GGUF`, `ggml-org/gemma-4-26B-A4B-it-GGUF`, `ggml-org/gemma-4-12b-it-GGUF`, `ggml-org/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-GGUF`, `unsloth/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-GGUF`, `ggml-org/Qwen3-Next-80B-A3B-Instruct-GGUF`, `unsloth/DeepSeek-V4-Flash-GGUF`, `ISTA-DASLab/DeepSeek-V4.1-Flash-1017-GSQ-RCO-GGUF`.
- GitHub metadata + README/ARCHITECTURE/RELEASE_NOTES/QUALITY_GATE for `synchopate/llama-modchip` (incl. the issue tracker benchmark submissions), and `Niko1221/Strata` + stratallm.org.
- HF API: Open LLM Leaderboard is retired (pinned discussion, 2025-03); LMArena human-preference datasets exist (`lmarena-ai/arena-human-preference-140k`). No official CPU benchmark suite exists for these models — vendor cards only publish GPU/cluster numbers.

Decode estimate: CPU token generation is dominated by weights read per token ≈ `active_params × bits_per_weight / 8` (MoE routes only `topk` experts per token, so active params, not file size, set the speed). Practical DDR5 dual-channel delivers ~40-50 GB/s; EPYC 8-channel ~200 GB/s. The estimates in the table are ceilings; real llama.cpp numbers land 30-60 % below. The llama.cpp docs' MoE cache (`-ncmoe`, `docs/moe-cache.md`) exists precisely to keep dormant experts in host RAM, and `docs/speculative.md` exists because batching n drafted tokens amortizes the bandwidth cost.

KV cache bytes/token = `2 × layers × kv_heads × head_dim × 2` (f16), computed from each model's config.json; sliding-window layers (Gemma 4 medium: 1024; gpt-oss: 128) and Mamba-2 SSM layers (Nemotron: constant-size state) are the long-context levers. Gemma 4 26B A4B at 128K needs 32.2 GB KV raw → in practice set `-ctk q8_0 -ctv q8_0` (server README) and rely on the sliding layers.

## Current o3 facts (from this repo + the skill)

- `nginx/vhosts.d/llm-proxy.conf`: upstream `llm_workers` least_conn, 3 workers `openqaworker26/27/28:8080`, `proxy_read_timeout 600s`, allow `10.0.0.0/8`.
- `openqa-llm-investigate` skill: o3 production endpoint runs `gemma-4-26B-A4B-it`; SUSE ollama runs `Qwen3.8-27B`-class; worker podman runs `gemma-4-E4B UD-IQ2_M --ctx-size 65536`; prompt is truncated at 4096 tokens per prompt (long-context headroom is currently unused).
- E4B is "4.5B effective (8B total)" per the Gemma 4 card's architecture table — the `E` means Per-Layer Embeddings, not a MoE. Its eval tier (MMLU Pro 69.4, GPQA 43.4, MRCR 128k 25.4) is the gap to close.

## llama-modchip credibility

Verified facts from the repo itself:
- 1147 stars, 4 contributors, Apache-2.0, created 2026-10-29 (≈ 2 weeks old at research date), Python + patched C++ engine, releases with SHA-256, x86_64 avx512/avx2 binaries, Windows 11 + macOS Metal + Linux support, CPU readiness script, 2 MB HugePages, `/dev/shm` prefix-cache ("HARP") restore in ms, Stockfish-PVS style speculative drafter.
- Claimed CPU-only throughput, with community-submitted leaderboard entries (issues #1-#33): 284B DeepSeek-V4-Flash at 8.1-10.7 tok/s on 16C/32T Zen 5 with 128 GB DDR5 and **0 GPU**; 30B-A3B Nemotron at 24.2-60.4 tok/s on 16C Zen 5, 111.6 tok/s on Threadripper PRO 7985WX; 13-20 tok/s on 8-16 GB/s-class laptops (Ryzen 5700U, 5600X, Intel Core Ultra 5 125H); Graviton3 aarch64 44.97 tok/s on 32 GB.
- README's own compatibility statement: `Fractal-64` repurposes `GGML_TYPE_Q2_0 = 42` and `LUT-VQ3-64` appends custom `type 43`; "Unpatched `llama.cpp`, `Ollama`, `vLLM`, and `LM Studio` will fail with `unknown tensor type 43`, a tensor bounds mismatch, or `unknown model architecture: 'deepseek4'`".
- Quality evidence is self-defined (WikiText-2 / AtomicChat PPL, logit KLD, own "100-Point Gate") at aggressive 3.00-3.75 bpw; no third-party evals published yet.
- Open issue #31: "v2.5.0 LUT-VQ3-64 engine cannot be built: vq3_codebook.h is not shipped and the two patches…" — build reproducibility is currently broken for the headline feature.

Assessment: the engineering is genuine (patched kernels, quant formats, community CPU benchmarks) and the "284B on 64-128 GB CPU" class claim is credible *on high-bandwidth server hardware*. It is a fork-of-llama.cpp ecosystem with custom, non-standard GGUF types, a single maintainer, no upstream acceptance, marketing-heavy README, and quality gated only by its own metrics. For o3 it is **not a drop-in**: the podman/nginx/openAI-API harness would need the modchip-patched server and its proprietary formats. Its quantization and speculative-decoding techniques are worth reading; adopting the fork is not advisable today.

## Ranking for o3 (CPU-only, keep llama.cpp harness)

1. **gemma-4-26B-A4B-it UD-Q4_K_M (16.9 GB) on each worker.** Same family as the production endpoint the skill already uses, so prompts/tool-calling stay compatible. 3.8B active params keeps decode CPU-feasible; sliding window 1024 bounds KV; 256K ctx. Official card evals put it far above the current E4B IQ2_M (GPQA 82.3 vs 43.4; MRCR 128k 44.1 vs 25.4). Needs ~17 GB model + KV quantization + 65-128K ctx RAM — fits 64 GB workers if ctx is managed (e.g. `-c 65536 -ctk q8_0 -ctv q8_0`, `-fa on`); fits 128 GB comfortably. Add MTP speculative decoding (Gemma 4 GGUF ships an MTP module; `docs/speculative.md`).
2. **gpt-oss-120b MXFP4 (63.4 GB)** where workers have >= 96-128 GB RAM: OpenAI's high-reasoning tier (model card reports gpt-oss-120b nearly matching o3 on HealthBench and beating GPT-4o/o1/o3-mini/o4-mini; AIME/GPQA/MMLU/HLE/Codeforces/tau-Bench scores in card Table 3), only 5.1B active params, sliding window 128 → only 9.7 GB KV at 128K. Cost: ~15 tok/s ceiling, single-slot-per-worker.
3. **Nemotron-3.5-Lightning-30B-A3B (18.9-25.3 GB)**: 3B active, Mamba-2 + MoE + GQA hybrid → constant-size SSM state (cheapest KV/long-context of the tier), 1M-token card. In NVIDIA's own harness it scores GPQA 75.4 / MMLU Pro 81.9 and AA-LCR 52.0 vs Gemma 4 26B A4B's 79.6 / 85.2 / 57.56, so Gemma 4 26B wins NVIDIA's comparison — Nemotron's case is its 1M context and constant-memory long-context cost, not eval superiority. Official ggml-org GGUF exists (Q4_0, Q8_0, BF16). License is OpenMDW-1.1.
4. **Qwen3-30B-A3B-Instruct-2507 (18.6 GB)**: 3.3B active, 262K native ctx, llama.cpp listed as supported by the Qwen card.
5. **gpt-oss-20b MXFP4 (12.1 GB)**: drop-in quality upgrade over the current E4B IQ2_M at a similar footprint; OpenAI states it "runs within 16GB of memory".

Tier above (needs 128 GB+ hosts): DeepSeek-V4-Flash 284B/13.17B active — unsloth GGUF UD-IQ3_S = 117.3 GB, UD-Q4_K_XL = 155.1 GB; modchip's 3 bpw MaxQ-Fractal = 56.4 GiB. At 13B active params, CPU decode is ~5-8 tok/s on desktop bandwidth. Qwen3-Next-80B-A3B (3B active, 262K, hybrid linear attention): Q8_0 GGUF is 84.8 GB, Q4 ~42 GB.

## Open questions

1. Worker RAM/budget per host (64 vs 128 GB) decides rank 1 vs 2 — measure, don't guess. The modchip `check_cpu_readiness.py` is a quick memory/AVX-512 probe, but o3's aarch64 fleet has no AVX-512; KleidiAI (`docs/build.md`) is the aarch64 path.
2. The skill truncates prompts at 4096 tokens; the 256K/1M context and long-context evals (MRCR/AA-LCR) only pay off if log feeding is widened — a prompt-structure change, not a model change.
3. No official CPU benchmark exists for any of these models; vendor tables are GPU-based. Any capacity decision should be validated with `llama-bench` on the actual worker hardware (`docs/`), not with leaderboard numbers.
