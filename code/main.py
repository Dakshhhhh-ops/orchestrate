"""Ghost Data Still Exists - deletion audit agent.

Usage (from the repository root):
    python code/main.py                       # audit dataset/deletion_requests.csv -> ./output.csv
    python code/main.py --requests dataset/sample_deletion_requests.csv --output sample_pred.csv
    python code/main.py --refresh-vision      # re-read screenshots with the vision model

Set ANTHROPIC_API_KEY to let Claude classify request intent and read screenshots; without it the
agent uses its cached model outputs and deterministic fallbacks, so a run never fails for lack of a key.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ghost_audit import intent as intent_mod  # noqa: E402
from ghost_audit import llm  # noqa: E402
from ghost_audit.audit import World, assign_run_resources, audit_case, discover  # noqa: E402
from ghost_audit.common import parse_ts  # noqa: E402


def natural_key(case_id: str):
    return [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", case_id)]


def load_requests(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def run(requests: list[dict], world: World | None = None, dataset: Path = ROOT / "dataset",
        refresh_vision: bool = False, all_requests: list[dict] | None = None):
    world = world or World(dataset, refresh_vision=refresh_vision)
    intents = intent_mod.classify(requests)
    # run-window assignment needs every request that may compete for the same deletion run
    pool = all_requests or requests
    subjects = {}
    for r in pool:
        s = world.idx.resolve_email(r["email"])
        if s:
            subjects[r["case_id"]] = s
    pool_intents = intent_mod.classify(pool) if pool is not requests else intents
    del_pool = [r for r in pool if pool_intents[r["case_id"]]["label"] == "DELETION"]
    direct = {r["case_id"]: discover(world, subjects[r["case_id"]], r, parse_ts(r["audit_at"]))
              for r in del_pool if r["case_id"] in subjects}
    run_assign = assign_run_resources(world, del_pool, subjects, direct)
    results = []
    for r in requests:
        cid = r["case_id"]
        results.append(audit_case(world, r, subjects.get(cid), intents[cid], direct.get(cid, {}),
                                  run_assign.get(cid, set())))
    results.sort(key=lambda x: natural_key(x.case_id))
    return world, results


def write_output(results, path: Path):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["case_id", "deletion_status", "steps_json"])
        for r in results:
            w.writerow([r.case_id, r.status, json.dumps(r.steps, ensure_ascii=False, separators=(",", ":"))])


def write_trace(results, path: Path):
    rows = []
    for r in results:
        rows.append({
            "case_id": r.case_id, "status": r.status, "subject": r.subject, "intent": r.intent,
            "resources": [{"rid": s.rid, "system": s.system, "category": s.category, "action": s.action,
                           "present": s.present, "exposed": s.exposed, "enabled": s.enabled, "restore": s.restore,
                           "evidence": s.evidence, "violation": s.violation, "linked_by": sorted(s.how_linked),
                           "notes": s.notes} for s in r.resources],
            "trace": r.trace,
        })
    path.write_text(json.dumps(rows, indent=1, ensure_ascii=False), encoding="utf-8")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", default=str(ROOT / "dataset"))
    ap.add_argument("--requests", default=None, help="requests CSV (default: <dataset>/deletion_requests.csv)")
    ap.add_argument("--output", default=str(ROOT / "output.csv"))
    ap.add_argument("--trace", default=None, help="optional JSON file with per-case reasoning")
    ap.add_argument("--refresh-vision", action="store_true")
    args = ap.parse_args(argv)
    dataset = Path(args.dataset)
    req_path = Path(args.requests) if args.requests else dataset / "deletion_requests.csv"
    t0 = time.time()
    requests = load_requests(req_path)
    world, results = run(requests, dataset=dataset, refresh_vision=args.refresh_vision)
    write_output(results, Path(args.output))
    if args.trace:
        write_trace(results, Path(args.trace))
    usage = llm.USAGE.summary()
    counts = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    print(f"audited {len(results)} requests in {time.time() - t0:.1f}s -> {args.output}")
    print("status counts:", counts)
    print("model usage:", json.dumps(usage) if usage else "no model calls (offline / cached-only run)")
    (ROOT / "code" / "evaluation").mkdir(parents=True, exist_ok=True)
    (ROOT / "code" / "evaluation" / "last_run_usage.json").write_text(
        json.dumps({"requests": len(results), "status_counts": counts, "usage": usage,
                    "llm_available": llm.available()}, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
