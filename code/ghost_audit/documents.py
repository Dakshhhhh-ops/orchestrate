"""Facts from unstructured evidence: PDFs (text layer) and screenshots (vision model).

PDF: the text layer is parsed deterministically - every resource id or business alias in the
document becomes a record that carries the identifiers printed in the same document.

Images: a vision model reads the screen and returns structured records (prompt in
code/prompts/image_facts.md). Results are stored in code/cache/image_facts.json so that
runs are reproducible without an API key; `--refresh-vision` re-extracts them.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import llm
from .common import EMAIL_RE, PHONE_RE, RESOURCE_RE, USER_RE

ROOT = Path(__file__).resolve().parent.parent
IMAGE_CACHE = ROOT / "cache" / "image_facts.json"
PROMPT_DIR = ROOT / "prompts"

ALIAS_RE = re.compile(r"\b(?:NST-\d{4}-\d{4}-\d{4}|CS-\d{3,6}|ORD-\d{6,})\b")
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def _date(text: str) -> str | None:
    m = re.search(r"(\d{1,2}) (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]* (\d{4})", text)
    if not m:
        return None
    return f"{m.group(3)}-{MONTHS[m.group(2).lower()]:02d}-{int(m.group(1)):02d}T00:00:00Z"


def pdf_facts(path: Path, text: str) -> dict:
    """Records for every resource id / alias in the PDF, with the document's identifiers."""
    if not text:
        return {"records": []}
    emails = EMAIL_RE.findall(text)
    phones = [p for p in PHONE_RE.findall(text) if len(re.sub(r"\D", "", p)) >= 8]
    users = USER_RE.findall(text)
    rids = sorted(set(RESOURCE_RE.findall(text)))
    aliases = sorted(set(ALIAS_RE.findall(text)))
    records = []
    # register-style rows: "ORD-... | user_08 | ... | Retain until ..." -> one legal-hold fact per user
    rows = re.findall(r"(ORD-\d+)\s*\|\s*(user_\d+)\s*\|([^\n]*)", text)
    if rows:
        for order, user, rest in rows:
            records.append({"resource_id": "", "alias": order, "identifiers": [user], "state": "PRESENT",
                            "record_type": "LEGAL_HOLD_RECORD", "note": rest.strip()})
        return {"records": records, "kind": "register"}
    ids = emails + phones + users
    exposed = bool(emails or phones)
    for rid in rids:
        rt = ""
        if rid.startswith("support_") and re.search(r"attachment|\.pdf|upload", text, re.I):
            rt = "SUPPORT_ATTACHMENT"
        if rid.startswith("audit_record_"):
            rt = "SECURITY_AUDIT_LOG"
        records.append({"resource_id": rid, "identifiers": ids, "state": "PRESENT",
                        "exposed": exposed if not rid.startswith("audit_record_") else False,
                        "record_type": rt})
    for a in aliases:
        records.append({"resource_id": "", "alias": a, "identifiers": ids, "state": "PRESENT", "exposed": exposed})
    return {"records": records}


def _load_cache() -> dict:
    if IMAGE_CACHE.exists():
        return json.loads(IMAGE_CACHE.read_text(encoding="utf-8"))
    return {}


def image_facts(path: Path, eid: str, refresh: bool = False) -> dict:
    cache = _load_cache()
    if eid in cache and not refresh:
        return cache[eid]
    prompt = (PROMPT_DIR / "image_facts.md").read_text(encoding="utf-8")
    reply = llm.call(prompt, [llm.image_block(path), {"type": "text", "text": f"Screenshot file: {path.name}"}],
                     purpose="image_facts", model=llm.VISION_MODEL, max_tokens=4096, effort="medium")
    facts = llm.parse_json_reply(reply) or {}
    if facts:
        facts["extracted_by"] = llm.VISION_MODEL
        cache[eid] = facts
        IMAGE_CACHE.parent.mkdir(parents=True, exist_ok=True)
        IMAGE_CACHE.write_text(json.dumps(cache, indent=1, ensure_ascii=False), encoding="utf-8")
    return facts
