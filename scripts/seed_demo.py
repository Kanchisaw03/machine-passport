"""Run a demo scenario using only WAV files supplied by the operator."""
import argparse, json, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT))
from backend.app import main

def add_clip(mid,path,kind,label):
    raw=path.read_bytes(); x,sr=main.wav_read(raw); f,q,d=main.analyze(x,sr); dest=main.AUDIO/f"m{mid}_{path.stem}.wav"
    dest.write_bytes(raw)
    with main.connect() as c:
        cur=c.execute("INSERT INTO clips(machine_id,kind,path,duration,sample_rate,quality,features,label,provenance,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",(mid,kind,str(dest),d,sr,json.dumps(q),json.dumps(f),label,"SEEDED",main.now())); cid=cur.lastrowid
    return cid

def main_seed(folder):
    healthy=sorted((folder/'healthy').glob('*.wav')); faulty=sorted((folder/'faulty').glob('*.wav'))
    if len(healthy)<8 or not faulty: raise SystemExit('Provide at least 8 healthy and 1 faulty WAV under healthy/ and faulty/.')
    machine=main.create_machine(main.MachineIn(name='Demo machine',type='User supplied audio',location='Demo area',placement_note='Seeded from user-supplied WAV clips; verify consistent placement.'))
    mid=machine['id']; print(f"Created machine {mid} from supplied clips.")
    for path in healthy[:8]: add_clip(mid,path,'baseline','healthy')
    baseline=main.build_baseline(mid); print(f"Baseline version {baseline['version']} built from {baseline['n_clips']} clips.")
    check=add_clip(mid,faulty[0],'check','faulty'); score=main.score_clip(mid,check); print(f"Check status {score['status']} (score {score['score']}, threshold {score['threshold']}).")
    notes=[
        'SEEDED: inspect coupling alignment and lubrication before replacing a bearing; verify the finding during inspection.',
        'SEEDED: compare the sound at the same operating speed and mounting location before escalating.',
        'SEEDED: confirm guards are secure and check for visible looseness using site safety procedures.',
    ]
    # Mark synthetic scenario records distinctly from measurements captured live.
    for text in notes:
        note=main.knowledge(mid,technician='Demo veteran (seeded)',transcript=text)
        with main.connect() as c: c.execute("UPDATE knowledge SET provenance='SEEDED' WHERE id=?",(note['id'],))
    rec=main.recommend(mid,check); print(f"Recommendation {rec['id']} created with {len(rec['evidence'])} stored evidence records.")
    main.decide(rec['id'],main.Decision(action='approve',comment='Seed scenario approval',decided_by='Demo operator'))
    main.outcome(rec['id'],main.Outcome(actual_cause='Recorded demo outcome; validate against supplied clip context',action_taken='Inspection logged for demo',notes='SEEDED scenario record'))
    print(f"Scenario complete. Open http://localhost:8000/m/{mid}")

if __name__=='__main__':
    p=argparse.ArgumentParser(); p.add_argument('--data',required=True,type=Path); args=p.parse_args(); main_seed(args.data)
