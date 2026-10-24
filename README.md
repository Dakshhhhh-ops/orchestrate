# Ghost Data Still Exists - deletion audit agent

HackerRank Orchestrate (October 2026). The agent audits every request in
`dataset/deletion_requests.csv`: does the person actually ask for deletion, which records
still expose them at `audit_at`, which processes can bring the data back, and what ordered
plan finishes the deletion safely. It writes `output.csv` (`case_id,deletion_status,steps_json`).

The challenge statement is in [`problem_statement.md`](problem_statement.md).

## Quick start

```bash
# Python 3.10+ ; the core pipeline uses only the standard library
pip install -r requirements.txt          # pymupdf (PDF text), anthropic (optional model calls)

python code/main.py                      # -> ./output.csv (518 rows), ~15 s
python code/evaluation/main.py           # score against the 32 public sample answers
python code/evaluation/behaviour_checks.py   # 19 targeted behaviour checks
python code/evaluation/run_assignment_check.py --stride 5   # leave-identifiers-out test of run assignment
```

Optional model calls: set `GROQ_API_KEY` (Groq, `openai/gpt-oss-120b`) or `ANTHROPIC_API_KEY`
(Claude) as an environment variable, or put it in an uncommitted `.env` file at the repository
root (see `.env.example`; `.env` is git-ignored and never packaged). Then run
`python code/main.py` again: the model classifies request intent in batches of 40 distinct texts.
With an Anthropic key, `--refresh-vision` also re-reads the screenshots. `GHOST_PROVIDER` forces a
provider and `GHOST_MODEL` a model. Every model reply is cached under `code/cache/`, so runs are
reproducible and a run without a key still completes using the cache and the rule fallbacks.

Useful flags: `--requests <csv>` (audit another request file), `--output <csv>`,
`--trace <json>` (per-case reasoning: linked resources, how each was linked, state, evidence).

## How it works

```
requests ──► intent ──► identity ──► discovery ──► state at audit_at ──► policy ──► plan
             (Claude      (current +    (identifiers,   (latest evidence,     (deletion_    (findings →
              + rules)     history)      catalog, runs)  recreation events)    rules.csv)    remediation →
                                                                                             verification)
```

| Stage | Module | What it does |
|---|---|---|
| Evidence extraction | `ghost_audit/evidence.py`, `documents.py` | Parses all 94 evidence files - SQLite snapshots (loaded into in-memory SQLite, every replica table kept), JSON exports, ten log dialects (key=value, JSON lines, syslog, pipe, bracket, nested `message="..."`), text handovers, PDF text layers and screenshot facts - into one observation shape: resource, system, time, identifiers it contains, identifiers used to look it up, and state facts (present / exposed / flow enabled / restore allowed). Log lines are joined by trace id so a lookup key on the `*_started` line links the result on the `*_completed` line. |
| Screenshots | `documents.py`, `prompts/image_facts.md` | A vision model reads each admin-console screenshot into structured records. Results are cached in `code/cache/image_facts.json`. |
| Intent | `ghost_audit/intent.py`, `prompts/intent.md` | Claude labels each distinct request text DELETION / NOT_DELETION in batches of 40. A multilingual rule classifier (negations first: "do not close", "nicht löschen", "ne supprimez pas"...) is the fallback and a cross-check. |
| Identity | `ghost_audit/identity.py` | Current snapshot + `user_audit_log.csv` history gives every e-mail, phone, e-mail hash and the stable `user_id`. Request e-mails match current or historical values, case-insensitively. Near-identical decoys (`g.silva76.jr@` vs `g.silva76@`) never match. |
| Discovery | `audit.py: discover / assign_run_resources` | A resource belongs to the subject when its evidence carries one of their identifiers, when the data catalog links it, when it is a derived copy of one of their records (`source_resource_id`), or when a restore/connector recreated it from one of their records. Ownership is read from all evidence because it does not change over time. Records with no identifiers left (e.g. an already-anonymised analytics row) are tied to a request through the deletion run that touched them. Actions of one run share a timing fingerprint (time mod 30 s, with a fixed per-step drift and per-kind offset learned from identifier-linked runs), so each record goes to the run it fits, one per system per request (a conversation and an attachment may share a run); steps of one run are also chained by their exact spacing, and records that fit only runs of people with no request are left out. |
| State | `audit.py: _state` | The state at `audit_at` comes from the latest evidence captured after the request and no later than `audit_at`. A record in any replica table still exists. Workflow results (`SUCCESS`, `COMPLETE`) are treated as claims: they only fill a gap when no evidence covers it. Recreation events after the last observation (restore jobs, active connectors re-populating search, failover sync) bring a record back. A full snapshot that covers the system but omits the record proves it absent. |
| Policy | `audit.py: _category`, `deletion_rules.csv` | Category comes from the record type in evidence, else from what the deletion workflow tried (a support record the worker tried to DELETE is an attachment; one it tried to REDACT is a shared conversation; a hash-keyed analytics contact is a secondary identifier), else from the resource family. The required action comes from `deletion_rules.csv`. Tombstones keep the `user_id` and must have no direct identifiers. |
| Plan | `ghost_audit/planner.py` | Findings → remediation → verification, ordered by the operation table then `system_id`, `resource_id`. Flows are disabled and restores blocked before records are changed. COMPLETE cases get evidence-backed checks for every compliant record. INCOMPLETE cases get the full fix plan, plus checks that the account is gone and that retained security/legal records are still present (empty `evidence_id`, since they describe future work). |

### Design choices worth knowing

- **Claims vs evidence.** `reported_status`, `reported_result` and activity details are never proof. Sample case_25 says the workflow FAILED, yet evidence shows the profile already gone, so the case is COMPLETE. Sample case_24 says COMPLETE, yet the profile still answers lookups, so it is INCOMPLETE.
- **Time discipline.** No evidence captured after `audit_at` is ever cited or used for state. Evidence captured before the request describes the record before deletion was requested, so it is not used to judge the outcome.
- **Deterministic.** Same input, same output. Model outputs are cached by request hash, and JSON is serialised with fixed key order.

## Results (public samples)

| Metric | Value |
|---|---|
| Case status accuracy | 100% (32/32) |
| Step precision / recall / F1 | 1.000 / 0.984 / 0.992 |
| Exact sequence match | 91% (29/32) |
| Behaviour checks | 19/19 |

Details, the custom cases, and failures found along the way are in
[`code/evaluation/evaluation_report.md`](code/evaluation/evaluation_report.md). Token usage is in
[`code/evaluation/usage_report.md`](code/evaluation/usage_report.md).

## Layout

```
code/
  main.py                    entry point
  ghost_audit/               evidence, identity, intent, audit, planner, llm client
  prompts/                   model prompts (intent, screenshot facts)
  cache/                     cached model outputs (image facts, LLM replies)
  evaluation/                evaluator, behaviour checks, custom gold cases, reports
dataset/                     challenge data (not included in code.zip)
output.csv                   predictions for the 518 evaluation requests
log.txt                      agent session log (chat transcript)
```

## Packaging

```bash
python code/package.py        # -> code.zip (README, requirements, code/; forward-slash paths)
```

`dataset/`, caches of Python bytecode and archives are excluded. No keys or credentials are stored
anywhere in the repository.
