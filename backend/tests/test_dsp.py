import io, wave
import numpy as np
import pytest
from backend.app.main import analyze, FEATURES, FEATURE_WEIGHTS, wav_read

def wav(duration=6, sr=16000, amplitude=.04, tone=None):
    t=np.arange(int(duration*sr))/sr
    x=amplitude*np.random.default_rng(7).normal(size=len(t))
    if tone: x += .12*np.sin(2*np.pi*tone*t)
    return x,sr

def test_features_are_measured_and_named():
    f,q,d=analyze(*wav())
    assert f['rms']>0 and q['duration_s']==d==6
    assert 'band_2000_4000_db' in f and 'psd' in f

def test_reject_quiet_and_clipped():
    with pytest.raises(ValueError,match='quiet'): analyze(*wav(amplitude=.0001))
    with pytest.raises(ValueError,match='clipped'): analyze(*wav(amplitude=.7,tone=1000))

def test_duration_gate():
    with pytest.raises(ValueError,match='5 to 60'): analyze(*wav(duration=2))

def test_supported_sample_rates_are_resampled_to_canonical_rate():
    for sr in (16000,22050,44100,48000):
        t=np.arange(sr*6)/sr
        f,q,d=analyze(.1*np.sin(2*np.pi*440*t),sr)
        assert d==pytest.approx(6)
        assert q['original_sample_rate']==sr and q['analysis_sample_rate']==16000
        assert abs(f['spectral_centroid_hz']-440)<200

def test_dc_offset_is_rejected_and_broadband_noise_is_flagged():
    x,sr=wav(tone=440)
    with pytest.raises(ValueError,match='DC offset'):
        analyze(x+.04,sr)
    noise=np.random.default_rng(123).normal(0,.08,size=sr*6)
    _,quality,_=analyze(noise,sr)
    assert 'broadband-noise estimate' in quality['background_noise_estimate']

def test_gain_change_has_low_effect_on_weighted_anomaly_score():
    x,sr=wav(amplitude=.04,tone=440)
    base_features=analyze(x,sr)[0]
    med=np.array([base_features[k] for k in FEATURES]); mad=np.maximum(np.abs(med)*.02,1e-5)
    scores=[]
    for gain in (.3,1,3):
        features=analyze(x*gain,sr)[0]
        z=np.abs((np.array([features[k] for k in FEATURES])-med)/mad)*FEATURE_WEIGHTS
        k=max(1,int(np.ceil(.2*len(z))))
        scores.append(float(np.sqrt(np.mean(np.sort(z)[-k:]**2))))
    assert max(scores)-min(scores)<.25

def test_analyzer_rejects_unsupported_rate():
    x,sr=wav(sr=11025)
    with pytest.raises(ValueError,match='16, 22.05, 44.1, or 48'):
        analyze(x,sr)
