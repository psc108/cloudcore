# Placing LLM work on the best available machine — Phased Implementation

**Status:** planned 2026-10-05. **Owner:** Paul Scott.

## Context

Direct requests (2026-10-05):
- "I don't want quality compromised, we're already on a 14b model which isn't the best."
- "we should prepare for faster better hardware and ensure that we simply choose the best specified system to run whatever we need where it's possible. that's the point of choosing where to place the work."

L25 put a 7B "lab reader" on Stourport so the lab's readings wouldn't slow students' answers. That worked (readings took 50–80 s), but it fell short in two ways:
- **Quality:** the 7B picked weaker checks than the 14B (#16).
- **Speed:** students' answers were still slow (median ~8½ min during lab runs). The readings were the smallest of the lab's model calls. Repairs, and especially "try another way" (a whole new answer), also run on the coordinator's 14B.

The placement recommender (`/v1/peers/recommend-placement`, `capacity_gate.best_fit`) already chooses a host and the largest flavor it can afford, ranking by traffic-light verdict and load. It doesn't know how **fast** a host is, or what a workload **needs**.

## Aim

Every piece of LLM work runs on the best machine that can run it **without lowering quality**. A faster host, or a GPU, is used as soon as it exists, with no template edits. When work must share a machine, students come first.

## Principles

- **A quality floor, never lowered for speed.** A role's model is the configured one (today Qwen2.5-Coder-14B Q4) or better. Never smaller.
- **Measured, not assumed.** Hosts are ranked by measured LLM throughput for the model in question, not by core counts.
- **Never all of a host.** `HOST_RESERVED_CORES` stays, and no host is named in code.

## Stages

| # | Stage | Who |
|---|---|---|
| C1 | **Students first, now.** Every lab model call (readings, repairs, another way) waits while a student's request is being answered on the same model. The 7B reader is switched off (quality floor). This gives students their speed back today, at the same quality. | **Done 2026-10-05** (6faa38d): 8 questions asked while their lab runs ran gave a median answer of 398 s (max 633), against L22's ~384 s before lab readings, L25's ~506 s with the 7B reader and up to 20 min in L24. Logged waits: a repair 82 s, another-way 159 s. Reader off, all on the 14B |
| C2 | **Roles and their needs.** llm-chat's LLM work becomes roles: `answer` (students) and `lab` (readings, repairs, another way). Each declares its model (the quality floor), its memory and its minimum cores. Placement chooses a host per role from those needs. | **Done 2026-10-05** (93db717): the llm-chat lab instance serves the coordinator's own model by default. The coordinator checks a lab endpoint's `/props` and refuses any other model, so a 7B endpoint is turned away (tested) |
| C3 | **Measure each host.** A short benchmark of the role's model: prompt and generation tokens/s for the actual model file, from the repo, at a fixed size. Each host runs it once and again when its hardware changes. The result lives in that host's stats, beside its cores, RAM and (later) GPUs. | **Done 2026-10-05** (9d9345e): `api/llm-bench.py`; host stats report `llm_bench` and `gpus`. Qwen2.5-Coder-14B Q4: Llwyn-y-Groes (i7-8650U, 6 threads) prompt 5.54 / generation 2.39 tok/s; Stourport (i5-6300U, 2 threads) 3.91 / 1.90. Both are 4- and 2-core laptop CPUs with hyperthreading; 14B generation is memory-bandwidth-bound, so Stourport runs it at ~80% of Llwyn-y-Groes's speed, not the crawl core counts suggested |
| C4 | **Place by measured speed.** The recommender ranks hosts that fit a role by measured throughput, then by load. The llm-chat build places `answer` and `lab` from it: the same host when only one is good enough, separate hosts when two are. | **Done 2026-10-05** (93db717): `GET /v1/peers/recommend-llm-placement?model=…&roles=answer,lab&min_ram_mb=…` ranks hosts that fit by measured tokens/s. For the 14B it placed `answer` on Llwyn-y-Groes (`standard.2xlarge`, 2.39 tok/s) and `lab` on Stourport (new `memory.large`: 2 vCPU, 16 GB; 1.90 tok/s). llm-chat was built from it. **Build manager** (9885621): for a template with `model_filename` and a lab instance, the form pre-fills the coordinator and lab model hosts and flavors from it and says why (measured tok/s). It falls back to the load-based pick, saying so, when no host has the memory free or none has measured the model |
| C5 | **Route at run time.** The coordinator knows every endpoint able to serve a role (same model or better), with its measured speed. Per request it takes the fastest idle one. Students first wherever one endpoint is shared. | **Done for the lab role** (93db717): readings, repairs and another-way go to an idle lab endpoint serving the same model, else students first on the coordinator. Measured with the same 8 questions: median answer **224 s** (max 359), against C1's 398 s; **0** lab calls waited for students. Lab verdicts comparable (2 goal-verified). The `answer` role has one endpoint, so per-request routing for students waits for a second answer-capable host |
| C6 | **Prove it.** Today: placement keeps both roles on Llwyn-y-Groes (Stourport's 2 spare vCPUs fall below the floor for the 14B). Then add a stand-in faster host (a peer with a better measured score): the `lab` role, and the `answer` role if it is faster, move there with no template change. | Both |

## What today's hardware means

| Host | Cores (spare) | RAM free | Fits the 14B role? |
|---|---|---|---|
| Llwyn-y-Groes | 8 (6) | ~9 GB beside the coordinator | Yes: runs it now |
| Stourport | 4 (2) | ~23 GB | Measured (C3): 1.90 tok/s generation, ~80% of Llwyn-y-Groes. Good enough for the `lab` role's own 14B |

C3's measurement changes the guess above: the best placement today is two 14Bs, `answer` on Llwyn-y-Groes and `lab` on Stourport, with no quality cost and no sharing. Students first (C1) stays for whenever roles must share. The point of C2–C5 is that the decision is made by measurement each time, not by this table.

## Risks

- **Benchmark cost:** a 14B benchmark takes minutes. It runs once per host and model, and isn't repeated per build.
- **Lab latency:** with students first, lab runs wait while students are active. That's accepted: the lab's verdicts are unchanged, only later.
- **GPUs:** the stats and the benchmark should report a GPU when one exists. The llama.cpp build used today is CPU-only, so GPU hosts would need a GPU build.
