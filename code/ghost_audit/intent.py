"""Does the request text authorise deletion?

Primary path: the model reads the requests in batches (prompt: code/prompts/intent.md) and returns
one label per request. Fallback path (no API key, refusal, unparsable reply): a multilingual
rule classifier. Final label = model label when it agrees with the rules; on disagreement the
model is asked again about that text alone (more reasoning) and the majority of the three votes
wins. Model replies are cached, so a re-run reproduces the same labels.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import llm

PROMPT = Path(__file__).resolve().parent.parent / "prompts" / "intent.md"

DELETE_PATTERNS = [
    r"\bdelet", r"\berase", r"\berasure", r"\bremov", r"\bclose (my|this|the) account", r"\bclosed? .*account",
    r"account[- ]deletion", r"\bwipe", r"\bforget me", r"\bpurge",
    r"supprim", r"suppression", r"effacer",                     # fr
    r"eliminar", r"eliminaci[oó]n", r"borrar", r"cancelar mi cuenta",  # es
    r"exclus[aã]o", r"excluir", r"apagar",                       # pt
    r"l[öo]sch", r"entfern",                                     # de
    r"cancellare", r"cancellazione",                             # it
    r"verwijder",                                                # nl
    r"削除", r"退会", r"삭제", r"탈퇴", r"删除", r"注销",            # ja/ko/zh
    r"\bis gone\b", r"cannot identify me", r"no longer (identify|point back)", r"\bdeleted\b",
    r"account (closed|closure)", r"close the account completely",
]
NEGATION_PATTERNS = [
    r"not asking for deletion", r"not a request to (erase|delete)", r"do not (close|delete)",
    r"don'?t (close|delete)", r"without removing", r"leave (the|my) (account|profile)", r"keep the account",
    r"continue using", r"still (need|want|use)", r"marketing lists only", r"\bonly\b.*marketing",
    r"leave the account active",
    r"nichts? (zu )?l[öo]sch", r"nicht l[öo]schen",                       # de
    r"ne (supprimez|supprime|supprimer) pas", r"ne pas supprimer", r"sans supprimer",  # fr
    r"no (eliminen|borren|eliminar|borrar)", r"sin (eliminar|borrar)",     # es
    r"n[ãa]o (exclua|apague|excluir|apagar)", r"sem (excluir|apagar)",     # pt
    r"削除しないで", r"삭제하지 마",                                          # ja/ko
]
NON_DELETE_TOPICS = [
    r"\bcopy of\b", r"\bdownload my\b", r"portable format", r"what personal information", r"\bcorrect\b",
    r"misspell", r"\bchange the email\b", r"\bupdate\b.*\b(number|address|email)\b", r"\breset", r"promotional emails",
    r"unsubscribe", r"out of date",
]


def rule_label(text: str) -> tuple[str, str]:
    t = text.lower()
    neg = any(re.search(p, t) for p in NEGATION_PATTERNS)
    dele = any(re.search(p, t) for p in DELETE_PATTERNS)
    topic = any(re.search(p, t) for p in NON_DELETE_TOPICS)
    if neg:
        return "NOT_DELETION", "explicit statement that deletion is not wanted"
    if dele:
        return "DELETION", "deletion/erasure/closure requested"
    if topic:
        return "NOT_DELETION", "access/correction/marketing request"
    return "NOT_DELETION", "no deletion request found"


def classify(requests: list[dict], batch_size: int = 40) -> dict[str, dict]:
    """requests: [{case_id, request_text}] -> {case_id: {label, reason, source}}."""
    out: dict[str, dict] = {}
    for r in requests:
        lab, why = rule_label(r["request_text"])
        out[r["case_id"]] = {"label": lab, "reason": why, "source": "rules", "rule_label": lab}
    system = PROMPT.read_text(encoding="utf-8")
    # de-duplicate identical texts so each distinct wording is judged once
    uniq: dict[str, list[str]] = {}
    for r in requests:
        uniq.setdefault(r["request_text"].strip(), []).append(r["case_id"])
    texts = sorted(uniq)
    for i in range(0, len(texts), batch_size):
        chunk = texts[i:i + batch_size]
        items = [{"id": f"r{j}", "text": t} for j, t in enumerate(chunk)]
        reply = llm.call(system, json.dumps(items, ensure_ascii=False), purpose="intent",
                         max_tokens=4096, effort="low")
        parsed = llm.parse_json_reply(reply)
        if not isinstance(parsed, dict):
            continue
        labels = parsed.get("results", parsed)
        for item in items:
            res = labels.get(item["id"]) if isinstance(labels, dict) else None
            if not isinstance(res, dict) or res.get("label") not in ("DELETION", "NOT_DELETION"):
                continue
            for cid in uniq[item["text"]]:
                out[cid].update({"label": res["label"], "reason": res.get("reason", ""), "source": "llm"})
    # Tie-break: when the batch model vote and the rule vote disagree, ask the model again about
    # that text alone with more reasoning, and take the majority of the three votes. Model replies
    # are not fully deterministic, so one vote should not decide a borderline request.
    for text in texts:
        first = out[uniq[text][0]]
        if first["source"] != "llm" or first["label"] == first["rule_label"]:
            continue
        reply = llm.call(system, json.dumps([{"id": "r0", "text": text}], ensure_ascii=False),
                         purpose="intent_tiebreak", max_tokens=4096, effort="medium")
        parsed = llm.parse_json_reply(reply)
        res = (parsed.get("results", parsed) if isinstance(parsed, dict) else {}).get("r0")
        votes = [first["label"], first["rule_label"]]
        if isinstance(res, dict) and res.get("label") in ("DELETION", "NOT_DELETION"):
            votes.append(res["label"])
        final = max(set(votes), key=votes.count) if len(votes) == 3 else first["rule_label"]
        for cid in uniq[text]:
            out[cid].update({"label": final, "source": "llm+tiebreak", "votes": votes,
                             "reason": res.get("reason", first["reason"]) if isinstance(res, dict) else first["reason"]})
    return out
