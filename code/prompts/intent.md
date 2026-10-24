You review privacy requests sent to an online shop. For each request decide whether the
person is asking for their account / personal data to be DELETED (erased, removed, closed
with data deletion, anonymised). Requests can be in any language.

Label NOT_DELETION when the person asks for something else - access or a copy of their data,
a data export, a correction or update, a password/login reset, a marketing or e-mail opt-out,
invoices - or when they explicitly say they do not want deletion or want to keep the account.
A request that mentions deletion only to rule it out is NOT_DELETION.

Also label DELETION when the person:
- follows up on an earlier deletion request, asking you to check, finish or confirm that their
  account or personal data has really been removed (a status question about a deletion is still
  a deletion request);
- asks to remove, clean out or erase their own profile, account or personal details, even if they
  accept that some shared or legally required records stay. Partial deletion is still DELETION:
  the policy decides which records are deleted, redacted or kept, so do not label it a "cleanup".

Input: a JSON array of {"id", "text"}.
Output: only JSON of the form
{"results": {"<id>": {"label": "DELETION" | "NOT_DELETION", "reason": "<max 12 words>"}}}
with one entry for every input id.
