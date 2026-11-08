# Spike notes — default role bindings

M0 was skipped in favour of measuring models on the real hardware with
[ai-bench](https://github.com/hofftodd/ai-bench) (Oct 23 – Nov 8, 2026). This
file is M0's output: the measurements that matter to Foreman, and the default
role → model bindings they lead to. It resolves spec OQ-1.

Sources: `ANALYSIS.md` (R9700), `ANALYSIS-mac.md` (Mac Studio) and
`ANALYSIS-compare.md` in ai-bench.

## What was measured

- **LiveCodeBench** (30 problems, 15 medium + 15 hard, v6): competitive-programming
  solutions at 32k and 64k output-token budgets. No tools. About ±12 points of
  run-to-run noise, so differences under ~3 problems don't rank models.
- **terminal-x3**: 18 agentic shell tasks, 3 runs each, in a Docker sandbox, through
  the model's own tool calls. Closest to what a Foreman coding agent does. It is
  saturated at the top (several models at 96–100%), so it confirms tool calling
  more than it ranks.
- **Speed**: generation and prompt processing at 512 and 8k tokens, time to first
  token, and total throughput with 1–8 requests in flight.

## Candidates

| model | node / engine | size | LCB 64k | terminal-x3 | gen @8k | first token @8k | total, 8 in flight |
|---|---|---|---|---|---|---|---|
| DeepSeek V4 Flash Q4 (`ds4-q4`) | Mac / DwarfStar | 153 GB | **93.3** | **100** | ~56 tok/s | ~9 s | batches poorly |
| flint (40B-A2.5B MoE) | Mac / oMLX | 23 GB | 76.7 | 96.3 | **161** tok/s | **1.6 s** | **351** tok/s |
| flint | R9700 / llama.cpp | 23 GB | 66.7 (Mac llama.cpp: 80.0) | 94.4 | 79 tok/s | 2.5 s | — |
| ThinkingCap-Qwen3.8-27B | R9700 / llama.cpp | 17 GB | 80.0 | 98.1 | 53 tok/s | 9.5 s | — |
| Qwen3.8-Flash-Next (MoE) | Mac / oMLX | 108 GB | 73.3 | — | 102 tok/s | 2.0 s | 226 tok/s |
| qwen3.8-27b | Mac / oMLX | 17.5 GB | 66.7 (83.3 at 128k) | 100 (R9700) | 112 tok/s | 4.4 s | 121 tok/s |
| Ripple-65B Preview | Mac / llama.cpp | 40 GB | 76.7 | **100** | 21 tok/s | 19 s | ~23 tok/s |

Not recommended: GLM-5.3-Flash and MiMo-V2.6-Flash (63–70% even at 128k, very long
answers); Qwen3.8-Flash-Next on sushi (its reasoning is cut short: 63–70%);
Ripple-65B (same scores as flint at a seventh of the speed).

## Default bindings

| role | binding | why |
|---|---|---|
| `project_planner` | **ds4-q4** (Mac, DwarfStar) | Strongest reasoner on either machine (93.3% at 64k, 14 of 15 hard). Planning is low-volume, so its weak batching doesn't matter. |
| `task_planner` | **ds4-q4** (Mac, DwarfStar) | Same model as the project planner: one resident model serves both. |
| `tester` | **ThinkingCap-Qwen3.8-27B** (R9700, llama.cpp) | 98.1% on terminal-x3 and 80.0% at 64k; the R9700's best model that fits in VRAM. Keeps test-writing on a different model family from the builder, so a shared blind spot is less likely to pass both. |
| `builder` | **flint** (Mac, oMLX) | 96.3% on terminal-x3 with no malformed tool calls, 161 tok/s, and 351 tok/s in total with 8 builders in flight: the best executor throughput measured. |
| `escalation` | **ds4-q4**, then cloud | The local model with the most headroom; cloud (Claude) when it fails too. |

Fallbacks: builder → flint on the R9700 (llama.cpp, 94.4%); planners →
Qwen3.8-Flash-Next on oMLX (faster, weaker, and it can't be resident alongside
ds4-q4); tester → qwen3.8-27b on the R9700 (100% on terminal-x3, but needs a
large budget: its answers are long).

## Node setup

| node | base_url (ai-bench port) | serves | context_window | capacity |
|---|---|---|---|---|
| mac-ds4 | `http://<mac>:8000/v1` (`ds4-server --metal -c 262144 --batched-session 4`) | `deepseek-v4-flash` | 262,144 | 2 |
| mac-omlx | `http://<mac>:8010/v1` (`omlx serve --model-dir ~/models/omlx`) | `flint-oq4e` | 135,168 | 8 |
| r9700 | `http://<mini-pc>:8080/v1` (llama-swap) | `thinkingcap-qwen3.8-27b` | as configured in llama-swap | 4 |

- **Memory on the Mac:** ds4-q4 (153 GB) + flint (23 GB) is 176 GB of 256 GB,
  leaving room for KV caches. Not yet load-tested with both busy at once.
- **One endpoint, swap-aware:** with ai-swap on both machines (Nov 2026), every
  model is reachable through either machine's endpoint (`http://studio.local:8080/v1`
  or `http://mini-ubuntu.local:8080/v1`), so nodes can be "one node per model" on
  the same URL, each with its own capacity and context window. Foreman reads
  ai-swap's status and won't start work that would swap out a model a running
  step is using; the bindings above fit together on each machine, so they never
  wait for each other.
- **Persistent servers:** ai-bench starts and stops each server per run. Foreman
  needs them always up, so ds4-server and oMLX need LaunchAgents on the Mac.
- **Sampling is set on the servers, not in Foreman.** Foreman's gateway passes
  the agent's request through, and bindings carry no sampling. Recommended:
  ds4-q4 temperature 1.0, top_p 1.0; flint temperature 1.0, top_p 0.95,
  top_k 20, earlier reasoning kept in context; ThinkingCap temperature 0.6,
  top_p 0.95, top_k 20.
- **oMLX settings:** turn MTP off for flint (`mtp_enabled: false` in
  `~/.omlx/model_settings.json`; it only helps single requests), and raise
  `max_context_window` above 32,768 in `~/.omlx/settings.json`.
- **Give planners room to think:** ds4-q4 scored 76.7% at 32k and 93.3% at 64k
  output tokens; the gain was answers that no longer got cut off.

## Still to check

- ds4-q4 and flint busy together on the Mac (memory pressure, speed).
- A second 64k LiveCodeBench pass for ds4-q4 and flint (the two picks lean on one run each).
- A harder terminal suite: terminal-x3 no longer separates the top models.
- Real Foreman runs: the bench tasks are smaller than Foreman's, and context
  packs may be longer than the 8k prompts measured.
