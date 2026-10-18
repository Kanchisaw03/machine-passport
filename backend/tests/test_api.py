import io, wave
import tempfile
from pathlib import Path
import numpy as np
from backend.app import main

def pcm_wav(seed=0, tone=False):
    sr=16000; t=np.arange(sr*5)/sr; rng=np.random.default_rng(seed)
    x=.04*rng.normal(size=len(t))
    if tone: x += .25*np.sin(2*np.pi*3200*t)
    b=io.BytesIO()
    with wave.open(b,'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr); w.writeframes(np.clip(x*32767,-32768,32767).astype('<i2').tobytes())
    return b.getvalue()

def test_machine_baseline_recommendation_and_audit(monkeypatch):
    with tempfile.TemporaryDirectory(dir=main.ROOT) as tmp:
        temp=Path(tmp)
        monkeypatch.setattr(main,'DB',temp/'test.sqlite3'); monkeypatch.setattr(main,'AUDIO',temp/'audio'); main.AUDIO.mkdir(); main.init_db()
        mid=main.create_machine(main.MachineIn(name='Pump A',type='Pump',location='Bay 1'))['id']
        try: main.build_baseline(mid); assert False, 'baseline minimum must be enforced'
        except Exception as e: assert 'requires 8' in str(e)
        for i in range(8):
            x,sr=main.wav_read(pcm_wav(i)); f,q,d=main.analyze(x,sr)
            with main.connect() as c: c.execute('INSERT INTO clips(machine_id,kind,path,duration,sample_rate,quality,features,label,provenance,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)',(mid,'baseline','',d,sr,__import__('json').dumps(q),__import__('json').dumps(f),'healthy','REAL',main.now()))
        assert main.build_baseline(mid)['n_clips']==8
        x,sr=main.wav_read(pcm_wav(99,True)); f,q,d=main.analyze(x,sr)
        with main.connect() as c:
            cur=c.execute('INSERT INTO clips(machine_id,kind,path,duration,sample_rate,quality,features,label,provenance,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)',(mid,'check','',d,sr,__import__('json').dumps(q),__import__('json').dumps(f),'unknown','REAL',main.now())); clip_id=cur.lastrowid
        scored=main.score_clip(mid,clip_id); assert scored['status'] in ('WATCH','ALERT')
        rec=main.recommend(mid,clip_id); assert rec['status']=='pending' and rec['evidence']
        assert main.decide(rec['id'],main.Decision(action='approve'))['status']=='approved'
        assert main.audit_verify()['valid'] is True

def test_guided_demo_seed_is_idempotent_and_sample_check_uses_scorer(monkeypatch):
    with tempfile.TemporaryDirectory(dir=main.ROOT) as tmp:
        temp=Path(tmp); monkeypatch.setattr(main,'DB',temp/'test.sqlite3'); monkeypatch.setattr(main,'AUDIO',temp/'audio'); main.AUDIO.mkdir(); main.init_db()
        mid=main.ensure_demo_machine()
        again=main.ensure_demo_machine()
        assert mid==again
        state=main.machine(mid)
        assert len([c for c in state['clips'] if c['kind']=='baseline' and c['label']=='healthy'])==8
        assert len(state['knowledge'])==3 and all(k['provenance']=='SEEDED' for k in state['knowledge'])
        assert state['baseline'] and __import__('json').loads(state['baseline']['quality'])['provenance']=='SEEDED'
        sample=main.demo_sample('faulty')
        assert sample['provenance']=='SEEDED' and sample['analysis']['status'] in ('OK','WATCH','ALERT')

def test_machine_deletion_requires_owner_token(monkeypatch):
    with tempfile.TemporaryDirectory(dir=main.ROOT) as tmp:
        temp=Path(tmp); monkeypatch.setattr(main,'DB',temp/'test.sqlite3'); monkeypatch.setattr(main,'AUDIO',temp/'audio'); main.AUDIO.mkdir(); main.init_db()
        created=main.create_machine(main.MachineIn(name='Delete-me test'))
        try: main.delete_my_machine_data(created['id'],'wrong-token'); assert False,'wrong token must be refused'
        except Exception as exc: assert getattr(exc,'status_code',None)==403
        assert main.delete_my_machine_data(created['id'],created['delete_token'])['deleted'] is True

def test_request_rate_limit_is_per_forwarded_ip(monkeypatch):
    monkeypatch.setenv('REQUESTS_PER_IP_PER_MINUTE','1'); main.rate_windows.clear()
    assert main.rate_limited('198.51.100.9',tick=10) is False
    assert main.rate_limited('198.51.100.9',tick=20) is True
    assert main.rate_limited('198.51.100.10',tick=20) is False
    assert main.rate_limited('198.51.100.9',tick=71) is False
