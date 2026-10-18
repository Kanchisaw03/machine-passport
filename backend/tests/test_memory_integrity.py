import json, tempfile
from pathlib import Path
from backend.app import main

def isolated(monkeypatch):
    tmp=tempfile.TemporaryDirectory(dir=main.ROOT); root=Path(tmp.name)
    monkeypatch.setattr(main,'DB',root/'test.sqlite3'); monkeypatch.setattr(main,'AUDIO',root/'audio'); main.AUDIO.mkdir(); main.init_db()
    return tmp

def test_empty_retrieval_is_insufficient_and_audit_tampering_fails(monkeypatch):
    with isolated(monkeypatch):
        mid=main.create_machine(main.MachineIn(name='Empty evidence machine'))['id']
        rec=main.recommend(mid)
        assert rec['validation']['reason']=='insufficient evidence'
        assert rec['content']['confidence']=='low'
        assert 'Insufficient evidence' in rec['content']['recommended_action']
        assert main.audit_verify()['valid'] is True
        with main.connect() as c: c.execute("UPDATE audit_log SET payload='{}' WHERE id=1")
        assert main.audit_verify()['valid'] is False

def test_note_retrieval_ranks_matching_observations(monkeypatch):
    with isolated(monkeypatch):
        mid=main.create_machine(main.MachineIn(name='Pump B'))['id']
        main.knowledge(mid,technician='Expert',transcript='Check bearing housing vibration and coupling alignment before replacement.')
        main.knowledge(mid,technician='Expert',transcript='Inspect paint, corrosion, and weld condition on the frame.')
        results=main.search_knowledge(mid,'bearing vibration alignment',limit=5)['results']
        assert len(results)==1
        assert results[0]['transcript'].startswith('Check bearing')
        assert results[0]['similarity']>0
        assert main.search_knowledge(mid,'unrelated spacecraft telemetry')['results']==[]

def test_audio_note_persists_pending_review_then_requires_confirmation(monkeypatch):
    with isolated(monkeypatch):
        monkeypatch.delenv('GEMINI_API_KEY',raising=False); monkeypatch.setenv('GOOGLE_GENAI_USE_VERTEXAI','false')
        mid=main.create_machine(main.MachineIn(name='Note machine'))['id']
        response=main.save_knowledge_audio(mid,'Ada',None,b'audio bytes','audio/webm')
        assert response.status_code==202
        state=json.loads(response.body)
        with main.connect() as c:
            row=c.execute('SELECT * FROM knowledge WHERE id=?',(state['id'],)).fetchone()
        assert Path(row['audio_path']).read_bytes()==b'audio bytes'
        assert row['human_confirmed']==0
        assert row['provider']=='PENDING'
        assert main.retrieve_knowledge(mid,'unspecified note',confirmed_only=True)==[]
        reviewed=main.review_knowledge(mid,state['id'],main.KnowledgeReview(transcript='Bearing vibration noted',english_text='Bearing vibration noted',confirm=True))
        assert reviewed['human_confirmed'] is True
        assert main.search_knowledge(mid,'bearing vibration')['results'][0]['id']==state['id']
        assert main.search_knowledge(mid,'bearing vibration')['results'][0]['retrieval_mode']=='LOCAL_TFIDF'

def test_feedback_is_stored_and_export_requires_admin_token(monkeypatch):
    with isolated(monkeypatch):
        monkeypatch.delenv('FEEDBACK_ADMIN_TOKEN',raising=False)
        mid=main.create_machine(main.MachineIn(name='Feedback machine'))['id']
        before=main.metrics(); assert 'feedback_summary' not in before
        saved=main.submit_feedback(main.FeedbackIn(useful=False,suggestion='Make uncertainty more visible.',role='technician',machine_id=mid))
        assert saved['stored'] is True
        after=main.metrics(); assert after['feedback_summary']=={'responses':1,'useful_yes':0,'useful_no':1}
        monkeypatch.setenv('FEEDBACK_ADMIN_TOKEN','local-test-token')
        try: main.export_feedback('wrong'); assert False,'incorrect token must be rejected'
        except Exception as exc: assert getattr(exc,'status_code',None)==403
        exported=main.export_feedback('local-test-token')
        assert 'Make uncertainty more visible.' in exported.body.decode()
