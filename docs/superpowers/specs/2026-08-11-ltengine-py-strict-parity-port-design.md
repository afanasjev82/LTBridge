# LTEngine-py — strict-parity port of Rust LTEngine api-mode (Medusa V2 · P1)

- **Date:** 2026-08-11
- **Status:** Approved in-session; pending written-spec review
- **Program context:** P1 of the Medusa V2 design
  (`smartech-medusa/docs/superpowers/specs/2026-08-06-medusa-v2-headless-fleet-and-lt-api-design.md`, local-only).
  This repo (`LTEngine-py`, package `ltengine`) IS the V2 spec's shared LT-API package
  (provisional name `smartech-lt-api` is retired — naming decision below).
- **Rust reference:** `LTEngine` repo, branch `development` (dev HEAD at time of writing:
  `3070771`), built `--no-default-features --features api`. No GPU required.
- **Live LLM for sanity/e2e:** `smartech-ltengine-vllm` container, `http://localhost:8000`,
  serving `gemma-4-12b-it` (AWQ-INT4, `max_model_len` 32768, unauthenticated on LAN dev box).

## 1. Goal & exit criteria

`LTEngine-py` becomes a **byte-parity** replacement for LTEngine api-mode, proven by a
**zero-waiver differential suite** against the live Rust binary, and benchmarked against it.

**P1 gate (all required):**
1. Differential suite green with zero waivers (layers 1–3, §4).
2. `ruff` + `mypy` clean (family quality bar).
3. Benchmarks within thresholds (§7) with a written report committed to the repo.
4. Every audit item (§5) closed.
5. Repo on `master` + `development`; one-command suite run (builds/launches the Rust
   reference automatically) documented in README.

## 2. Decisions log

| Decision | Choice |
|---|---|
| Parity bar | **Strict byte-parity, zero waivers** — quirks preserved bug-for-bug (V2 spec D2 strict mode). The one deliberate non-parity: the scaffold's silent-200-on-error bug is *fixed to match Rust* (Rust's taxonomy is the contract). |
| Detection (V2 spec D1) | whatlang-parity required (§6). lingua remains only as a non-default fallback flag if PyO3 routes fail — escalate before compromising. |
| Naming | Repo `LTEngine-py`, package/CLI `ltengine`, branches `master` + `development` (family convention). P2/P3 depend on package `ltengine`. |
| Approach | Tests-first fix-forward: differential harness + golden fixtures first (red) → fix scaffold to green → suite-protected pure-structure refactor into core/server layering. No mixed refactor+behavior commits. |
| Frontend workspace member | Stays as-is (custom dev UI, outside parity scope; Rust ships a different embedded Vue UI anyway). Unknown-path *API* semantics must still match Rust (404), see §5. |
| `/metrics` | Endpoint restored (prometheus, `ltengine` prefix). Presence + content-type tested; metric values exempt from byte-parity (runtime-dependent). |

## 3. End-state layout (after the protected restructure)

```
backend/src/ltengine/
  core/            # transport-free, importable without Litestar:
                   #   prompt.py, languages.py, detection.py, token_budget.py,
                   #   formatting.py, errors.py
  server/          # routes.py  — create_router(config, client): the P2/P3 mount point
                   # streaming.py — grace-race + heartbeat machinery
                   # app.py — standalone Litestar app; cli.py — argument parsing
  llm_client.py, config.py
tests/
  unit/  fixtures/golden/*.json  differential/   # harness + Rust/Python runners
bench/             # B1 overhead bench (mock upstream) + B2 e2e sanity (real vLLM)
frontend/          # unchanged workspace member (static dev UI)
```

The restructure lands ONLY after the differential suite is green on the current shape,
as pure-structure commits with the suite proving behavior-neutrality.

## 4. Differential validation harness (built first)

**Layer 1 — payload-capture parity (core).** A recording mock upstream (small ASGI app):
serves canned OpenAI-compatible responses, records each incoming request JSON. The runner
launches Rust and Python servers side by side against the same mock and fires every golden
fixture at both, asserting byte-equality of:
- HTTP status + response body (+ headers that carry contract: `Content-Type`,
  chunked/heartbeat behavior);
- the captured `/v1/chat/completions` payloads: `model`, `messages` (system+user text
  exact), `temperature: 0.0`, `chat_template_kwargs: {"enable_thinking": false}`,
  `max_tokens` **presence/absence** and value.
Deterministic, GPU-free, runs on Windows and Linux.

**Layer 2 — error-path parity.** Mock scripted per-case: connect-refused, read-timeout,
upstream 429/502/503/504, upstream 4xx with body, non-JSON garbage, empty `choices` →
identical status + `{"error": ...}` body from both servers. Includes the **slow-failure
wart**: mock delays > 1 s then fails → BOTH servers emit a chunked 200 with leading
space heartbeats and the original text (bug-for-bug).

**Layer 3 — live-vLLM sanity.** Against `smartech-ltengine-vllm`: both servers translate a
small real set (200s, plausible output, `detectedLanguage` shape), one long-text case
exercises the heartbeat end-to-end. Model *output text* is never byte-compared (vLLM
batching nondeterminism); Layer 1 already pins the request side.

**Golden fixtures (~40–60 cases):** language-table coverage incl. aliases
(`zh-Hans`/`zh-Hant`/`pt-BR`), `source=auto` detection, punctuation-transfer + case-mirroring
quirks, multibyte text straddling the **byte**-limit boundary, `format=html`, identity
(`source==target`), missing/blank params (exact messages), bad format, bad `api_key` (403),
`alternatives` valid + garbage, unsupported content-type (400), form-urlencoded + multipart
bodies, unknown language codes (`{code} is not supported`), `/detect` (incl. api-key +
char-limit), `/languages` full body, `/frontend/settings` full body, `/translate_file` +
`/suggest` 501s, unknown path → 404.

## 5. Parity fix list (each item lands as a red differential/unit test first)

**Critical**
1. Silent-200-on-error (`app.py` catch-all → returns original text) → Rust taxonomy:
   timeout / unreachable / upstream 502·503·504·429 → **503**; other upstream status,
   parse failure, empty choices → **500**; error text mirrors Rust `Display` (upstream
   body passed through verbatim in the message).
2. `chat_template_kwargs: {"enable_thinking": false}` in every completion payload.
3. `max_tokens` pipeline: `--llm-max-tokens` (default 0 ⇒ **omit field**); dynamic budget
   active only when `llm_max_tokens`, `--llm-chars-per-token`, `--llm-max-tokens-mult`
   all > 0: `cap = ceil(ceil(chars / cpt) · mult)`, then `max(--llm-max-tokens-floor
   [64])`, then `min(ceiling)` — **ceiling wins over floor**; else static ceiling.
4. `--llm-timeout` (default 120 s; `0` = no timeout), applied client-wide.

**Major**
5. Grace-race + heartbeat: poll the LLM future against a 1 s timer; finish ≤ 1 s →
   buffered response with true status; > 1 s → commit chunked 200, emit one ASCII space
   per second, JSON last; client-disconnect on failed keepalive write cancels the
   upstream call (KV-slot reclaim).
6. Global exception handlers: every error body is `{"error": "..."}` — Litestar default
   `{"detail":...}` shapes (validation, 404, 405, 500) eliminated; malformed-body message
   parity with actix serde messages where fixtures pin them.
7. Content-type gate: only JSON / form-urlencoded / multipart; else 400
   `{"error":"Unsupported content-type"}`.
8. `/detect`: add api-key (403) + char-limit (400) checks (Rust `check_params` parity).
9. Char limit counts **UTF-8 bytes** (`len(q.encode())`), error message uses the same
   numbers as Rust.
10. `/frontend/settings`: `apiKeys: false`, `keyRequired: false` **hardcoded** (quirk kept).
11. Identity path (`source == target`) runs `improve_formatting(q, q)` (trim parity).
12. `alternatives` garbage input → 400 (serde-parity message), never 500.
13. Unknown paths → 404 (no index.html fallback for non-API paths that don't resolve to
    real static files); static files still served.
14. `/metrics` prometheus endpoint (prefix `ltengine`), presence-tested.

**Verify-only (believed correct; property/differential tests confirm)**
prompts (verbatim), language table + aliases + self-including targets, model resolution
(`data[0].id`, startup-fatal on failure), 501 stubs, `improve_formatting` edge semantics
(`islower`/`isupper` on uncased strings vs Rust behavior).

## 6. Detection — whatlang parity

Required behavior: whatlang `0.18` (same version as the Rust `Cargo.toml`) with allowlist
restricted to the 49 languages, Mandarin→Chinese remap, confidence
`int(conf * 100)` truncated, and **English / confidence 0 on any failure or unmappable
result** — never an error. Route: (a) evaluate an existing PyPI whatlang binding if one
exists and pins v0.18 semantics; (b) else vendor a ~30-line PyO3/maturin crate in this
repo wrapping `whatlang 0.18` (wheels built locally now, CI wheels later); (c) if both
fail → **escalate before compromising** (lingua stays behind a non-default flag only).
Differential `/detect` fixtures (~30 multilingual texts incl. short/ambiguous strings)
must match Rust exactly on code AND confidence.

## 7. Benchmarks

**B1 — wrapper overhead (the performance gate).** Both servers → same zero-delay mock
upstream; one shared asyncio+httpx bencher; C ∈ {1, 8, 32}; ≥ 2000 requests/arm;
p50/p99 + RPS. **Acceptance (amended 2026-08-12, user decision): Python p50 ≤ Rust p50
+ 3 ms at C=1 (the queue-free per-request wrapper cost); RPS ratio py/rust ≥ 0.70 at
C=32 taken as the median of ≥ 5 runs.** Rationale: actix will win raw RPS; per-worker
production concurrency is ≤ ~32 at multi-second GPU latencies, so sub-ms per-request
deltas are invisible — the gate proves "irrelevant at production load", not "beats Rust".
> Amendment note: the original absolute p50-overhead bar at C=32 was measured unphysical
> during implementation — the in-process mock harness saturates (~642 rps, 35 ms p50
> empty) at C=32, the direct-mock floor term cancels out of the gate algebraically, and
> the 3 ms bar demands a 94 µs/request differential, below the instrument's own ~4 ms
> run-to-run noise. Measured truth retained in `bench/REPORT.md`: C=1 delta ≈ 0.8 ms/req.

**B2 — e2e sanity (real vLLM).** Same 20 texts, C=4, both servers via
`smartech-ltengine-vllm`: end-to-end latency parity within noise (model dominates).
Dev-box GPU contention (incl. IDE/agent processes) noted as a caveat; affects both arms
equally; absolute numbers not gated.

Report committed to `bench/REPORT.md` with environment notes and raw numbers.

## 8. Out of scope

Local mode (llama.cpp) — LTEngine remains the offline engine; batching / array `q`;
Medusa integration (P3) beyond the `create_router` mount-point shape; CI beyond a minimal
lint+unit workflow; frontend changes; performance work beyond the B1 thresholds.

## 9. Risks

| Risk | Mitigation |
|---|---|
| whatlang binding unavailable/stale on PyPI | Vendored PyO3 crate route (b); escalation gate (c) before any behavior compromise |
| actix serde error-message parity is fiddly | Fixtures pin only messages clients can hit; if a message proves runtime-version-dependent on the Rust side, pin the Rust build in the harness (reference is `development` HEAD, recorded in the suite) |
| Heartbeat/chunked behavior differs across ASGI servers | Streaming tests run under the same server the package ships with (uvicorn/granian pinned); behavior asserted at the HTTP byte level |
| Windows/Linux differences in the harness | Suite runs on the Windows dev box now; Linux parity checked when CI lands |
