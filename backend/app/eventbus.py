"""Optional Cloud Pub/Sub event publishing; disabled when no topic is configured."""
from __future__ import annotations

import json
import os


def publish_audit_event(event: dict) -> str | None:
    project = os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GOOGLE_CLOUD_PROJECT_ID")
    topic = os.getenv("PUBSUB_AUDIT_TOPIC")
    if not topic:
        return None
    if not project:
        raise RuntimeError("PUBSUB_AUDIT_TOPIC is set but GOOGLE_CLOUD_PROJECT is missing.")
    from google.cloud import pubsub_v1
    client = pubsub_v1.PublisherClient()
    topic_path = topic if topic.startswith("projects/") else client.topic_path(project, topic)
    payload = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return client.publish(topic_path, payload, event_type="machine_passport.audit.v1").result(
        timeout=float(os.getenv("PUBSUB_PUBLISH_TIMEOUT_SECONDS", "10"))
    )
