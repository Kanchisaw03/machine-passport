"""Daily Gemini call budget; Cloud Run uses a shared Firestore transaction."""
from __future__ import annotations
import os, sqlite3
from datetime import datetime, timezone
from pathlib import Path

class DailyGeminiCapReached(RuntimeError): pass
class QuotaBackendUnavailable(RuntimeError): pass

def consume_gemini_call()->dict:
    cap=max(0,int(os.getenv("GEMINI_DAILY_CALL_CAP","60")))
    if cap==0: raise DailyGeminiCapReached("The daily Gemini call limit is disabled.")
    day=datetime.now(timezone.utc).date().isoformat()
    backend=os.getenv("GEMINI_QUOTA_BACKEND","firestore" if os.getenv("K_SERVICE") else "local").lower()
    if backend=="firestore":
        project=os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GOOGLE_CLOUD_PROJECT_ID")
        if not project: raise QuotaBackendUnavailable("Shared daily quota requires a Google Cloud project.")
        try:
            from google.cloud import firestore
            client=firestore.Client(project=project)
            reference=client.collection("machine_passport_quota").document("gemini-"+day)
            transaction=client.transaction()
            @firestore.transactional
            def increment(tx):
                snapshot=reference.get(transaction=tx); count=int(snapshot.get("count") or 0) if snapshot.exists else 0
                if count>=cap: raise DailyGeminiCapReached("The shared daily Gemini call limit has been reached. Try again tomorrow.")
                tx.set(reference,{"count":count+1,"day":day,"limit":cap,"updated_at":datetime.now(timezone.utc).isoformat()})
                return count+1
            return {"backend":"firestore","day":day,"count":increment(transaction),"limit":cap}
        except DailyGeminiCapReached: raise
        except Exception as exc: raise QuotaBackendUnavailable("Shared Gemini quota storage is unavailable; Gemini calls are paused to protect the daily cap.") from exc
    if backend!="local": raise QuotaBackendUnavailable("GEMINI_QUOTA_BACKEND must be firestore or local.")
    data=Path(os.getenv("DATA_DIR","data")); data.mkdir(parents=True,exist_ok=True); db=data/"gemini_quota.sqlite3"
    try:
        conn=sqlite3.connect(db,timeout=5,isolation_level="IMMEDIATE")
        try:
            with conn:
                conn.execute("CREATE TABLE IF NOT EXISTS daily_usage(day TEXT PRIMARY KEY,count INTEGER NOT NULL)")
                row=conn.execute("SELECT count FROM daily_usage WHERE day=?",(day,)).fetchone(); count=int(row[0]) if row else 0
                if count>=cap: raise DailyGeminiCapReached("The daily Gemini call limit has been reached. Try again tomorrow.")
                conn.execute("INSERT INTO daily_usage(day,count) VALUES(?,?) ON CONFLICT(day) DO UPDATE SET count=excluded.count",(day,count+1))
                return {"backend":"local","day":day,"count":count+1,"limit":cap}
        finally: conn.close()
    except DailyGeminiCapReached: raise
    except Exception as exc: raise QuotaBackendUnavailable("Local Gemini quota storage is unavailable; Gemini calls are paused.") from exc
