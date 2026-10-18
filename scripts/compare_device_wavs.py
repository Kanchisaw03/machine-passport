"""Compare analysis scores for two recordings believed to contain the same source."""
from __future__ import annotations
import argparse
import json
import sys
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.app.main import analyze, FEATURES, FEATURE_WEIGHTS  # noqa: E402


def load(path: Path):
    with wave.open(str(path), "rb") as wav:
        channels, width, rate, frames = wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getnframes()
        if width != 2:
            raise ValueError(f"{path}: expected PCM16 WAV")
        raw = np.frombuffer(wav.readframes(frames), dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        raw = raw.reshape(-1, channels).mean(axis=1)
    return rate, raw


def features(path: Path):
    rate, samples = load(path)
    feat, quality, duration = analyze(samples, rate)
    return rate, duration, feat, quality


def score(feat, median, mad):
    z = np.abs((np.array([feat[k] for k in FEATURES]) - median) / mad) * FEATURE_WEIGHTS
    n = max(1, int(np.ceil(.2 * len(z))))
    return float(np.sqrt(np.mean(np.sort(z)[-n:] ** 2)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("healthy_folder", type=Path, help="At least eight healthy calibration WAVs, recorded at the reference device/placement")
    parser.add_argument("first", type=Path, help="First PCM16 WAV")
    parser.add_argument("second", type=Path, help="Second PCM16 WAV of the same source")
    args = parser.parse_args()
    healthy_paths = sorted(args.healthy_folder.glob("*.wav"))
    if len(healthy_paths) < 8:
        parser.error("healthy_folder must contain at least eight healthy reference WAVs")
    reference = [features(path)[2] for path in healthy_paths]
    matrix = np.array([[record[key] for key in FEATURES] for record in reference])
    median = np.median(matrix, axis=0)
    mad = np.maximum(1.4826 * np.median(np.abs(matrix - median), axis=0), 1e-6)
    a_data, b_data = features(args.first), features(args.second)
    a_score, b_score = score(a_data[2], median, mad), score(b_data[2], median, mad)
    a = {"path": str(args.first), "sample_rate_hz": a_data[0], "duration_s": round(a_data[1], 3), "score": round(a_score, 6), "quality": a_data[3]}
    b = {"path": str(args.second), "sample_rate_hz": b_data[0], "duration_s": round(b_data[1], 3), "score": round(b_score, 6), "quality": b_data[3]}
    difference = abs(a_score - b_score)
    print(json.dumps({"comparison": "same-source cross-device sanity check; reference healthy clips calibrate the comparison; source equivalence is user asserted",
                      "first": a, "second": b, "absolute_score_difference": round(difference, 6)}, indent=2))


if __name__ == "__main__":
    main()
