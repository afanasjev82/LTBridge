# Graph Report - .  (2026-10-20)

## Corpus Check
- 69 files · ~70,208 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 829 nodes · 1496 edges · 39 communities (34 shown, 5 thin omitted)
- Extraction: 89% EXTRACTED · 11% INFERRED · 0% AMBIGUOUS · INFERRED: 160 edges (avg confidence: 0.73)
- Token cost: 167,508 input · 0 output

## Community Hubs (Navigation)
- [[_COMMUNITY_Differential Harness Core|Differential Harness Core]]
- [[_COMMUNITY_Routes & Response Shape|Routes & Response Shape]]
- [[_COMMUNITY_Error Taxonomy|Error Taxonomy]]
- [[_COMMUNITY_Mock Bench & Prompt|Mock Bench & Prompt]]
- [[_COMMUNITY_ASGI App & Boundary|ASGI App & Boundary]]
- [[_COMMUNITY_CLI, Config & Reasoning Tests|CLI, Config & Reasoning Tests]]
- [[_COMMUNITY_LLM Client|LLM Client]]
- [[_COMMUNITY_Startup Banner|Startup Banner]]
- [[_COMMUNITY_Whatlang Detection Port|Whatlang Detection Port]]
- [[_COMMUNITY_Static File Serving|Static File Serving]]
- [[_COMMUNITY_Real-vLLM Bench|Real-vLLM Bench]]
- [[_COMMUNITY_Whatlang Data Generator|Whatlang Data Generator]]
- [[_COMMUNITY_Server-Launch Fixtures|Server-Launch Fixtures]]
- [[_COMMUNITY_Heartbeat Streaming|Heartbeat Streaming]]
- [[_COMMUNITY_Boundary Tests|Boundary Tests]]
- [[_COMMUNITY_Bench Reports & Data|Bench Reports & Data]]
- [[_COMMUNITY_API Endpoints & UI|API Endpoints & UI]]
- [[_COMMUNITY_Concurrency Sweep Bench|Concurrency Sweep Bench]]
- [[_COMMUNITY_Rust Oracle Builder|Rust Oracle Builder]]
- [[_COMMUNITY_B1 Gate & Logging Fix|B1 Gate & Logging Fix]]
- [[_COMMUNITY_Real-Bench Protocol|Real-Bench Protocol]]
- [[_COMMUNITY_Whatlang Port Tests|Whatlang Port Tests]]
- [[_COMMUNITY_Suite Runner|Suite Runner]]
- [[_COMMUNITY_Reasoning Model Handling|Reasoning Model Handling]]
- [[_COMMUNITY_Mock Sweep & Detection Gate|Mock Sweep & Detection Gate]]
- [[_COMMUNITY_Embedding Surface Docs|Embedding Surface Docs]]
- [[_COMMUNITY_Workers & Metrics|Workers & Metrics]]
- [[_COMMUNITY_Frontend JS App|Frontend JS App]]
- [[_COMMUNITY_Detection Sweep Tests|Detection Sweep Tests]]
- [[_COMMUNITY_Rust Response Dumper|Rust Response Dumper]]
- [[_COMMUNITY_Core Isolation Guard|Core Isolation Guard]]
- [[_COMMUNITY_Package Init|Package Init]]
- [[_COMMUNITY_Package Metadata|Package Metadata]]

## God Nodes (most connected - your core abstractions)
1. `fire_both()` - 43 edges
2. `assert_parity()` - 35 edges
3. `Config` - 25 edges
4. `render_banner()` - 23 edges
5. `LlmError` - 19 edges
6. `LLMClient` - 19 edges
7. `MockBehavior` - 19 edges
8. `_client()` - 19 edges
9. `_client()` - 17 edges
10. `create_app()` - 16 edges

## Surprising Connections (you probably didn't know these)
- `Config` --uses--> `Config`  [INFERRED]
  tests/unit/test_banner.py → backend/src/ltbridge/config.py
- `TestClient` --uses--> `Config`  [INFERRED]
  tests/unit/test_static_files.py → backend/src/ltbridge/config.py
- `Litestar default LoggingConfig QueueHandler/QueueListener` --conceptually_related_to--> `Litestar`  [INFERRED]
  bench/REPORT.md → README.md
- `--workers flag (Granian worker processes)` --semantically_similar_to--> `--workers N>1 breaks /metrics`  [INFERRED] [semantically similar]
  README.md → bench/REPORT.md
- `Any` --uses--> `Config`  [INFERRED]
  tests/unit/test_response_key_order.py → backend/src/ltbridge/config.py

## Import Cycles
- 1-file cycle: `backend/src/ltbridge/server/app.py -> backend/src/ltbridge/server/app.py`
- 1-file cycle: `tests/unit/test_boundary.py -> tests/unit/test_boundary.py`

## Hyperedges (group relationships)
- **Hybrid reasoning model handling (thinking suppression and failure modes)** — readme_hybrid_reasoning_models, readme_enable_thinking_kwarg, readme_llm_reasoning_effort, readme_think_block_stripping, readme_grace_race_fallback [EXTRACTED 1.00]
- **B1 wrapper-overhead optimizations (each re-validated against the differential suite)** — bench_report_b1_wrapper_overhead, bench_report_logging_pipeline_fix, bench_report_workers_optimization, bench_report_double_json_parse_fix [EXTRACTED 1.00]
- **Real-vLLM benchmark measurement protocol** — bench_report_abab_ordering, bench_report_time_boxed_cells, bench_report_fresh_salts, bench_report_engine_idle_gate, bench_report_token_volume_fairness, bench_report_poisoned_row_rerun, bench_report_foreground_invocation_pattern [EXTRACTED 1.00]

## Communities (39 total, 5 thin omitted)

### Community 0 - "Differential Harness Core"
Cohesion: 0.07
Nodes (59): make_pair(), pair(), Factory: launch a Rust+Python pair, each with its own mock. Module-scoped., Default-flag server pair (char-limit 5000, no api key, no token caps)., ServerPair, assert_parity(), _fire(), fire_both() (+51 more)

### Community 1 - "Routes & Response Shape"
Cohesion: 0.05
Nodes (62): Any, ASGIResponse, Config, LLMClient, Request, Response, State, improve_formatting() (+54 more)

### Community 2 - "Error Taxonomy"
Cohesion: 0.05
Nodes (60): Any, bad_response(), _byte_offset(), byte_position(), _closed_string_end(), empty_choices(), _enclosing_container(), field_spans() (+52 more)

### Community 3 - "Mock Bench & Prompt"
Cohesion: 0.06
Nodes (42): _arm_rows(), b1(), b2(), bench_c1(), bench_c32(), bench_c8(), _launch_mock(), main() (+34 more)

### Community 4 - "ASGI App & Boundary"
Cohesion: 0.07
Nodes (42): ASGIApp, Any, AsyncClient, Config, Litestar, LLMClient, Request, Response (+34 more)

### Community 5 - "CLI, Config & Reasoning Tests"
Cohesion: 0.07
Nodes (38): ArgumentParser, Config, Server configuration dataclass., Runtime configuration for ltbridge, sourced from CLI arguments., Rebuild a Config from the LTBRIDGE_* environment variables.          Used by `ap, _build_parser(), main(), CLI entry point for ltbridge.  Parses command-line arguments (mirroring LTEngine (+30 more)

### Community 6 - "LLM Client"
Cohesion: 0.07
Nodes (34): AsyncClient, _is_valid_completion_shape(), LLMClient, Async OpenAI-compatible LLM API client.  Port of LTEngine's llm_api.rs — calls a, Close the underlying HTTP client (no-op if externally managed)., Fetch the first available model from /v1/models and cache it., Run a chat completion and return the assistant's reply text.          Raises:, Drop a leading reasoning block from ``message.content``.      Not in the Rust re (+26 more)

### Community 7 - "Startup Banner"
Cohesion: 0.10
Nodes (39): Config, _build_rows(), _fmt_num(), _git(), _git_summary(), _model_row(), _pkg_version(), Startup banner: ASCII art, build info, and the applied-config table.  Python por (+31 more)

### Community 8 - "Whatlang Detection Port"
Cohesion: 0.08
Nodes (33): ltbridge.core — transport-free translation logic.  Importable without Litestar (, _alphabet_raw_detect(), _alphabet_tables(), _as_i32(), _calc_alphabet_weight(), _calculate_confidence(), _calculate_distance(), _case_ignorable() (+25 more)

### Community 9 - "Static File Serving"
Cohesion: 0.11
Nodes (29): Path, Path, get_static_dir(), ltbridge-frontend: static web UI assets for ltbridge., Return the path to the bundled static files directory., Resolve a request PATH — independent of method — to a real static     asset. Ret, _resolve_static_path(), TestClient (+21 more)

### Community 10 - "Real-vLLM Bench"
Cohesion: 0.13
Nodes (30): _bodies_for(), _build_report(), _fairness_table(), _load_results(), main(), measure_cell(), _median_over_rounds(), _median_table() (+22 more)

### Community 11 - "Whatlang Data Generator"
Cohesion: 0.12
Nodes (29): Match, dump_case_tables(), emit(), _fmt_ranges(), _fmt_str_tuple(), locate_crate(), main(), parse_alphabets() (+21 more)

### Community 12 - "Server-Launch Fixtures"
Cohesion: 0.14
Nodes (21): free_port(), _launch_py(), _launch_rust(), MockControl, ServerHandle, start_mock(), _wait_http(), create_mock_app() (+13 more)

### Community 13 - "Heartbeat Streaming"
Cohesion: 0.10
Nodes (24): Any, Response, ResultBody, _encode(), _heartbeat_stream(), Grace-race + heartbeat streaming — port of main.rs's ``GRACE_PERIOD`` logic.  LT, Race ``work`` against the grace period; buffer if fast, stream if slow.      Arg, Serialize a response body exactly as the BUFFERED path would.      This must be (+16 more)

### Community 14 - "Boundary Tests"
Cohesion: 0.15
Nodes (19): Litestar, TestClient, _client(), _inner(), Unit coverage for the EXPORTED request boundary (v0.1.2).  ``boundary()`` is the, A host-style app: LTBridge-shaped route + a route of the host's own., test_boundary_carries_no_metrics_of_its_own(), test_exact_api_path_is_delegated_to_the_inner_app() (+11 more)

### Community 15 - "Bench Reports & Data"
Cohesion: 0.15
Nodes (19): ltbridge Python backend package, B2 end-to-end sanity benchmark (real vLLM), Task 13 B1/B2 benchmark report, Confound 1: engine cold-start ordering, Live sanity tests (test_live_sanity.py), Confound 2: prefix-cache contamination across arms, terminate -> wait(10) -> kill teardown pattern, build_llm_client() (+11 more)

### Community 16 - "API Endpoints & UI"
Cohesion: 0.15
Nodes (19): Double JSON parse elimination (_has_type_mismatch / _field_type_mismatch), Auto-detect source language (source=auto), --char-limit (max characters per request), POST /detect, GET /frontend/settings, GET /health, GET /languages, LibreTranslate API compatibility (+11 more)

### Community 17 - "Concurrency Sweep Bench"
Cohesion: 0.24
Nodes (17): _build_report(), _derived_table(), _env_lines(), _load_results(), main(), _measure_arms(), _median_arm(), _median_table() (+9 more)

### Community 18 - "Rust Oracle Builder"
Cohesion: 0.24
Nodes (15): _checkout_and_pull(), _gitdir_pointer(), main(), Path, Build the Rust LTEngine api-mode reference binary in a dedicated worktree.  Neve, Raw target of REF_DIR/.git ('gitdir: <path>'), or None if unreadable., Recreate REF_DIR as a Linux-native worktree. Returns True on success.      The e, Like run(), but returns False (with a warning) instead of raising. (+7 more)

### Community 19 - "B1 Gate & Logging Fix"
Cohesion: 0.21
Nodes (14): Amended B1 gate (C=1 p50 +3ms; C=32 median RPS ratio >= 0.70), B1 wrapper-overhead benchmark (mock upstream), In-process mock upstream self-saturation, Litestar default LoggingConfig QueueHandler/QueueListener, Double logging pipeline fix (logging_config=None, httpx at WARNING), Original (retired) B1 gate, Out-of-process mock upstream (mock_upstream --port), Randomized arm order + per-arm warmup (+6 more)

### Community 20 - "Real-Bench Protocol"
Cohesion: 0.21
Nodes (12): ABAB round ordering, Desktop GPU contention on local RTX 3090, Engine-idle gate (vllm:num_requests_running/waiting), Fresh salts per arm per cell (bench.texts.salted / long_text), Poisoned-row re-run rule, PRODUCTION_CEILING_RPS headroom line, PromptBuilder, Real-vLLM benchmark, dedicated engine box (authoritative) (+4 more)

### Community 21 - "Whatlang Port Tests"
Cohesion: 0.17
Nodes (7): Structural guards for the pure-Python whatlang port.  The bulk of the parity evi, ``allowlist=None`` mirrors ``FilterList::All``.      The recorded sweep can only, The port sorts with Python's stable ``sorted``; that is only exact <= 32., U+1D2B and U+1D78 belong to two scripts, so the counter scan order matters., test_detect_without_an_allowlist_sees_unsupported_languages(), test_every_sorted_slice_stays_within_the_stable_sort_bound(), test_latin_and_cyrillic_range_tables_overlap()

### Community 22 - "Suite Runner"
Cohesion: 0.27
Nodes (10): _find_summary_line(), main(), Path, One command, either OS: ensure the Rust oracle, then run the full suite.  Enviro, Translate a Windows path (e.g. ``W:\\Work\\...``) to its WSL mount point., Re-exec this same script inside WSL and relay its exit code.      The build/test, _run_native(), _run_via_wsl() (+2 more)

### Community 23 - "Reasoning Model Handling"
Cohesion: 0.29
Nodes (10): Opt-in compat mode omitting enable_thinking (not built), DeepSeek API (not usable today), chat_template_kwargs enable_thinking=false, Grace-race fallback to untranslated source text, Hybrid reasoning models (Qwen3.x, QwQ, DeepSeek-R1 distills), --llm-reasoning-effort flag, --llm-timeout, LM Studio (Apple MLX) (+2 more)

### Community 24 - "Mock Sweep & Detection Gate"
Cohesion: 0.28
Nodes (9): Extended mock sweep (C up to 64, Rust-free tree), One bounded foreground invocation per rung/cell, Generator/mock floor ceiling from C=8, Detection sweep zero-divergence gate, Generated whatlang tables (_whatlang_data.py), whatlang-py PyO3/maturin binding (removed), Stable sorted() vs Rust sort_unstable fidelity choice, whatlang 0.18.0 crate (+1 more)

### Community 25 - "Embedding Surface Docs"
Cohesion: 0.33
Nodes (9): boundary() outer ASGI layer, Byte-parity port, create_router(), Embedding LTBridge in a host Litestar app, Exact-path gate (API_PATHS | extra_api_paths), exception_handlers(), Litestar, Rust std case-table snapshot for lowercasing (+1 more)

### Community 26 - "Workers & Metrics"
Cohesion: 0.36
Nodes (8): actix-web (Rust server runtime), py-w1 GIL-bound RPS decline past C=8, --workers N>1 breaks /metrics, --workers N benchmark optimization, Granian, GET /metrics (Prometheus, actix_web_prom-compatible), Startup banner (build info + config), --workers flag (Granian worker processes)

### Community 28 - "Detection Sweep Tests"
Cohesion: 0.33
Nodes (3): Recorded zero-divergence gate for the pure-Python whatlang port.  ``tests/fixtur, A crate bump must re-record the sweep, not silently reuse the old one., test_fixture_matches_the_generated_tables()

## Ambiguous Edges - Review These
- `Real-vLLM benchmark, local RTX 3090 (superseded)` → `PRODUCTION_CEILING_RPS headroom line`  [AMBIGUOUS]
  bench/REPORT.md · relation: references

## Knowledge Gaps
- **42 isolated node(s):** `ltbridge`, `JSONDecodeError`, `AsyncClient`, `LlmError`, `NotFoundException` (+37 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **5 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **What is the exact relationship between `Real-vLLM benchmark, local RTX 3090 (superseded)` and `PRODUCTION_CEILING_RPS headroom line`?**
  _Edge tagged AMBIGUOUS (relation: references) - confidence is low._
- **Why does `PromptBuilder` connect `Mock Bench & Prompt` to `Routes & Response Shape`?**
  _High betweenness centrality (0.259) - this node is a cross-community bridge._
- **Are the 10 inferred relationships involving `Config` (e.g. with `ArgumentParser` and `MonkeyPatch`) actually correct?**
  _`Config` has 10 INFERRED edges - model-reasoned connections that need verification._
- **Are the 13 inferred relationships involving `render_banner()` (e.g. with `test_render_banner_contains_ascii_art_and_subtitle()` and `test_render_banner_explicit_model_has_no_resolved_suffix()`) actually correct?**
  _`render_banner()` has 13 INFERRED edges - model-reasoned connections that need verification._
- **Are the 9 inferred relationships involving `LlmError` (e.g. with `Exception` and `MonkeyPatch`) actually correct?**
  _`LlmError` has 9 INFERRED edges - model-reasoned connections that need verification._
- **What connects `ltbridge`, `ltbridge — LibreTranslate-compatible translation server.`, `CLI entry point for ltbridge.  Parses command-line arguments (mirroring LTEngine` to the rest of the system?**
  _231 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Differential Harness Core` be split into smaller, more focused modules?**
  _Cohesion score 0.07002012072434607 - nodes in this community are weakly interconnected._