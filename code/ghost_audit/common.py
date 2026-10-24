"""Shared helpers: identifier normalisation, resource/system mapping, time parsing."""
from __future__ import annotations

import re
from datetime import datetime, timezone

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
HASH_RE = re.compile(r"\b[0-9a-f]{64}\b")
USER_RE = re.compile(r"\buser_\d+\b")
PHONE_RE = re.compile(r"\+\d[\d\s().\-]{6,}\d")
RESOURCE_RE = re.compile(
    r"\b(?:account|search|analytics|audit_record|support|email_delivery|data_flow|cache|export|"
    r"backup|tombstone|legal_hold|marketing|telemetry|object)_\d+\b"
)

# resource prefix -> canonical system id used in the expected output
PREFIX_SYSTEM = {
    "account": "account_service",
    "search": "search_service",
    "analytics": "analytics_platform",
    "support": "support_platform",
    "export": "export_service",
    "data_flow": "integration_service",
    "backup": "backup_service",
    "audit_record": "security_service",
    "tombstone": "identity_service",
    "cache": "cache_service",
    "email_delivery": "email_delivery_service",
    "marketing": "marketing_platform",
    "legal_hold": "legal_service",
    "telemetry": "telemetry_service",
    "object": "object_storage",
}

# resource prefix -> data category in deletion_rules.csv
PREFIX_CATEGORY = {
    "account": "USER_PROFILE",
    "search": "SEARCH_INDEX_ENTRY",
    "analytics": "ANALYTICS_PROFILE",
    "support": "SUPPORT_MESSAGE",
    "export": "USER_EXPORT",
    "data_flow": "DATA_FLOW",
    "backup": "BACKUP_ARCHIVE",
    "audit_record": "SECURITY_AUDIT_LOG",
    "tombstone": "ACCOUNT_TOMBSTONE",
    "cache": "CACHE_ENTRY",
    "email_delivery": "EMAIL_DELIVERY_LOG",
    "marketing": "MARKETING_PROFILE",
    "legal_hold": "LEGAL_HOLD_RECORD",
    "telemetry": "PRODUCT_TELEMETRY",
    "object": "SUPPORT_ATTACHMENT",
}


def prefix_of(resource_id: str) -> str:
    m = re.match(r"^(.*)_\d+$", resource_id or "")
    return m.group(1) if m else ""


def system_of(resource_id: str) -> str:
    return PREFIX_SYSTEM.get(prefix_of(resource_id), "")


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    v = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def iso(dt: datetime | None) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else ""


def norm_email(v: str) -> str:
    return v.strip().lower()


def norm_phone(v: str) -> str:
    return re.sub(r"\D", "", v or "")


def norm_name(v: str) -> str:
    import unicodedata

    s = unicodedata.normalize("NFKD", v or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s).strip().lower()


NULLS = {"", "null", "none", "nil", "[redacted]", "redacted", "n/a", "-", "no direct identifier"}


def is_null(v) -> bool:
    if v is None:
        return True
    if isinstance(v, str) and v.strip().lower() in NULLS:
        return True
    return False


def identifier_tokens(value) -> set[str]:
    """Extract canonical identifier tokens (email:, phone:, hash:, ref:) from any value."""
    out: set[str] = set()
    if value is None:
        return out
    if isinstance(value, (dict, list)):
        items = value.values() if isinstance(value, dict) else value
        for x in items:
            out |= identifier_tokens(x)
        return out
    s = str(value)
    for e in EMAIL_RE.findall(s):
        out.add("email:" + norm_email(e))
    for h in HASH_RE.findall(s):
        out.add("hash:" + h)
    for u in USER_RE.findall(s):
        out.add("ref:" + u)
    for p in PHONE_RE.findall(s):
        d = norm_phone(p)
        if len(d) >= 8:
            out.add("phone:" + d)
    return out
