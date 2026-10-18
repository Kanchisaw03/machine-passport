"""Pull telemetry messages from Cloud Pub/Sub and ingest them into Machine Passport."""
from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.app.main import TelemetryBatch, ingest_telemetry  # noqa: E402

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))


def process_message(data: bytes, ack, nack, message_id: str | None = None) -> bool:
    """Validate and ingest one versioned message; invalid messages are nacked."""
    try:
        body = json.loads(data)
        if body.get("schema") != "machine_passport.telemetry.v1":
            raise ValueError("unsupported schema")
        batch = TelemetryBatch.model_validate({"machine_id": body["machine_id"], "rows": body["rows"]})
        result = ingest_telemetry(batch, message_id=message_id)
        logging.info("telemetry_ingested rows=%s machine_id=%s duplicate=%s", result["inserted"], batch.machine_id, result.get("duplicate",False))
        ack()
        return True
    except Exception as exc:
        logging.warning("telemetry_message_rejected error=%s", type(exc).__name__)
        nack()
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subscription", default=os.getenv("PUBSUB_SUBSCRIPTION"))
    parser.add_argument("--project", default=os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GOOGLE_CLOUD_PROJECT_ID"))
    args = parser.parse_args()
    if not args.project or not args.subscription:
        parser.error("Set GOOGLE_CLOUD_PROJECT and PUBSUB_SUBSCRIPTION, or pass both arguments.")
    from google.cloud import pubsub_v1
    subscriber = pubsub_v1.SubscriberClient()
    path = args.subscription if args.subscription.startswith("projects/") else subscriber.subscription_path(args.project, args.subscription)
    future = subscriber.subscribe(path, callback=lambda message: process_message(message.data, message.ack, message.nack, message.message_id))
    logging.info("pubsub_subscriber_started subscription=%s", path)
    try:
        future.result()
    except KeyboardInterrupt:
        future.cancel()
        future.result()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
