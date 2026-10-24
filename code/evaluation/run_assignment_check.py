"""Leave-identifiers-out check for run assignment.

Identifier-less records (anonymised analytics rows, 404 account lookups) are tied to a request
only through the deletion run that touched them (`audit.assign_run_resources`). The public samples
contain just a handful of such ties, so this check builds a much larger test from the dataset:

1. Take every record that the deletion worker acted on and whose owner IS known from identifiers,
   inside the +-3 h report span of exactly one of that owner's deletion requests (the true case).
2. Hide those identifiers (the record looks like an anonymised one) and drop the direct link.
3. Run the assignment and count how often the record goes back to its true case.

No labels are used: the truth comes from identifiers in participant evidence.

Modes
    python code/evaluation/run_assignment_check.py --stride 5          # in-sample leave-one-out
    python code/evaluation/run_assignment_check.py --cv --stride 5     # out-of-fold, by month
    python code/evaluation/run_assignment_check.py --cv --ablate all   # every pass/signal switched off in turn

In-sample: the timing model is learned once from all identifier-linked runs, then each record is
hidden and re-assigned. Out-of-fold (--cv): requests are split by the month they were reported;
for each month the model is learned ONLY from the other months' linked runs, and the month's
records are hidden and re-assigned with that frozen model. Out-of-fold is the number to quote.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "code"))
os.environ.setdefault("GHOST_OFFLINE", "1")   # never spend model quota on a check

import main as agent  # noqa: E402
from ghost_audit import intent as intent_mod  # noqa: E402
from ghost_audit.audit import FLAGS_DEFAULT, World, assign_run_resources, discover, learn_run_model  # noqa: E402
from ghost_audit.common import parse_ts, system_of  # noqa: E402

TRUTH_SPAN = timedelta(hours=3)   # definition of the test set (fixed, independent of the model)
POOL_SPAN = timedelta(days=1)     # only requests reported this close can compete for a record


def build(world: World):
    pool = agent.load_requests(ROOT / "dataset" / "deletion_requests.csv")
    pool += agent.load_requests(ROOT / "dataset" / "sample_deletion_requests.csv")
    intents = intent_mod.classify(pool)
    subjects = {r["case_id"]: world.idx.resolve_email(r["email"]) for r in pool}
    pool = [r for r in pool if intents[r["case_id"]]["label"] == "DELETION" and subjects[r["case_id"]]]
    direct = {r["case_id"]: discover(world, subjects[r["case_id"]], r, parse_ts(r["audit_at"])) for r in pool}
    return pool, subjects, direct


def truth_pairs(world: World, pool, direct) -> dict[str, str]:
    """rid -> case_id for owned records the worker acted on inside exactly one owner request's span."""
    out = {}
    rep = {r["case_id"]: parse_ts(r["reported_at"]) for r in pool}
    owners_of = defaultdict(list)
    for r in pool:
        for rid in direct[r["case_id"]]:
            owners_of[rid].append(r["case_id"])
    for rid, acts in world.act_by_rid.items():
        dw = [a for a in acts if a["source_system"] == "deletion_worker"]
        if not dw:
            continue
        t = dw[0]["t"]
        inside = [c for c in owners_of.get(rid, []) if abs(t - rep[c]) <= TRUTH_SPAN]
        if len(inside) == 1:
            out[rid] = inside[0]
    return out


def month(r: dict) -> str:
    return r["reported_at"][:7]


def hide_and_assign(world, pool, subjects, direct, rid, model, flags):
    """Hide one record's identifiers and direct links, re-assign, return the case it went to."""
    t = next(a["t"] for a in world.act_by_rid[rid] if a["source_system"] == "deletion_worker")
    near = [r for r in pool if abs(parse_ts(r["reported_at"]) - t) <= POOL_SPAN]
    saved = world.owner_tokens.get(rid, set())
    world.owner_tokens[rid] = set()
    try:
        d2 = {r["case_id"]: {k: v for k, v in direct[r["case_id"]].items() if k != rid} for r in near}
        got = assign_run_resources(world, near, subjects, d2, model=model, flags=flags)
    finally:
        world.owner_tokens[rid] = saved
    return next((cid for cid, rids in got.items() if rid in rids), None)


def evaluate(world, pool, subjects, direct, truth, stride, flags=None, cv=False, verbose=False):
    """Returns (hits, total, unassigned, per-fold rows, wrong list, models)."""
    by_case = {r["case_id"]: r for r in pool}
    tested = sorted(truth)[::stride]
    rows, wrong, models = [], [], {}
    hits = total = unassigned = 0
    folds = sorted({month(by_case[truth[rid]]) for rid in tested}) if cv else ["all"]
    full_model = None if cv else learn_run_model(world, pool, direct)
    for fold in folds:
        if cv:
            train = [r for r in pool if month(r) != fold]
            model = learn_run_model(world, train, direct)
            test = [rid for rid in tested if month(by_case[truth[rid]]) == fold]
        else:
            model, test = full_model, tested
        models[fold] = model
        fh = fu = 0
        for rid in test:
            back = hide_and_assign(world, pool, subjects, direct, rid, model, flags)
            fh += back == truth[rid]
            fu += back is None
            if back != truth[rid]:
                wrong.append((rid, truth[rid], back, fold))
        rows.append((fold, fh, len(test), fu))
        hits, total, unassigned = hits + fh, total + len(test), unassigned + fu
        if verbose:
            print(f"   fold {fold}: {fh}/{len(test)}  (unassigned {fu})", flush=True)
    return hits, total, unassigned, rows, wrong, models


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--system", default=None, help="only hide records of this system_id")
    ap.add_argument("--stride", type=int, default=1, help="test every Nth record")
    ap.add_argument("--cv", action="store_true", help="out-of-fold: learn on other months only")
    ap.add_argument("--ablate", default="", help="comma list of FLAGS to switch off, or 'all' for one run per flag")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args(argv)

    world = World(ROOT / "dataset")
    pool, subjects, direct = build(world)
    truth = truth_pairs(world, pool, direct)
    if args.system:
        truth = {r: c for r, c in truth.items() if system_of(r) == args.system}

    configs = [("full", {})]
    if args.ablate == "all":
        configs += [(f"-{k}", {k: False}) for k in FLAGS_DEFAULT]
    elif args.ablate:
        configs = [("-" + args.ablate, {k: False for k in args.ablate.split(",")})]
    mode = "out-of-fold by month" if args.cv else "in-sample"
    for name, flags in configs:
        t0 = time.time()
        hits, total, unassigned, rows, wrong, models = evaluate(world, pool, subjects, direct, truth, args.stride,
                                                                flags, args.cv, args.verbose)
        wrong_case = len(wrong) - unassigned
        print(f"[{name}] {mode}: {hits}/{total} = {hits / total:.3f}  "
              f"(wrong case {wrong_case}, unassigned {unassigned}, {time.time() - t0:.0f}s)", flush=True)
        if args.cv and name == "full":
            for fold, h, n, u in rows:
                print(f"   {fold}: {h}/{n}" + (f"  unassigned {u}" if u else ""))
            periods = Counter(m.period for m in models.values())
            print(f"   learned period per fold: {dict(periods)}")
        if args.verbose:
            per = Counter(system_of(r) for r, *_ in wrong)
            print("   misses by system:", dict(per))
            for w in wrong:
                print("   wrong", w)
    return 0


if __name__ == "__main__":
    main()
