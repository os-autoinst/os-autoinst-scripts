# Benchmark Report: Swift-1.5-Qwen3.8-27B-GSQ-RCO (IQ3_S) on AMD EPYC 7773X

- **Date**: 2026-11-17
- **Candidate**: `ukisai/Swift-1.5-Qwen3.8-27B-GSQ-RCO-GGUF` (file `Swift-1.5-Qwen3.8-27B-GSQ-RCO-IQ3_S.gguf`, 10.95 GiB)
- **Architecture**: Qwen 3.5 / 3.8 hybrid (64 recurrent Gated Delta Net layers + Attention, 26.9B dense parameters)
- **Test Host**: `openqaworker26` (w26) — AMD EPYC 7773X 64-Core (128 threads), 512 GiB RAM, Milan-X AVX2
- **Port**: Isolated test port 8082 (non-production)

---

## Deployment Configuration

```bash
./scripts/benchmarking/deploy_candidate.sh start \
  "ukisai/Swift-1.5-Qwen3.8-27B-GSQ-RCO-GGUF@Swift-1.5-Qwen3.8-27B-GSQ-RCO-IQ3_S.gguf" \
  8082 64 65536
```

Note: The Hugging Face repository ships an `-mtp.gguf` multi-token prediction module alongside the base model. Omitting `--hf-file` causes `llama.cpp` to load the draft module first and fail with missing tensor `blk.64.ssm_conv1d.weight`. Pinning the base file resolves initialization.

---

## Empirical Performance Measurements

Measured via direct API completion request on live w26 hardware:

| Metric | Swift-1.5-Qwen3.8-27B (IQ3_S) | Gemma-4-26B-A4B-it (UD-Q4_K_M) | Ratio / Delta |
|---|---|---|---|
| **Architecture** | Dense (26.9B active) + Gated Delta Net | MoE (3.8B active) + Sliding Window | 7.1x fewer active params in Gemma |
| **Prompt Eval Speed (Prefill)** | **18.7 tokens/second** | **142.8 tokens/second** | Gemma is **7.6x faster** |
| **Token Generation Speed (Decode)** | **8.19 tokens/second** | **23.24 tokens/second** | Gemma is **2.8x faster** |
| **Prefill Time on 5,000 Token Log** | **~267 seconds (4.5 min)** | **~35 seconds** | Gemma finishes prefill in 35s |
| **Prefill Time on 11,000 Token Log** | **~588 seconds (9.8 min)** | **~77 seconds** | Qwen pushes 600s proxy timeout |
| **Chat Template Formatting** | Emits thinking in `reasoning_content` | Standard OpenAI `message.content` | Qwen output requires custom parser |
| **Memory Footprint** | ~21 GiB total | ~18 GiB total | Both fit 512 GiB easily |

---

## Architectural Analysis & CPU Bottleneck

1. **Gated Delta Net (SSM) Recurrence on CPU**:
   Qwen 3.8 employs 64 layers of fused Gated Delta Net recurrence (`qwen35.ssm.state_size = 128`, `inner_size = 6144`). Unlike FlashAttention's highly parallel vector reductions, the sequential recurrence steps in SSM state transitions are memory-latency bound on multi-core x86 CPUs without specialized matrix coprocessors. This caps CPU prompt evaluation to 18.7 tok/s.

2. **Dense vs MoE Token Generation**:
   Generating each token in Swift-1.5 reads all 26.9B weights (~11 GB/token), yielding an 8.19 tok/s decode ceiling. In contrast, Gemma-4-26B's MoE router activates only 3.8B parameters per token (~1.6 GB/token), reaching 23.2 tok/s.

3. **Proxy Timeout Risk on o3**:
   Because `openqa-llm-investigate` resends failure logs across 3 consecutive turns, prefilling 5,000–11,000 tokens at 18.7 tok/s takes 4.5 to 9.8 minutes for Question 1 alone. Adding reasoning token generation exceeds the nginx 600s `proxy_read_timeout`.

---

## Verdict & Recommendation

- **Verdict**: Excellent model for interactive single-prompt coding and code review on worker38, but **unsuitable for automated o3 job failure analysis on CPU** due to prompt evaluation latency on large test logs.
- **Recommendation**: Deploy **`gemma-4-26B-A4B-it` (UD-Q4_K_M)** for production openQA job investigation on `w26` and `w27`.
