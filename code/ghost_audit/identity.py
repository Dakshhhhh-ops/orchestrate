"""Identity resolution: current snapshot + audit-log history -> every identifier a subject has used."""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

from .common import norm_email, norm_name, norm_phone, parse_ts


@dataclass
class Subject:
    user_id: str
    name: str
    email: str
    phone: str
    email_hash: str
    updated_at: str
    emails: set = field(default_factory=set)       # current + historical (lower-case)
    phones: set = field(default_factory=set)       # digits only
    hashes: set = field(default_factory=set)
    history: list = field(default_factory=list)    # audit rows

    @property
    def tokens(self) -> set:
        t = {"ref:" + self.user_id}
        t |= {"email:" + e for e in self.emails}
        t |= {"phone:" + p for p in self.phones if p}
        t |= {"hash:" + h for h in self.hashes if h}
        return t

    @property
    def norm_name(self) -> str:
        return norm_name(self.name)


class IdentityIndex:
    def __init__(self, dataset: Path):
        self.subjects: dict[str, Subject] = {}
        with open(dataset / "user_identities.csv", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                s = Subject(r["user_id"], r["name"], r["email"], r["phone"], r["email_hash"], r["updated_at"])
                s.emails.add(norm_email(r["email"]))
                s.phones.add(norm_phone(r["phone"]))
                s.hashes.add(r["email_hash"].strip().lower())
                self.subjects[s.user_id] = s
        with open(dataset / "user_audit_log.csv", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                s = self.subjects.get(r["user_id"])
                if not s:
                    continue
                s.history.append(r)
                for v in (r["old_value"], r["new_value"]):
                    if not v:
                        continue
                    if r["field_name"] == "email":
                        s.emails.add(norm_email(v))
                    elif r["field_name"] == "phone":
                        s.phones.add(norm_phone(v))
                    elif r["field_name"] == "email_hash":
                        s.hashes.add(v.strip().lower())
        # reverse index token -> users
        self.by_token: dict[str, set] = {}
        for s in self.subjects.values():
            for t in s.tokens:
                self.by_token.setdefault(t, set()).add(s.user_id)
        self.by_name: dict[str, set] = {}
        for s in self.subjects.values():
            self.by_name.setdefault(s.norm_name, set()).add(s.user_id)

    def resolve_email(self, email: str) -> Subject | None:
        """Map a request email (current or historical, any case) to one subject."""
        e = norm_email(email)
        users = self.by_token.get("email:" + e, set())
        if not users:
            return None
        if len(users) == 1:
            return self.subjects[next(iter(users))]
        # prefer the subject whose *current* email it is, else the most recent change away from it
        cur = [u for u in users if norm_email(self.subjects[u].email) == e]
        if cur:
            return self.subjects[sorted(cur)[0]]
        best, best_t = None, None
        for u in users:
            for h in self.subjects[u].history:
                if h["field_name"] == "email" and norm_email(h["old_value"]) == e:
                    t = parse_ts(h["event_time"])
                    if best_t is None or t > best_t:
                        best, best_t = u, t
        return self.subjects[best or sorted(users)[0]]

    def owners(self, tokens: set) -> set:
        out = set()
        for t in tokens:
            out |= self.by_token.get(t, set())
        return out
