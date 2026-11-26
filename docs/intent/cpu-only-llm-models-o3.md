# Intent: Best CPU-Only LLM Inference for o3 Job Investigation

- **Outcome**: Upgrade o3 LLM job-investigation (root-cause reasoning + classification) from gemma-4-E4B to the best CPU-only **model + harness**, chosen by measurement.
- **User**: okurz + openQA team (plusky as `openqa-llm-investigate` author; consumers of LLM comments on o3).
- **Why now**: gemma-4 is months old, newer MoE models + alternative harnesses (llama-modchip, strata, etc.) have emerged, and LLMs now sit on dedicated w26+w27 free to experiment.
- **Success**: A candidate that achieves higher classification-match against human verdicts (cross-checking every `comment=LLM` job on o3: bugzilla⇒Product, progress⇒Test, infra⇒Infra; any non-matching class = model too weak) and stronger scores on generic benchmarks (e.g. artificialanalysis.ai), while running within the CPU-only latency/timeout budget on measured live hardware.
- **Constraint**: CPU-only on w26+w27; all choices driven by inspecting live hardware specs + benchmarking candidates, not theory. **All harnesses in scope** (llama.cpp, llama-modchip, strata if CPU-runnable, any better alternative).
- **Out of scope**: Pure GPU-only paths; the #209982 small-classifier log-retention/offload work; the noisy-neighbor operational politics (dedicated workers today; sharing as "nice neighbors" is a subsequent phase).
