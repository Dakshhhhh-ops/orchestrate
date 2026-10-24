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


LEARN_WINDOW = timedelta(hours=6)   # generous search span used only while learning the model


def run_window(req: dict, world: "World | None" = None) -> tuple[datetime, datetime]:
    """Time span in which the deletion workflow acted for this request.

    Learned from identifier-linked runs (see `learn_run_model`) once a model exists on the world;
    before that a generous span is used so the learner itself can see whole runs.
    """
    rep = parse_ts(req["reported_at"])
    model = getattr(world, "run_model", None) if world is not None else None
    if model is None:
        return rep - LEARN_WINDOW, rep + LEARN_WINDOW
    return rep + timedelta(seconds=model.window[0]), rep + timedelta(seconds=model.window[1])


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
    lo, hi = run_window(req, world)
    return [a for a in world.activities if lo <= a["t"] <= hi and a["source_system"] == "deletion_worker"]


# --------------------------------------------------------------------------- run timing model
#
# The deletion worker leaves a timing fingerprint: the actions of one run are spaced by a few
# exact intervals and share a seconds-phase with the run's account deletion. Nothing about this
# fingerprint is hard-coded. `learn_run_model` measures it on runs whose records are already
# linked to a person by identifiers (no labels, no answer key), using these documented rules:
#
#   period      the cycle length P in 2..120 s that best ranks each linked action's own account
#               deletion first among the candidate accounts around it (phase distance mod P)
#   gaps        spacings between consecutive linked steps of one kind that recur at least
#               max(3, 2 %) times; one skipped step is allowed by also accepting pairwise sums
#   slots       minute-of-hour values of linked policy steps that recur at least max(3, 2 %)
#               times; a policy step's position in its run is the index of its nearest slot
#   drift       least-squares slope of the phase residual against step position, per kind
#   offset      circular mean of the drift-corrected residual, per kind
#   tolerance   99th percentile of |residual - offset| per kind; account deletions get half
#               a second (timestamps are whole seconds)
#   time slots  central 98 % of (report - account), (report - policy step) and
#               (first follow-up - report) on linked runs, widened by MARGIN
#   window      central 99 % of all linked run actions around the report, widened by MARGIN
#   multi       systems where at least max(3, 1 %) linked runs touch two records of the system

MARGIN = 5 * 60.0       # seconds added on both sides of every learned time range
FLAGS_DEFAULT = {
    "chains": True,          # exact-gap chains: chain pass, same-chain rule, chain-aware clustering
    "slots": True,           # learned time slots for accounts, policy steps and follow-up chains
    "hand_run": True,        # pass 0: runs logged at the report time itself
    "clustering": True,      # pass 2: unclaimed records grouped by phase, given to unanchored requests
    "pass3": True,           # pass 3: hand-run fallback for leftovers near the report time
    "multi": True,           # allow two records of a learned multi-record system per run
    "singleton_drop": True,  # leave out a lone record that also fits another open run
}


@dataclass
class RunModel:
    period: float
    gaps: dict            # kind -> recurring exact gaps between consecutive steps (s)
    slots: tuple          # policy minute-of-hour slots
    drift: dict           # kind -> seconds of phase drift per step
    offset: dict          # kind -> phase offset to the account deletion
    tol: dict             # kind -> phase tolerance (s)
    account_lead: tuple   # (min, max) seconds before the report
    policy_lead: tuple
    followup_start: tuple # (min, max) seconds after the report
    window: tuple         # (min, max) seconds around the report
    multi: frozenset      # systems that may hold two records in one run
    n_runs: int = 0
    n_actions: int = 0

    def summary(self) -> dict:
        r = lambda x: round(x, 2) if isinstance(x, float) else x  # noqa: E731
        return {
            "period_s": self.period,
            "gaps_s": {k: list(v) + [f"+sums of two ({len(v) * (len(v) + 1) // 2})"] for k, v in self.gaps.items()},
            "policy_minute_slots": list(self.slots),
            "drift_s_per_step": {k: r(v) for k, v in self.drift.items()},
            "offset_s": {k: r(v) for k, v in self.offset.items()},
            "tolerance_s": {k: r(v) for k, v in self.tol.items()},
            "account_lead_min": [round(x / 60, 1) for x in self.account_lead],
            "policy_lead_min": [round(x / 60, 1) for x in self.policy_lead],
            "followup_start_min": [round(x / 60, 1) for x in self.followup_start],
            "window_min": [round(x / 60, 1) for x in self.window],
            "multi_systems": sorted(self.multi), "linked_runs": self.n_runs, "linked_actions": self.n_actions,
        }


def _action_kind(a: dict) -> str:
    """Which part of a deletion run an activity belongs to (account delete, policy step, follow-up)."""
    if a["event_type"] == "DELETE_RECORD" and a["target_system"] == "account_service":
        return "account"
    if a["details"].startswith("Policy action completed"):
        return "policy"
    return "other"


def _circ(d: float, period: float) -> float:
    d = d % period
    return min(d, period - d)


def _quantile(vals: list, q: float) -> float:
    s = sorted(vals)
    if not s:
        return 0.0
    i = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return s[i]


def _recurring(vals: list, frac: float, floor: int = 3) -> list:
    from collections import Counter

    c = Counter(vals)
    need = max(floor, frac * len(vals))
    return sorted(v for v, n in c.items() if n >= need)


def _circ_mean(vals: list, period: float) -> float:
    import math

    if not vals:
        return 0.0
    ang = [2 * math.pi * v / period for v in vals]
    m = math.atan2(sum(map(math.sin, ang)), sum(map(math.cos, ang)))
    return (m * period / (2 * math.pi)) % period


def _dw_actions(world: World) -> list[dict]:
    if "_dw" not in world.__dict__:
        world._dw = [a for a in world.activities if a["source_system"] == "deletion_worker"]
    return world._dw


def _linked_runs(world: World, requests: list[dict], direct: dict) -> list[tuple[dict, list]]:
    """(request, its identifier-linked deletion-worker actions in the learning span)."""
    out = []
    for r in requests:
        linked = direct.get(r["case_id"])
        if not linked:
            continue
        rep = parse_ts(r["reported_at"])
        acts = [a for rid in linked for a in world.act_by_rid.get(rid, [])
                if a["source_system"] == "deletion_worker" and abs(a["t"] - rep) <= LEARN_WINDOW]
        if acts:
            out.append((r, sorted(acts, key=lambda a: a["t"])))
    return out


def _chain_ids(world: World, gaps: dict) -> dict[str, int]:
    parent: dict[str, str] = {}

    def find(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    dw = _dw_actions(world)
    for kind, base in gaps.items():
        gs = sorted(set(base) | {x + y for x in base for y in base})
        acts = [a for a in dw if _action_kind(a) == kind]
        by_t = defaultdict(list)
        for a in acts:
            by_t[a["t"]].append(a)
        for a in acts:
            for g in gs:
                for b in by_t.get(a["t"] - timedelta(seconds=g), []):
                    parent[find(a["activity_id"])] = find(b["activity_id"])
    ids, out = {}, {}
    for a in dw:
        out[a["activity_id"]] = ids.setdefault(find(a["activity_id"]), len(ids))
    return out


def _steps(world: World, gaps: dict, slots: tuple) -> dict[str, int]:
    """Position of each deletion-worker action inside its run (chain position / policy slot)."""
    dw = _dw_actions(world)
    steps: dict[str, int] = {}
    other = sorted((a for a in dw if _action_kind(a) == "other"), key=lambda a: a["t"])
    by_t = defaultdict(list)
    for a in other:
        by_t[a["t"]].append(a)
    for a in other:
        k = 0
        for g in gaps.get("other", ()):
            for b in by_t.get(a["t"] - timedelta(seconds=g), []):
                k = max(k, steps.get(b["activity_id"], 0) + 1)
        steps[a["activity_id"]] = k
    for a in dw:
        kind = _action_kind(a)
        if kind == "policy" and slots:
            steps[a["activity_id"]] = min(range(len(slots)), key=lambda i: abs(a["t"].minute - slots[i]))
        elif kind != "other":
            steps[a["activity_id"]] = 0
    return steps


def learn_run_model(world: World, requests: list[dict], direct: dict) -> RunModel:
    """Measure the deletion-run timing fingerprint on identifier-linked runs (see block comment)."""
    runs = _linked_runs(world, requests, direct)
    kinds = ("policy", "other")
    # --- exact gaps between consecutive linked steps of one kind
    gap_obs = defaultdict(list)
    for _, acts in runs:
        for kind in kinds:
            ks = [a for a in acts if _action_kind(a) == kind]
            gap_obs[kind] += [(y["t"] - x["t"]).total_seconds() for x, y in zip(ks, ks[1:])]
    gaps = {}
    for kind in kinds:
        gaps[kind] = tuple(_recurring(gap_obs[kind], 0.02))   # skipped steps handled in _chain_ids
    # --- policy minute slots
    minutes = [a["t"].minute for _, acts in runs for a in acts if _action_kind(a) == "policy"]
    slots = tuple(_recurring(minutes, 0.02))
    steps = _steps(world, gaps, slots)
    # --- pairs (account action, other action) inside one linked run
    pairs = []
    for _, acts in runs:
        acc = [a for a in acts if _action_kind(a) == "account"]
        if len(acc) != 1:
            continue
        for a in acts:
            if _action_kind(a) in kinds:
                pairs.append((acc[0], a))
    # --- period: best ranking of the true account among nearby linked accounts
    accounts = [acc for acc, _ in pairs]
    acc_by_id = {a["activity_id"]: a for a in accounts}
    acc_list = sorted(acc_by_id.values(), key=lambda a: a["t"])

    def rank_score(period):
        offs = {}
        for kind in kinds:
            offs[kind] = _circ_mean([(acc["t"].timestamp() - a["t"].timestamp()) % period
                                     for acc, a in pairs if _action_kind(a) == kind], period)
        hits = n = 0
        for acc, a in pairs:
            cands = [c for c in acc_list if abs((c["t"] - a["t"]).total_seconds()) <= 4 * 3600]
            if len(cands) < 2:
                continue
            p = (a["t"].timestamp() + offs[_action_kind(a)]) % period
            best = min(cands, key=lambda c: (_circ(c["t"].timestamp() - p, period), abs((c["t"] - a["t"]).total_seconds())))
            hits += best is acc
            n += 1
        return hits / n if n else 0.0

    scores = {p: rank_score(float(p)) for p in range(2, 121)} if pairs else {30: 0.0}
    period = float(max(scores, key=lambda p: (round(scores[p], 3), -p)))
    # --- drift, offset, tolerance per kind
    drift, offset, tol = {"account": 0.0}, {"account": 0.0}, {"account": 0.5}
    for kind in kinds:
        rows = [(steps.get(a["activity_id"], 0), (acc["t"].timestamp() - a["t"].timestamp()) % period)
                for acc, a in pairs if _action_kind(a) == kind]
        if not rows:
            drift[kind], offset[kind], tol[kind] = 0.0, 0.0, 1.0
            continue
        c0 = _circ_mean([r for _, r in rows], period)
        unwrapped = [(k, ((r - c0 + period / 2) % period) - period / 2) for k, r in rows]
        mk = sum(k for k, _ in unwrapped) / len(unwrapped)
        mr = sum(r for _, r in unwrapped) / len(unwrapped)
        var = sum((k - mk) ** 2 for k, _ in unwrapped)
        slope = sum((k - mk) * (r - mr) for k, r in unwrapped) / var if var else 0.0
        drift[kind] = slope     # residual grows by `slope` per step; phase() adds it back
        corrected = [r - slope * k for k, r in unwrapped]
        off = sum(corrected) / len(corrected)
        offset[kind] = (c0 + off) % period
        tol[kind] = max(0.5, _quantile([abs(x - off) for x in corrected], 0.99))
    # --- time slots and window
    acc_lead, pol_lead, fol_start, window = [], [], [], []
    for r, acts in runs:
        rep = parse_ts(r["reported_at"])
        window += [(a["t"] - rep).total_seconds() for a in acts]
        acc_lead += [(rep - a["t"]).total_seconds() for a in acts if _action_kind(a) == "account"]
        pol_lead += [(rep - a["t"]).total_seconds() for a in acts if _action_kind(a) == "policy"]
        oth = [a for a in acts if _action_kind(a) == "other"]
        if oth:
            fol_start.append((oth[0]["t"] - rep).total_seconds())

    def span(vals, q):
        if not vals:
            return (-LEARN_WINDOW.total_seconds(), LEARN_WINDOW.total_seconds())
        return (_quantile(vals, q) - MARGIN, _quantile(vals, 1 - q) + MARGIN)

    # --- systems that hold two records in one run
    from collections import Counter

    multi_runs = Counter()
    for _, acts in runs:
        per = Counter(system_of(a["resource_id"]) for a in {a["resource_id"]: a for a in acts}.values())
        for s, n in per.items():
            if n >= 2:
                multi_runs[s] += 1
    multi = frozenset(s for s, n in multi_runs.items() if n >= max(3, 0.01 * len(runs)))
    return RunModel(period=period, gaps=gaps, slots=slots, drift=drift, offset=offset, tol=tol,
                    account_lead=span(acc_lead, 0.01), policy_lead=span(pol_lead, 0.01),
                    followup_start=span(fol_start, 0.01), window=span(window, 0.005), multi=multi,
                    n_runs=len(runs), n_actions=sum(len(a) for _, a in runs))


class _Phases:
    """Phase arithmetic for one learned model (chains and step positions cached per model)."""

    def __init__(self, world: World, model: RunModel):
        self.world, self.m = world, model
        key = (model.gaps.get("policy"), model.gaps.get("other"), model.slots)
        cache = world.__dict__.setdefault("_phase_cache", {})
        if key not in cache:
            cache[key] = (_chain_ids(world, model.gaps), _steps(world, model.gaps, model.slots))
        self.chains, self.steps = cache[key]

    def phase(self, a: dict) -> tuple[float, float]:
        """(implied account-deletion phase, tolerance) for one run action."""
        k = _action_kind(a)
        m = self.m
        raw = a["t"].timestamp() % m.period + m.drift.get(k, 0.0) * self.steps.get(a["activity_id"], 0)
        return (raw + m.offset.get(k, 0.0)) % m.period, m.tol.get(k, 1.0)

    def dist(self, p, q) -> float:
        """Phase distance in units of the combined tolerance (<= 1 means same run)."""
        return _circ(p[0] - q[0], self.m.period) / (p[1] + q[1])


def assign_run_resources(world: World, requests: list[dict], subjects: dict, direct: dict,
                         model: RunModel | None = None, flags: dict | None = None) -> dict[str, set]:
    """Tie identifier-less resources to requests through the deletion run that touched them.

    The run timing model is learned from identifier-linked runs unless one is passed in (the
    cross-validation check passes a model learned on training months only). Each request takes
    at most one such resource per system (two for learned multi-record systems) and only for
    systems it has no linked record in. `flags` switches passes off for ablation.

    chain pass  a record on the exact-gap chain of a step already linked to one request joins it
    pass 0      hand-run deletions logged at the report time itself
    pass 1      best-first: phase matches a request's anchored run (all anchors must fit)
    pass 2      unclaimed records grouped into runs by phase; each run goes whole to an unanchored
                request; runs anchored by people without a request are left alone
    pass 3      leftovers within half an hour of a report, when the phase does not contradict
    Records that fit no request are left out rather than guessed.
    """
    f = dict(FLAGS_DEFAULT, **(flags or {}))
    if model is None:
        world.run_model = None
        model = learn_run_model(world, requests, direct)
    world.run_model = model
    ph = _Phases(world, model)
    chains = ph.chains
    multi = model.multi if f["multi"] else frozenset()
    window_len = model.window[1] - model.window[0]

    first_act = {}
    for rid, acts in world.act_by_rid.items():
        dw = [a for a in acts if a["source_system"] == "deletion_worker"]
        if dw and not world.idx.owners(world.owner_tokens.get(rid, set())):
            first_act[rid] = dw[0]
    phase_of = {rid: ph.phase(a) for rid, a in first_act.items()}

    reqs = {r["case_id"]: r for r in requests if r["case_id"] in subjects}
    chain_start: dict[int, datetime] = {}
    for a in _dw_actions(world):
        c = chains[a["activity_id"]]
        chain_start[c] = min(chain_start.get(c, a["t"]), a["t"])
    acct_time, has_sys, anchors = {}, {}, defaultdict(list)
    other_chains: dict[str, set] = defaultdict(set)
    run_cache = {cid: run_resources(world, r) for cid, r in reqs.items()}
    for cid, r in reqs.items():
        linked = direct.get(cid, {})
        has_sys[cid] = {system_of(x) for x in linked}
        for a in run_cache[cid]:
            if a["resource_id"] not in linked:
                continue
            if _action_kind(a) == "account":
                acct_time[cid] = a["t"]
            if _action_kind(a) == "other":
                other_chains[cid].add(chains[a["activity_id"]])
            anchors[cid].append(ph.phase(a))

    def open_for(cid, rid):
        s = system_of(rid)
        return s not in has_sys[cid] or s in multi

    def in_window(cid, t, rid=None, strict=True):
        """Is the action at t (of record rid) placed where this request's run would put it?"""
        lo, hi = run_window(reqs[cid], world)
        if not (lo <= t <= hi):
            return False
        if rid is None or rid not in first_act or not f["slots"]:
            return True
        a = first_act[rid]
        kind = _action_kind(a)
        rep_t = parse_ts(reqs[cid]["reported_at"])
        if kind == "policy":
            return model.policy_lead[0] <= (rep_t - t).total_seconds() <= model.policy_lead[1]
        if not strict:
            return True
        if kind == "account":
            return model.account_lead[0] <= (rep_t - t).total_seconds() <= model.account_lead[1]
        start = chain_start.get(chains.get(a["activity_id"]), t)
        return model.followup_start[0] <= (start - rep_t).total_seconds() <= model.followup_start[1]

    def same_chain(cid, rid):
        """A follow-up must continue the run's own follow-up chain when one is known."""
        if not f["chains"]:
            return True
        a = first_act[rid]
        if _action_kind(a) != "other" or not other_chains[cid]:
            return True
        return chains.get(a["activity_id"]) in other_chains[cid]

    def bucket(cid, t):
        return abs((parse_ts(reqs[cid]["reported_at"]) - t).total_seconds()) // 1800

    def gap(cid, t):
        at = acct_time.get(cid)
        return (t - at).total_seconds() if at and at <= t else 1e9

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

    # chain pass
    if f["chains"]:
        chain_owner: dict[int, set] = defaultdict(set)
        for cid in reqs:
            for a in run_cache[cid]:
                if a["resource_id"] in direct.get(cid, {}):
                    chain_owner[chains[a["activity_id"]]].add(cid)
        for rid, a in sorted(first_act.items(), key=lambda x: x[1]["t"]):
            owners = chain_owner.get(chains.get(a["activity_id"]), set())
            if len(owners) != 1:
                continue
            cid = next(iter(owners))
            if rid in done or not in_window(cid, a["t"], rid) or not open_for(cid, rid):
                continue
            give(cid, rid)

    # pass 0: hand-run deletions
    if f["hand_run"]:
        for cid, r in reqs.items():
            rep_t = parse_ts(r["reported_at"])
            own = [a for a in run_cache[cid] if a["resource_id"] in direct.get(cid, {})]
            if not own or not all(abs((a["t"] - rep_t).total_seconds()) <= 1800 for a in own):
                continue
            if any(_action_kind(a) == "policy" for a in own):
                continue
            for rid, a in sorted(first_act.items(), key=lambda x: x[1]["t"]):
                if rid in done or system_of(rid) in has_sys[cid]:
                    continue
                if abs((a["t"] - rep_t).total_seconds()) <= 900:
                    give(cid, rid)

    # pass 1: best-first matching against anchored runs
    pairs = []
    for rid, a in first_act.items():
        t = a["t"]
        for cid in reqs:
            if not anchors[cid] or not open_for(cid, rid) or not in_window(cid, t, rid) or not same_chain(cid, rid):
                continue
            d = max(ph.dist(phase_of[rid], q) for q in anchors[cid])
            if d <= 1.0:
                pairs.append((d, bucket(cid, t), gap(cid, t), rid, cid))
    for d, _, _, rid, cid in sorted(pairs):
        if rid in done or not open_for(cid, rid) or not same_chain(cid, rid):
            continue
        if max(ph.dist(phase_of[rid], q) for q in anchors[cid]) <= 1.0:
            give(cid, rid)

    # pass 2: clustering of unclaimed records
    used = {a["activity_id"] for cid in reqs for a in run_cache[cid] if a["resource_id"] in direct.get(cid, {})}
    foreign = set()
    for a in _dw_actions(world):
        rid = a["resource_id"]
        if a["activity_id"] in used or rid in first_act or rid in phase_of:
            continue
        phase_of[rid] = ph.phase(a)
        first_act[rid] = a
        foreign.add(rid)
    left = sorted((first_act[rid]["t"], rid) for rid in first_act if rid not in done)
    if f["clustering"]:
        clusters: list[list] = []
        for t, rid in left:
            best = None
            for c in clusters:
                if system_of(rid) in {system_of(x) for _, x in c}:
                    continue
                if f["chains"] and _action_kind(first_act[rid]) == "other" and any(
                        _action_kind(first_act[x]) == "other"
                        and chains.get(first_act[x]["activity_id"]) != chains.get(first_act[rid]["activity_id"]) for _, x in c):
                    continue
                if abs((t - c[0][0]).total_seconds()) > window_len:
                    continue
                d = max(ph.dist(phase_of[rid], phase_of[x]) for _, x in c)
                if d <= 1.0 and (best is None or d < best[0]):
                    best = (d, c)
            if best:
                best[1].append((t, rid))
            else:
                clusters.append([(t, rid)])
        clusters.sort(key=lambda c: (-len(c), c[0][0]))
        queue = list(clusters)
        i = -1
        while i + 1 < len(queue):
            i += 1
            c = queue[i]
            if any(rid in foreign or rid in done for _, rid in c):
                continue
            systems = {system_of(x) for _, x in c}
            t0 = min(t for t, _ in c)
            if len(c) == 1 and f["singleton_drop"]:
                rid0 = c[0][1]
                if any(anchors[cid] and in_window(cid, t0, rid0) and open_for(cid, rid0)
                       and max(ph.dist(phase_of[rid0], q) for q in anchors[cid]) <= 1.0 for cid in reqs):
                    continue
            cands = [(bucket(cid, t0), gap(cid, t0), cid) for cid in reqs
                     if not anchors[cid] and not (systems & has_sys[cid]) and all(in_window(cid, t, x) for t, x in c)]
            if cands:
                cid = min(cands)[-1]
                for _, rid in c:
                    give(cid, rid)
            elif len(c) > 1:
                queue.extend([[m] for m in c])

    # pass 3: hand-run fallback
    if f["pass3"]:
        for t, rid in left:
            if rid in done or rid in foreign:
                continue
            cands = [(abs((parse_ts(reqs[cid]["reported_at"]) - t).total_seconds()), cid) for cid in reqs
                     if system_of(rid) not in has_sys[cid] and bucket(cid, t) == 0 and in_window(cid, t, rid, strict=False)
                     and (not anchors[cid] or max(ph.dist(phase_of[rid], q) for q in anchors[cid]) <= 1.0)]
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
