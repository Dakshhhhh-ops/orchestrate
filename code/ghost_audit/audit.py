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
    if cat == "BACKUP_ARCHIVE" and st.restore is None and st.present:
        st.restore = None
    return st


def _visible(obs: list[Obs], audit_at: datetime) -> list[Obs]:
    return [o for o in obs if o.captured <= audit_at and o.ts <= audit_at]


def discover(world: World, subj: Subject, req: dict, audit_at: datetime, trace: list) -> dict[str, set]:
    """Return rid -> set(link reasons) for resources tied to the subject."""
    tokens = subj.tokens
    linked: dict[str, set] = defaultdict(set)
    other_emails = set()
    for rid, obs in world.by_rid.items():
        vis = _visible(obs, audit_at)
        if not vis:
            continue
        toks = set()
        names = set()
        for o in vis:
            toks |= o.ids | o.keys
            names |= o.names
        hit = toks & tokens
        if hit:
            linked[rid].add("identifier:" + ",".join(sorted(h.split(":")[0] for h in hit)))
            continue
        if subj.norm_name and subj.norm_name in names:
            emails = {t for t in toks if t.startswith("email:")}
            if not emails or emails & tokens:
                linked[rid].add("name")
    # catalog links
    for rid, links in world.catalog.items():
        for l in links:
            if l["t"] <= audit_at and l["tokens"] & tokens:
                linked[rid].add("catalog:" + l["aid"])
    # replica/source links: a document derived from one of the subject's records
    for rid, obs in world.by_rid.items():
        for o in _visible(obs, audit_at):
            for k in o.keys:
                if k.startswith("rid:") and k[4:] in linked and rid not in linked:
                    linked[rid].add("source:" + k[4:])
    # recreation targets of linked backups / flows
    for rid in list(linked):
        for a in world.act_by_rid.get(rid, []):
            m = RECREATE_RE.search(a["details"])
            if m and a["t"] <= audit_at:
                linked[m.group(1)].add("recreated_from:" + rid)
    return linked


def run_window(req: dict) -> tuple[datetime, datetime]:
    rep = parse_ts(req["reported_at"])
    return rep - timedelta(hours=3), rep + timedelta(hours=3)


def assign_run_resources(world: World, requests: list[dict], subjects: dict[str, Subject]) -> dict[str, set]:
    """Resources acted on by the deletion worker that carry no identifiers anywhere.

    They can only be tied to a request through the deletion run that touched them. Each such
    resource goes to the request whose run window contains it; ties go to the request whose
    account deletion happened closest before the action.
    """
    unowned = set()
    for rid, acts in world.act_by_rid.items():
        if not any(a["source_system"] == "deletion_worker" for a in acts):
            continue
        toks = world.owner_tokens.get(rid, set())
        if not world.idx.owners(toks):
            unowned.add(rid)
    acct_time: dict[str, datetime] = {}
    for r in requests:
        s = subjects.get(r["case_id"])
        if not s:
            continue
        lo, hi = run_window(r)
        for a in world.activities:
            if lo <= a["t"] <= hi and a["event_type"] == "DELETE_RECORD" and a["target_system"] == "account_service":
                if world.idx.owners(world.owner_tokens.get(a["resource_id"], set())) == {s.user_id}:
                    acct_time[r["case_id"]] = a["t"]
    out: dict[str, set] = defaultdict(set)
    for rid in sorted(unowned):
        acts = [a for a in world.act_by_rid[rid] if a["source_system"] == "deletion_worker"]
        t = acts[0]["t"]
        cands = []
        for r in requests:
            if r["case_id"] not in subjects:
                continue
            lo, hi = run_window(r)
            if lo <= t <= hi:
                at = acct_time.get(r["case_id"])
                gap = (t - at).total_seconds() if at and at <= t else 1e9
                cands.append((gap, abs((parse_ts(r["reported_at"]) - t).total_seconds()), r["case_id"]))
        if cands:
            cands.sort()
            out[cands[0][2]].add(rid)
    return out


def audit_case(world: World, req: dict, subj: Subject | None, intent: dict, run_rids: set) -> CaseResult:
    from .planner import build_steps

    cid = req["case_id"]
    audit_at = parse_ts(req["audit_at"])
    trace = []
    if intent.get("label") != "DELETION":
        return CaseResult(cid, "NOT_APPLICABLE", [], subj.user_id if subj else "", intent, [], ["request does not authorise deletion"])
    if subj is None:
        trace.append("request email not found in identities; nothing can be verified")
        return CaseResult(cid, "COMPLETE", [], "", intent, [], trace)
    linked = discover(world, subj, req, audit_at, trace)
    for rid in run_rids:
        linked[rid].add("deletion_run")
    states: list[ResourceState] = []
    for rid in sorted(linked):
        obs = _visible(world.by_rid.get(rid, []), audit_at)
        cat = _category(world, rid, obs, audit_at)
        st = _state(world, rid, obs, audit_at, cat)
        st.how_linked = linked[rid]
        if not st.system:
            continue
        states.append(st)
    status, steps = build_steps(states)
    return CaseResult(cid, status, steps, subj.user_id, intent, states, trace)
