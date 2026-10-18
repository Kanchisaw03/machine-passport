"""Smoke-test a public Machine Passport deployment without Google authentication."""
from __future__ import annotations
import argparse, json, sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

def request(base,path,method="GET",body=None,headers=None):
    data=None if body is None else json.dumps(body).encode()
    request=Request(base.rstrip("/")+path,data=data,headers={"Content-Type":"application/json",**(headers or {})},method=method)
    with urlopen(request,timeout=25) as response:
        raw=response.read(); return response.status,json.loads(raw) if raw else {}

def smoke(base):
    checks=[]
    def check(name,fn):
        try: detail=fn(); checks.append((name,"PASS",str(detail)))
        except Exception as exc: checks.append((name,"FAIL",f"{type(exc).__name__}: {exc}"))
    demo={}
    # Cloud Run's generated `run.app` host reserves bare /healthz at the edge and
    # returns a Google 404 before the container sees it; probe the app-prefixed
    # equivalent so this verifies the service handler from an anonymous client.
    check("unauthenticated health",lambda:request(base,"/api/healthz")[1].get("status") or (_ for _ in ()).throw(AssertionError("healthz status missing")))
    def seeded_demo():
        _,data=request(base,"/api/demo"); demo.update(data)
        if data.get("provenance")!="SEEDED" or not data.get("baseline") or data.get("baseline",{}).get("n_clips",0)<8: raise AssertionError("Seeded demo baseline is missing or mislabeled")
        return f"machine {data['machine_id']} with {data['baseline']['n_clips']} healthy baseline clips"
    check("SEEDED guided machine",seeded_demo)
    sample={}
    def sample_analysis():
        _,data=request(base,"/api/demo/sample/faulty","POST"); sample.update(data)
        if data.get("provenance")!="SEEDED" or not data.get("analysis",{}).get("status"): raise AssertionError("Sample clip was not labeled or analyzed")
        return f"clip {data['clip_id']} status {data['analysis']['status']}"
    check("sample-clip analysis",sample_analysis)
    recommendation={}
    def make_recommendation():
        if not sample: raise AssertionError("No analyzed sample clip available")
        query=urlencode({"clip_id":sample["clip_id"]})
        _,data=request(base,f"/api/machines/{demo['machine_id']}/recommendations?{query}","POST")
        recommendation.update(data)
        content=data.get("content",{})
        if not data.get("id") or not content.get("provider") or not content.get("recommended_action"): raise AssertionError("Recommendation or provider label missing")
        if "evidence" not in data: raise AssertionError("Evidence list missing")
        return f"recommendation {data['id']} · provider {content['provider']} · {len(data['evidence'])} evidence items"
    check("recommendation",make_recommendation)
    def audit():
        _,data=request(base,"/api/audit/verify")
        if not data.get("valid"): raise AssertionError(f"audit chain invalid: {data}")
        return f"valid · {data['rows']} records"
    check("audit verify",audit)
    return checks

def main():
    parser=argparse.ArgumentParser(); parser.add_argument("base_url",help="Public Cloud Run URL, for example https://service-xyz.run.app"); args=parser.parse_args()
    try: checks=smoke(args.base_url)
    except (HTTPError,URLError,TimeoutError) as exc: checks=[("deployment request","FAIL",f"{type(exc).__name__}: {exc}")]
    print("CHECK | STATUS | DETAIL")
    for name,status,detail in checks: print(f"{name} | {status} | {detail}")
    if any(status=="FAIL" for _,status,_ in checks): return 1
    return 0

if __name__=="__main__": raise SystemExit(main())
