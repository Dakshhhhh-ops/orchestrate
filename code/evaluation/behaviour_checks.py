"""Targeted behaviour checks (participant-created evaluation cases).

Each check builds a small request (or perturbs a real one) and asserts one property the
challenge cares about. Run from the repository root:

    python code/evaluation/behaviour_checks.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "code"))

import main as agent  # noqa: E402
from ghost_audit.audit import World  # noqa: E402
from ghost_audit.intent import rule_label  # noqa: E402


def req(case_id, email, requested, reported, audit, text, status="COMPLETE"):
    return {"case_id": case_id, "email": email, "requested_at": requested, "reported_status": status,
            "reported_at": reported, "audit_at": audit, "request_text": text}


def rids(result):
    return {s["resource_id"] for s in result.steps}


def main():
    world = World(ROOT / "dataset")
    checks = []

    def check(name, ok, detail=""):
        checks.append((name, bool(ok), detail))

    # 1. decoy with a near-identical e-mail must not be attributed to the requester
    _, res = agent.run([req("x_decoy", "g.silva76@gmail.com", "2026-08-30T03:00:00Z", "2026-08-31T21:00:00Z",
                            "2026-09-02T08:00:00Z", "Please delete my account.")], world=world)
    check("decoy search_1874 (g.silva76.jr@) not linked to g.silva76@", "search_1874" not in rids(res[0]),
          sorted(rids(res[0])))
    check("real search_428 for g.silva76@ reported", "search_428" in rids(res[0]))

    # 2. a request sent from a historical e-mail resolves to the same person and resources
    base = req("x_cur", "minjun.kim@gmail.com", "2026-08-24T12:00:00Z", "2026-08-25T12:00:00Z",
               "2026-09-15T12:00:00Z", "Please delete my account and everything linked to my old address.")
    old = dict(base, case_id="x_old", email="minjun.kim90@gmail.com")
    _, res = agent.run([base, old], world=world)
    check("old e-mail resolves to same plan as current e-mail", res[0].steps == res[1].steps and res[0].steps,
          f"{len(res[0].steps)} vs {len(res[1].steps)} steps")
    check("hash-keyed analytics_9914 found through the old e-mail hash", "analytics_9914" in rids(res[1]))

    # 3. evidence captured after audit_at must never be cited
    late = req("x_early", "ananya-nair@gmail.com", "2026-08-29T12:00:00Z", "2026-08-30T12:00:00Z",
               "2026-09-10T12:00:00Z", "Please delete my account.")
    _, res = agent.run([late], world=world)
    cited = {s["evidence_id"] for s in res[0].steps if s["evidence_id"]}
    too_late = [e for e in cited if e.startswith("evidence_") and world.info[e]["captured"] > agent.parse_ts(late["audit_at"])]
    check("no evidence captured after audit_at is cited", not too_late, sorted(cited))

    # 4. upper-case request e-mail resolves
    s = world.idx.resolve_email("SIPHO.MOKOENA@GMAIL.COM")
    check("case-insensitive e-mail resolution", s is not None and s.user_id == "user_36", s.user_id if s else None)

    # 5. recreated data: connector repopulated a deleted search entry -> flow disabled before delete
    _, res = agent.run([r for r in agent.load_requests(ROOT / "dataset" / "deletion_requests.csv")
                        if r["case_id"] == "case_37"], world=world)
    ops = [(s["operation"], s["resource_id"]) for s in res[0].steps]
    check("active connector disabled before the search entry it recreates is deleted",
          ops.index(("DISABLE_DATA_FLOW", "data_flow_2528")) < ops.index(("DELETE_RECORD", "search_1934")), ops)

    # --- traps verified by hand against raw evidence (evaluation report 3d) ------------------
    # One full run of the 518 evaluation requests feeds checks 7-12. Each check first asserts
    # that the trap really exists in the participant data, then asserts the agent's answer.
    eval_reqs = agent.load_requests(ROOT / "dataset" / "deletion_requests.csv")
    _, results = agent.run(eval_reqs, world=world)
    by_case = {r.case_id: r for r in results}
    audit_at = {r["case_id"]: agent.parse_ts(r["audit_at"]) for r in eval_reqs}

    def ops_for(cid, rid):
        return {(s["operation"], s["evidence_id"]) for s in by_case[cid].steps if s["resource_id"] == rid}

    # 7. replica drift: account deleted from the primary table but still in the read replica
    tables = {o.variant: o.present for o in world.by_rid["account_6345"] if o.eid == "evidence_10"}
    drift = tables.get("customer_data_read_replica") and "customer_data_primary" not in tables
    check("replica drift: account_6345 only in evidence_10's replica table is a remaining record",
          drift and ("REPORT_REMAINING_RECORD", "evidence_10") in ops_for("case_105", "account_6345"),
          (tables, sorted(ops_for("case_105", "account_6345"))))

    # 8. later-evidence trap: account recreated only after audit_at stays ABSENT at audit_at
    for cid, rid in (("case_215", "account_1956"), ("case_335", "account_8892")):
        later = [o for o in world.by_rid[rid] if o.present and o.ts > audit_at[cid]]
        ops = {op for op, _ in ops_for(cid, rid)}
        check(f"later evidence ignored: {rid} recreated after {cid}'s audit stays absent",
              later and "VERIFY_RECORD_ABSENT" in ops and "REPORT_REMAINING_RECORD" not in ops,
              (len(later), sorted(ops)))

    # 9. claim vs evidence: the workflow says the profile was deleted, the audit-time lookup finds it
    claimed = [a for a in world.act_by_rid["account_3064"]
               if a["event_type"] == "DELETE_RECORD" and a["reported_result"] == "SUCCESS"]
    check("workflow claim not trusted: account_3064 'DELETE SUCCESS' but rows=1 at audit -> remaining",
          claimed and ("REPORT_REMAINING_RECORD", "evidence_85") in ops_for("case_165", "account_3064"),
          sorted(ops_for("case_165", "account_3064")))

    # 10. tombstone: stable reference kept, direct identifiers gone, account proven absent
    c549 = by_case["case_549"]
    check("tombstone_3963 verified REDACTED and account_4842 ABSENT, both from evidence_52",
          c549.status == "COMPLETE"
          and ("VERIFY_RECORD_REDACTED", "evidence_52") in ops_for("case_549", "tombstone_3963")
          and ("VERIFY_RECORD_ABSENT", "evidence_52") in ops_for("case_549", "account_4842"),
          (c549.status, [(s["operation"], s["resource_id"], s["evidence_id"]) for s in c549.steps]))

    # 11. run fingerprint: hide the uploaded_by link and let the run timing place the record
    sys.path.insert(0, str(ROOT / "code" / "evaluation"))
    import run_assignment_check as rac  # noqa: E402
    from ghost_audit.audit import assign_run_resources  # noqa: E402

    pool, subjects, direct = rac.build(world)
    rid = "support_3802"
    saved = world.owner_tokens.get(rid, set())
    world.owner_tokens[rid] = set()
    try:
        hidden = {c: {k: v for k, v in links.items() if k != rid} for c, links in direct.items()}
        got = assign_run_resources(world, pool, subjects, hidden)
    finally:
        world.owner_tokens[rid] = saved
    back = next((cid for cid, rs in got.items() if rid in rs), None)
    check("run fingerprint: support_3802 (uploaded_by hidden) returns to case_407", back == "case_407", back)

    # 12. every generated deletion case audits exactly one account
    bad = []
    for r in results:
        if int(r.case_id.split("_")[1]) < 64 or r.status == "NOT_APPLICABLE":
            continue
        accts = {s["resource_id"] for s in r.steps if s["system_id"] == "account_service"}
        if len(accts) != 1:
            bad.append((r.case_id, sorted(accts)))
    check("one account per generated deletion case", not bad, bad[:5])

    # 13. intent rules: non-deletion requests in several languages
    samples = {
        "Bitte nichts löschen, ich möchte nur eine Kopie meiner Daten.": "NOT_DELETION",
        "Je veux seulement corriger mon adresse, ne supprimez pas mon compte.": "NOT_DELETION",
        "Solo quiero darme de baja de los correos promocionales, no eliminen mi cuenta.": "NOT_DELETION",
        "Merci de supprimer définitivement mon compte.": "DELETION",
        "アカウントを削除してください。": "DELETION",
    }
    for text, want in samples.items():
        got = rule_label(text)[0]
        check(f"intent fallback: {text[:40]}...", got == want, got)

    width = max(len(c[0]) for c in checks)
    passed = 0
    for name, ok, detail in checks:
        passed += ok
        print(f"{'PASS' if ok else 'FAIL'}  {name.ljust(width)}  {detail if not ok else ''}")
    print(f"\n{passed}/{len(checks)} checks passed")
    return passed, len(checks)


if __name__ == "__main__":
    main()
