"""Per-case deletion audit.

Pipeline for one request:
  1. intent        - does the text authorise deletion? (intent.py)
  2. identity      - resolve the request email to a subject and all current/historical identifiers
  3. discovery     - every resource whose evidence (<= audit_at) carries one of those identifiers,
                     plus catalog links, replica/source links and the subject's deletion-run actions
  4. state         - reconstruct each resource's state at audit_at from the latest evidence,
                     then apply recreation events (restores, connector syncs) seen after it
  5. policy        - map each resource to its deletion_rules.csv category and required treatment
  6. plan          - findings -> remediation -> verification (planner.py)
"""
from __future__ import annotations

import csv
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from .common import PREFIX_CATEGORY, identifier_tokens, norm_email, parse_ts, prefix_of, system_of
from .evidence import SCOPES, Obs, extract_all
from .identity import IdentityIndex, Subject

# deletion_rules.csv category -> treatment
DEFAULT_ACTIONS = {
    "USER_PROFILE": "DELETE", "SECONDARY_IDENTIFIER": "DELETE", "SEARCH_INDEX_ENTRY": "DELETE",
    "CACHE_ENTRY": "DELETE", "SUPPORT_MESSAGE": "REDACT", "SUPPORT_ATTACHMENT": "DELETE",
    "ANALYTICS_PROFILE": "ANONYMIZE", "PRODUCT_TELEMETRY": "ANONYMIZE", "USER_EXPORT": "DELETE",
    "MARKETING_PROFILE": "DELETE", "EMAIL_DELIVERY_LOG": "REDACT", "SECURITY_AUDIT_LOG": "RETAIN",
    "LEGAL_HOLD_RECORD": "RETAIN", "BACKUP_ARCHIVE": "RETAIN", "ACCOUNT_TOMBSTONE": "REDACT",
    "DATA_FLOW": "DISABLE",
}

RECREATE_RE = re.compile(r"repopulated downstream resource (\w+)")
CATALOG_RE = re.compile(r"linked (\w+) \(([^)]+)\) to ([^\s]+@[^\s]+?) and (user_\d+)")
ATTACH_RE = re.compile(r"confirmed (\w+) \(([^)]+)\) belongs only to ([^\s]+@[^\s]+?) and (user_\d+)")


@dataclass
class ResourceState:
    rid: str
    system: str
    category: str
    action: str
    present: bool | None = None
    exposed: bool | None = None
    enabled: bool | None = None
    restore: bool | None = None
    evidence: str = ""            # evidence id proving the current state
    how_linked: set = field(default_factory=set)
    notes: list = field(default_factory=list)

    @property
    def violation(self) -> str | None:
        """Return the finding operation if the resource breaks policy, else None."""
        if self.action == "DISABLE":
            return "REPORT_ACTIVE_DATA_FLOW" if self.enabled else None
        if self.category == "BACKUP_ARCHIVE":
            return "REPORT_RESTORE_RISK" if self.restore else None
        if self.action == "RETAIN":
            return None
        if self.present is False:
            return None
        if self.action == "DELETE":
            return "REPORT_REMAINING_RECORD" if self.present else None
        if self.action in ("REDACT", "ANONYMIZE"):
            return "REPORT_EXPOSED_PII" if (self.present and self.exposed) else None
        return None


class World:
    """All participant-visible data, parsed once."""

    def __init__(self, dataset: Path, refresh_vision: bool = False):
        self.dataset = dataset
        self.idx = IdentityIndex(dataset)
        self.rules = self._rules()
        self.activities = self._activities()
        self.aliases, self.catalog = self._catalog()
        self.obs, self.info = extract_all(dataset, self.aliases, refresh_vision=refresh_vision)
        self.by_rid: dict[str, list[Obs]] = defaultdict(list)
        for o in self.obs:
            self.by_rid[o.rid].append(o)
        for lst in self.by_rid.values():
            lst.sort(key=lambda o: (o.ts, o.captured, o.eid))
        self.by_eid: dict[str, list[Obs]] = defaultdict(list)
        for o in self.obs:
            self.by_eid[o.eid].append(o)
        self.act_by_rid: dict[str, list[dict]] = defaultdict(list)
        self.recreated: dict[str, list[dict]] = defaultdict(list)
        for a in self.activities:
            self.act_by_rid[a["resource_id"]].append(a)
            m = RECREATE_RE.search(a["details"])
            if m:
                self.recreated[m.group(1)].append(a)
            if a["event_type"] == "SYNC_RECORD" and a["target_system"] == "account_failover_replica":
                self.recreated[a["resource_id"]].append(a)
        self.non_deletion_accounts = {a["resource_id"]: a for a in self.activities if a["event_type"] == "CLASSIFY_REQUEST"}
        # resources ever tied to a known subject by identifiers (any time) - used to avoid stealing
        self.owner_tokens: dict[str, set] = defaultdict(set)
        for o in self.obs:
            self.owner_tokens[o.rid] |= o.ids | o.keys
        for rid, links in self.catalog.items():
            for l in links:
                self.owner_tokens[rid] |= l["tokens"]

    def _rules(self) -> dict:
        out = {}
        with open(self.dataset / "deletion_rules.csv", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                out[r["data_category"]] = r
        return out

    def _activities(self) -> list[dict]:
        with open(self.dataset / "system_activity.csv", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        for r in rows:
            r["t"] = parse_ts(r["event_time"])
        rows.sort(key=lambda r: (r["t"], r["activity_id"]))
        return rows

    def _catalog(self):
        aliases, links = {}, defaultdict(list)
        for a in self.activities:
            if a["event_type"] != "LINK_RESOURCE_IDENTITY":
                continue
            m = CATALOG_RE.search(a["details"]) or ATTACH_RE.search(a["details"])
            if not m:
                continue
            rid, alias, email, user = m.groups()
            aliases[alias.lower()] = rid
            links[rid].append({"t": a["t"], "aid": a["activity_id"], "tokens": {"email:" + norm_email(email), "ref:" + user},
                               "exclusive": "belongs only" in a["details"]})
        return aliases, links

    def action_for(self, category: str) -> str:
        r = self.rules.get(category)
        if r:
            return r["required_action"]
        return DEFAULT_ACTIONS.get(category, "DELETE")


# --------------------------------------------------------------------------- case audit

@dataclass
class CaseResult:
    case_id: str
    status: str
    steps: list
    subject: str = ""
    intent: dict = field(default_factory=dict)
    resources: list = field(default_factory=list)
    trace: list = field(default_factory=list)


def _category(world: World, rid: str, obs: list[Obs], audit_at: datetime) -> str:
    pfx = prefix_of(rid)
    for o in reversed(obs):
        rt = (o.record_type or "").upper()
        if rt in world.rules or rt in DEFAULT_ACTIONS:
            if rt == "SUPPORT_MESSAGE" and pfx != "support":
                continue
            return rt
    # what the deletion workflow itself tried to do is a strong hint of the policy category
    for a in reversed(world.act_by_rid.get(rid, [])):
        if a["t"] > audit_at or a["source_system"] != "deletion_worker":
            continue
        et = a["event_type"]
        if pfx == "support":
            if et == "DELETE_RECORD":
                return "SUPPORT_ATTACHMENT"
            if et == "REDACT_RECORD":
                return "SUPPORT_MESSAGE"
        if pfx == "analytics" and et == "DELETE_RECORD":
            return "SECONDARY_IDENTIFIER"
        if pfx == "tombstone":
            return "ACCOUNT_TOMBSTONE"
    if pfx == "analytics":
        for o in obs:
            if o.note == "contact_lookup_completed":
                return "SECONDARY_IDENTIFIER"
    return PREFIX_CATEGORY.get(pfx, "USER_PROFILE")


def _state(world: World, rid: str, obs: list[Obs], audit_at: datetime, cat: str) -> ResourceState:
    st = ResourceState(rid, system_of(rid) or (obs[-1].system if obs else ""), cat, world.action_for(cat))
    if cat == "DATA_FLOW":
        st.action = "DISABLE"
    # group by snapshot (same evidence + time): a record in any replica table means it still exists
    groups: dict[tuple, list[Obs]] = defaultdict(list)
    for o in obs:
        groups[(o.ts, o.captured, o.eid)].append(o)
    for key in sorted(groups):
        g = groups[key]
        pres = [o.present for o in g if o.present is not None]
        if pres:
            st.present = any(pres)
            st.evidence = key[2]
            if not st.present:
                st.exposed = False
        exp = [o.exposed for o in g if o.exposed is not None]
        if exp:
            st.exposed = any(exp)
            st.evidence = key[2]
        en = [o.enabled for o in g if o.enabled is not None]
        if en:
            st.enabled = any(en)
            st.evidence = key[2]
        rs = [o.restore for o in g if o.restore is not None]
        if rs:
            st.restore = any(rs)
            st.evidence = key[2]
    last_t = max((o.ts for o in obs), default=None)
    # activity-stream facts used only where no evidence covers the facet
    for a in world.act_by_rid.get(rid, []):
        if a["t"] > audit_at:
            continue
        if last_t and a["t"] <= last_t:
            continue
        et, res = a["event_type"], a["reported_result"]
        if et == "CHECK_DATA_FLOW" and "remains enabled" in a["details"]:
            st.enabled, st.evidence = True, a["activity_id"]
        if et == "CHECK_RESTORE_ACCESS" and "can still be restored" in a["details"]:
            st.restore, st.evidence = True, a["activity_id"]
        if st.enabled is None and et == "DISABLE_DATA_FLOW" and res == "SUCCESS":
            st.enabled, st.evidence = False, a["activity_id"]
        if st.restore is None and et == "BLOCK_RESTORE" and res == "SUCCESS":
            st.restore, st.evidence = False, a["activity_id"]
        if et == "RESTORE_BACKUP":
            st.restore, st.evidence = True, a["activity_id"]
    # recreation after the last observation: the record is back
    for a in world.recreated.get(rid, []):
        if a["t"] <= audit_at and (last_t is None or a["t"] > last_t):
            st.present, st.exposed, st.evidence = True, True, a["activity_id"]
            st.notes.append(f"recreated by {a['activity_id']}")
    if cat == "BACKUP_ARCHIVE" and st.restore is None and st.present and obs:
        # the archive holding the subject's data is listed and nothing shows restores are blocked
        st.restore = True
        st.notes.append("archive present with no restore block in evidence")
    return st


def _visible(obs: list[Obs], audit_at: datetime) -> list[Obs]:
    return [o for o in obs if o.captured <= audit_at and o.ts <= audit_at]


def run_window(req: dict) -> tuple[datetime, datetime]:
    """Time span in which the deletion workflow acted for this request."""
    rep = parse_ts(req["reported_at"])
    return rep - timedelta(hours=3), rep + timedelta(hours=3)


def discover(world: World, subj: Subject, req: dict, audit_at: datetime) -> dict[str, set]:
    """rid -> link reasons, for resources owned by the subject.

    Ownership (which person a record belongs to) does not change over time, so it is read from
    all evidence; what the record looked like is decided later from evidence <= audit_at only.
    """
    tokens = subj.tokens
    linked: dict[str, set] = defaultdict(set)
    for rid, toks in world.owner_tokens.items():
        hit = toks & tokens
        if hit:
            linked[rid].add("identifier:" + ",".join(sorted({h.split(":")[0] for h in hit})))
    if subj.norm_name:
        for rid, obs in world.by_rid.items():
            if rid in linked:
                continue
            if any(subj.norm_name in o.names for o in obs):
                emails = {t for t in world.owner_tokens[rid] if t.startswith("email:")}
                if not emails or emails & tokens:
                    linked[rid].add("name")
    for rid, links in world.catalog.items():
        for l in links:
            if l["tokens"] & tokens:
                linked[rid].add("catalog:" + l["aid"])
    # derived copies (e.g. search document built from one of the subject's records)
    for rid, obs in world.by_rid.items():
        for o in obs:
            for k in o.keys:
                if k.startswith("rid:") and k[4:] in linked and rid not in linked:
                    linked[rid].add("source:" + k[4:])
    for rid in list(linked):
        for a in world.act_by_rid.get(rid, []):
            m = RECREATE_RE.search(a["details"])
            if m and a["t"] <= audit_at:
                linked[m.group(1)].add("recreated_from:" + rid)
    # keep only resources that existed in some visible evidence or activity by audit time
    out = {}
    for rid, why in linked.items():
        vis = _visible(world.by_rid.get(rid, []), audit_at)
        acts = [a for a in world.act_by_rid.get(rid, []) if a["t"] <= audit_at]
        retained = prefix_of(rid) in ("audit_record", "legal_hold")
        if vis or acts or retained or any(w.startswith("recreated_from") for w in why):
            out[rid] = why
    return out


def run_resources(world: World, req: dict) -> list[dict]:
    lo, hi = run_window(req)
    return [a for a in world.activities if lo <= a["t"] <= hi and a["source_system"] == "deletion_worker"]


def assign_run_resources(world: World, requests: list[dict], subjects: dict, direct: dict) -> dict[str, set]:
    """Tie identifier-less resources to requests through the deletion run that touched them.

    A resource the deletion worker acted on, which no evidence links to any known person, is
    offered to the requests whose run window contains the action. Each request takes at most
    one such resource per system and only for systems it has no linked record in; ties go to
    the request whose own account deletion happened closest before the action.
    """
    unowned = []
    for rid, acts in world.act_by_rid.items():
        dw = [a for a in acts if a["source_system"] == "deletion_worker"]
        if not dw:
            continue
        if world.idx.owners(world.owner_tokens.get(rid, set())):
            continue
        unowned.append((dw[0]["t"], rid))
    # accounts first: the run's own account anchors the timing of the other actions
    unowned.sort(key=lambda x: (prefix_of(x[1]) != "account", x[0]))
    acct_time, has_sys = {}, {}
    for r in requests:
        cid = r["case_id"]
        if cid not in subjects:
            continue
        linked = direct.get(cid, {})
        has_sys[cid] = {system_of(x) for x in linked}
        for a in run_resources(world, r):
            if a["event_type"] == "DELETE_RECORD" and a["resource_id"] in linked and a["target_system"] == "account_service":
                acct_time[cid] = a["t"]
    out: dict[str, set] = defaultdict(set)
    for t, rid in unowned:
        sysid = system_of(rid)
        cands = []
        for r in requests:
            cid = r["case_id"]
            if cid not in subjects or sysid in has_sys.get(cid, set()):
                continue
            lo, hi = run_window(r)
            if not (lo <= t <= hi):
                continue
            at = acct_time.get(cid)
            gap = (t - at).total_seconds() if at and at <= t else 1e9
            cands.append((abs((parse_ts(r["reported_at"]) - t).total_seconds()) // 1800, gap, cid))
        if cands:
            cands.sort()
            cid = cands[0][2]
            out[cid].add(rid)
            has_sys[cid].add(sysid)
            if sysid == "account_service":
                acct_time[cid] = t
    return out


def omission_evidence(world: World, subj, system: str, rid: str, since: datetime, audit_at: datetime) -> str:
    """Latest full snapshot of `system` (captured in [since, audit_at]) that does not contain `rid`.

    Snapshots that also hold other records of the same subject are preferred, because they were
    clearly taken with this person in scope; otherwise any snapshot listing that system's records.
    """
    best = None
    for eid, systems in SCOPES.items():
        if system not in systems:
            continue
        cap = world.info.get(eid, {}).get("captured")
        if not cap or cap < since or cap > audit_at:
            continue
        obs = world.by_eid.get(eid, [])
        if any(o.rid == rid for o in obs):
            continue
        covers = any(world.owner_tokens.get(o.rid, set()) & subj.tokens for o in obs)
        has_sys = any(o.system == system for o in obs)
        if not (covers or has_sys):
            continue
        key = (covers, cap)
        if best is None or key > best[0]:
            best = (key, eid)
    return best[1] if best else ""


def case_accounts(world: World, req: dict, states: list) -> set:
    """Accounts that belong in the plan: present ones, plus the one(s) this request's run deleted."""
    accts = [s for s in states if s.system == "account_service"]
    run_ids = {a["resource_id"] for a in run_resources(world, req)}
    keep = {s.rid for s in accts if s.present or s.rid in run_ids or "deletion_run" in s.how_linked}
    if not keep and accts:
        # no run evidence: keep the most recently observed account
        keep = {max(accts, key=lambda s: (s.evidence != "", s.rid)).rid}
    return keep


def audit_case(world: World, req: dict, subj, intent: dict, linked: dict, run_rids: set) -> CaseResult:
    from .planner import build_steps

    cid = req["case_id"]
    audit_at = parse_ts(req["audit_at"])
    trace = []
    if intent.get("label") != "DELETION":
        return CaseResult(cid, "NOT_APPLICABLE", [], subj.user_id if subj else "", intent, [],
                          ["request does not authorise deletion"])
    if subj is None:
        trace.append("request email not found in identities; nothing can be verified")
        return CaseResult(cid, "COMPLETE", [], "", intent, [], trace)
    linked = {k: set(v) for k, v in linked.items()}
    for rid in run_rids:
        linked.setdefault(rid, set()).add("deletion_run")
    since = parse_ts(req["requested_at"])
    states = []
    for rid in sorted(linked):
        vis = _visible(world.by_rid.get(rid, []), audit_at)
        cat = _category(world, rid, vis, audit_at)
        # state must be established by evidence gathered after the request was made;
        # older sightings only describe the record before deletion was asked for
        obs = [o for o in vis if o.captured >= since]
        acts = [a for a in world.act_by_rid.get(rid, []) if since <= a["t"] <= audit_at]
        pfx = prefix_of(rid)
        if not obs and not acts and pfx not in ("audit_record", "legal_hold", "account")                 and not any(w.startswith("recreated_from") for w in linked[rid]):
            trace.append(f"{rid}: only seen before the request ({len(vis)} older sightings) - state at audit unknown")
            continue
        st = _state(world, rid, obs, audit_at, cat)
        st.how_linked = linked[rid]
        if not st.system:
            continue
        if st.present is None and st.action == "DELETE":
            eid = omission_evidence(world, subj, st.system, rid, since, audit_at)
            if eid:
                st.present, st.exposed, st.evidence = False, False, eid
                st.notes.append(f"absent from {eid} snapshot of {st.system}")
        states.append(st)
    keep_accts = case_accounts(world, req, states)
    states = [s for s in states if s.system != "account_service" or s.rid in keep_accts]
    status, steps = build_steps(states)
    return CaseResult(cid, status, steps, subj.user_id, intent, states, trace)
