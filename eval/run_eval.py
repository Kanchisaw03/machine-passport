"""Repeated train-only calibration evaluation on supplied healthy/faulty WAV folders."""
import argparse, json, sys
from pathlib import Path
from datetime import datetime
import numpy as np

ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT))
from backend.app.main import analyze, wav_read, FEATURES, FEATURE_WEIGHTS

REPEATS=30
def robust_stats(base):
    med=np.median(base,axis=0)
    mad=np.maximum(1.4826*np.median(abs(base-med),axis=0),np.maximum(abs(med)*.02,1e-5))
    return med,mad

def anomaly_score(row,med,mad):
    z=abs((row-med)/mad)*FEATURE_WEIGHTS
    k=max(1,int(np.ceil(.2*len(z))))
    return float(np.sqrt(np.mean(np.sort(z)[-k:]**2)))

def loo_scores(base):
    values=[]
    for i,row in enumerate(base):
        med,mad=robust_stats(np.delete(base,i,axis=0))
        values.append(anomaly_score(row,med,mad))
    return values

def calibrate_threshold(healthy_training_features):
    """The threshold API intentionally accepts healthy training data only."""
    return max(float(np.percentile(loo_scores(np.asarray(healthy_training_features)),95)),1.0)

def roc_auc(y,scores):
    y=np.asarray(y,dtype=int); scores=np.asarray(scores,dtype=float)
    positives=int(y.sum()); negatives=len(y)-positives
    if not positives or not negatives: return float("nan")
    points=[]
    for threshold in np.r_[np.inf,np.unique(scores)[::-1]]:
        pred=scores>=threshold
        points.append((float(np.sum((y==0)&pred)/negatives),float(np.sum((y==1)&pred)/positives)))
    points=sorted(set(points))
    return float(np.trapezoid([p[1] for p in points],[p[0] for p in points]))

def bootstrap_mean_ci(values,seed):
    values=np.asarray(values,dtype=float); rng=np.random.default_rng(seed)
    samples=rng.choice(values,size=(5000,len(values)),replace=True).mean(axis=1)
    return [float(np.percentile(samples,2.5)),float(np.percentile(samples,97.5))]

def summarize(values,seed):
    values=np.asarray(values,dtype=float)
    return {"mean":float(values.mean()),"std":float(values.std(ddof=1)) if len(values)>1 else 0.0,"bootstrap_95_ci_of_mean":bootstrap_mean_ci(values,seed)}

def source_metadata(machine_dir,evaluation_type):
    source_file=machine_dir/"source.json"
    if source_file.is_file():
        source=json.loads(source_file.read_text(encoding="utf-8"))
        provenance=source.get("provenance","SEEDED" if evaluation_type=="BENCHMARK" else "REAL")
    else:
        source={"source":"Operator-supplied recordings" if evaluation_type=="USER_RECORDINGS" else "User-supplied benchmark folder"}
        provenance="REAL" if evaluation_type=="USER_RECORDINGS" else "SEEDED"
    return source,provenance

def evaluate_machine(machine_dir:Path,evaluation_type:str,repeats:int=REPEATS,seed:int=20261018):
    healthy_files=sorted((machine_dir/"healthy").glob("*.wav")); faulty_files=sorted((machine_dir/"faulty").glob("*.wav"))
    if len(healthy_files)<8 or not faulty_files: return None
    healthy=[]; faulty=[]; skipped=[]
    for path,dest in [(p,healthy) for p in healthy_files]+[(p,faulty) for p in faulty_files]:
        try:
            x,sr=wav_read(path.read_bytes()); features,_,_=analyze(x,sr); dest.append([features[k] for k in FEATURES])
        except ValueError as exc:
            skipped.append({"file":path.name,"reason":str(exc)})
    if len(healthy)<8 or not faulty: return None
    healthy=np.asarray(healthy); faulty=np.asarray(faulty); n_h=len(healthy); n_f=len(faulty)
    train_n=min(max(8,int(np.floor(n_h*.6))),n_h-1)
    rng=np.random.default_rng(seed); aucs=[]; tprs=[]; fprs=[]; thresholds=[]; confusion=[]
    for _ in range(repeats):
        order=rng.permutation(n_h); train=healthy[order[:train_n]]; healthy_test=healthy[order[train_n:]]
        threshold=calibrate_threshold(train) # no faulty/test features or labels enter this call
        med,mad=robust_stats(train)
        scores_h=np.asarray([anomaly_score(x,med,mad) for x in healthy_test])
        scores_f=np.asarray([anomaly_score(x,med,mad) for x in faulty])
        y=np.r_[np.zeros(len(scores_h),dtype=int),np.ones(len(scores_f),dtype=int)]
        scores=np.r_[scores_h,scores_f]; pred=scores>=threshold
        tn=int(np.sum((y==0)&~pred)); fp=int(np.sum((y==0)&pred)); fn=int(np.sum((y==1)&~pred)); tp=int(np.sum((y==1)&pred))
        aucs.append(roc_auc(y,scores)); tprs.append(tp/max(1,tp+fn)); fprs.append(fp/max(1,fp+tn)); thresholds.append(threshold); confusion.append((tn,fp,fn,tp))
    source,provenance=source_metadata(machine_dir,evaluation_type)
    return {"machine_id":machine_dir.name,"evaluation_type":evaluation_type,"provenance":provenance,"source":source.get("source"),"dataset":source.get("dataset"),"repeats":repeats,"random_seed":seed,"sample_sizes":{"healthy_total":n_h,"faulty_total":n_f,"healthy_baseline_per_repeat":train_n,"healthy_test_per_repeat":n_h-train_n,"faulty_test_per_repeat":n_f,"healthy_test_predictions_across_repeats":repeats*(n_h-train_n),"faulty_test_predictions_across_repeats":repeats*n_f},"metrics":{"roc_auc":summarize(aucs,seed+1),"tpr_at_calibrated_threshold":summarize(tprs,seed+2),"fpr_at_calibrated_threshold":summarize(fprs,seed+3)},"threshold":{"rule":"95th percentile of leave-one-out healthy training scores; minimum 1.0","mean":float(np.mean(thresholds)),"std":float(np.std(thresholds,ddof=1)) if len(thresholds)>1 else 0.0,"train_only":True},"pooled_confusion_across_repeat_predictions":{"tn":int(sum(c[0] for c in confusion)),"fp":int(sum(c[1] for c in confusion)),"fn":int(sum(c[2] for c in confusion)),"tp":int(sum(c[3] for c in confusion))},"skipped_files":skipped}

def discover(root:Path):
    found=[]
    for name in ("benchmark","user_recordings"):
        parent=root/name
        if parent.is_dir():
            kind="USER_RECORDINGS" if name=="user_recordings" else "BENCHMARK"
            found.extend((child,kind) for child in sorted(parent.iterdir()) if child.is_dir())
    for child in sorted(root.iterdir()):
        if child.is_dir() and child.name not in ("benchmark","user_recordings") and (child/"healthy").is_dir(): found.append((child,"BENCHMARK"))
    return found

def run(root:Path,repeats:int=REPEATS):
    out={"available":False,"generated_at":datetime.now().astimezone().isoformat(),"protocol":{"repeats":repeats,"split":"healthy clips randomly split 60/40 per repeat; faulty clips are test-only","threshold":"training healthy clips only: leave-one-out 95th percentile, floor 1.0","confidence_intervals":"bootstrap 95% interval of mean across repeated split metrics; splits share recordings and are not independent population samples"},"benchmark":[],"user_recordings":[],"requirements":{"fan_available":False,"pump_available":False}}
    for directory,kind in discover(root):
        result=evaluate_machine(directory,kind,repeats)
        if result is None: continue
        target=out["user_recordings"] if kind=="USER_RECORDINGS" else out["benchmark"]
        target.append(result)
        if kind=="BENCHMARK":
            label=(directory.name+" "+str(result.get("dataset") or "")).lower()
            if "fan" in label: out["requirements"]["fan_available"]=True
            if "pump" in label: out["requirements"]["pump_available"]=True
    out["available"]=bool(out["benchmark"] or out["user_recordings"])
    # Compatibility alias for clients built before benchmark/user sections existed.
    out["machines"]=out["benchmark"]
    return out

def fmt(value):
    ci=value["bootstrap_95_ci_of_mean"]
    return f"{value['mean']:.3f} ± {value['std']:.3f} (bootstrap 95% CI {ci[0]:.3f}–{ci[1]:.3f})"

def report(out):
    lines=["# Machine Passport repeated evaluation","",f"Generated: {out['generated_at']}",f"Protocol: {out['protocol']['repeats']} repeats; {out['protocol']['split']}; {out['protocol']['threshold']}.","Confidence intervals bootstrap the mean across repeats. Repeated splits share source recordings and do not estimate transfer to new machines.",""]
    for section,title in (("benchmark","Public benchmark data"),("user_recordings","Own-machine recordings")):
        lines += [f"## {title}",""]
        machines=out[section]
        if not machines: lines += ["No eligible recordings available.",""]; continue
        for m in machines:
            lines += [f"### {m['machine_id']} — {m['provenance']}",f"Source: {m.get('source') or 'operator supplied'}",f"Samples: {m['sample_sizes']['healthy_total']} healthy, {m['sample_sizes']['faulty_total']} faulty; {m['repeats']} repeats; baseline {m['sample_sizes']['healthy_baseline_per_repeat']}/split, healthy test {m['sample_sizes']['healthy_test_per_repeat']}/split, all faulty clips test-only.",f"ROC AUC: {fmt(m['metrics']['roc_auc'])}",f"TPR at calibrated threshold: {fmt(m['metrics']['tpr_at_calibrated_threshold'])}",f"FPR at calibrated threshold: {fmt(m['metrics']['fpr_at_calibrated_threshold'])}",f"Mean threshold: {m['threshold']['mean']:.4f} ± {m['threshold']['std']:.4f}; train-only={m['threshold']['train_only']}",f"Pooled repeat confusion counts TN/FP/FN/TP: {m['pooled_confusion_across_repeat_predictions']}",""]
    if not out["user_recordings"]: lines += ["No own-machine evaluation yet.",""]
    if not out["requirements"]["pump_available"]: lines += ["Pump benchmark not available in the supplied folders; it was not evaluated.",""]
    lines += ["All dataset metrics are clip-level and do not establish field accuracy, alarm rates for new machines, or detection lead time."]
    return "\n".join(lines)

if __name__=="__main__":
    parser=argparse.ArgumentParser(); parser.add_argument("--data",type=Path,default=ROOT/"eval"/"data"); parser.add_argument("--repeats",type=int,default=REPEATS); args=parser.parse_args()
    if args.repeats<30: parser.error("Use at least 30 repeats.")
    output=run(args.data,args.repeats); dest=ROOT/"eval"/"results"; dest.mkdir(exist_ok=True)
    (dest/"latest.json").write_text(json.dumps(output,indent=2),encoding="utf-8"); rendered=report(output); (dest/"latest.md").write_text(rendered,encoding="utf-8"); print(rendered)
