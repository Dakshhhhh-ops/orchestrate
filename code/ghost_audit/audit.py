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
    # the record's own container or log event says what it is (ticket_attachments,
    # support_conversations, message_pii_scan, ...)
    if pfx == "support":
        for o in reversed(obs):
            v = f"{o.variant or ''} {o.note or ''}".lower()
            if "attachment" in v:
                return "SUPPORT_ATTACHMENT"
            if "conversation" in v or "message" in v:
                return "SUPPORT_MESSAGE"
    # last resort: what the deletion workflow tried to do. On every record that states its own
    # type, this agrees with it (31 conversations -> REDACT, 7 attachments -> DELETE).
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
        # a workflow "SUCCESS" is a claim, never proof of a compliant state, so it sets nothing
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


PHASE_PERIOD = 30.0          # seconds
FOLLOWUP_GAPS = (1110.0, 1169.0)   # exact spacing between consecutive follow-up actions of one run
STEP_DRIFT = 0.5             # each run step lands 0.5 s earlier in the 30 s cycle
TOL = {"account": 0.5, "policy": 0.8, "other": 0.8}
MULTI_SYSTEM = "support_platform"   # the only system where one person's run can touch two records
POLICY_GAPS = (390.0, 449.0, 839.0)            # exact spacing between policy steps of one run
CHAIN_GAPS = {"policy": POLICY_GAPS, "other": FOLLOWUP_GAPS + (2279.0,)}
POLICY_LEAD = (0.0, 35 * 60.0)                 # policy steps run in the half hour before the report
ACCOUNT_LEAD = (35 * 60.0, 85 * 60.0)          # account deletions run 40-80 min before the report
FOLLOWUP_START = (25 * 60.0, 95 * 60.0)        # a run's follow-up chain starts 30-90 min after it


def run_chains(world: World) -> dict[str, int]:
    """Group deletion-worker actions into runs by their exact spacing.

    Steps of one run are spaced exactly 390/449 s (policy) or 1110/1169 s (follow-ups), so two
    actions at exactly such a gap belong to the same run. Returns activity_id -> chain id.
    """
    if hasattr(world, "_chains"):
        return world._chains
    parent: dict[str, str] = {}

    def find(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    dw = [a for a in world.activities if a["source_system"] == "deletion_worker"]
    for kind, gaps in CHAIN_GAPS.items():
        acts = [a for a in dw if _action_kind(a) == kind]
        by_t = defaultdict(list)
        for a in acts:
            by_t[a["t"]].append(a)
        for a in acts:
            for g in gaps:
                for b in by_t.get(a["t"] - timedelta(seconds=g), []):
                    parent[find(a["activity_id"])] = find(b["activity_id"])
    ids, out = {}, {}
    for a in dw:
        out[a["activity_id"]] = ids.setdefault(find(a["activity_id"]), len(ids))
    world._chains = out
    return out


def _action_kind(a: dict) -> str:
    """Which part of a deletion run an activity belongs to (account delete, policy step, follow-up)."""
    if a["event_type"] == "DELETE_RECORD" and a["target_system"] == "account_service":
        return "account"
    if a["details"].startswith("Policy action completed"):
        return "policy"
    return "other"


def _circ(d: float) -> float:
    d = d % PHASE_PERIOD
    return min(d, PHASE_PERIOD - d)


def _step_index(world: World) -> dict[str, int]:
    """Position of each deletion-worker action inside its run.

    Policy steps sit on fixed minutes (:31, :38, :45, :52). Follow-up steps of one run are spaced
    exactly 1110 s or 1169 s apart, so an action's position is the length of that exact-gap chain.
    """
    if hasattr(world, "_steps"):
        return world._steps
    dw = [a for a in world.activities if a["source_system"] == "deletion_worker"]
    steps = {}
    other = sorted((a for a in dw if _action_kind(a) == "other"), key=lambda a: a["t"])
    by_time = defaultdict(list)
    for a in other:
        by_time[a["t"]].append(a)
    for a in other:
        k = 0
        for g in FOLLOWUP_GAPS:
            for b in by_time.get(a["t"] - timedelta(seconds=g), []):
                k = max(k, steps[b["activity_id"]] + 1)
        steps[a["activity_id"]] = k
    for a in dw:
        if _action_kind(a) == "policy":
            steps[a["activity_id"]] = max(0, round((a["t"].minute - 31) / 7.0))
        elif _action_kind(a) == "account":
            steps[a["activity_id"]] = 0
    world._steps = steps
    return steps


def _raw_phase(world: World, a: dict) -> float:
    k = _step_index(world).get(a["activity_id"], 0)
    return (a["t"].timestamp() % PHASE_PERIOD + STEP_DRIFT * k) % PHASE_PERIOD


def learn_run_phase(world: World, requests: list[dict], subjects: dict, direct: dict) -> dict[str, float]:
    """Learn how each kind of run action is timed relative to the run's account deletion.

    All actions of one deletion run share a seconds-phase with that run's account deletion:
    (account phase - drift-corrected action phase) mod 30 s is constant per action kind. The
    constant is measured on runs whose records are all linked by identifiers (circular mean),
    then used to place identifier-less records into the right run.
    """
    import math

    diffs: dict[str, list] = defaultdict(list)
    for r in requests:
        linked = direct.get(r["case_id"])
        if not linked:
            continue
        run = [a for a in run_resources(world, r) if a["resource_id"] in linked]
        acc = [a for a in run if _action_kind(a) == "account"]
        if len(acc) != 1:
            continue
        pa = _raw_phase(world, acc[0])
        for a in run:
            k = _action_kind(a)
            if k != "account":
                diffs[k].append((pa - _raw_phase(world, a)) % PHASE_PERIOD)
    offsets = {"account": 0.0, "policy": 19.25, "other": 8.25}
    for k, vals in diffs.items():
        if len(vals) < 5:
            continue
        ang = [2 * math.pi * v / PHASE_PERIOD for v in vals]
        mean = math.atan2(sum(map(math.sin, ang)), sum(map(math.cos, ang)))
        offsets[k] = (mean * PHASE_PERIOD / (2 * math.pi)) % PHASE_PERIOD
    return offsets


def run_phase(world: World, a: dict, offsets: dict) -> tuple[float, float]:
    """(implied account-deletion phase, tolerance) for one run action."""
    k = _action_kind(a)
    return (_raw_phase(world, a) + offsets.get(k, 0.0)) % PHASE_PERIOD, TOL[k]


def assign_run_resources(world: World, requests: list[dict], subjects: dict, direct: dict) -> dict[str, set]:
    """Tie identifier-less resources to requests through the deletion run that touched them.

    Signal: every action of one deletion run shares the seconds-phase of that run's account
    deletion (after removing a 0.5 s drift per step; offsets learned by `learn_run_phase` from
    identifier-linked runs). Each request takes at most one such resource per system and only
    for systems it has no linked record in.

    1. best-first: identifier-less records whose phase matches a request's anchored run
    2. records nobody claims are grouped into runs by phase; each run goes, whole, to a request
       in its window that has no anchor yet
    3. hand-run deletions logged at the report time itself (no fingerprint) go to that request
    Records that fit no request are left out rather than guessed.
    """
    offsets = learn_run_phase(world, requests, subjects, direct)
    first_act = {}
    for rid, acts in world.act_by_rid.items():
        dw = [a for a in acts if a["source_system"] == "deletion_worker"]
        if dw and not world.idx.owners(world.owner_tokens.get(rid, set())):
            first_act[rid] = dw[0]
    phase_of = {rid: run_phase(world, a, offsets) for rid, a in first_act.items()}

    reqs = {r["case_id"]: r for r in requests if r["case_id"] in subjects}
    chains = run_chains(world)
    chain_start: dict[int, datetime] = {}
    for a in world.activities:
        if a["source_system"] == "deletion_worker":
            c = chains[a["activity_id"]]
            chain_start[c] = min(chain_start.get(c, a["t"]), a["t"])
    acct_time, has_sys, anchors = {}, {}, defaultdict(list)
    other_chains: dict[str, set] = defaultdict(set)   # follow-up chains already known per request
    for cid, r in reqs.items():
        linked = direct.get(cid, {})
        has_sys[cid] = {system_of(x) for x in linked}
        for a in run_resources(world, r):
            if a["resource_id"] not in linked:
                continue
            if _action_kind(a) == "account":
                acct_time[cid] = a["t"]
            if _action_kind(a) == "other":
                other_chains[cid].add(chains[a["activity_id"]])
            anchors[cid].append(run_phase(world, a, offsets))

    def in_window(cid, t, rid=None, strict=True):
        """Is the action at t (of record rid) placed where this request's run would put it?"""
        lo, hi = run_window(reqs[cid])
        if not (lo <= t <= hi):
            return False
        if rid is None or rid not in first_act:
            return True
        a = first_act[rid]
        kind = _action_kind(a)
        rep_t = parse_ts(reqs[cid]["reported_at"])
        if kind == "policy":
            return POLICY_LEAD[0] <= (rep_t - t).total_seconds() <= POLICY_LEAD[1]
        if not strict:
            return True
        if kind == "account":
            return ACCOUNT_LEAD[0] <= (rep_t - t).total_seconds() <= ACCOUNT_LEAD[1]
        start = chain_start.get(chains.get(a["activity_id"]), t)
        return FOLLOWUP_START[0] <= (start - rep_t).total_seconds() <= FOLLOWUP_START[1]

    def same_chain(cid, rid):
        """A follow-up must continue the run's own follow-up chain when one is known."""
        a = first_act[rid]
        if _action_kind(a) != "other" or not other_chains[cid]:
            return True
        return chains.get(a["activity_id"]) in other_chains[cid]

    def bucket(cid, t):
        return abs((parse_ts(reqs[cid]["reported_at"]) - t).total_seconds()) // 1800

    def gap(cid, t):
        at = acct_time.get(cid)
        return (t - at).total_seconds() if at and at <= t else 1e9

    def dist(p, q):
        """Phase distance in units of the combined tolerance (<= 1 means same run)."""
        return _circ(p[0] - q[0]) / (p[1] + q[1])

    out: dict[str, set] = defaultdict(set)
    done = set()

    def give(cid, rid):
        t = first_act[rid]["t"]
        out[cid].add(rid)
        done.add(rid)
        has_sys[cid].add(system_of(rid))
        anchors[cid].append(phase_of[rid])
        if _action_kind(first_act[rid]) == "other":
            other_chains[cid].add(chains.get(first_act[rid]["activity_id"]))
        if system_of(rid) == "account_service":
            acct_time[cid] = t

    # chain pass: a record whose exact-gap chain contains a step already linked to one request
    # is part of that request's run
    chain_owner: dict[int, set] = defaultdict(set)
    for cid, r in reqs.items():
        for a in run_resources(world, r):
            if a["resource_id"] in direct.get(cid, {}):
                chain_owner[chains[a["activity_id"]]].add(cid)
    for rid, a in sorted(first_act.items(), key=lambda x: x[1]["t"]):
        owners = chain_owner.get(chains.get(a["activity_id"]), set())
        if len(owners) != 1:
            continue
        cid = next(iter(owners))
        if rid in done or not in_window(cid, a["t"], rid):
            continue
        if system_of(rid) in has_sys[cid] and system_of(rid) != MULTI_SYSTEM:
            continue
        give(cid, rid)

    # pass 0: hand-run deletions - the request's own run happened at the report time itself,
    # so records the worker touched within minutes of that report belong to it
    for cid, r in reqs.items():
        rep_t = parse_ts(r["reported_at"])
        own = [a for a in run_resources(world, r) if a["resource_id"] in direct.get(cid, {})]
        if own and not all(abs((a["t"] - rep_t).total_seconds()) <= 1800 for a in own):
            continue
        # a run with policy steps (:31/:38/:45/:52 cadence) is a normal fingerprinted run,
        # not a hand-run deletion, even when those steps fall close to the report time
        if any(_action_kind(a) == "policy" for a in own):
            continue
        for rid, a in sorted(first_act.items(), key=lambda x: x[1]["t"]):
            if rid in done or system_of(rid) in has_sys[cid]:
                continue
            if abs((a["t"] - rep_t).total_seconds()) <= 900 and own:
                give(cid, rid)

    # pass 1: best-first matching against anchored runs
    pairs = []
    for rid, a in first_act.items():
        t = a["t"]
        for cid in reqs:
            if not anchors[cid] or not in_window(cid, t, rid):
                continue
            if system_of(rid) in has_sys[cid] and system_of(rid) != MULTI_SYSTEM:
                continue
            if not same_chain(cid, rid):
                continue
            d = max(dist(phase_of[rid], q) for q in anchors[cid])
            if d <= 1.0:
                pairs.append((d, bucket(cid, t), gap(cid, t), rid, cid))
    for d, _, _, rid, cid in sorted(pairs):
        # one record per system and run, except support: a person can have both a shared
        # conversation and an attachment of their own in the same deletion run
        if rid in done or (system_of(rid) in has_sys[cid] and system_of(rid) != MULTI_SYSTEM):
            continue
        if not same_chain(cid, rid):
            continue
        # re-check against anchors added since (a claimed record may have refined the run)
        if max(dist(phase_of[rid], q) for q in anchors[cid]) <= 1.0:
            give(cid, rid)

    # pass 2: unclaimed records form runs of their own; give each run to an unanchored request.
    # Actions owned by people with no request in the window seed "foreign" runs, so records
    # sharing their phase are recognised as someone else's and left alone.
    used = {a["activity_id"] for cid, r in reqs.items() for a in run_resources(world, r)
            if a["resource_id"] in direct.get(cid, {})}
    foreign = set()
    for a in world.activities:
        if a["source_system"] != "deletion_worker" or a["activity_id"] in used or a["resource_id"] in first_act:
            continue
        rid = a["resource_id"]
        if rid in phase_of:
            continue
        phase_of[rid] = run_phase(world, a, offsets)
        first_act[rid] = a
        foreign.add(rid)
    left = sorted((first_act[rid]["t"], rid) for rid in first_act if rid not in done)
    clusters: list[list] = []
    for t, rid in left:
        best = None
        for c in clusters:
            if system_of(rid) in {system_of(x) for _, x in c}:
                continue
            # two follow-ups of one run always sit on the same exact-gap chain
            if _action_kind(first_act[rid]) == "other" and any(
                    _action_kind(first_act[x]) == "other"
                    and chains.get(first_act[x]["activity_id"]) != chains.get(first_act[rid]["activity_id"]) for _, x in c):
                continue
            if abs((t - c[0][0]).total_seconds()) > 4 * 3600:
                continue
            d = max(dist(phase_of[rid], phase_of[x]) for _, x in c)
            if d <= 1.0 and (best is None or d < best[0]):
                best = (d, c)
        if best:
            best[1].append((t, rid))
        else:
            clusters.append([(t, rid)])
    clusters.sort(key=lambda c: (-len(c), c[0][0]))
    queue = list(clusters)   # clusters no request can take whole are re-queued member by member
    i = -1
    while i + 1 < len(queue):
        i += 1
        c = queue[i]
        if any(rid in foreign for _, rid in c) or any(rid in done for _, rid in c):
            continue
        systems = {system_of(x) for _, x in c}
        t0 = min(t for t, _ in c)
        if len(c) == 1:
            # a lone record that also fits another request's anchored run is a coin flip:
            # leaving it out costs one missed step, a wrong guess costs a missed and a wrong one
            rid0 = c[0][1]
            sys0 = system_of(rid0)
            # only runs that could still take this record compete (one account per run, etc.)
            if any(anchors[cid] and in_window(cid, t0, rid0)
                   and (sys0 not in has_sys[cid] or sys0 == MULTI_SYSTEM)
                   and max(dist(phase_of[rid0], q) for q in anchors[cid]) <= 1.0 for cid in reqs):
                continue
        cands = [(bucket(cid, t0), gap(cid, t0), cid) for cid in reqs
                 if not anchors[cid] and not (systems & has_sys[cid]) and all(in_window(cid, t, x) for t, x in c)]
        if cands:
            cid = min(cands)[-1]
            for _, rid in c:
                give(cid, rid)
        elif len(c) > 1:
            queue.extend([[m] for m in c])

    # pass 3: hand-run deletions logged right at the report time (no fingerprint applies)
    for t, rid in left:
        if rid in done or rid in foreign:
            continue
        cands = [(abs((parse_ts(reqs[cid]["reported_at"]) - t).total_seconds()), cid) for cid in reqs
                 if system_of(rid) not in has_sys[cid] and bucket(cid, t) == 0 and in_window(cid, t, rid, strict=False)
                 and (not anchors[cid] or max(dist(phase_of[rid], q) for q in anchors[cid]) <= 1.0)]
        if cands:
            give(min(cands)[-1], rid)
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
