# Calibration and repeated evaluation

## Runtime threshold

The scorer computes each clip's distance from healthy baseline medians using robust MAD scales. The absolute RMS feature is downweighted by 0.003 because handset gain is not a reliable condition indicator; spectral-shape and normalized band features carry the remaining score. To calibrate a baseline threshold, the system computes leave-one-out scores using only the healthy training/baseline clips and selects their 95th percentile, floored at 1.0. Fault labels and future check clips are never inputs to threshold calculation.

The 95th percentile is a simple target for screening ordinary healthy variation. It is not a guarantee of 5% false alarms: leave-one-out training scores are dependent, small baselines produce coarse percentiles, and a new phone, placement, speed, or environment can shift the feature distribution. WATCH and ALERT are review priorities, not failure probabilities.

## Evaluation protocol

`python eval/run_eval.py` performs 30 repeated splits by default. On each split, 60% of healthy clips (at least 8 and at most `n-1`) form the baseline; remaining healthy clips are held out. All faulty clips are test-only. For every repeat, the threshold is calibrated from that repeat's healthy baseline only. The script reports mean and standard deviation for ROC AUC, true-positive rate and false-positive rate at the calibrated threshold, plus a deterministic bootstrap 95% confidence interval for the mean across repeats.

Bootstrap intervals resample repeated split metrics. Repeats reuse the same source recordings, so these intervals describe split variability in this dataset and do not establish uncertainty for new machines or sites. Machine sample counts and repeats are included in the report. Multiple machine IDs are evaluated separately.

## Dataset layout

Download and prepare the dataset yourself. Put each public benchmark machine ID in `eval/data/benchmark/<machine-id>/healthy/` and `faulty/`. For MIMII, import a manually extracted machine-specific folder (for example a fan ID or pump ID) using:

```powershell
python scripts/import_mimii.py --source C:\datasets\MIMII\fan\id_00 --machine-id fan_id_00 --destination eval/data/benchmark
```

Repeat for a pump ID. Keep each machine ID separate and retain the upstream dataset's license/attribution. The included fan teaching subset is labeled SEEDED and is only one machine ID; no pump result is claimed until pump clips are supplied.

Put own recordings under `eval/data/user_recordings/<machine-id>/healthy/` and `faulty/`. The app evaluates these separately from benchmark data and labels them REAL by default. Add a `source.json` file if you need to record provenance/conditions. The operator must verify that clip labels and machine identity are correct.
