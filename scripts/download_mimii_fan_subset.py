"""Download the small public MIMII fan id_06 teaching subset used for evaluation."""
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
from urllib.request import Request, urlopen

ROOT=Path(__file__).resolve().parents[1]
REPO="WSU-AI-in-ME/ai-in-me-1"
DEST=ROOT/"eval"/"data"/"mimii_fan_id_06"

def get(url):
    req=Request(url,headers={"User-Agent":"machine-passport-eval/1.0"})
    with urlopen(req,timeout=90) as response: return response.read()

def main():
    tree=json.loads(get(f"https://api.github.com/repos/{REPO}/git/trees/main?recursive=1"))
    selection=[]
    for entry in tree.get("tree",[]):
        path=entry.get("path","")
        if path.startswith("data/mimii/audio/fan/id_06/normal/") and path.endswith(".wav"):
            selection.append(("healthy",path,entry["size"]))
        elif path.startswith("data/mimii/audio/fan/id_06/anomaly/") and path.endswith(".wav"):
            selection.append(("faulty",path,entry["size"]))
    if len([x for x in selection if x[0]=="healthy"])<8 or not any(x[0]=="faulty" for x in selection):
        raise SystemExit("Expected normal and anomaly MIMII fan id_06 WAV files were not found.")
    DEST.mkdir(parents=True,exist_ok=True)
    def fetch(item):
        label,path,size=item; target=DEST/label/Path(path).name; target.parent.mkdir(parents=True,exist_ok=True)
        if target.exists() and target.stat().st_size==size: return target
        data=get(f"https://raw.githubusercontent.com/{REPO}/main/{path}")
        if len(data)!=size: raise IOError(f"Size mismatch for {path}: expected {size}, got {len(data)}")
        target.write_bytes(data); return target
    done=[]
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures=[pool.submit(fetch,item) for item in selection]
        for future in as_completed(futures): done.append(future.result())
    license_path=DEST/"LICENSE_DATA.txt"
    license_path.write_bytes(get(f"https://raw.githubusercontent.com/{REPO}/main/data/mimii/LICENSE_DATA.txt"))
    source={"provenance":"SEEDED","dataset":"MIMII public 1.0, fan id_06 course teaching subset","source":f"https://github.com/{REPO}/tree/main/data/mimii","original_dataset":"https://zenodo.org/records/3384388","license":"CC BY-SA 4.0; see LICENSE_DATA.txt","selection":"50 normal and 20 anomaly recordings, mono PCM WAV at 16 kHz, 10 seconds each","downloaded_files":len(done)}
    (DEST/"source.json").write_text(json.dumps(source,indent=2),encoding="utf-8")
    print(f"Downloaded {len(done)} WAV files: {sum(x[0]=='healthy' for x in selection)} healthy, {sum(x[0]=='faulty' for x in selection)} faulty")
    print(f"Destination: {DEST}")
    print("Provenance: SEEDED (public benchmark recordings; not a recording of the operator's machine).")

if __name__=="__main__": main()
