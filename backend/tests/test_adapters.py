import json
import sqlite3
import sys
import tempfile
import types
from pathlib import Path

from backend.app import eventbus, storage


def test_local_audio_adapter_round_trip(monkeypatch):
    from backend.app import main
    with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2]) as temp:
        monkeypatch.delenv("GCS_BUCKET", raising=False)
        monkeypatch.setattr(main, "AUDIO", Path(temp))
        uri = storage.store_audio("clip.wav", b"wav bytes", "audio/wav")
        assert Path(uri).read_bytes() == b"wav bytes"
        assert storage.read_audio(uri) == b"wav bytes"
        storage.delete_audio(uri)
        assert not Path(uri).exists()


def test_gcs_audio_and_sqlite_snapshot_restore(monkeypatch):
    from backend.app import main
    objects = {}

    class Blob:
        def __init__(self, name): self.name = name
        def upload_from_string(self, data, content_type=None): objects[("test-bucket", self.name)] = bytes(data)
        def upload_from_filename(self, path, content_type=None): objects[("test-bucket", self.name)] = Path(path).read_bytes()
        def download_as_bytes(self): return objects[("test-bucket", self.name)]
        def download_to_filename(self, path): Path(path).write_bytes(objects[("test-bucket", self.name)])
        def exists(self): return ("test-bucket", self.name) in objects
        def delete(self): objects.pop(("test-bucket", self.name), None)

    class Bucket:
        name = "test-bucket"
        def blob(self, name): return Blob(name)

    class Client:
        def bucket(self, name): assert name == "test-bucket"; return Bucket()

    fake_storage = types.ModuleType("google.cloud.storage")
    fake_storage.Client = Client
    fake_cloud = types.ModuleType("google.cloud")
    fake_cloud.storage = fake_storage
    monkeypatch.setitem(sys.modules, "google.cloud", fake_cloud)
    monkeypatch.setitem(sys.modules, "google.cloud.storage", fake_storage)
    monkeypatch.setenv("GCS_BUCKET", "test-bucket")

    uri = storage.store_audio("note.ogg", b"audio bytes", "audio/ogg")
    assert uri == "gs://test-bucket/audio/note.ogg"
    assert storage.read_audio(uri) == b"audio bytes"

    with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2]) as temp:
        db = Path(temp) / "data.sqlite3"
        conn = sqlite3.connect(db)
        try:
            conn.execute("CREATE TABLE sample (value TEXT)")
            conn.execute("INSERT INTO sample VALUES ('persisted')")
            conn.commit()
        finally:
            conn.close()
        storage.persist_db_snapshot(db)
        db.unlink()
        assert storage.restore_db_snapshot(db) is True
        conn = sqlite3.connect(db)
        try:
            assert conn.execute("SELECT value FROM sample").fetchone()[0] == "persisted"
        finally:
            conn.close()
    storage.delete_audio(uri)
    assert ("test-bucket", "audio/note.ogg") not in objects


def test_pubsub_audit_publisher_is_optional_and_serialized(monkeypatch):
    monkeypatch.delenv("PUBSUB_AUDIT_TOPIC", raising=False)
    assert eventbus.publish_audit_event({"action": "ok"}) is None
    calls = {}

    class Future:
        def result(self, timeout): calls["timeout"] = timeout; return "message-123"
    class Publisher:
        def topic_path(self, project, topic): return f"projects/{project}/topics/{topic}"
        def publish(self, topic, data, **attrs): calls.update(topic=topic, data=json.loads(data), attrs=attrs); return Future()
    fake_pubsub = types.ModuleType("google.cloud.pubsub_v1")
    fake_pubsub.PublisherClient = Publisher
    fake_cloud = types.ModuleType("google.cloud")
    fake_cloud.pubsub_v1 = fake_pubsub
    monkeypatch.setitem(sys.modules, "google.cloud", fake_cloud)
    monkeypatch.setitem(sys.modules, "google.cloud.pubsub_v1", fake_pubsub)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    monkeypatch.setenv("PUBSUB_AUDIT_TOPIC", "passport-audit")
    result = eventbus.publish_audit_event({"action": "ok"})
    assert result == "message-123"
    assert calls["topic"] == "projects/test-project/topics/passport-audit"
    assert calls["data"] == {"action": "ok"}
    assert calls["attrs"]["event_type"] == "machine_passport.audit.v1"


def test_pubsub_telemetry_ingestion_is_idempotent_by_message_id(monkeypatch):
    from backend.app import main
    monkeypatch.delenv("GCS_BUCKET", raising=False)
    monkeypatch.delenv("PUBSUB_AUDIT_TOPIC", raising=False)
    with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2]) as temp:
        root = Path(temp)
        monkeypatch.setattr(main, "DB", root / "db.sqlite3")
        monkeypatch.setattr(main, "AUDIO", root / "audio")
        main.AUDIO.mkdir()
        main.init_db()
        machine_id = main.create_machine(main.MachineIn(name="PubSub idempotency test"))["id"]
        batch = main.TelemetryBatch(machine_id=machine_id, rows=[{"metric":"rpm","value":1400,"unit":"rpm","provenance":"REAL"}])
        first = main.ingest_telemetry(batch, message_id="message-once")
        duplicate = main.ingest_telemetry(batch, message_id="message-once")
        with main.connect() as conn:
            count = conn.execute("SELECT COUNT(*) FROM telemetry WHERE machine_id=?", (machine_id,)).fetchone()[0]
        assert first["inserted"] == 1
        assert duplicate["duplicate"] is True
        assert count == 1
