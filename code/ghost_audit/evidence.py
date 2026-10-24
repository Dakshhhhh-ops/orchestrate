"""Evidence extraction.

Every evidence file (SQL snapshot, JSON export, log, text handover, PDF, screenshot facts)
is turned into a flat list of `Obs` records: one observation of one resource at one time,
with the identifiers it contains, the identifiers used to look it up, and the state facts
(present / exposed / flow enabled / restore allowed) it establishes.

Everything downstream (identity linking, state reconstruction, planning) works on Obs only,
so the format zoo stays inside this module.
"""
from __future__ import annotations

import csv
import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .common import (
    EMAIL_RE,
    RESOURCE_RE,
    identifier_tokens,
    is_null,
    norm_name,
    parse_ts,
    prefix_of,
    system_of,
)

# Fields whose non-null value means a direct identifier is still exposed in the record.
DIRECT_ID_FIELDS = {
    "full_name", "name", "display_name", "requester_name", "customer_name",
    "email", "requester_email", "owner_email", "customer_email", "contact_email",
    "phone", "requester_phone", "owner_phone", "customer_phone", "email_hash",
    "matched_value", "uploaded_by",
}
# Fields that link a record to a subject without being an exposed identifier on their own
# (stable internal references are allowed to remain on tombstones / retained records).
REFERENCE_FIELDS = {"customer_reference", "user_id", "subject_reference", "subject_ref", "subject_selector"}
LOOKUP_FIELDS = {
    "lookup_key", "match_key", "subject_key", "scan_key", "subject_ref", "correlation_key",
    "subject_reference", "subject_selector", "owner_key", "lookup_value",
}


@dataclass
class Obs:
    eid: str                      # evidence file_id (or activity id)
    captured: datetime            # capture time of the evidence item
    ts: datetime                  # time the observation describes
    rid: str                      # resource id
    system: str                   # canonical system id
    present: bool | None = None   # does the record exist
    exposed: bool | None = None   # are direct identifiers still in it
    enabled: bool | None = None   # data flow enabled
    restore: bool | None = None   # restore allowed
    ids: set = field(default_factory=set)    # identifier tokens contained in the record
    keys: set = field(default_factory=set)   # identifier tokens used to find the record
    names: set = field(default_factory=set)  # normalised person names in the record
    record_type: str = ""         # data category if the source states it
    variant: str = ""             # table / replica / store variant
    source: str = ""              # parser that produced it
    note: str = ""


def _bool(v) -> bool | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in {"1", "true", "yes", "y", "enabled", "allowed", "active", "on"}:
        return True
    if s in {"0", "false", "no", "n", "disabled", "denied", "deny", "blocked", "off", "stopped"}:
        return False
    return None


def _flatten(d, prefix="") -> dict:
    out = {}
    if isinstance(d, dict):
        for k, v in d.items():
            if isinstance(v, dict):
                out.update(_flatten(v, prefix))
            elif isinstance(v, list):
                out[k] = v
            else:
                out[k] = v
    return out


RESULT_PRESENT = {
    "FOUND": True, "PRESENT": True, "IDENTIFIABLE": True, "ACTIVE": True, "EXPOSED": True,
    "NOT_FOUND": False, "ABSENT": False, "DELETED": False, "MISSING": False,
    "ANONYMIZED": True, "REDACTED": True, "BLOCKED": True, "DISABLED": True, "RETAINED": True,
}


def interpret(rid: str, f: dict, ob: Obs) -> Obs:
    """Fill state facts of `ob` from a flat field dict describing resource `rid`."""
    pfx = prefix_of(rid)
    low = {str(k).lower(): v for k, v in f.items()}

    # identifiers contained in the record
    exposed_vals = []
    for k, v in low.items():
        if k in DIRECT_ID_FIELDS and not is_null(v):
            if k in ("matched_value", "uploaded_by") or k.endswith("email") or k.endswith("phone") or k.endswith("hash") or "name" in k:
                exposed_vals.append(v)
                if "name" in k and isinstance(v, str) and "@" not in v and not v.endswith(".pdf"):
                    ob.names.add(norm_name(v))
        if k in REFERENCE_FIELDS and not is_null(v):
            ob.ids |= identifier_tokens(v)
    for v in exposed_vals:
        ob.ids |= identifier_tokens(v)

    # free-text fields: identifiers written inside messages count as exposed
    for k in ("message_text", "message_excerpt", "message", "body", "note_text"):
        v = low.get(k)
        if isinstance(v, str) and not is_null(v):
            toks = {t for t in identifier_tokens(v) if not t.startswith("ref:")}
            if toks:
                ob.ids |= toks
                exposed_vals.append(v)
    has_direct = bool(exposed_vals)
    di = low.get("direct_identifiers")
    if di is not None and str(di).isdigit():
        has_direct = has_direct or int(di) > 0

    result = str(low.get("result", "") or "").strip().upper()
    rt = str(low.get("record_type", "") or "").strip().upper()
    if rt:
        ob.record_type = rt

    # --- presence
    present = None
    if result in RESULT_PRESENT:
        present = RESULT_PRESENT[result]
    if result in {"HIT"}:
        present = True
    if result in {"MISS", "NO_HIT"}:
        present = False
    rows = low.get("rows")
    if rows is not None and str(rows).isdigit():
        present = int(rows) > 0
    hs = low.get("http_status") or low.get("status_code")
    if hs is not None and str(hs).isdigit():
        present = int(hs) < 400
    th = low.get("total_hits")
    if th is not None and str(th).isdigit():
        present = int(th) > 0
    for k in ("available", "download_available", "archive_present"):
        if k in low and _bool(low[k]) is not None and pfx in ("export", "backup"):
            present = _bool(low[k]) if k != "archive_present" else (present if present is not None else _bool(low[k]))
    if "marker_found" in low and _bool(low["marker_found"]) is False:
        present = True
    st = str(low.get("account_status", "") or "").upper()
    if st in {"DELETED", "CLOSED", "PURGED"} and pfx == "account":
        present = False
    if "profile" in f and f.get("profile") is None and hs is not None:
        present = int(hs) < 400 if str(hs).isdigit() else present
    ob.present = present

    # --- exposure
    if result == "IDENTIFIABLE" or result == "EXPOSED":
        ob.exposed = True
    elif result in {"ANONYMIZED", "REDACTED"}:
        ob.exposed = False
    elif present is False:
        ob.exposed = False
    else:
        id_fields_seen = any(k in DIRECT_ID_FIELDS for k in low) or di is not None
        if id_fields_seen:
            ob.exposed = has_direct
        elif "message_text" in low or "message_excerpt" in low:
            ob.exposed = has_direct
        elif low.get("event") in {"message_pii_scan", "attachment_pii_scan", "analytics_identity_scan"}:
            ob.exposed = True
        elif result == "FOUND" and pfx in ("tombstone", "support", "analytics") and ob.ids:
            ob.exposed = any(t.startswith(("email:", "phone:", "hash:")) for t in ob.ids)

    # --- data flow
    if pfx == "data_flow":
        en = None
        for k in ("enabled", "scheduler_registered"):
            if k in low and _bool(low[k]) is not None:
                en = _bool(low[k]) if en is None else (en or _bool(low[k]))
                break
        if result in {"ACTIVE"}:
            en = True
        if result in {"DISABLED", "STOPPED", "INACTIVE"}:
            en = False
        ob.enabled = en
        if en is not None:
            ob.present = True

    # --- backup restore
    if pfx == "backup":
        ra = None
        if "restore_allowed" in low:
            ra = _bool(low["restore_allowed"])
        if "eligible" in low:
            ra = _bool(low["eligible"])
            if _bool(low.get("deletion_block")) is True:
                ra = False
        if "decision" in low:
            d = str(low["decision"]).lower()
            ra = d in {"allow", "allowed", "approve", "approved", "permit"}
        if result == "ACTIVE":
            ra = True
        if result in {"BLOCKED", "DENIED"}:
            ra = False
        ob.restore = ra
        if ra is not None and ob.present is None:
            ob.present = True
    return ob


# --------------------------------------------------------------------------- SQL

def parse_sql(path: Path, eid: str, captured: datetime) -> list[Obs]:
    text = path.read_text(encoding="utf-8")
    con = sqlite3.connect(":memory:")
    try:
        con.executescript(text)
    except sqlite3.Error:
        # tolerate partial scripts
        con = sqlite3.connect(":memory:")
        for stmt in text.split(";\n"):
            try:
                con.execute(stmt)
            except sqlite3.Error:
                pass
    out: list[Obs] = []
    scope: dict[str, datetime] = {}
    tables = [r[0] for r in con.execute("select name from sqlite_master where type='table'")]
    if "snapshot_scope" in tables:
        for sysname, cap in con.execute("select origin_system, captured_at from snapshot_scope"):
            scope[sysname] = parse_ts(cap) or captured
    for t in tables:
        if t == "snapshot_scope":
            continue
        cur = con.execute(f'select * from "{t}"')
        cols = [c[0] for c in cur.description]
        for row in cur.fetchall():
            rec = dict(zip(cols, row))
            rid = rec.get("resource_id")
            if not rid:
                continue
            fields = dict(rec)
            if "record_json" in rec:
                try:
                    fields.update(_flatten(json.loads(rec["record_json"])))
                except (TypeError, ValueError):
                    pass
            sysid = system_of(rid) or fields.get("origin_system", "")
            ts = scope.get(sysid) or captured
            ob = Obs(eid, captured, ts, rid, sysid, variant=t, source="sql")
            interpret(rid, fields, ob)
            if ob.present is None:
                ob.present = True
            if "source_resource_id" in fields and fields["source_resource_id"]:
                ob.keys.add("rid:" + str(fields["source_resource_id"]))
            out.append(ob)
    # record which systems this snapshot covers (used for absence-by-omission)
    covered = set(scope) if scope else set()
    for t in tables:
        for pfx, sysid in (("profile", "account_service"), ("search", "search_service"),
                           ("analytics", "analytics_platform"), ("support", "support_platform"),
                           ("export", "export_service"), ("connector", "integration_service"),
                           ("backup", "backup_service"), ("security", "security_service")):
            if pfx in t:
                covered.add(sysid)
        if t.startswith("customer_data_"):
            # a generic customer-data table holds every system's rows for the people it covers,
            # so a person present in it with no account row has no account in that store
            covered.add("account_service")
            for r in con.execute(f'select distinct origin_system from "{t}"'):
                covered.add(r[0])
    con.close()
    SCOPES[eid] = covered
    return out


SCOPES: dict[str, set] = {}   # eid -> systems whose full contents (for covered subjects) are in the file


# --------------------------------------------------------------------------- JSON

def _walk_json(node, ctx: dict, out: list, eid: str, captured: datetime):
    if isinstance(node, dict):
        ctx = dict(ctx)
        for k in ("captured_at", "recorded_at", "observed_at", "exported_at", "data_store", "collection", "system", "source"):
            if k in node and not isinstance(node[k], (dict, list)):
                ctx[k] = node[k]
        if "resource_id" in node and isinstance(node.get("resource_id"), str):
            rid = node["resource_id"]
            fields = _flatten(node)
            hits = node.get("hits")
            if isinstance(hits, dict):
                for d in hits.get("documents") or []:
                    if isinstance(d, dict) and d.get("resource_id") in (None, rid):
                        fields.update(_flatten(d))
                if str(hits.get("total", "")).isdigit():
                    fields["total_hits"] = hits["total"]
                fields.pop("total", None)
            sysid = system_of(rid) or ctx.get("system") or ctx.get("data_store", "")
            ts = parse_ts(node.get("recorded_at") or node.get("observed_at") or ctx.get("captured_at")
                          or ctx.get("recorded_at") or ctx.get("exported_at")) or captured
            ob = Obs(eid, captured, ts, rid, sysid, variant=str(ctx.get("collection") or ctx.get("data_store") or ""),
                     source="json")
            interpret(rid, fields, ob)
            if ob.present is None:
                ob.present = True
            for k in ("subject_reference", "query", "lookup_key", "lookup_value"):
                if k in node and not is_null(node[k]):
                    ob.keys |= identifier_tokens(node[k])
            out.append(ob)
            return
        if "data_store" in node and "records" in node:
            SCOPES.setdefault(eid, set()).add(node["data_store"])
        for v in node.values():
            _walk_json(v, ctx, out, eid, captured)
    elif isinstance(node, list):
        for v in node:
            _walk_json(v, ctx, out, eid, captured)


def parse_json(path: Path, eid: str, captured: datetime) -> list[Obs]:
    data = json.loads(path.read_text(encoding="utf-8"))
    out: list[Obs] = []
    _walk_json(data, {}, out, eid, captured)
    return out


# --------------------------------------------------------------------------- logs

KV_RE = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)=("(?:[^"\\]|\\.)*"|\S+)')
TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\s*(.*)$")
SERVICE_NAMES = ("account_service", "profile_service", "search_service", "analytics_platform", "support_platform",
                 "support_replica", "integration_service", "backup_service", "security_service", "export_service",
                 "identity_service", "marketing_platform", "cache_service", "email_delivery_service")


def _kv(s: str) -> dict:
    d = {}
    for k, v in KV_RE.findall(s):
        if v.startswith('"') and v.endswith('"'):
            v = v[1:-1].replace('\\"', '"')
        d[k] = v
    if "message" in d and "=" in d["message"]:
        d.update(_kv(d["message"]))
    return d


def parse_log_line(line: str) -> dict | None:
    m = TS_RE.match(line.strip())
    if not m:
        return None
    ts, rest = m.group(1), m.group(2)
    f: dict = {"_ts": ts}
    j = rest.find("{")
    if rest.startswith("{") or (j >= 0 and rest[j:].rstrip().endswith("}") and '":' in rest):
        try:
            f.update({k: v for k, v in json.loads(rest[j:]).items()})
            return f
        except ValueError:
            pass
    if rest.startswith("|"):
        parts = [p.strip() for p in rest.strip("|").split("|")]
        # | LEVEL | service | trace | event | k=v...
        if len(parts) >= 5:
            f.update({"level": parts[0], "service": parts[1], "trace_id": parts[2], "event": parts[3]})
            f.update(_kv(" | ".join(parts[4:])))
            return f
    if rest.startswith("["):
        b = re.match(r"\[(\w+)\]\s*\[([^\]]+)\]\s*\[([^\]]+)\]\s*([A-Za-z0-9_]+):\s*(.*)$", rest)
        if b:
            f.update({"level": b.group(1), "service": b.group(2), "trace_id": b.group(3), "event": b.group(4)})
            f.update(_kv(b.group(5)))
            return f
    if rest.startswith("<"):
        s = re.match(r"<\d+>\d+\s+(\S+)\s+(\S+)\s+(\S+)\s+\S+\s+\S+\s+(.*)$", rest)
        if s:
            f.update({"host": s.group(1), "service": s.group(2), "trace_id": s.group(3)})
            f.update(_kv(s.group(4)))
            return f
    d = _kv(rest)
    if "requestId" in d:
        d.setdefault("trace_id", d["requestId"])
    if "function" in d:
        d.setdefault("service", d["function"])
    f.update(d)
    return f


def parse_log(path: Path, eid: str, captured: datetime) -> list[Obs]:
    lines = [parse_log_line(x) for x in path.read_text(encoding="utf-8").splitlines()]
    lines = [x for x in lines if x]
    traces: dict[str, list] = {}
    for f in lines:
        tr = f.get("trace_id") or f.get("requestId")
        if tr:
            traces.setdefault(tr, []).append(f)
    out: list[Obs] = []
    for f in lines:
        rid = f.get("resource_id")
        if not rid and f.get("event") == "record_lookup":
            rid = f.get("resource_id")
        if not rid or not RESOURCE_RE.fullmatch(str(rid)):
            continue
        ev = str(f.get("event", ""))
        if ev.endswith("_started"):
            continue
        tr = f.get("trace_id") or f.get("requestId")
        keys = set()
        group = traces.get(tr, [f]) if tr else [f]
        for g in group:
            for k in LOOKUP_FIELDS:
                if k in g and not is_null(g[k]):
                    keys |= identifier_tokens(g[k])
                    if RESOURCE_RE.fullmatch(str(g[k])) and g[k] != rid:
                        keys.add("rid:" + g[k])
        sysid = system_of(rid) or f.get("system") or f.get("service", "")
        ts = parse_ts(f.get("_ts")) or captured
        ob = Obs(eid, captured, ts, rid, sysid, source="log", note=ev)
        fields = {k: v for k, v in f.items() if not k.startswith("_")}
        if "result" in fields and "record_type" not in fields and ev == "index_query_completed":
            fields["result"] = "FOUND" if str(fields["result"]).lower() == "hit" else "NOT_FOUND"
        interpret(rid, fields, ob)
        ob.keys |= keys
        if ev == "attachment_pii_scan" or "file" in fields:
            ob.record_type = ob.record_type or "SUPPORT_ATTACHMENT"
        if ev in {"message_pii_scan", "attachment_pii_scan", "analytics_identity_scan"}:
            ob.present = True
            ob.exposed = True
        if ev == "message_read":
            ob.present = True
        if ev in {"contact_query_completed", "contact_lookup_completed"} and ob.present is None:
            ob.present = True
        if ev == "deletion_marker_check":
            ob.present = True
        if ob.present is None and (ev.endswith(("_completed", "_read", "_scan", "_loaded", "_heartbeat", "_evaluated"))
                                   or ev in {"connector_heartbeat", "record_lookup"}):
            ob.present = True
        if ob.present is None and ob.exposed is None and ob.enabled is None and ob.restore is None:
            continue
        out.append(ob)
    return out


# --------------------------------------------------------------------------- text handover

PIPE_ROW = re.compile(r"^-\s*(\S+)\s*\|\s*(\S+)\s*\|\s*(\S+)\s*\|\s*(\S+)\s*\|\s*(\S+)\s*\|\s*(.*)$")


def parse_text(path: Path, eid: str, captured: datetime) -> list[Obs]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        m = PIPE_ROW.match(line.strip())
        if not m:
            continue
        ts, sysid, rid, rtype, result, subj = m.groups()
        fields = {"system": sysid, "resource_id": rid, "record_type": rtype, "result": result,
                  "subject_reference": subj}
        ob = Obs(eid, captured, parse_ts(ts) or captured, rid, system_of(rid) or sysid, source="text")
        interpret(rid, fields, ob)
        if not is_null(subj):
            ob.keys |= identifier_tokens(subj)
        out.append(ob)
    return out


# --------------------------------------------------------------------------- PDF / image facts

def pdf_text(path: Path) -> str:
    try:
        import pymupdf  # type: ignore
        doc = pymupdf.open(str(path))
        return "\n".join(p.get_text() for p in doc)
    except ImportError:
        pass
    try:
        from pypdf import PdfReader  # type: ignore
        return "\n".join((p.extract_text() or "") for p in PdfReader(str(path)).pages)
    except ImportError:
        return ""


def facts_to_obs(facts: dict, eid: str, captured: datetime, aliases: dict | None = None) -> list[Obs]:
    """Convert structured facts (from PDF parsing or the vision model) into observations.

    facts = {"records": [{"resource_id", "system"?, "state": PRESENT|ABSENT|..., "exposed": bool,
                          "restore_allowed": bool, "enabled": bool, "identifiers": [...],
                          "record_type": ..., "observed_at": ...}]}
    """
    out = []
    for r in facts.get("records", []):
        rid = r.get("resource_id") or (aliases or {}).get(str(r.get("alias", "")).lower(), "")
        if not rid or not RESOURCE_RE.fullmatch(rid):
            continue
        ts = parse_ts(r.get("observed_at")) or captured
        ob = Obs(eid, captured, ts, rid, system_of(rid) or r.get("system", ""), source="facts",
                 record_type=r.get("record_type", ""), note=r.get("note", ""))
        state = str(r.get("state", "")).upper()
        ob.present = {"PRESENT": True, "ABSENT": False}.get(state, True)
        ob.exposed = r.get("exposed")
        ob.enabled = r.get("enabled")
        ob.restore = r.get("restore_allowed")
        for v in r.get("identifiers", []) or []:
            ob.ids |= identifier_tokens(v)
            if isinstance(v, str) and "@" not in v and not v.startswith(("+", "user_")) and not EMAIL_RE.search(v):
                ob.names.add(norm_name(v))
        out.append(ob)
    return out


# --------------------------------------------------------------------------- driver

def load_manifest(dataset: Path) -> list[dict]:
    with open(dataset / "evidence_files.csv", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def extract_all(dataset: Path, aliases: dict | None = None, refresh_vision: bool = False) -> tuple[list[Obs], dict]:
    """Parse every manifest file. Returns (observations, info per evidence id)."""
    from . import documents

    obs: list[Obs] = []
    info: dict[str, dict] = {}
    for row in load_manifest(dataset):
        eid = row["file_id"]
        path = dataset / row["file_path"]
        captured = parse_ts(row["captured_at"])
        info[eid] = {"path": row["file_path"], "origin": row["origin"], "captured": captured}
        suffix = path.suffix.lower()
        try:
            if suffix == ".sql":
                got = parse_sql(path, eid, captured)
            elif suffix == ".json":
                got = parse_json(path, eid, captured)
            elif suffix == ".log":
                got = parse_log(path, eid, captured)
            elif suffix == ".txt":
                got = parse_text(path, eid, captured)
            elif suffix == ".pdf":
                facts = documents.pdf_facts(path, pdf_text(path))
                info[eid]["facts"] = facts
                got = facts_to_obs(facts, eid, captured, aliases)
            elif suffix in (".png", ".jpg", ".jpeg"):
                facts = documents.image_facts(path, eid, refresh=refresh_vision)
                info[eid]["facts"] = facts
                got = facts_to_obs(facts or {}, eid, captured, aliases)
            else:
                got = []
        except Exception as exc:  # keep going; one bad file must not sink the run
            info[eid]["error"] = repr(exc)
            got = []
        info[eid]["n_obs"] = len(got)
        obs.extend(got)
    return obs, info
