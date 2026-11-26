# Benchmark Report: gemma-4-26B-A4B-it (UD-Q4_K_M) on AMD EPYC 7773X

- **Date**: 2026-11-17
- **Test Host**: `openqaworker26` (w26)
- **Processor**: AMD EPYC 7773X (64 cores, 128 threads, 768 MiB L3 cache, Zen 3 Milan-X, AVX2)
- **Memory**: 512 GiB DDR4-3200 (8 channels, ~204.8 GB/s bandwidth)
- **Harness**: `ghcr.io/ggml-org/llama.cpp:server` (build `b8740-e34f04215`)
- **Port**: Isolated test port 8082 (non-production)

---

## Configuration & Tuning Parameters

```bash
podman run --name llama-bench-8082 -d \
  -v /opt/llm/models:/root/.cache/huggingface:z \
  -p 8082:8080 \
  ghcr.io/ggml-org/llama.cpp:server \
  -hf unsloth/gemma-4-26B-A4B-it-GGUF:UD-Q4_K_M \
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

### Key Optimizations Tested on w26
1. **Thread Count (`--threads 64`)**: Bound to all 64 physical cores, maximizing memory bus saturation and AVX2 throughput on Milan-X without SMT cache thrashing.
2. **FlashAttention (`-fa on`)**: Accelerates prompt processing across long failure logs.
3. **KV Cache Quantization (`-ctk q8_0 -ctv q8_0`)**: Halves KV memory consumption per slot while retaining full attention fidelity.
4. **Cache RAM (`--cache-ram 65536`)**: Critical fix. Default 8 GiB cache was causing prompt eviction between the 3 sequential investigation questions. 64 GiB cache preserves the full multi-turn chat in RAM.
5. **No Multimodal Projector (`--no-mmproj`)**: Saves ~1 GB RAM and eliminates unnecessary 1 GB image projection weights.

---

## Empirical Performance Metrics

### Inference Speeds (from llama-server telemetry)
- **Prompt Evaluation (prefill)**:
  - Cache miss (initial prompt): **62.5 – 75.0 tokens/second**
  - Cache hit (subsequent question turns): **142.8 tokens/second** (588 ms to evaluate new prompt tokens)
- **Token Generation (decode)**:
  - Average: **20.4 – 23.5 tokens/second**
- **End-to-End Investigation Duration (3 Question Turns)**:
  - Typical job (~5,000 token context): **145 seconds (2m 25s)**
  - Large job (~12,000 token context): **216 seconds (3m 36s)**
  - Margin to 600s proxy timeout: **2.8x – 4.1x safety margin**

### Host Resource Footprint
- **RAM Used**: 18.2 GiB total (Model weights: 15.8 GiB + 4 slots KV cache @ 64k tokens)
- **Host RAM Utilization**: **3.6%** of 512 GiB total (475 GiB remaining free)
- **CPU Load Average**: ~2.1 on 128 logical threads (virtually idle)

---

## Verdict Accuracy & Diagnostic Quality Comparison

Evaluated on historical jobs from `eval_dataset.json`:

| Job ID | Test Scenario | Ground Truth Reference | Baseline (`gemma-4-E4B-it`) | Candidate (`gemma-4-26B-A4B-it`) | Result |
|---|---|---|---|---|---|
| **6341261** | `create_hdd_agama_textmode@uefi-3G` | `boo#1288617` (Product Bug) | `Test issue` (FAIL) | **`Product bug`** (PASS) | **+1 Correct** |
| **6341516** | `create_hdd_textmode@64bit` | `boo#1288617` (Product Bug) | `Test issue` (FAIL) | **`Product bug`** (PASS) | **+1 Correct** |
| **6311672** | `autoyast_gnome@64bit` | `boo#1289038` (Product Bug) | `Infrastructure problem` (FAIL) | **`Product bug`** (PASS) | **+1 Correct** |
| **6311674** | `autoyast_btrfs_quota@64bit` | `boo#1289038` (Product Bug) | `Infrastructure problem` (FAIL) | **`Product bug`** (PASS) | **+1 Correct** |
| **6319607** | `create_hdd_gnome_libyui@aarch64` | `boo#1289464` (Product Bug) | `Product bug` (PASS) | **`Product bug`** (PASS) | **PASS** |

### Qualitative Diagnostic Accuracy
In Job 6341261 and 6341516, the baseline model failed with a generic `Test issue` assumption about needle matching. In contrast, `gemma-4-26B-A4B-it` isolated the exact root cause:
> *"The check_selinux_fails module failed because grep detected SELinux denials in /root/selinux_audit_logs.txt. Specifically, systemd-hostnamed was denied { search } access to the pressure directory in /proc."*

This exact finding corresponds directly to openSUSE Bugzilla bug **boo#1288617** (*"[SELinux] systemd-hostname generates new denials"*).

---

## Assessment & Conclusion

1. **Definite Quality Win**: Correctly reverses the baseline's false-positive `Infrastructure` and `Test issue` verdicts on real product bugs.
2. **CPU-Feasible Throughput**: 23.5 tokens/sec decode and 142 tokens/sec prompt eval on 64 threads easily satisfies the interactive requirements of o3.
3. **RAM Overhead Negligible**: 18 GiB footprint leaves >450 GiB headroom on w26/w27, allowing multiple parallel worker instances or concurrent openQA jobs when shared in the future.
4. **Harness Stability**: Standard upstream `llama.cpp:server` container operates flawlessly with zero custom patches or proprietary tensor formats.
