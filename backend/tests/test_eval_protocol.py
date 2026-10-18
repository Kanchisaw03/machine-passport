import inspect, tempfile, wave
from pathlib import Path
import numpy as np
from eval.run_eval import calibrate_threshold, evaluate_machine, run

def write_wav(path:Path,freq:float,seed:int):
    sr=16000; t=np.arange(sr*6)/sr
    x=.03*np.random.default_rng(seed).normal(size=len(t))+.10*np.sin(2*np.pi*freq*t)
    pcm=np.clip(x*32767,-32768,32767).astype('<i2').tobytes()
    path.parent.mkdir(parents=True,exist_ok=True)
    with wave.open(str(path),'wb') as out: out.setnchannels(1); out.setsampwidth(2); out.setframerate(sr); out.writeframes(pcm)

def test_threshold_api_has_only_training_features_input():
    assert list(inspect.signature(calibrate_threshold).parameters)==['healthy_training_features']
    training=np.arange(8*len(__import__('backend.app.main',fromlist=['FEATURES']).FEATURES),dtype=float).reshape(8,-1)+1
    first=calibrate_threshold(training)
    # Arbitrary changes to held-out labels/features cannot enter a threshold call.
    heldout_labels=np.array([0,1,0,1]); heldout_labels[:]=1-heldout_labels
    second=calibrate_threshold(training.copy())
    assert first==second and heldout_labels.tolist()==[1,0,1,0]

def test_repeated_protocol_and_user_recording_partition():
    with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2]) as temp:
        root=Path(temp); machine=root/'user_recordings'/'own_fan'
        for i in range(10): write_wav(machine/'healthy'/f'h{i}.wav',440+(i%2)*15,i)
        for i in range(3): write_wav(machine/'faulty'/f'f{i}.wav',880+i*20,100+i)
        output=run(root,repeats=30)
        assert output['available'] is True
        assert output['benchmark']==[]
        assert len(output['user_recordings'])==1
        result=output['user_recordings'][0]
        assert result['repeats']==30 and result['threshold']['train_only'] is True
        assert result['sample_sizes']['healthy_test_predictions_across_repeats']==30*2
        assert len(result['metrics']['roc_auc']['bootstrap_95_ci_of_mean'])==2
        assert result['evaluation_type']=='USER_RECORDINGS'

def test_train_only_calibration_ignores_fault_files():
    with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2]) as temp:
        machine=Path(temp)/'fan_id_06'
        for i in range(10): write_wav(machine/'healthy'/f'h{i}.wav',440+(i%2)*20,i)
        for i in range(2): write_wav(machine/'faulty'/f'f{i}.wav',800+i*50,200+i)
        result=evaluate_machine(machine,'BENCHMARK',repeats=30)
        assert result['threshold']['train_only'] is True
        assert result['threshold']['rule'].startswith('95th percentile of leave-one-out healthy training scores')
        assert result['metrics']['tpr_at_calibrated_threshold']['std']>=0
        original_threshold=result['threshold']['mean']
        # Change every test-only fault file; calibration must remain unchanged.
        for i in range(2): write_wav(machine/'faulty'/f'f{i}.wav',1800+i*50,300+i)
        changed_faults=evaluate_machine(machine,'BENCHMARK',repeats=30)
        assert changed_faults['threshold']['mean']==original_threshold
