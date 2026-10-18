# Final QA report

Run on 2026-10-18 against the repository state in this workspace. Cloud Run revision `machine-passport-00008-vrv` serves 100% of traffic at the user's URL. The Gemini Developer API key is configured through Secret Manager, not stored in the repository or image. This report distinguishes code checks from real-world validation.

| Item | Status | Evidence |
|---|---|---|
| Backend test suite | PASS | `python -m pytest backend/tests -q`: 28 passed, 1 live test skipped by default. |
| Frontend JavaScript syntax | PASS | `node --check frontend/app.js` exited 0. |
| Train-only threshold rule | PASS | `backend/tests/test_eval_protocol.py`; 30 repeated splits in `eval/results/latest.md`; training threshold calculation receives healthy baseline clips only. |
| Public fan benchmark separation | FAIL / weak evidence | MIMII `fan_id_06`, 50 healthy and 20 faulty, SEEDED: ROC AUC 0.792 ± 0.050 (bootstrap mean 95% CI 0.774–0.810); TPR at calibrated threshold 0.127 ± 0.034 (CI 0.113–0.138); FPR 0.075 ± 0.065 (CI 0.052–0.098). The conservative operating point misses most faulty clips; this is not a reliable fault detector. |
| Pump benchmark | NOT VERIFIED | No pump clips were supplied. |
| Own-machine benchmark / real physical recording | NOT VERIFIED | No eligible REAL clips are present; current evaluation explicitly reports “No own-machine evaluation yet.” |
| Gemini audio, embedding and grounded recommendation live loop | PASS (live API, standalone verifier) | `python scripts/verify_gemini.py`: three synthetic English voice notes structured with translation fields, confirmed note retrieved with Gemini embeddings, and recommendation passed schema plus evidence-ID/numeric grounding checks. No multilingual fixture was available. A later repeat through `pytest -m live` hit Gemini free-tier HTTP 429 quota after the daily request allowance was exhausted. |
| Injection, fabricated evidence, empty retrieval and audit-tampering safeguards | PASS (live + unit) | Live Gemini prompt-injection response stayed cautious and grounded; fabricated evidence ID was rejected. `backend/tests/test_providers.py` and `backend/tests/test_memory_integrity.py` cover injection-as-data, empty retrieval as “insufficient evidence,” and audit-chain tampering. |
| Guided demo smoke test on public URL | PASS | `python scripts/smoke_test.py https://machine-passport-401398332897.asia-south1.run.app`: anonymous health (`/api/healthz`), SEEDED baseline, sample analysis, TEMPLATE recommendation, and audit verify all PASS. |
| Public homepage | PASS | Opened the reported URL; Machines page and Guided demo link rendered without login. |
| Mobile layout and browser console | PASS | Guided demo checked at 360×640 and 390×844: no horizontal overflow, tap controls are 42 px high, and no console errors were captured. |
| Request/upload guardrails and owner deletion | PASS (code/test evidence) | `backend/app/main.py`, `backend/app/quotas.py`, `scripts/deploy_cloud_run.ps1`; local tests cover request limits/feedback/retrieval. IP limiting is per instance; deployment caps the service at one instance. |
| Shared Gemini daily quota on Cloud Run | NOT VERIFIED | The live verifier used local quota storage. Firestore quota path still requires Firestore setup and runtime IAM validation. |
| Cloud Run data durability | NOT IMPLEMENTED | SQLite and uploaded audio are on ephemeral container storage. Cloud Storage persistence adapter is not present. |
| Cross-device WAV comparison | NOT VERIFIED | `scripts/compare_device_wavs.py` was syntax/CLI checked, but no two recordings of the same source from different devices were supplied. |
| Accelerometer capture path | NOT VERIFIED on physical hardware | A phone browser DeviceMotion flow now captures timestamped axes as REAL telemetry after permission. No physical phone sensor run was available. |
| Pub/Sub adapter | PASS (unit/mock evidence); live NOT VERIFIED | Audit event publishing and a versioned telemetry pull consumer are implemented; telemetry ingestion deduplicates message IDs transactionally. Adapter tests pass. No live topic/subscription or IAM was available. |
| Ollama provider | PASS (mocked API evidence); live NOT VERIFIED | Schema-constrained generation and raw-output capture are implemented and tested; no running Ollama endpoint/model was available. |
| GCS storage adapter | PASS (unit/mock evidence); live NOT VERIFIED | Private audio object writes/reads/deletes and SQLite snapshot restore/backup are implemented and fake-client round-trip tested. No live bucket or IAM was available. Without `GCS_BUCKET`, the deployment remains ephemeral. |
| Translation | PARTIALLY VERIFIED live | Gemini returned transcript, detected-language, and English-translation fields for synthetic English speech; multilingual translation quality is not verified. |
| Feedback pilot tooling | PASS (code/tests) | Feedback/export test passes; no real user feedback has been submitted or claimed. |

## What the human must still do

- Record at least eight healthy clips and repeatable real fault clips from the target machine, at a fixed placement. Review the resulting metrics before relying on alerts; the current public fan result is weak at its calibrated threshold.
- Rotate the Gemini key in Google AI Studio because it was shared in chat; update Secret Manager after rotation. The service currently reads `machine-passport-gemini-api-key`.
- Supply pump data if pump evaluation is required, and supply same-source WAVs from two devices to run `scripts/compare_device_wavs.py`.
- Show the deployed walkthrough to one real technician or maintenance person and record only their actual feedback in `docs/PILOT_SHEET.md`.
- Record the requested demo video and label all included public benchmark examples SEEDED.
- Configure Firestore and grant the Cloud Run runtime identity access before depending on the shared daily Gemini cap. Use durable storage before treating cloud uploads or records as persistent.

## Scope items still outside this implementation

Live sensor capture, live Pub/Sub configuration, live GCS configuration, live Ollama inference, and live multilingual Gemini structuring remain unverified. The local/configurable integration code exists; the Cloud Run deployment was not configured with a bucket, Pub/Sub topic, or Ollama service. Gemini is configured on Cloud Run through Secret Manager.

Cloud Run reserves/intercepts bare `/healthz` on the generated public hostname and returns its own 404 before forwarding the request to FastAPI. The app's `/api/healthz` route is reachable and is the path used by the passing smoke test.
