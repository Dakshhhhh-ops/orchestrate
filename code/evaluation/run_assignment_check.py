"""Leave-identifiers-out check for run assignment.

Identifier-less records (anonymised analytics rows, 404 account lookups) are tied to a request
only through the deletion run that touched them (`audit.assign_run_resources`). The public samples
contain just a handful of such ties, so this check builds a much larger test from the dataset:

1. Take every record that the deletion worker acted on and whose owner IS known from identifiers,
   inside the run window of exactly one of that owner's deletion requests (the true case).
2. Hide those identifiers (the record looks like an anonymised one) and drop the direct link.
3. Run the assignment and count how often the record goes back to its true case.

No labels are used: the truth comes from identifiers in participant evidence.

    python code/evaluation/run_assignment_check.py --stride 5 # every 5th record, ~3 min
    python code/evaluation/run_assignment_check.py --system analytics_platform
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "code"))

import main as agent  # noqa: E402
from ghost_audit import intent as intent_mod  # noqa: E402
from ghost_audit.audit import World, assign_run_resources, discover, run_window  # noqa: E402
from ghost_audit.common import parse_ts, system_of  # noqa: E402


def build(world: World):
    pool = agent.load_requests(ROOT / "dataset" / "deletion_requests.csv")
    pool += agent.load_requests(ROOT / "dataset" / "sample_deletion_requests.csv")
    intents = intent_mod.classify(pool)
    subjects = {r["case_id"]: world.idx.resolve_email(r["email"]) for r in pool}
    pool = [r for r in pool if intents[r["case_id"]]["label"] == "DELETION" and subjects[r["case_id"]]]
    direct = {r["case_id"]: discover(world, subjects[r["case_id"]], r, parse_ts(r["audit_at"])) for r in pool}
    return pool, subjects, direct


def truth_pairs(world: World, pool, direct) -> dict[str, str]:
    """rid -> case_id for owned records the worker acted on inside exactly one owner request's window."""
    out = {}
    for rid, acts in world.act_by_rid.items():
        dw = [a for a in acts if a["source_system"] == "deletion_worker"]
        if not dw:
            continue
        t = dw[0]["t"]
        owners = [r["case_id"] for r in pool if rid in direct[r["case_id"]]]
        inside = [c for c in owners if (lambda lo_hi: lo_hi[0] <= t <= lo_hi[1])(run_window(next(r for r in pool if r["case_id"] == c)))]
        if len(inside) == 1:
            out[rid] = inside[0]
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--system", default=None, help="only hide records of this system_id")
    ap.add_argument("--stride", type=int, default=1, help="test every Nth record (full run takes ~15 min)")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args(argv)

    world = World(ROOT / "dataset")
    pool, subjects, direct = build(world)
    truth = truth_pairs(world, pool, direct)
    if args.system:
        truth = {r: c for r, c in truth.items() if system_of(r) == args.system}

    # leave-one-out: hide one record at a time, as in the real data where a run has only a few
    # anonymous records next to many identifiable ones
    hits, total, by_sys, wrong = 0, 0, Counter(), []
    by_sys_tot = Counter()
    systems = sorted({system_of(r) for r in truth})
    for rid in sorted(truth)[::args.stride]:
        sysid = system_of(rid)
        saved = world.owner_tokens.get(rid, set())
        world.owner_tokens[rid] = set()
        d2 = {c: ({k: v for k, v in links.items() if k != rid} if rid in links else links) for c, links in direct.items()}
        try:
            got = assign_run_resources(world, pool, subjects, d2)
        finally:
            world.owner_tokens[rid] = saved
        back = next((cid for cid, rids in got.items() if rid in rids), None)
        total += 1
        by_sys_tot[sysid] += 1
        if back == truth[rid]:
            hits += 1
            by_sys[sysid] += 1
        else:
            wrong.append((rid, truth[rid], back))

    print(f"run assignment, identifiers hidden: {hits}/{total} = {hits / total:.3f}" if total else "no test records")
    for s in systems:
        print(f"  {s:22s} {by_sys[s]}/{by_sys_tot[s]}")
    if args.verbose:
        for w in wrong:
            print("   wrong", w)
    return hits, total


if __name__ == "__main__":
    main()
