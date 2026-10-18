"""Copy an already downloaded MIMII normal/abnormal tree into the eval layout."""
import argparse, json, shutil
from pathlib import Path

def main():
    p=argparse.ArgumentParser(); p.add_argument('--source',type=Path,required=True,help='Root of an already downloaded/extracted MIMII subset'); p.add_argument('--machine-id',required=True); p.add_argument('--destination',type=Path,default=Path('eval/data')); a=p.parse_args()
    if not a.source.is_dir(): raise SystemExit(f'Not a directory: {a.source}')
    dest=a.destination/a.machine_id; copied=0
    for source_name,target_name in [('normal','healthy'),('abnormal','faulty')]:
        files=[f for d in a.source.rglob(source_name) if d.is_dir() for f in d.rglob('*.wav')]
        target=dest/target_name; target.mkdir(parents=True,exist_ok=True)
        for i,src in enumerate(files):
            out=target/f'{src.parent.parent.name}_{src.parent.name}_{i}_{src.name}'
            shutil.copy2(src,out); copied+=1
        print(f'{source_name}: copied {len(files)} WAV files to {target}')
    if not copied: raise SystemExit('No normal/abnormal WAV folders found. Inspect the extracted dataset layout.')
    (dest/'source.json').write_text(json.dumps({'provenance':'SEEDED','dataset':'operator-supplied MIMII subset','source':'MIMII recordings copied from a manually downloaded machine/ID folder','license':'See the original MIMII dataset license and attribution.'},indent=2),encoding='utf-8')
if __name__=='__main__': main()
