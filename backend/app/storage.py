"""Local-first audio and SQLite snapshot adapter with optional private GCS bucket."""
from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path
from urllib.parse import unquote


def bucket_name() -> str | None:
    return os.getenv("GCS_BUCKET") or None


def _bucket():
    name = bucket_name()
    if not name:
        return None
    from google.cloud import storage
    return storage.Client().bucket(name)


def store_audio(name: str, data: bytes, content_type: str) -> str:
    bucket = _bucket()
    if bucket is None:
        from .main import AUDIO
        path = AUDIO / Path(name).name
        path.write_bytes(data)
        return str(path)
    object_name = "audio/" + Path(name).name
    bucket.blob(object_name).upload_from_string(data, content_type=content_type)
    return f"gs://{bucket.name}/{object_name}"


def read_audio(location: str) -> bytes:
    if location.startswith("gs://"):
        _, _, remainder = location.partition("gs://")
        name, _, object_name = remainder.partition("/")
        if not name or not object_name:
            raise ValueError("Invalid GCS audio URI.")
        from google.cloud import storage
        return storage.Client().bucket(name).blob(unquote(object_name)).download_as_bytes()
    return Path(location).read_bytes()


def delete_audio(location: str) -> None:
    if location.startswith("gs://"):
        remainder = location[5:]
        name, _, object_name = remainder.partition("/")
        from google.cloud import storage
        storage.Client().bucket(name).blob(unquote(object_name)).delete()
    else:
        path = Path(location)
        if path.is_file():
            path.unlink()


def restore_db_snapshot(db_path: Path) -> bool:
    """Restore the most recent committed backup only when the local DB is absent."""
    bucket = _bucket()
    if bucket is None or db_path.exists():
        return False
    blob = bucket.blob("database/machine_passport.sqlite3")
    if not blob.exists():
        return False
    db_path.parent.mkdir(parents=True, exist_ok=True)
    blob.download_to_filename(str(db_path))
    return True


def persist_db_snapshot(db_path: Path) -> None:
    bucket = _bucket()
    if bucket is None:
        return
    # SQLite's online backup API captures a consistent committed view.
    with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as temp:
        temp_path = Path(temp.name)
    try:
        source = sqlite3.connect(db_path)
        target = sqlite3.connect(temp_path)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        bucket.blob("database/machine_passport.sqlite3").upload_from_filename(
            str(temp_path), content_type="application/vnd.sqlite3"
        )
    finally:
        temp_path.unlink(missing_ok=True)
