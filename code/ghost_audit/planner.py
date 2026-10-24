"""Turn reconstructed resource states into the ordered FINDING -> REMEDIATION -> VERIFICATION plan."""
from __future__ import annotations

FINDING_ORDER = ["REPORT_REMAINING_RECORD", "REPORT_EXPOSED_PII", "REPORT_ACTIVE_DATA_FLOW", "REPORT_RESTORE_RISK"]
REMEDIATION_ORDER = ["DISABLE_DATA_FLOW", "BLOCK_RESTORE", "DELETE_RECORD", "REDACT_RECORD", "ANONYMIZE_RECORD"]
VERIFICATION_ORDER = ["VERIFY_RECORD_ABSENT", "VERIFY_RECORD_PRESENT", "VERIFY_RECORD_REDACTED",
                      "VERIFY_RECORD_ANONYMIZED", "VERIFY_DATA_FLOW_DISABLED", "VERIFY_RESTORE_BLOCKED"]
PHASES = ["FINDING", "REMEDIATION", "VERIFICATION"]

STATE_OF = {
    "REPORT_REMAINING_RECORD": "PRESENT", "REPORT_EXPOSED_PII": "PRESENT", "REPORT_ACTIVE_DATA_FLOW": "ACTIVE",
    "REPORT_RESTORE_RISK": "ACTIVE", "DISABLE_DATA_FLOW": "DISABLED", "BLOCK_RESTORE": "BLOCKED",
    "DELETE_RECORD": "ABSENT", "REDACT_RECORD": "REDACTED", "ANONYMIZE_RECORD": "ANONYMIZED",
    "VERIFY_RECORD_ABSENT": "ABSENT", "VERIFY_RECORD_PRESENT": "PRESENT", "VERIFY_RECORD_REDACTED": "REDACTED",
    "VERIFY_RECORD_ANONYMIZED": "ANONYMIZED", "VERIFY_DATA_FLOW_DISABLED": "DISABLED",
    "VERIFY_RESTORE_BLOCKED": "BLOCKED",
}
# finding -> (remediation, verification)
FIX = {
    "REPORT_ACTIVE_DATA_FLOW": ("DISABLE_DATA_FLOW", "VERIFY_DATA_FLOW_DISABLED"),
    "REPORT_RESTORE_RISK": ("BLOCK_RESTORE", "VERIFY_RESTORE_BLOCKED"),
}
ACTION_FIX = {
    "DELETE": ("DELETE_RECORD", "VERIFY_RECORD_ABSENT"),
    "REDACT": ("REDACT_RECORD", "VERIFY_RECORD_REDACTED"),
    "ANONYMIZE": ("ANONYMIZE_RECORD", "VERIFY_RECORD_ANONYMIZED"),
}


def _order(step: dict) -> tuple:
    ph = PHASES.index(step["phase"])
    table = (FINDING_ORDER, REMEDIATION_ORDER, VERIFICATION_ORDER)[ph]
    return (ph, table.index(step["operation"]), step["system_id"], step["resource_id"])


def _step(phase, st, op, evidence=""):
    return {"phase": phase, "system_id": st.system, "resource_id": st.rid, "operation": op,
            "state": STATE_OF[op], "evidence_id": evidence}


def compliant_check(st) -> str | None:
    """Verification operation proving a compliant resource's required state (COMPLETE cases)."""
    if st.category == "DATA_FLOW":
        return "VERIFY_DATA_FLOW_DISABLED" if st.enabled is False else None
    if st.category == "BACKUP_ARCHIVE":
        return "VERIFY_RESTORE_BLOCKED" if st.restore is False else None
    if st.action == "RETAIN":
        return "VERIFY_RECORD_PRESENT" if st.present else None
    if st.action == "DELETE":
        return "VERIFY_RECORD_ABSENT" if st.present is False else None
    if st.action == "REDACT":
        return "VERIFY_RECORD_REDACTED" if st.present and st.exposed is False else None
    if st.action == "ANONYMIZE":
        return "VERIFY_RECORD_ANONYMIZED" if st.present and st.exposed is False else None
    return None


def build_steps(states: list) -> tuple[str, list]:
    findings, remediation, verification = [], [], []
    violations = [(st, st.violation) for st in states]
    violations = [(st, v) for st, v in violations if v]
    incomplete = bool(violations)
    for st, v in violations:
        findings.append(_step("FINDING", st, v, st.evidence))
        if v in FIX:
            rem, ver = FIX[v]
        else:
            rem, ver = ACTION_FIX[st.action]
        remediation.append(_step("REMEDIATION", st, rem))
        verification.append(_step("VERIFICATION", st, ver))
    violated = {st.rid for st, _ in violations}
    for st in states:
        if st.rid in violated:
            continue
        if incomplete:
            # after remediation we still check the account is gone and retained records stayed
            if st.system == "account_service" and st.category == "USER_PROFILE":
                verification.append(_step("VERIFICATION", st, "VERIFY_RECORD_ABSENT"))
            elif st.action == "RETAIN" and st.present and st.category != "BACKUP_ARCHIVE":
                verification.append(_step("VERIFICATION", st, "VERIFY_RECORD_PRESENT"))
        else:
            op = compliant_check(st)
            if op:
                verification.append(_step("VERIFICATION", st, op, st.evidence))
    steps = sorted(findings + remediation + verification, key=_order)
    # one step per (phase, operation, resource)
    seen, uniq = set(), []
    for s in steps:
        k = (s["phase"], s["operation"], s["system_id"], s["resource_id"])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(s)
    for i, s in enumerate(uniq, 1):
        s["step_no"] = i
    status = "INCOMPLETE" if incomplete else "COMPLETE"
    return status, [{k: s[k] for k in ("step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id")}
                    for s in uniq]
