# Repository audit

Audit date: 2026-10-18 (Asia/Calcutta)

## Scope and commands

- Read the tracked-looking repository files under `backend/`, `frontend/`, `eval/`, `scripts/`, `docs/`, plus the root build/deployment files. The workspace has no `.git` directory, so tracked/untracked status could not be distinguished.
- `python -m pytest backend/tests -q`: **9 passed**.
- `python eval/run_eval.py`: succeeded on `mimii_fan_id_06` (SEEDED public teaching subset).
- No linter configuration or linter command is present. `node --check frontend/app.js` passed in the previous work session; no browser automation or viewport QA is present.
- `make eval` was attempted previously but `make` was not installed on Windows and WSL returned Access Denied. The Makefile target's Python command was run directly.
- Current eval result: AUC 0.9925; TPR at 5% FPR 1.0000; threshold confusion matrix TN 20 / FP 0 / FN 10 / TP 10. This is a single deterministic split on one SEEDED dataset subset, not a field result.

## What works

- FastAPI stores machine records, audio clip features, baselines, telemetry, knowledge notes, recommendations, decisions, outcomes, events, and an append-only hash-linked audit log in SQLite.
- WAV ingestion accepts PCM16, converts multichannel to mono, computes DSP features, enforces a 5–60 second gate, and checks low RMS/clipping.
- A baseline requires at least eight healthy clips. It computes robust feature medians/MADs and leave-one-out healthy scores; the alert threshold is the 95th percentile with a minimum of 1.0.
- Recommendations can use stored clip/outcome/confirmed-note evidence. Empty evidence returns a low-confidence “Insufficient evidence” template. Recommendation output is Pydantic-shaped and the grounding validator removes unknown IDs and detects unsupported numeric claims.
- Text notes are searchable with local hashed-term vectors and IDF-weighted cosine similarity. Audio notes are retained, can be sent to Gemini for structuring, and are held out of recommendation retrieval until a technician confirms them.
- Offline shell caching and a seeded-data command exist. The app currently has home, machine, and metrics views.
- Existing tests cover the machine flow, basic DSP gates, lexical note retrieval, pending audio-note review, prompt-injection resistance of the deterministic mock, empty retrieval, fabricated evidence IDs/numbers, and audit payload tampering.

## Mocked, fallback, and unverified behavior

- `LLM_PROVIDER` defaults to `mock`, even if `GEMINI_API_KEY` is set. The mock is a deterministic template, not model reasoning. The current UI exposes recommendation `mode` text but does not consistently show a strong provider badge on every recommendation or voice note.
- Gemini recommendation and voice-note calls are implemented via `google-genai`, but no live call was run: no API key or Vertex configuration was available. There is no live pytest suite or verification script.
- Gemini structured output is validated with Pydantic and retried once. Raw model responses are not persisted; only parsed fields are stored.
- Embeddings are not implemented. Note retrieval is lexical hashed TF-IDF-style matching, including for audio-derived English text.
- On provider construction/call/grounding failure, recommendation generation returns a deterministic evidence template with fallback metadata. This is fail-closed in content, but the template can be visually mistaken for AI unless the UI labels it clearly.
- Audio structuring unavailable/configuration failure stores the recording and returns `awaiting_gemini_configuration`; frontend feedback is a transient toast. No durable provider badge or raw response exists.
- Seed script creates one scenario only when manually invoked; there is no idempotent startup-seeded demo machine, reset action, guided demo, or one-click walkthrough.
- Cloud Run is public now (the operator link was fixed on 2026-10-17), but deployment settings do not configure rate limits, daily Gemini caps, min instances, CPU/memory/concurrency, Secret Manager, cleanup, or durable storage. No live smoke-test script exists. Instance-local SQLite/audio is ephemeral.
- Ollama is a best-effort recommendation provider only. Accelerometer capture, Pub/Sub, translation beyond the Gemini voice-note structured response, durable upload queue, Cloud Storage/BigQuery/Firestore adapters, feedback collection/export, per-user deletion, and scheduled cleanup are absent.

## Known bugs and risks found

- `README.md` says the runtime threshold is leave-one-out P95 * 1.25. Current runtime and eval use P95 without the margin. This is stale and must be corrected.
- Eval is one fixed split with no repeated stratified trials, confidence intervals, per-machine fan/pump comparison, train-only threshold proof test, or user-vs-benchmark separation.
- `eval/run_eval.py` treats every folder without `source.json` as SEEDED. User recordings therefore need an explicit provenance mechanism before a report can classify them honestly.
- Clip upload reads the upload once for analysis, then writes a normalized PCM WAV; it discards the original source bytes. Microphone constraints/settings and original sample rate are not recorded separately.
- DSP is computed on raw amplitude, so phone gain changes RMS, band energies, crest factor scaling and anomaly scores. There is no DC-offset or background-noise gate; accepted sample rates are not resampled to a common analysis rate.
- Current user audio recording requests `getUserMedia({audio:true})` with browser defaults; the actual applied settings are neither requested nor stored.
- Public app has no authentication, request/IP throttling, upload byte cap for machine clips, daily Gemini cap, or data deletion control. Voice-note endpoint does cap size at 20 MB; machine clip endpoint reads unbounded request content before validation.
- `/api/health` exists but `/healthz` does not. There is no structured startup logging or explicit secret-presence-only report.
- Machine records are process-global/local SQLite and audio paths. Cloud Run filesystem loss and multiple-instance SQLite divergence make deployed persistence unreliable.
- Feedback/admin export endpoints and the required technician pilot/demo/final report docs do not exist.
- Accessibility/mobile behavior and browser console have not been verified at 360×640 or 390×844.

## Claims-to-evidence table

| README/product claim | Evidence in repository | Audit status |
|---|---|---|
| Baseline needs 8 healthy clips; score uses robust median/MAD features | `backend/app/main.py`; `backend/tests/test_api.py`, `backend/tests/test_dsp.py` | Backed for current implementation |
| Threshold is leave-one-out P95 * 1.25 | README line 13 vs runtime `build_baseline` and `eval/run_eval.py` | **Contradicted/stale**: implementation uses P95 without 1.25 |
| Public fan subset AUC 0.9925 and threshold confusion 20/0/10/10 | `eval/results/latest.json`, `eval/results/latest.md` | Reproducible for one seeded split only; no field claim |
| Gemini transcribes/translates/structures voice notes | `backend/app/providers.py`, audio routes in `backend/app/main.py` | Code path exists; no live Gemini verification |
| Matching past notes are retrieved | `retrieve_knowledge`, search route, `test_memory_integrity.py` | Backed for lexical matching examples; not semantic embedding retrieval |
| Recommendations are grounded in stored evidence | `validate_grounding`, recommendation route, `test_providers.py`, `test_memory_integrity.py` | Partial: evidence IDs and numeric claims checked; no live Gemini output test and not a general semantic entailment proof |
| Audit chain detects tampering | `audit_verify`, `test_memory_integrity.py` | Backed for payload tampering case |
| App works offline | `frontend/sw.js`, README | Only app-shell GET caching is implemented; uploads and data operations require network |
| Cloud Run deployment is available | `scripts/deploy_cloud_run.ps1`, Dockerfile, live URL | Service responds publicly; durability, limits, live end-to-end smoke, and secret wiring are unverified |
| No seeded data created by default | README | True for current app startup; conflicts with requested judge-ready seeded demo which is not implemented |
| Product differentiation from named vendors | README related-work paragraph | Framed as scope description; no comparative product evidence |
| Pitch deck claims | No pitch deck found in repository file inventory | Not applicable / none to audit |

## Tools and limitations

No pytest plugins for browser/live Gemini, no lint config, and no local Cloud Run emulator are configured. Real machine recordings, a Gemini credential, mobile browser access, and a second MIMII machine subset were not available during this audit.
