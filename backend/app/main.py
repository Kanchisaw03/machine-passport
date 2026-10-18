from __future__ import annotations

import asyncio, csv, hashlib, hmac, io, json, logging, os, secrets, sqlite3, threading, wave
import re
import time
from collections import Counter, deque
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import random
from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, Response, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from scipy import signal, stats
from .storage import delete_audio, persist_db_snapshot, read_audio, restore_db_snapshot, store_audio
from .eventbus import publish_audit_event

ROOT = Path(__file__).resolve().parents[2]
DATA = Path(os.getenv("DATA_DIR", ROOT / "data")); DATA.mkdir(parents=True, exist_ok=True)
DB = DATA / "machine_passport.sqlite3"
AUDIO = DATA / "audio"; AUDIO.mkdir(exist_ok=True)
restore_db_snapshot(DB)
logger=logging.getLogger("machine_passport")
rate_lock=threading.Lock(); rate_windows={}

@asynccontextmanager
async def lifespan(app):
    startup_seed_demo()
    cleanup_old_nonseeded()
    key_present=bool(os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_GENAI_USE_VERTEXAI","false").lower()=="true")
    logger.info(json.dumps({"event":"startup","service":"machine-passport","gemini_key_present":key_present,"gemini_provider":__import__("backend.app.providers",fromlist=["configured_provider_name"]).configured_provider_name(),"quota_backend":os.getenv("GEMINI_QUOTA_BACKEND","firestore" if os.getenv("K_SERVICE") else "local")}))
    cleanup_task=asyncio.create_task(periodic_cleanup())
    try: yield
    finally:
        cleanup_task.cancel()
        try: await cleanup_task
        except asyncio.CancelledError: pass

app = FastAPI(title="Machine Passport", version="0.1.0",lifespan=lifespan)

def rate_limited(ip:str,tick:float|None=None)->bool:
    limit=max(1,int(os.getenv("REQUESTS_PER_IP_PER_MINUTE","60"))); tick=time.monotonic() if tick is None else tick
    with rate_lock:
        queue=rate_windows.setdefault(ip,deque())
        while queue and tick-queue[0]>=60: queue.popleft()
        if len(queue)>=limit: return True
        queue.append(tick)
        if len(rate_windows)>5000:
            stale=[key for key,value in rate_windows.items() if not value or tick-value[-1]>=60]
            for key in stale: rate_windows.pop(key,None)
    return False

@app.middleware("http")
async def request_guardrails(request:Request,call_next):
    started=time.monotonic(); path=request.url.path
    if path.startswith("/api/") and request.method not in ("OPTIONS",):
        forwarded=request.headers.get("x-forwarded-for","").split(",",1)[0].strip()
        ip=forwarded or (request.client.host if request.client else "unknown")
        if rate_limited(ip): return JSONResponse(status_code=429,content={"detail":"Too many requests. Please wait a minute and try again."},headers={"Retry-After":"60"})
    response=await call_next(request)
    logger.info(json.dumps({"event":"http_request","method":request.method,"path":path,"status_code":response.status_code,"duration_ms":round((time.monotonic()-started)*1000,2)}))
    return response

def now() -> str: return datetime.now(timezone.utc).isoformat()
@contextmanager
def connect():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback(); raise
    finally:
        c.close()
def init_db():
    with connect() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS machines(id INTEGER PRIMARY KEY, name TEXT NOT NULL, type TEXT, location TEXT, placement_note TEXT, created_at TEXT, demo_key TEXT UNIQUE, owner_token_hash TEXT);
        CREATE TABLE IF NOT EXISTS clips(id INTEGER PRIMARY KEY, machine_id INTEGER, kind TEXT, path TEXT, duration REAL, sample_rate INTEGER, quality TEXT, features TEXT, label TEXT, provenance TEXT, created_at TEXT, recorder_settings TEXT);
        CREATE TABLE IF NOT EXISTS baselines(id INTEGER PRIMARY KEY, machine_id INTEGER, version INTEGER, n_clips INTEGER, stats TEXT, threshold REAL, quality TEXT, created_at TEXT);
        CREATE TABLE IF NOT EXISTS telemetry(id INTEGER PRIMARY KEY, machine_id INTEGER, ts TEXT, metric TEXT, value REAL, unit TEXT, provenance TEXT);
        CREATE TABLE IF NOT EXISTS knowledge(id INTEGER PRIMARY KEY, machine_id INTEGER, technician TEXT, transcript TEXT, structured TEXT, provenance TEXT, created_at TEXT, clip_id INTEGER, audio_path TEXT, audio_mime TEXT, original_language TEXT, english_text TEXT, embedding TEXT, human_confirmed INTEGER DEFAULT 1, provider TEXT DEFAULT 'USER', raw_model_output TEXT);
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, machine_id INTEGER, ts TEXT, type TEXT, payload TEXT);
        CREATE TABLE IF NOT EXISTS recommendations(id INTEGER PRIMARY KEY, machine_id INTEGER, status TEXT, content TEXT, evidence TEXT, validation TEXT, created_at TEXT, decided_by TEXT, decided_at TEXT, provider TEXT DEFAULT 'TEMPLATE', raw_model_output TEXT);
        CREATE TABLE IF NOT EXISTS outcomes(id INTEGER PRIMARY KEY, recommendation_id INTEGER, actual_cause TEXT, action_taken TEXT, notes TEXT, created_at TEXT);
        CREATE TABLE IF NOT EXISTS feedback(id INTEGER PRIMARY KEY, machine_id INTEGER, recommendation_id INTEGER, useful INTEGER NOT NULL, suggestion TEXT NOT NULL, role TEXT, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS pubsub_inbox(message_id TEXT PRIMARY KEY, received_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS audit_log(id INTEGER PRIMARY KEY, ts TEXT, actor TEXT, action TEXT, payload TEXT, prev_hash TEXT, hash TEXT);
        """)
        columns={r[1] for r in c.execute("PRAGMA table_info(knowledge)").fetchall()}
        for name,kind in (("clip_id","INTEGER"),("audio_path","TEXT"),("audio_mime","TEXT"),("original_language","TEXT"),("english_text","TEXT"),("embedding","TEXT"),("human_confirmed","INTEGER NOT NULL DEFAULT 1"),("provider","TEXT NOT NULL DEFAULT 'USER'"),("raw_model_output","TEXT")):
            if name not in columns: c.execute(f"ALTER TABLE knowledge ADD COLUMN {name} {kind}")
        columns={r[1] for r in c.execute("PRAGMA table_info(recommendations)").fetchall()}
        for name,kind in (("provider","TEXT NOT NULL DEFAULT 'TEMPLATE'"),("raw_model_output","TEXT")):
            if name not in columns: c.execute(f"ALTER TABLE recommendations ADD COLUMN {name} {kind}")
        columns={r[1] for r in c.execute("PRAGMA table_info(machines)").fetchall()}
        if "demo_key" not in columns: c.execute("ALTER TABLE machines ADD COLUMN demo_key TEXT")
        if "owner_token_hash" not in columns: c.execute("ALTER TABLE machines ADD COLUMN owner_token_hash TEXT")
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_machines_demo_key ON machines(demo_key)")
        columns={r[1] for r in c.execute("PRAGMA table_info(clips)").fetchall()}
        if "recorder_settings" not in columns: c.execute("ALTER TABLE clips ADD COLUMN recorder_settings TEXT")
init_db()

def rowdict(r): return dict(r) if r else None
def log_action(actor: str, action: str, payload: dict):
    body=json.dumps(payload,sort_keys=True,separators=(",",":"));
    with connect() as c:
        prev=c.execute("SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1").fetchone(); prev_hash=prev[0] if prev else "0"*64
        ts=now(); digest=hashlib.sha256((prev_hash+json.dumps({"ts":ts,"actor":actor,"action":action,"payload":payload},sort_keys=True,separators=(",",":"))).encode()).hexdigest()
        c.execute("INSERT INTO audit_log(ts,actor,action,payload,prev_hash,hash) VALUES(?,?,?,?,?,?)",(ts,actor,action,body,prev_hash,digest))
    # The audit event follows its committed SQLite transaction; the online
    # backup therefore includes the new chain link when GCS persistence is on.
    persist_db_snapshot(DB)
    publish_audit_event({"schema":"machine_passport.audit.v1","ts":ts,"actor":actor,"action":action,"payload":payload,"prev_hash":prev_hash,"hash":digest})

def wav_read(raw: bytes):
    try:
        with wave.open(io.BytesIO(raw),"rb") as w:
            channels=w.getnchannels(); width=w.getsampwidth(); sr=w.getframerate(); n=w.getnframes(); data=w.readframes(n)
        if width != 2: raise ValueError("Upload 16-bit PCM WAV audio.")
        x=np.frombuffer(data,dtype="<i2").astype(np.float64)/32768
        if channels>1: x=x.reshape(-1,channels).mean(axis=1)
        if sr not in (16000,22050,44100,48000): raise ValueError("Use a 16, 22.05, 44.1, or 48 kHz PCM WAV recording.")
        return x,sr
    except (wave.Error, EOFError) as e: raise ValueError("Invalid WAV file.") from e

FEATURES=["rms","crest_factor","kurtosis","zero_crossing_rate","spectral_centroid_hz","spectral_bandwidth_hz","rolloff_hz","spectral_flatness","band_0_500_db","band_500_2000_db","band_2000_4000_db","band_4000_8000_db"]
FEATURE_WEIGHTS=np.array([.003,1,1,1,1,1,1,1,1,1,1,1],dtype=float)

def text_vector(text:str)->dict[str,float]:
    """Compact local hashed term-frequency vector; IDF is applied at retrieval time."""
    tokens=[t.lower() for t in re.findall(r"[\w-]+",text,flags=re.UNICODE) if len(t)>1]
    buckets=[str(int.from_bytes(hashlib.sha256(t.encode()).digest()[:2],"little")%4096) for t in tokens]
    counts=Counter(buckets); total=max(1,sum(counts.values()))
    return {bucket:count/total for bucket,count in counts.items()}

def note_embedding(text:str)->dict[str,Any]:
    """Use Gemini dense vectors when selected; otherwise return an explicit local lexical vector."""
    try:
        from .providers import configured_provider_name, GeminiProvider
        if configured_provider_name()=="gemini":
            return {"kind":"gemini","model":os.getenv("GEMINI_EMBEDDING_MODEL","gemini-embedding-2"),"values":GeminiProvider().embed_text(text)}
    except Exception:
        pass
    return {"kind":"local_tfidf","values":text_vector(text)}

def note_text(transcript:str,english_text:str,structured:dict)->str:
    fields=[transcript,english_text]
    for key in ("component","symptom","suspected_cause","diagnostic_sequence","conditions"):
        value=structured.get(key,""); fields.extend(value if isinstance(value,list) else [value])
    return " ".join(str(x) for x in fields if x)

def retrieve_knowledge(mid:int,query:str,limit:int=5,confirmed_only:bool=False)->list[dict]:
    q_local=text_vector(query)
    if not q_local: return []
    q_dense=None
    try:
        from .providers import configured_provider_name, GeminiProvider
        if configured_provider_name()=="gemini": q_dense=GeminiProvider().embed_text(query)
    except Exception:
        q_dense=None
    with connect() as c: rows=c.execute("SELECT * FROM knowledge WHERE machine_id=? AND (?=0 OR human_confirmed=1) ORDER BY id DESC",(mid,0 if not confirmed_only else 1)).fetchall()
    vectors=[]; document_frequency=Counter()
    for row in rows:
        structured=json.loads(row["structured"]); stored=json.loads(row["embedding"] or "{}")
        if not stored: stored={"kind":"local_tfidf","values":text_vector(note_text(row["transcript"],row["english_text"] or "",structured))}
        if "kind" not in stored: stored={"kind":"local_tfidf","values":stored}
        vectors.append((row,stored))
        if stored["kind"]=="local_tfidf": document_frequency.update(stored["values"].keys())
    n_docs=max(1,len(vectors)); idf={token:1+np.log((n_docs+1)/(df+1)) for token,df in document_frequency.items()}
    q={k:v*idf.get(k,1) for k,v in q_local.items()}; qnorm=sum(v*v for v in q.values())**.5; scored=[]
    for row,stored_record in vectors:
        stored=stored_record["values"]
        if stored_record["kind"]=="gemini" and q_dense is not None:
            a=np.asarray(q_dense,dtype=float); b=np.asarray(stored,dtype=float); dot=float(np.dot(a,b)); norm=float(np.linalg.norm(a)*np.linalg.norm(b)); mode="GEMINI_EMBEDDING"
        elif stored_record["kind"]=="local_tfidf":
            stored={k:v*idf.get(k,1) for k,v in stored.items()}; dot=sum(v*stored.get(k,0) for k,v in q.items()); norm=qnorm*(sum(v*v for v in stored.values())**.5); mode="LOCAL_TFIDF"
        else: continue
        similarity=dot/norm if norm else 0
        if similarity>0: scored.append((similarity,row,mode))
    scored.sort(key=lambda item:(item[0],item[1]["id"]),reverse=True)
    return [{**dict(row),"similarity":round(score,4),"retrieval_mode":mode,"structured":json.loads(row["structured"]),"human_confirmed":bool(row["human_confirmed"]),"embedding":None,"raw_model_output":None} for score,row,mode in scored[:limit]]
def analyze(x: np.ndarray, sr: int):
    original_sr=sr; dur=len(x)/sr
    if not 5 <= dur <= 60: raise ValueError("Clip must be 5 to 60 seconds.")
    if sr not in (16000,22050,44100,48000): raise ValueError("Use a 16, 22.05, 44.1, or 48 kHz recording.")
    rms=float(np.sqrt(np.mean(x*x)))
    if rms < .003: raise ValueError("Clip is too quiet. Move closer and record again.")
    clipped=float(np.mean(np.abs(x)>=.999))
    if clipped > .01: raise ValueError("More than 1% of samples are clipped. Reduce input level.")
    dc_offset=float(np.mean(x)); dc_ratio=abs(dc_offset)/(rms+1e-12)
    if dc_ratio>.1: raise ValueError("Microphone DC offset is too high. Reconnect the microphone and record again.")
    if sr!=16000:
        divisor=np.gcd(sr,16000); x=signal.resample_poly(x,16000//divisor,sr//divisor); sr=16000
    x=x-np.mean(x)
    nper=min(4096,len(x)); f,p=signal.welch(x,fs=sr,nperseg=nper,nfft=4096)
    # Use a per-clip normalized spectrum so handset gain does not shift shape features.
    shape_power=p/(rms*rms+1e-20)
    ps=shape_power/(shape_power.sum()+1e-20); centroid=float(np.sum(f*ps)); bandwidth=float(np.sqrt(np.sum((f-centroid)**2*ps)))
    cdf=np.cumsum(ps); roll=float(f[min(np.searchsorted(cdf,.85),len(f)-1)])
    flat=float(np.exp(np.mean(np.log(shape_power+1e-20)))/(np.mean(shape_power)+1e-20))
    ny=sr/2; bands=[]
    for lo,hi in [(0,500),(500,2000),(2000,4000),(4000,8000)]:
        mask=(f>=lo)&(f<min(hi,ny)); bands.append(float(10*np.log10(np.sum(shape_power[mask])+1e-20)))
    zc=float(np.mean(np.diff(np.signbit(x)))); features={"rms":rms,"crest_factor":float(np.max(np.abs(x))/(rms+1e-20)),"kurtosis":float(stats.kurtosis(x,fisher=False)),"zero_crossing_rate":zc,"spectral_centroid_hz":centroid,"spectral_bandwidth_hz":bandwidth,"rolloff_hz":roll,"spectral_flatness":flat,**{f"band_{a}_{b}_db":v for (a,b),v in zip([(0,500),(500,2000),(2000,4000),(4000,8000)],bands)}}
    peaks,_=signal.find_peaks(p)
    top=sorted(peaks,key=lambda i:p[i],reverse=True)[:8]
    features["spectral_peaks"]=[{"frequency_hz":round(float(f[i]),1),"level_db":round(float(10*np.log10(p[i]+1e-20)),2)} for i in top]
    envelope=np.abs(signal.hilbert(x))
    ef,ep=signal.welch(envelope,fs=sr,nperseg=min(4096,len(envelope)),nfft=4096)
    epeaks,_=signal.find_peaks(ep); features["envelope_peaks"]=[{"frequency_hz":round(float(ef[i]),1),"level_db":round(float(10*np.log10(ep[i]+1e-20)),2)} for i in sorted(epeaks,key=lambda i:ep[i],reverse=True)[:5]]
    features["psd"]={"frequency_hz":f[::16].round(1).tolist(),"power":shape_power[::16].tolist()}
    noise_estimate="high broadband-noise estimate" if flat>.65 else "not flagged by spectral-flatness heuristic"
    quality={"rms":rms,"clipped_fraction":clipped,"dc_offset":dc_offset,"dc_offset_ratio":dc_ratio,"duration_s":dur,"original_sample_rate":original_sr,"analysis_sample_rate":16000,"background_noise_estimate":noise_estimate,"quality_guidance":"Move closer to the machine and away from wind/traffic if broadband noise is flagged." if flat>.65 else "Keep the same placement and avoid handling the phone."}
    return features,quality,dur

class MachineIn(BaseModel): name:str=Field(min_length=1,max_length=120); type:str=Field(default="Unknown",max_length=100); location:str=Field(default="",max_length=160); placement_note:str=Field(default="Hold 10 to 30 cm from the bearing housing, at the same spot each time.",max_length=500)
class Decision(BaseModel): action:str=Field(min_length=1,max_length=20); comment:str=Field(default="",max_length=2000); decided_by:str=Field(default="Operator",max_length=120)
class Outcome(BaseModel): actual_cause:str=Field(min_length=1,max_length=1000); action_taken:str=Field(min_length=1,max_length=1000); notes:str=Field(default="",max_length=4000)
class TelemetryBatch(BaseModel): machine_id:int; rows:list[dict[str,Any]]=Field(max_length=500)
class KnowledgeReview(BaseModel): transcript:str|None=Field(default=None,max_length=20000); english_text:str|None=Field(default=None,max_length=20000); structured:dict[str,Any]|None=None; reviewed_by:str=Field(default="Technician",max_length=120); confirm:bool=True
class FeedbackIn(BaseModel): useful:bool; suggestion:str=Field(default="",max_length=3000); role:str=Field(default="",max_length=40); machine_id:int|None=None; recommendation_id:int|None=None

@app.get("/api/health")
def health():
    from .providers import configured_provider_name
    return {"status":"ok","mode":os.getenv("APP_MODE","local"),"llm_provider":configured_provider_name(),"gemini_key_present":bool(os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_GENAI_USE_VERTEXAI","false").lower()=="true")}
@app.get("/healthz")
@app.get("/api/healthz")
def healthz(): return {"status":"ok"}
@app.get("/api/machines")
def machines():
    with connect() as c:
        out=[]
        for r in c.execute("SELECT * FROM machines ORDER BY id DESC").fetchall():
            m=dict(r); m.pop("owner_token_hash",None); m["clip_count"]=c.execute("SELECT COUNT(*) FROM clips WHERE machine_id=?",(m["id"],)).fetchone()[0]; m["baseline"]=rowdict(c.execute("SELECT * FROM baselines WHERE machine_id=? ORDER BY version DESC LIMIT 1",(m["id"],)).fetchone()); out.append(m)
        return out
@app.post("/api/machines")
def create_machine(m:MachineIn):
    delete_token=secrets.token_urlsafe(32); token_hash=hashlib.sha256(delete_token.encode()).hexdigest()
    with connect() as c:
        cur=c.execute("INSERT INTO machines(name,type,location,placement_note,created_at,owner_token_hash) VALUES(?,?,?,?,?,?)",(m.name,m.type,m.location,m.placement_note,now(),token_hash)); mid=cur.lastrowid
    log_action("Operator","machine.created",{"machine_id":mid,"name":m.name}); return {"id":mid,**m.model_dump(),"created_at":now(),"delete_token":delete_token}
@app.delete("/api/machines/{mid}")
def delete_my_machine_data(mid:int,x_delete_token:str=Header(default="")):
    record=require_machine(mid)
    if record.get("demo_key"): raise HTTPException(409,"The shared SEEDED demo machine is restored with Reset demo and cannot be deleted.")
    saved=record.get("owner_token_hash") or ""
    supplied=hashlib.sha256(x_delete_token.encode()).hexdigest() if x_delete_token else ""
    if not saved or not hmac.compare_digest(saved,supplied): raise HTTPException(403,"This deletion token does not own this machine. It is returned only once at machine creation.")
    delete_machine_records(mid,delete_machine=True)
    log_action("Owner token","machine.deleted",{"machine_id":mid,"scope":"machine and associated non-seeded records"})
    return {"deleted":True,"machine_id":mid}
def require_machine(mid:int):
    with connect() as c: r=c.execute("SELECT * FROM machines WHERE id=?",(mid,)).fetchone()
    if not r: raise HTTPException(404,"Machine not found")
    return dict(r)
@app.get("/api/machines/{mid}")
def machine(mid:int):
    m=require_machine(mid)
    with connect() as c:
        m["clips"]=[dict(r) for r in c.execute("SELECT * FROM clips WHERE machine_id=? ORDER BY id",(mid,))]
        m["baseline"]=rowdict(c.execute("SELECT * FROM baselines WHERE machine_id=? ORDER BY version DESC LIMIT 1",(mid,)).fetchone())
        m["recommendations"]=[dict(r) for r in c.execute("SELECT * FROM recommendations WHERE machine_id=? ORDER BY id DESC",(mid,))]
        for item in m["recommendations"]: item.pop("raw_model_output",None)
        m["events"]=[dict(r) for r in c.execute("SELECT * FROM events WHERE machine_id=? ORDER BY id DESC LIMIT 30",(mid,))]
        m["knowledge"]=[]
        for r in c.execute("SELECT * FROM knowledge WHERE machine_id=? ORDER BY id DESC",(mid,)):
            item=dict(r); item["structured"]=json.loads(item["structured"]); item["audio_available"]=bool(item["audio_path"]); item["human_confirmed"]=bool(item["human_confirmed"]); item["retrieval_mode"]=(json.loads(item["embedding"] or "{}").get("kind") or "local_tfidf").upper(); item.pop("raw_model_output",None); item.pop("audio_path",None); item.pop("audio_mime",None); item.pop("embedding",None); m["knowledge"].append(item)
        m["telemetry"]=[dict(r) for r in c.execute("SELECT * FROM telemetry WHERE machine_id=? ORDER BY id DESC LIMIT 100",(mid,))]
    m.pop("owner_token_hash",None)
    return m
@app.get("/api/machines/{mid}/qr")
def qr(mid:int):
    require_machine(mid)
    import qrcode
    from qrcode.image.svg import SvgPathImage
    image=qrcode.make(f"{os.getenv('PUBLIC_BASE_URL','http://localhost:8000')}/m/{mid}",image_factory=SvgPathImage)
    b=io.BytesIO(); image.save(b); return Response(b.getvalue(),media_type="image/svg+xml")

@app.post("/api/machines/{mid}/clips")
async def upload_clip(mid:int,kind:str=Form(...),label:str=Form("unknown"),recorder_settings:str=Form("{}"),file:UploadFile=File(...)):
    require_machine(mid)
    if kind not in ("baseline","check"): raise HTTPException(422,"kind must be baseline or check.")
    if label not in ("healthy","faulty","unknown"): raise HTTPException(422,"label must be healthy, faulty, or unknown.")
    raw=await file.read(8*1024*1024+1)
    if len(raw)>8*1024*1024: raise HTTPException(413,"Clip upload must be 8 MB or smaller.")
    try:
        settings=json.loads(recorder_settings)
        if not isinstance(settings,dict) or len(recorder_settings)>2000: raise ValueError("Invalid recorder settings metadata.")
        x,sr=wav_read(raw); features,quality,dur=analyze(x,sr); quality["recorder_settings"]=settings
    except ValueError as e: raise HTTPException(422,str(e))
    except json.JSONDecodeError as e: raise HTTPException(422,"Recorder settings must be valid JSON.") from e
    # Preserve the validated mono PCM analysis source in local storage or GCS.
    pcm=np.clip(x*32767,-32768,32767).astype("<i2").tobytes()
    audio_buffer=io.BytesIO()
    with wave.open(audio_buffer,"wb") as w: w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr); w.writeframes(pcm)
    path=store_audio(f"m{mid}_{time.time_ns()}.wav",audio_buffer.getvalue(),"audio/wav")
    with connect() as c:
        cur=c.execute("INSERT INTO clips(machine_id,kind,path,duration,sample_rate,quality,features,label,provenance,created_at,recorder_settings) VALUES(?,?,?,?,?,?,?,?,?,?,?)",(mid,kind,str(path),dur,sr,json.dumps(quality),json.dumps(features),label,"REAL",now(),json.dumps(settings))); cid=cur.lastrowid
        c.execute("INSERT INTO events(machine_id,ts,type,payload) VALUES(?,?,?,?)",(mid,now(),"clip",json.dumps({"clip_id":cid,"kind":kind,"provenance":"REAL"})))
    log_action("Operator","clip.uploaded",{"machine_id":mid,"clip_id":cid,"kind":kind}); return {"id":cid,"kind":kind,"duration_s":dur,"sample_rate":sr,"quality":quality,"features":features,"provenance":"REAL"}

@app.post("/api/machines/{mid}/baseline/build")
def build_baseline(mid:int):
    require_machine(mid)
    with connect() as c: rs=c.execute("SELECT * FROM clips WHERE machine_id=? AND kind='baseline' AND label='healthy' ORDER BY id",(mid,)).fetchall()
    if len(rs)<8: raise HTTPException(409,f"Baseline requires 8 healthy clips; currently {len(rs)}.")
    mat=np.array([[json.loads(r["features"])[k] for k in FEATURES] for r in rs]); med=np.median(mat,axis=0); mad=np.median(np.abs(mat-med),axis=0)*1.4826; mad=np.maximum(mad,np.maximum(np.abs(med)*.02,1e-5))
    # Leave-one-out calibration avoids scoring each training clip against itself.
    scores=[]
    for i in range(len(mat)):
        train=np.delete(mat,i,axis=0); loo_med=np.median(train,axis=0); loo_mad=np.median(np.abs(train-loo_med),axis=0)*1.4826
        loo_mad=np.maximum(loo_mad,np.maximum(np.abs(loo_med)*.02,1e-5))
        z=np.abs((mat[i]-loo_med)/loo_mad)*FEATURE_WEIGHTS; scores.append(float(np.sqrt(np.mean(np.sort(z)[-max(1,int(np.ceil(.2*len(z)))):]**2))))
    # Use the healthy leave-one-out 95th percentile directly. The former 1.25
    # margin suppressed false alarms on the public subset but missed half of
    # the injected faults; keep sensitivity tuning in the evaluation report.
    threshold=max(float(np.percentile(scores,95)),1.0)
    dispersion=float(np.median(np.std(mat,axis=0)/(np.abs(med)+1e-6)))
    provenance="SEEDED" if any(r["provenance"]=="SEEDED" for r in rs) else "REAL"
    quality={"consistency":"inconsistent" if dispersion>.8 else "usable","relative_dispersion":dispersion,"placement_note":require_machine(mid)["placement_note"],"provenance":provenance}
    with connect() as c:
        prev=c.execute("SELECT MAX(version) FROM baselines WHERE machine_id=?",(mid,)).fetchone()[0] or 0; version=prev+1
        c.execute("INSERT INTO baselines(machine_id,version,n_clips,stats,threshold,quality,created_at) VALUES(?,?,?,?,?,?,?)",(mid,version,len(rs),json.dumps({"features":FEATURES,"median":med.tolist(),"mad":mad.tolist()}),threshold,json.dumps(quality),now()))
        c.execute("INSERT INTO events(machine_id,ts,type,payload) VALUES(?,?,?,?)",(mid,now(),"baseline",json.dumps({"version":version,"n_clips":len(rs)})))
    log_action("Operator","baseline.built",{"machine_id":mid,"version":version,"n_clips":len(rs),"threshold":threshold}); return {"version":version,"n_clips":len(rs),"threshold":threshold,"quality":quality}

@app.post("/api/machines/{mid}/clips/{cid}/score")
def score_clip(mid:int,cid:int):
    require_machine(mid)
    with connect() as c:
        b=c.execute("SELECT * FROM baselines WHERE machine_id=? ORDER BY version DESC LIMIT 1",(mid,)).fetchone(); r=c.execute("SELECT * FROM clips WHERE id=? AND machine_id=?",(cid,mid)).fetchone()
    if not b: raise HTTPException(409,"Build a baseline with at least 8 healthy clips first.")
    if not r: raise HTTPException(404,"Clip not found")
    ft=json.loads(r["features"]); st=json.loads(b["stats"]); z=(np.array([ft[k] for k in FEATURES])-np.array(st["median"]))/np.array(st["mad"]); weighted_z=np.abs(z)*FEATURE_WEIGHTS; order=np.argsort(weighted_z)[::-1]; n=max(1,int(np.ceil(.2*len(z)))); score=float(np.sqrt(np.mean(np.sort(weighted_z)[-n:]**2))); threshold=float(b["threshold"]); status="OK" if score<=threshold else ("WATCH" if score<=threshold*1.5 else "ALERT")
    names={"rms":"overall sound energy","crest_factor":"impact/crest factor","kurtosis":"signal kurtosis","zero_crossing_rate":"zero crossing rate","spectral_centroid_hz":"spectral centroid","spectral_bandwidth_hz":"spectral bandwidth","rolloff_hz":"high frequency rolloff","spectral_flatness":"spectral flatness","band_0_500_db":"0–500 Hz band energy","band_500_2000_db":"500 Hz–2 kHz band energy","band_2000_4000_db":"2–4 kHz band energy","band_4000_8000_db":"4–8 kHz band energy"}
    top=[{"feature":FEATURES[i],"description":names[FEATURES[i]],"z":round(float(z[i]),2),"value":ft[FEATURES[i]],"baseline_median":st["median"][i],"unit":"sigma"} for i in order[:3]]
    provenance=r["provenance"]
    result={"score":round(score,3),"threshold":round(threshold,3),"status":status,"top_features":top,"baseline_version":b["version"],"provenance":provenance}
    with connect() as c:
        c.execute("UPDATE clips SET features=? WHERE id=?",(json.dumps({**ft,"analysis":result}),cid)); c.execute("INSERT INTO events(machine_id,ts,type,payload) VALUES(?,?,?,?)",(mid,now(),"analysis",json.dumps({"clip_id":cid,**result})))
        c.execute("INSERT INTO telemetry(machine_id,ts,metric,value,unit,provenance) VALUES(?,?,?,?,?,?)",(mid,now(),"audio_anomaly_score",score,"score",provenance))
    log_action("Analyzer","clip.scored",{"machine_id":mid,"clip_id":cid,"score":score,"threshold":threshold,"status":status}); return result

@app.post("/api/machines/{mid}/recommendations")
def recommend(mid:int,clip_id:int|None=None):
    require_machine(mid); evidence=[]
    with connect() as c:
        if clip_id:
            clip=c.execute("SELECT * FROM clips WHERE id=? AND machine_id=?",(clip_id,mid)).fetchone()
            if clip:
                a=json.loads(clip["features"]).get("analysis",{}); evidence.append({"id":f"EV-CLIP-{clip_id}","kind":"audio_analysis","clip_id":clip_id,"analysis":a,"provenance":clip["provenance"]})
        outcomes=c.execute("SELECT o.*,r.machine_id FROM outcomes o JOIN recommendations r ON r.id=o.recommendation_id WHERE r.machine_id=? ORDER BY o.id DESC LIMIT 3",(mid,)).fetchall()
        for o in outcomes: evidence.append({"id":f"EV-OUTCOME-{o['id']}","kind":"past_outcome","cause":o["actual_cause"],"action":o["action_taken"]})
    analysis=evidence[0].get("analysis",{}) if evidence else {}; risky=analysis.get("status") in ("WATCH","ALERT")
    note_query=" ".join([analysis.get("status",""),*[x.get("description","") for x in analysis.get("top_features",[])],"maintenance inspection"])
    for n in retrieve_knowledge(mid,note_query,5,confirmed_only=True):
        note=n.get("english_text") or n.get("transcript") or json.dumps(n.get("structured",{}),ensure_ascii=False)
        evidence.append({"id":f"EV-KNOW-{n['id']}","kind":"technician_note","text":note,"similarity":n["similarity"],"provenance":n["provenance"]})
    if evidence and (risky or len(evidence)>1):
        feature=analysis.get("top_features",[{}])[0]
        content={"recommended_action":"Inspect the machine using the site's approved maintenance procedure; verify the indicated sound feature and check alignment and lubrication before replacing parts." if risky else "Continue routine checks and compare the next recording at the same placement.","rationale":f"Audio check scored {analysis.get('score')} against a calibrated threshold of {analysis.get('threshold')} ({analysis.get('status')})." if analysis else "A technician note is available, but there is no scored audio evidence.","ordered_diagnostic_steps":["Repeat the recording at the documented placement.",f"Inspect {feature.get('description','the machine condition')} and compare with the baseline.","Escalate to a qualified technician if the abnormal condition persists."],"urgency":"inspect soon" if risky else "routine","confidence":"medium" if len(evidence)>=2 and risky else "low","evidence":[{"id":e["id"],"how_it_supports":"Stored machine-specific observation supplied as recommendation evidence."} for e in evidence],"what_would_change_my_mind":"A repeat recording within the machine baseline, or an inspection finding that explains the signal.","safety_notes":"Follow site lockout/tagout and safety procedures. Recommendation requires human approval.","mode":"OFFLINE MODE (no AI reasoning)"}
        validation={"validated":True,"unknown_evidence_ids":[],"grounding":"template from stored evidence"}
    else:
        content={"recommended_action":"Insufficient evidence. Perform a generic safe visual and listening inspection under the site's approved procedure, then record a comparable check.","rationale":"No relevant scored event, technician note, or outcome was retrieved.","ordered_diagnostic_steps":["Confirm the machine is operating under normal conditions.","Inspect for visible damage, loose guards, leaks, or abnormal heat without bypassing safeguards.","Record a check at the documented placement."],"urgency":"routine","confidence":"low","evidence":[],"what_would_change_my_mind":"A baseline-comparable recording or a relevant technician observation.","safety_notes":"Follow site safety and lockout/tagout procedures. Recommendation requires human approval.","mode":"OFFLINE MODE (no AI reasoning)"}; validation={"validated":False,"reason":"insufficient evidence","unknown_evidence_ids":[]}
    if evidence and (risky or len(evidence)>1):
        from .providers import configured_provider_name
        template=json.loads(json.dumps(content)); provider_name=configured_provider_name(); raw_model_output=None
        try:
            from .providers import make_provider, validate_grounding
            generated=make_provider(provider_name).recommend(evidence,analysis)
            raw_model_output=generated.pop("_raw_model_output",None)
            provider_validation=validate_grounding(generated,evidence)
            if provider_validation["validated"]:
                content=generated
                if len(evidence)<2: content["confidence"]="low"
                content["mode"]="TEMPLATE, not AI reasoning" if provider_name=="mock" else f"{provider_name.upper()} PROVIDER"
                validation={**provider_validation,"provider":provider_name}
            else:
                content=template; validation={**provider_validation,"provider":provider_name,"fallback":"TEMPLATE, not AI reasoning"}; provider_name="TEMPLATE"; raw_model_output=None
        except Exception as exc:
            content=template; validation={"validated":False,"provider":provider_name,"provider_error":type(exc).__name__,"fallback":"TEMPLATE, not AI reasoning"}; provider_name="TEMPLATE"; raw_model_output=None
            content["provider_notice"]="Daily Gemini limit reached; showing a clearly labeled template." if type(exc).__name__=="DailyGeminiCapReached" else ("Gemini quota service unavailable; showing a clearly labeled template." if type(exc).__name__=="QuotaBackendUnavailable" else "AI provider unavailable; showing a clearly labeled template.")
    else:
        provider_name="TEMPLATE"; raw_model_output=None
    content["provider"]=provider_name.upper()
    with connect() as c:
        cur=c.execute("INSERT INTO recommendations(machine_id,status,content,evidence,validation,created_at,provider,raw_model_output) VALUES(?,?,?,?,?,?,?,?)",(mid,"pending",json.dumps(content),json.dumps(evidence),json.dumps(validation),now(),provider_name.upper(),raw_model_output)); rid=cur.lastrowid
        c.execute("INSERT INTO events(machine_id,ts,type,payload) VALUES(?,?,?,?)",(mid,now(),"recommendation",json.dumps({"recommendation_id":rid,"status":"pending"})))
    raw_hash=hashlib.sha256(raw_model_output.encode()).hexdigest() if raw_model_output else None
    log_action("Recommendation","recommendation.created",{"machine_id":mid,"recommendation_id":rid,"evidence_ids":[e["id"] for e in evidence],"provider":provider_name.upper(),"raw_model_output_sha256":raw_hash}); return {"id":rid,"status":"pending","content":content,"evidence":evidence,"validation":validation}

@app.post("/api/recommendations/{rid}/decision")
def decide(rid:int,d:Decision):
    action=d.action.lower()
    if action not in ("approve","reject","modify"): raise HTTPException(422,"action must be approve, reject, or modify")
    status={"approve":"approved","reject":"rejected","modify":"modified"}[action]
    with connect() as c:
        r=c.execute("SELECT * FROM recommendations WHERE id=?",(rid,)).fetchone()
        if not r: raise HTTPException(404,"Recommendation not found")
        content=json.loads(r["content"])
        if action=="modify" and d.comment: content["operator_edit"]=d.comment
        c.execute("UPDATE recommendations SET status=?,content=?,decided_by=?,decided_at=? WHERE id=?",(status,json.dumps(content),d.decided_by,now(),rid)); c.execute("INSERT INTO events(machine_id,ts,type,payload) VALUES(?,?,?,?)",(r["machine_id"],now(),"approval",json.dumps({"recommendation_id":rid,"status":status,"comment":d.comment})))
    log_action(d.decided_by,f"recommendation.{status}",{"recommendation_id":rid,"comment":d.comment}); return {"id":rid,"status":status,"decided_by":d.decided_by}
@app.post("/api/recommendations/{rid}/outcomes")
def outcome(rid:int,o:Outcome):
    with connect() as c:
        if not c.execute("SELECT 1 FROM recommendations WHERE id=?",(rid,)).fetchone(): raise HTTPException(404,"Recommendation not found")
        cur=c.execute("INSERT INTO outcomes(recommendation_id,actual_cause,action_taken,notes,created_at) VALUES(?,?,?,?,?)",(rid,o.actual_cause,o.action_taken,o.notes,now())); oid=cur.lastrowid
    log_action("Operator","outcome.logged",{"recommendation_id":rid,"outcome_id":oid,"actual_cause":o.actual_cause}); return {"id":oid,"created_at":now()}

@app.post("/api/feedback",status_code=201)
def submit_feedback(feedback:FeedbackIn):
    role=feedback.role.strip().lower()
    if role not in ("","technician","supervisor","other"): raise HTTPException(422,"role must be technician, supervisor, other, or empty.")
    mid=feedback.machine_id
    if feedback.recommendation_id is not None:
        with connect() as c: rec=c.execute("SELECT machine_id FROM recommendations WHERE id=?",(feedback.recommendation_id,)).fetchone()
        if not rec: raise HTTPException(404,"Recommendation not found")
        if mid is not None and mid!=rec[0]: raise HTTPException(422,"Recommendation and machine IDs do not match.")
        mid=int(rec[0])
    if mid is not None: require_machine(mid)
    with connect() as c:
        cur=c.execute("INSERT INTO feedback(machine_id,recommendation_id,useful,suggestion,role,created_at) VALUES(?,?,?,?,?,?)",(mid,feedback.recommendation_id,int(feedback.useful),feedback.suggestion.strip(),role,now())); fid=cur.lastrowid
    log_action("Visitor","feedback.submitted",{"feedback_id":fid,"machine_id":mid,"recommendation_id":feedback.recommendation_id,"useful":feedback.useful,"role":role,"suggestion_length":len(feedback.suggestion)})
    return {"id":fid,"stored":True}

@app.get("/admin/feedback")
def export_feedback(x_admin_token:str=Header(default="")):
    expected=os.getenv("FEEDBACK_ADMIN_TOKEN","")
    if not expected: raise HTTPException(503,"Feedback export is disabled until FEEDBACK_ADMIN_TOKEN is configured.")
    if not hmac.compare_digest(expected,x_admin_token): raise HTTPException(403,"Invalid admin token.")
    with connect() as c: rows=c.execute("SELECT created_at,machine_id,recommendation_id,useful,role,suggestion FROM feedback ORDER BY id").fetchall()
    output=io.StringIO(newline=""); writer=csv.writer(output); writer.writerow(["created_at","machine_id","recommendation_id","useful","role","suggestion"])
    for row in rows: writer.writerow(list(row))
    return Response(output.getvalue(),media_type="text/csv; charset=utf-8",headers={"Content-Disposition":"attachment; filename=machine-passport-feedback.csv"})
@app.post("/api/machines/{mid}/knowledge")
def knowledge(mid:int,technician:str=Form("Technician"),transcript:str=Form(...)):
    require_machine(mid)
    if len(technician)>120 or not transcript.strip() or len(transcript)>20000: raise HTTPException(422,"Technician name must be at most 120 characters and note text must contain 1–20,000 characters.")
    # Text is stored as data. Local mode does not claim AI transcription/structuring.
    structured={"component":"unspecified","symptom":"technician observation","suspected_cause":"not assessed","diagnostic_sequence":[],"confidence":"low","uncertainty_notes":"Locally captured note; technician review required."}
    english=transcript; embedding=note_embedding(note_text(transcript,english,structured))
    with connect() as c:
        cur=c.execute("INSERT INTO knowledge(machine_id,technician,transcript,structured,provenance,created_at,original_language,english_text,embedding,provider) VALUES(?,?,?,?,?,?,?,?,?,?)",(mid,technician,transcript,json.dumps(structured),"REAL",now(),"unspecified",english,json.dumps(embedding),"USER")); kid=cur.lastrowid
    log_action(technician,"knowledge.created",{"machine_id":mid,"knowledge_id":kid}); return {"id":kid,"transcript":transcript,"english_text":english,"structured":structured,"provenance":"REAL"}

def structure_saved_audio(kid:int):
    with connect() as c: row=c.execute("SELECT * FROM knowledge WHERE id=?",(kid,)).fetchone()
    if not row or not row["audio_path"]: raise HTTPException(404,"Audio note not found")
    try:
        from .providers import GeminiProvider
        structured=GeminiProvider().structure_audio_note(read_audio(row["audio_path"]),row["audio_mime"] or "audio/wav")
    except Exception as exc:
        if type(exc).__name__=="DailyGeminiCapReached": return JSONResponse(status_code=429,content={"id":kid,"status":"daily_gemini_limit_reached","detail":"The shared daily Gemini limit has been reached. The saved audio is safe; try again tomorrow."})
        if type(exc).__name__=="QuotaBackendUnavailable": return JSONResponse(status_code=503,content={"id":kid,"status":"gemini_quota_unavailable","detail":"Gemini is paused because the shared daily quota service is unavailable. The original audio remains saved."})
        if isinstance(exc,RuntimeError):
            return JSONResponse(status_code=202,content={"id":kid,"status":"awaiting_gemini_configuration","detail":str(exc),"provenance":row["provenance"]})
        with connect() as c: c.execute("UPDATE knowledge SET structured=? WHERE id=?",(json.dumps({"processing_status":"failed","error":type(exc).__name__}),kid))
        raise HTTPException(502,"Gemini audio structuring failed; the original recording remains saved. Retry after checking provider configuration.")
    raw_model_output=structured.pop("_raw_model_output",None)
    english=structured["english_translation"] or structured["transcript"]
    vector=note_embedding(note_text(structured["transcript"],english,structured))
    with connect() as c: c.execute("UPDATE knowledge SET transcript=?,structured=?,original_language=?,english_text=?,embedding=?,provider='GEMINI',raw_model_output=? WHERE id=?",(structured["transcript"],json.dumps(structured),structured["detected_language"],english,json.dumps(vector),raw_model_output,kid))
    raw_hash=hashlib.sha256(raw_model_output.encode()).hexdigest() if raw_model_output else None
    log_action(row["technician"],"knowledge.audio_structured",{"machine_id":row["machine_id"],"knowledge_id":kid,"provider":"GEMINI","model":os.getenv("GEMINI_MODEL","gemini-3.8-flash"),"raw_model_output_sha256":raw_hash})
    return {"id":kid,"status":"structured","transcript":structured["transcript"],"english_text":english,"structured":structured,"provenance":row["provenance"]}

@app.post("/api/machines/{mid}/knowledge/audio")
async def knowledge_audio(mid:int,technician:str=Form("Technician"),clip_id:int|None=Form(None),file:UploadFile=File(...)):
    raw=await file.read(20*1024*1024+1)
    return save_knowledge_audio(mid,technician,clip_id,raw,file.content_type or "audio/wav")

def save_knowledge_audio(mid:int,technician:str,clip_id:int|None,raw:bytes,content_type:str):
    require_machine(mid)
    if len(raw)>20*1024*1024: raise HTTPException(413,"Voice note must be 20 MB or smaller.")
    mime=content_type.split(";",1)[0].strip().lower()
    if not mime.startswith("audio/"): raise HTTPException(415,"Upload an audio recording.")
    suffix={"audio/wav":".wav","audio/x-wav":".wav","audio/webm":".webm","audio/mpeg":".mp3","audio/mp4":".m4a","audio/ogg":".ogg","audio/flac":".flac","audio/aac":".aac"}.get(mime,".audio")
    path=store_audio(f"knowledge_m{mid}_{time.time_ns()}{suffix}",raw,mime)
    structured={"processing_status":"awaiting_gemini_configuration","component":"unspecified","symptom":"unspecified","suspected_cause":"unspecified","diagnostic_sequence":[],"severity":"low","confidence":"low","uncertainty_notes":"Original recording saved. Configure Gemini and retry structuring."}
    with connect() as c:
        cur=c.execute("INSERT INTO knowledge(machine_id,clip_id,technician,transcript,structured,provenance,created_at,audio_path,audio_mime,embedding,human_confirmed,provider) VALUES(?,?,?,?,?,?,?,?,?,?,0,'PENDING')",(mid,clip_id,technician,"",json.dumps(structured),"REAL",now(),str(path),mime,"{}")); kid=cur.lastrowid
    log_action(technician,"knowledge.audio_uploaded",{"machine_id":mid,"knowledge_id":kid,"bytes":len(raw),"mime":mime})
    return structure_saved_audio(kid)

@app.post("/api/machines/{mid}/knowledge/{kid}/structure")
def retry_structure(mid:int,kid:int):
    require_machine(mid)
    with connect() as c:
        if not c.execute("SELECT 1 FROM knowledge WHERE id=? AND machine_id=?",(kid,mid)).fetchone(): raise HTTPException(404,"Knowledge note not found")
    return structure_saved_audio(kid)

@app.get("/api/machines/{mid}/knowledge/{kid}/audio")
def get_knowledge_audio(mid:int,kid:int):
    require_machine(mid)
    with connect() as c: row=c.execute("SELECT audio_path,audio_mime FROM knowledge WHERE id=? AND machine_id=?",(kid,mid)).fetchone()
    if not row or not row["audio_path"]: raise HTTPException(404,"Original audio is unavailable")
    try: audio=read_audio(row["audio_path"])
    except Exception: raise HTTPException(404,"Original audio is unavailable")
    suffix=Path(row["audio_path"].split("?")[0]).suffix or ".audio"
    return Response(audio,media_type=row["audio_mime"],headers={"Content-Disposition":f'inline; filename="knowledge-{kid}{suffix}"'})

@app.get("/api/machines/{mid}/knowledge/search")
def search_knowledge(mid:int,q:str,limit:int=5):
    require_machine(mid); results=retrieve_knowledge(mid,q,max(1,min(limit,20)))
    for item in results: item.pop("audio_path",None); item.pop("audio_mime",None)
    return {"query":q,"results":results,"retrieval":"local hashed TF-IDF-style cosine similarity"}

@app.post("/api/machines/{mid}/knowledge/{kid}/review")
def review_knowledge(mid:int,kid:int,review:KnowledgeReview):
    require_machine(mid)
    with connect() as c:
        row=c.execute("SELECT * FROM knowledge WHERE id=? AND machine_id=?",(kid,mid)).fetchone()
        if not row: raise HTTPException(404,"Knowledge note not found")
        transcript=review.transcript if review.transcript is not None else row["transcript"]
        structured=review.structured if review.structured is not None else json.loads(row["structured"])
        english=review.english_text if review.english_text is not None else row["english_text"] or transcript
        structured["human_confirmed"]=review.confirm
        c.execute("UPDATE knowledge SET transcript=?,english_text=?,structured=?,embedding=?,human_confirmed=? WHERE id=?",(transcript,english,json.dumps(structured),json.dumps(note_embedding(note_text(transcript,english,structured))),int(review.confirm),kid))
    log_action(review.reviewed_by,"knowledge.reviewed",{"machine_id":mid,"knowledge_id":kid,"confirmed":review.confirm})
    return {"id":kid,"transcript":transcript,"english_text":english,"structured":structured,"human_confirmed":review.confirm}
@app.post("/api/telemetry")
def telemetry(batch:TelemetryBatch):
    return ingest_telemetry(batch)

def ingest_telemetry(batch:TelemetryBatch,message_id:str|None=None):
    require_machine(batch.machine_id); out=[]
    with connect() as c:
        if message_id and c.execute("SELECT 1 FROM pubsub_inbox WHERE message_id=?",(message_id,)).fetchone(): return {"inserted":0,"ids":[],"duplicate":True}
        for item in batch.rows:
            metric=str(item.get("metric","")); unit=str(item.get("unit",""))
            try: value=float(item.get("value",float("nan")))
            except (TypeError,ValueError): raise HTTPException(422,"Telemetry values must be numeric and finite.")
            if not metric or len(metric)>120 or len(unit)>40 or not np.isfinite(value): raise HTTPException(422,"Each telemetry row needs a metric, finite numeric value, and short unit.")
            prov=item.get("provenance","REAL")
            if prov not in ("REAL","SIMULATED","SEEDED"): raise HTTPException(422,"Invalid provenance")
            cur=c.execute("INSERT INTO telemetry(machine_id,ts,metric,value,unit,provenance) VALUES(?,?,?,?,?,?)",(batch.machine_id,item.get("ts",now()),metric,value,unit,prov)); out.append(cur.lastrowid)
        if message_id: c.execute("INSERT INTO pubsub_inbox(message_id,received_at) VALUES(?,?)",(message_id,now()))
    log_action("Telemetry","telemetry.ingested",{"machine_id":batch.machine_id,"count":len(out)}); return {"inserted":len(out),"ids":out}
@app.post("/api/machines/{mid}/simulate")
def simulate_telemetry(mid:int,fault:bool=False):
    require_machine(mid); rng=random.SystemRandom(); drift=18 if fault else 0
    rows=[{"metric":"temperature","value":round(48+rng.uniform(-1.5,1.5)+drift,2),"unit":"°C","provenance":"SIMULATED"},{"metric":"current","value":round(8+rng.uniform(-.35,.35)+(1.8 if fault else 0),2),"unit":"A","provenance":"SIMULATED"},{"metric":"rpm","value":round(1450+rng.uniform(-12,12)-(45 if fault else 0),1),"unit":"rpm","provenance":"SIMULATED"},{"metric":"vibration","value":round(.8+rng.uniform(-.08,.08)+(1.1 if fault else 0),3),"unit":"mm/s","provenance":"SIMULATED"}]
    return telemetry(TelemetryBatch(machine_id=mid,rows=rows))
@app.get("/api/audit/verify")
def audit_verify():
    with connect() as c: rows=c.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
    prev="0"*64
    for r in rows:
        payload={"ts":r["ts"],"actor":r["actor"],"action":r["action"],"payload":json.loads(r["payload"])}
        digest=hashlib.sha256((prev+json.dumps(payload,sort_keys=True,separators=(",",":"))).encode()).hexdigest()
        if r["prev_hash"]!=prev or r["hash"]!=digest: return {"valid":False,"failed_at":r["id"],"rows":len(rows)}
        prev=r["hash"]
    return {"valid":True,"rows":len(rows),"head":prev}
@app.get("/api/machines/{mid}/handover",response_class=HTMLResponse)
def handover(mid:int,lang:str="en"):
    m=machine(mid); recs=m["recommendations"]; openr=[r for r in recs if r["status"]=="pending"]
    def esc(v): return str(v).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
    open_items="".join(f"<li>{esc(json.loads(x['content'])['recommended_action'])}</li>" for x in openr)
    recent="".join(f"<li>{esc(e['ts'])} — {esc(e['type'])}: {esc(e['payload'])}</li>" for e in m['events'][:12])
    html=f"<!doctype html><title>Handover · {esc(m['name'])}</title><style>body{{font:16px system-ui;max-width:800px;margin:40px auto;padding:0 20px}}@media print{{button{{display:none}}}}</style><button onclick='print()'>Print</button><h1>Machine handover: {esc(m['name'])}</h1><p>{esc(m['type'])} · {esc(m['location'])} · Generated {now()} · Language: {esc(lang)}</p><h2>Open recommendations</h2><ul>{open_items}</ul><h2>Recent events</h2><ul>{recent}</ul><h2>Placement</h2><p>{esc(m['placement_note'])}</p>"
    return HTMLResponse(html)

@app.get("/api/metrics/latest")
def metrics():
    p=ROOT/"eval"/"results"/"latest.json"
    result=json.loads(p.read_text()) if p.exists() else {"available":False,"message":"No evaluation run yet."}
    with connect() as c:
        counts=c.execute("SELECT COUNT(*),SUM(CASE WHEN useful=1 THEN 1 ELSE 0 END),SUM(CASE WHEN useful=0 THEN 1 ELSE 0 END) FROM feedback").fetchone()
    if counts and counts[0]>0: result["feedback_summary"]={"responses":int(counts[0]),"useful_yes":int(counts[1] or 0),"useful_no":int(counts[2] or 0)}
    return result
@app.get("/api/events/{mid}")
def events(mid:int):
    require_machine(mid)
    with connect() as c: return [dict(r) for r in c.execute("SELECT * FROM events WHERE machine_id=? ORDER BY id DESC LIMIT 100",(mid,))]

DEMO_KEY="machine-passport-guided-demo"
DEMO_SOURCE=ROOT/"frontend"/"sample_clips"
DEMO_NOTES=[
    ("Sample maintenance record","Bearing housing vibration was previously observed; compare at the same speed and placement."),
    ("Sample maintenance record","A loose motor mount can produce a low rumble. Inspect mounting hardware under site safety procedures."),
    ("Sample maintenance record","Check coupling alignment and lubrication before replacing parts; record inspection findings."),
]

def delete_machine_records(mid:int,delete_machine:bool=False):
    with connect() as c:
        paths=[r[0] for r in c.execute("SELECT path FROM clips WHERE machine_id=?",(mid,)).fetchall()]
        paths += [r[0] for r in c.execute("SELECT audio_path FROM knowledge WHERE machine_id=? AND audio_path IS NOT NULL",(mid,)).fetchall()]
        rec_ids=[r[0] for r in c.execute("SELECT id FROM recommendations WHERE machine_id=?",(mid,)).fetchall()]
        if rec_ids:
            marks=",".join("?" for _ in rec_ids); c.execute(f"DELETE FROM outcomes WHERE recommendation_id IN ({marks})",rec_ids)
        for table in ("telemetry","events","clips","baselines","knowledge","recommendations"):
            c.execute(f"DELETE FROM {table} WHERE machine_id=?",(mid,))
        if delete_machine: c.execute("DELETE FROM machines WHERE id=?",(mid,))
    for value in paths:
        try: delete_audio(value)
        except Exception as exc: logger.warning(json.dumps({"event":"audio_delete_failed","error":type(exc).__name__}))

def _clear_demo_records(mid:int): delete_machine_records(mid,delete_machine=False)

def _insert_demo_clip(mid:int,path:Path,kind:str,label:str,index:int):
    raw=path.read_bytes(); x,sr=wav_read(raw); features,quality,duration=analyze(x,sr)
    dest=store_audio(f"demo_m{mid}_{kind}_{label}_{index}.wav",raw,"audio/wav")
    with connect() as c:
        cur=c.execute("INSERT INTO clips(machine_id,kind,path,duration,sample_rate,quality,features,label,provenance,created_at,recorder_settings) VALUES(?,?,?,?,?,?,?,?,?,?,?)",(mid,kind,str(dest),duration,sr,json.dumps(quality),json.dumps(features),label,"SEEDED",now(),json.dumps({"source":"bundled public MIMII teaching subset","capture_settings":"not applicable; dataset WAV"})))
        cid=cur.lastrowid
        c.execute("INSERT INTO events(machine_id,ts,type,payload) VALUES(?,?,?,?)",(mid,now(),"clip",json.dumps({"clip_id":cid,"kind":kind,"provenance":"SEEDED","source":"public MIMII teaching subset"})))
    return cid

def ensure_demo_machine(reset:bool=False)->int:
    with connect() as c: row=c.execute("SELECT id FROM machines WHERE demo_key=?",(DEMO_KEY,)).fetchone()
    if row: mid=int(row[0])
    else:
        machine_record=create_machine(MachineIn(name="Guided demo · public fan sample",type="Fan (public sample)",location="Seeded walkthrough",placement_note="SEEDED sample clips from public MIMII fan teaching data; not a field recording.")); mid=machine_record["id"]
        with connect() as c: c.execute("UPDATE machines SET demo_key=? WHERE id=?",(DEMO_KEY,mid))
    if reset: _clear_demo_records(mid)
    with connect() as c:
        ready=c.execute("SELECT (SELECT COUNT(*) FROM clips WHERE machine_id=? AND kind='baseline' AND label='healthy'),(SELECT COUNT(*) FROM baselines WHERE machine_id=?),(SELECT COUNT(*) FROM knowledge WHERE machine_id=?)",(mid,mid,mid)).fetchone()
    if not reset and ready and ready[0]>=8 and ready[1]>0 and ready[2]>=3: return mid
    if not reset: _clear_demo_records(mid)
    healthy=sorted((DEMO_SOURCE/"healthy").glob("*.wav"))[:8]; faulty=sorted((DEMO_SOURCE/"faulty").glob("*.wav"))[:2]
    if len(healthy)<8 or len(faulty)<2: raise RuntimeError("Bundled SEEDED demo clips are missing; demo startup cannot be completed.")
    for i,path in enumerate(healthy): _insert_demo_clip(mid,path,"baseline","healthy",i)
    build_baseline(mid)
    for i,path in enumerate(faulty):
        cid=_insert_demo_clip(mid,path,"check","faulty",i)
        score_clip(mid,cid)
    for technician,transcript in DEMO_NOTES:
        structured={"component":"fan/motor mounting","symptom":"sample maintenance observation","suspected_cause":"sample note, not a real inspection finding","diagnostic_sequence":["Compare at the same placement.","Inspect safely before replacing parts."],"confidence":"low","uncertainty_notes":"SEEDED demonstration text; not a technician testimonial."}
        vector={"kind":"local_tfidf","values":text_vector(note_text(transcript,transcript,structured))}
        with connect() as c:
            cur=c.execute("INSERT INTO knowledge(machine_id,technician,transcript,structured,provenance,created_at,original_language,english_text,embedding,human_confirmed,provider) VALUES(?,?,?,?,?,?,?,?,?,1,'SEEDED')",(mid,technician,transcript,json.dumps(structured),"SEEDED",now(),"en","SEEDED: "+transcript,json.dumps(vector))); kid=cur.lastrowid
        log_action("Demo seed","knowledge.seeded",{"machine_id":mid,"knowledge_id":kid,"provenance":"SEEDED"})
    log_action("Demo seed","demo.machine_ready",{"machine_id":mid,"healthy_clips":8,"faulty_clips":2,"notes":3,"provenance":"SEEDED"})
    return mid

def cleanup_old_nonseeded()->int:
    days=max(1,int(os.getenv("DATA_TTL_DAYS","30"))); cutoff=(datetime.now(timezone.utc)-timedelta(days=days)).isoformat()
    with connect() as c:
        ids=[int(r[0]) for r in c.execute("SELECT id FROM machines WHERE demo_key IS NULL AND created_at<?",(cutoff,)).fetchall()]
    for mid in ids:
        delete_machine_records(mid,delete_machine=True)
        log_action("Retention cleanup","machine.expired",{"machine_id":mid,"retention_days":days})
    return len(ids)

async def periodic_cleanup():
    while True:
        await asyncio.sleep(24*60*60)
        try:
            count=await asyncio.to_thread(cleanup_old_nonseeded)
            logger.info(json.dumps({"event":"retention_cleanup","machines_deleted":count,"retention_days":max(1,int(os.getenv("DATA_TTL_DAYS","30")))}))
        except Exception as exc:
            logger.error(json.dumps({"event":"retention_cleanup_failed","error_type":type(exc).__name__}))

def startup_seed_demo(): ensure_demo_machine()

@app.get("/api/demo")
def demo_info():
    mid=ensure_demo_machine()
    m=machine(mid)
    return {"machine_id":mid,"name":m["name"],"provenance":"SEEDED","baseline":m["baseline"],"clips":m["clips"],"notes":m["knowledge"],"sample_urls":{"healthy":"/assets/sample_clips/healthy/00000027.wav","faulty":"/assets/sample_clips/faulty/00000024.wav"},"notice":"Bundled sample recordings and notes are SEEDED public demonstration data, not records from this site."}

@app.post("/api/demo/reset")
def demo_reset():
    mid=ensure_demo_machine(reset=True)
    return {"machine_id":mid,"status":"reset","provenance":"SEEDED"}

@app.post("/api/demo/sample/{label}")
def demo_sample(label:str):
    if label not in ("healthy","faulty"): raise HTTPException(422,"Sample label must be healthy or faulty.")
    mid=ensure_demo_machine(); folder=DEMO_SOURCE/label; files=sorted(folder.glob("*.wav"))
    if not files: raise HTTPException(503,"SEEDED sample clips are unavailable.")
    with connect() as c: idx=c.execute("SELECT COUNT(*) FROM clips WHERE machine_id=? AND kind='check'",(mid,)).fetchone()[0]
    cid=_insert_demo_clip(mid,files[idx%len(files)],"check",label,idx)
    score=score_clip(mid,cid)
    return {"clip_id":cid,"machine_id":mid,"label":label,"provenance":"SEEDED","analysis":score}

@app.post("/api/demo/note")
def demo_note():
    mid=ensure_demo_machine(); transcript=DEMO_NOTES[1][1]
    with connect() as c:
        row=c.execute("SELECT id FROM knowledge WHERE machine_id=? AND transcript=? ORDER BY id DESC LIMIT 1",(mid,transcript)).fetchone()
    if row: return {"id":row[0],"provenance":"SEEDED","status":"already present"}
    technician,text=DEMO_NOTES[1]
    return knowledge_seeded(mid,technician,text)

def knowledge_seeded(mid:int,technician:str,transcript:str):
    require_machine(mid)
    structured={"component":"fan/motor mounting","symptom":"sample maintenance observation","suspected_cause":"sample note, not a real inspection finding","diagnostic_sequence":["Compare at the same placement.","Inspect safely before replacing parts."],"confidence":"low","uncertainty_notes":"SEEDED demonstration text; not a technician testimonial."}
    vector={"kind":"local_tfidf","values":text_vector(note_text(transcript,transcript,structured))}
    with connect() as c:
        cur=c.execute("INSERT INTO knowledge(machine_id,technician,transcript,structured,provenance,created_at,original_language,english_text,embedding,human_confirmed,provider) VALUES(?,?,?,?,?,?,?,?,?,1,'SEEDED')",(mid,technician,transcript,json.dumps(structured),"SEEDED",now(),"en","SEEDED: "+transcript,json.dumps(vector))); kid=cur.lastrowid
    log_action("Demo seed","knowledge.seeded",{"machine_id":mid,"knowledge_id":kid,"provenance":"SEEDED"})
    return {"id":kid,"transcript":transcript,"provider":"SEEDED","provenance":"SEEDED"}

@app.get("/m/{mid}",response_class=HTMLResponse)
def machine_shortcut(mid:int): require_machine(mid); return HTMLResponse((ROOT/"frontend"/"index.html").read_text())
@app.get("/demo",response_class=HTMLResponse)
def guided_demo_page(): return HTMLResponse((ROOT/"frontend"/"index.html").read_text())
@app.get("/metrics",response_class=HTMLResponse)
def metrics_page(): return HTMLResponse((ROOT/"frontend"/"index.html").read_text())
@app.get("/",response_class=HTMLResponse)
def home(): return HTMLResponse((ROOT/"frontend"/"index.html").read_text())
app.mount("/assets",StaticFiles(directory=ROOT/"frontend"),name="assets")
