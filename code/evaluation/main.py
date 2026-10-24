"""Evaluate predictions against the public sample answers (and any extra labelled cases).

    python code/evaluation/main.py                    # runs the agent on the 32 sample requests
    python code/evaluation/main.py --pred sample_pred.csv --gold dataset/sample_output.csv

Metrics
- case status accuracy           : deletion_status exact match
- step precision / recall / F1   : steps matched as (phase, system_id, resource_id, operation, state, evidence_id)
- field accuracy                 : per field, over gold steps aligned to predicted steps by (phase, resource, operation)
- sequence accuracy              : the full steps_json array is identical (order included)
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "code"))

FIELDS = ["step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id"]


def load(path: Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        return {r["case_id"]: (r["deletion_status"], json.loads(r["steps_json"])) for r in csv.DictReader(fh)}


def key(s, with_evidence=True):
    k = (s["phase"], s["system_id"], s["resource_id"], s["operation"], s["state"])
    return k + ((s.get("evidence_id", ""),) if with_evidence else ())


def score(pred: dict, gold: dict) -> dict:
    n = len(gold)
    status_ok = 0
    tp = fp = fn = 0
    tp_ne = fp_ne = fn_ne = 0
    seq_ok = 0
    field_hits, field_tot = Counter(), Counter()
    per_case = []
    for cid, (gs, gsteps) in sorted(gold.items()):
        ps, psteps = pred.get(cid, ("<missing>", []))
        status_ok += ps == gs
        G = Counter(key(s) for s in gsteps)
        P = Counter(key(s) for s in psteps)
        t = sum((G & P).values())
        tp, fp, fn = tp + t, fp + sum(P.values()) - t, fn + sum(G.values()) - t
        G2 = Counter(key(s, False) for s in gsteps)
        P2 = Counter(key(s, False) for s in psteps)
        t2 = sum((G2 & P2).values())
        tp_ne, fp_ne, fn_ne = tp_ne + t2, fp_ne + sum(P2.values()) - t2, fn_ne + sum(G2.values()) - t2
        exact = [{k: s[k] for k in FIELDS} for s in psteps] == [{k: s[k] for k in FIELDS} for s in gsteps]
        seq_ok += exact
        pmap = {(s["phase"], s["resource_id"], s["operation"]): s for s in psteps}
        for gstep in gsteps:
            p = pmap.get((gstep["phase"], gstep["resource_id"], gstep["operation"]))
            for f in FIELDS:
                field_tot[f] += 1
                field_hits[f] += bool(p) and p.get(f) == gstep.get(f)
        per_case.append({"case_id": cid, "gold": gs, "pred": ps, "exact": exact,
                         "missing": sorted(set(map(str, (G - P).elements()))),
                         "extra": sorted(set(map(str, (P - G).elements())))})
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    prec2 = tp_ne / (tp_ne + fp_ne) if tp_ne + fp_ne else 0.0
    rec2 = tp_ne / (tp_ne + fn_ne) if tp_ne + fn_ne else 0.0
    f1 = lambda p, r: 2 * p * r / (p + r) if p + r else 0.0  # noqa: E731
    return {
        "cases": n,
        "status_accuracy": status_ok / n if n else 0,
        "step_precision": prec, "step_recall": rec, "step_f1": f1(prec, rec),
        "step_f1_ignoring_evidence": f1(prec2, rec2),
        "sequence_accuracy": seq_ok / n if n else 0,
        "field_accuracy": {f: field_hits[f] / field_tot[f] for f in FIELDS if field_tot[f]},
        "per_case": per_case,
    }


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--pred", default=None)
    ap.add_argument("--gold", default=str(ROOT / "dataset" / "sample_output.csv"))
    ap.add_argument("--requests", default=str(ROOT / "dataset" / "sample_deletion_requests.csv"))
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args(argv)
    if args.pred:
        pred = load(Path(args.pred))
    else:
        import main as agent  # code/main.py

        reqs = agent.load_requests(Path(args.requests))
        all_reqs = reqs + agent.load_requests(ROOT / "dataset" / "deletion_requests.csv")
        _, results = agent.run(reqs, all_requests=all_reqs)
        pred = {r.case_id: (r.status, r.steps) for r in results}
    res = score(pred, load(Path(args.gold)))
    print(json.dumps({k: v for k, v in res.items() if k != "per_case"}, indent=1))
    for c in res["per_case"]:
        if not c["exact"] and (args.verbose or c["gold"] != c["pred"]):
            print(f"\n{c['case_id']}: gold={c['gold']} pred={c['pred']}")
            for m in c["missing"]:
                print("   missing", m)
            for e in c["extra"]:
                print("   extra  ", e)
    return res


if __name__ == "__main__":
    main()
