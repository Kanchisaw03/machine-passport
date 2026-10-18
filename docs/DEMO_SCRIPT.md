# Three-minute guided demo

Use the public Cloud Run URL or a local instance. The seeded machine and included fan clips are labeled SEEDED; do not describe them as recordings of the viewer's machine. The seeded machine is created idempotently at startup.

## Exact clicks and talking points

1. Open **Guided demo** from the home page. Point out the SEEDED label and the machine-specific baseline. The demo needs no account or typing.
2. Click **Run the whole loop**. Progress is shown as the app submits a healthy sample, then a faulty sample through the normal upload and analysis routes.
3. Show the expert note step. The sample notes are seeded evidence. In a Gemini-enabled deployment, voice notes show the Gemini provider and must be reviewed before they can support a recommendation. Without credentials the badge says `MOCK · TEMPLATE, not AI reasoning`.
4. Show the recommendation, evidence list, provider label, and audit verification. Explain that evidence is limited to the stored machine data and that this is a screening demonstration, not a certified diagnosis.
5. Click **Reset demo** to restore the repeatable starting point. Ask the viewer what would make the recommendation useful to their maintenance workflow; do not record names or identifying details.

## If the live demo fails

Refresh once and check `/healthz`. If the service is unavailable, use the local app and the same Guided demo flow. If neither is available, show the included SEEDED clips, the computed report in `eval/results/latest.md`, and this script. State clearly that this is a fallback and do not claim the live deployment passed. Live Gemini is optional for this walkthrough; show the provider badge and explain whether the result is Gemini or the template fallback.
