# Evaluation report

All numbers come from `python code/evaluation/main.py` (32 public sample requests scored against
`dataset/sample_output.csv`) and `python code/evaluation/behaviour_checks.py`. The sample requests
are audited inside the same world as the 518 evaluation requests, so they compete for the same
deletion runs exactly as they would in the real run.

## 1. Headline metrics (final version)

| Metric | Definition | Score |
|---|---|---|
| Case-level status accuracy | `deletion_status` exact match | **100.0%** (32/32) |
| Step precision | predicted steps that exactly match a gold step (phase, system, resource, operation, state, evidence) | **1.000** |
| Step recall | gold steps reproduced exactly | **0.984** |
| Step F1 | | **0.992** |
| Step F1 ignoring `evidence_id` | same, without the evidence field | 0.992 |
| Sequence accuracy | whole `steps_json` array identical, order included | **90.6%** (29/32) |

Field-level accuracy (gold steps aligned to predicted steps on phase + resource + operation):

| step_no | phase | system_id | resource_id | operation | state | evidence_id |
|---|---|---|---|---|---|---|
| 95.9% | 98.4% | 98.4% | 98.4% | 98.4% | 98.4% | 98.4% |

Every produced step is correct (precision 1.0) and every aligned step has the right evidence id. The
1.6% gap is three gold steps that no participant-visible data can produce (see section 4).

## 2. Progress across iterations

| Version | Change | Status acc. | Step F1 | Sequence acc. |
|---|---|---|---|---|
| v1 | first end-to-end pipeline | 96.9% | 0.826 | 25.0% |
| v2 | ownership read from all evidence; support/analytics categories from workflow intent; JSON search hits and lookup queries parsed; log reads count as presence | 100% | 0.890 | 34.4% |
| v3 | run window narrowed to ±3 h of `reported_at`, accounts assigned first, one record per system per run; retained security records always verified | 100% | 0.958 | 65.6% |
| v4 | state only from evidence captured after the request; absence proven by full snapshots that omit the record | 100% | 0.971 | 78.1% |
| v5 | sample requests join the run-assignment pool; restorable archives without a block are restore risks; multilingual negations in the intent fallback | 100% | 0.971 | 78.1% |
| v6 | identifier-less records matched to deletion runs by the run's timing fingerprint instead of nearest report time (section 4, item 9) | 100% | **0.992** | **90.6%** |

## 3. Participant-created evaluation cases

### 3a. Hand-audited evaluation requests (`custom_gold.csv`)

Six evaluation requests were audited by hand against the raw evidence before being used as a
regression set. Each one exercises a different trap.

| Case | Trap | Expected (verified by hand) | Agent |
|---|---|---|---|
| case_37 | Connector re-creates deleted data | `data_flow_2528` is ACTIVE in the 1 Oct handover, and `activity_3755` shows it repopulated `search_1934` after the delete failed. Disable the flow **before** deleting the search entry. Verify the account is absent and `audit_record_2019` is retained. | match |
| case_43 | Reported COMPLETE, profile still live | Readback trace (evidence_94) returns `account_8681` with rows=1 for the requester. Expected: remaining record, delete, verify absent. | match |
| case_46 | Tombstone | Account lookup returns 404 (evidence_67). The customer-reference registry screenshot shows `user_17` kept with no name, e-mail or phone, so the tombstone is correctly redacted. Expected: COMPLETE with two cited checks. | match |
| case_55 | Backup restore path | The backup console screenshot (evidence_32) lists archive `backup_9287` for the requester, and nothing blocks restore. Expected: restore risk, block restore, verify blocked; archive retained, not deleted. | match |
| case_61 | Support attachment owned by one person | Object-storage screenshot shows `delivery_confirmation_19.pdf`. The data catalog says it "belongs only to" the requester. Expected: SUPPORT_ATTACHMENT, delete it. | match |
| case_533 | Not a deletion request | "Please replace [my phone] … and leave the account active". Expected: NOT_APPLICABLE, `[]`. | match |

### 3b. Behaviour checks (`behaviour_checks.py`), 12/12 pass

| # | Check | Why it matters |
|---|---|---|
| 1-2 | `g.silva76@gmail.com` gets `search_428` and never the decoy `search_1874` (`g.silva76.jr@gmail.com`) | decoys share names and e-mail prefixes |
| 3-4 | A request from the **old** address `minjun.kim90@gmail.com` produces the same plan as the current address and finds `analytics_9914`, which is keyed only by the old e-mail **hash** | historical identifiers |
| 5 | Re-auditing a case with an earlier `audit_at` cites no evidence captured after the cut-off | time discipline |
| 6 | `SIPHO.MOKOENA@GMAIL.COM` resolves to `user_36` | case-insensitive identity |
| 7 | In case_37 `DISABLE_DATA_FLOW` comes before `DELETE_RECORD` of the record the flow re-creates | safe remediation order |
| 8-12 | Intent fallback on German, French, Spanish, Japanese texts, including "do not delete" phrasing | multilingual intent |

### 3c. Leave-identifiers-out run assignment (`run_assignment_check.py`)

The samples hold only four run-assignment ties, too few to tune on. This check builds about a
thousand test cases from the dataset itself, without labels: take a record the deletion worker
acted on whose owner **is** known from identifiers (and whose action falls in exactly one of that
owner's request windows), hide its identifiers so it looks anonymised, re-run the assignment and
check that it goes back to the right request. Every 5th record is tested (203 records, ~3 min).

| Version | Correct owner | account | search | support | analytics | data flow | backup |
|---|---|---|---|---|---|---|---|
| v5 nearest report time | 91/203 = **44.8%** | 35/47 | 32/56 | 10/37 | 3/15 | 5/25 | 4/13 |
| v6 run timing fingerprint | 170/203 = **83.7%** | 39/47 | 51/56 | 25/37 | 14/15 | 21/25 | 12/13 |

Almost every v5 error went to a neighbouring request reported in the same minute. In v6 most
remaining misses are left unassigned rather than given to the wrong request.

A second, independent label source confirms the direction: six support attachments carry an
`uploaded_by` e-mail that v5 and v6 did not read. Before that field was parsed, v5 put 0/6 of them
on the right request and v6 put 4/6. The field is now read as an identifier, so all six link
directly (section 4, item 11).

## 4. Failures found and improvements made

1. **Identifier-less records were missed** (v1 recall 0.75). Anonymised analytics rows and 404
   account lookups carry no e-mail, so identifier matching cannot find them. *Fix:* tie them to a
   request through the deletion run that touched them (`assign_run_resources`).
2. **Run assignment stole neighbours' records.** Several requests are reported in the same hour,
   and the first version gave a second account to a request that already had one. *Fix:* accounts
   are assigned first, each request takes at most one identifier-less record per system, and
   candidates are ranked by distance to `reported_at`, then to the request's own account deletion.
3. **Search hits and lookup keys inside nested JSON were ignored.** The service export nests
   `hits.documents[]` and the lookup `query`. *Fix:* the JSON walker merges hit documents and
   treats `query` / `lookup_key` as link keys.
4. **Read-log events did not count as "record exists".** `analytics_profile_read_completed` has no
   `rows` field. *Fix:* completed read/scan/heartbeat events imply presence unless rows=0 or 404.
5. **Retained security records were dropped** when their only sighting was after `audit_at`.
   *Fix:* retained categories are always part of the post-remediation checks (empty evidence),
   but never cited as proof in a COMPLETE case.
6. **Old sightings were treated as current.** A February screenshot of an attachment was reported
   for a September request. *Fix:* state comes only from evidence captured after `requested_at`.
7. **Absent accounts had no proof.** Some requests' accounts never appear in a lookup. *Fix:* a
   full snapshot of the account system, captured in the request window, that omits the account
   proves it absent. Snapshots that also hold other records of the same person are preferred.
8. **Intent fallback missed negated deletion in German and French** (found by checks 8-9).
   *Fix:* negation patterns for de/fr/es/pt/ja/ko are checked before deletion keywords.
9. **Run-assignment ties were close to guesses.** Nearest-report-time matching sent one record per
   same-minute tie to the wrong request (case_21, 22, 23, 25), and a leave-one-out test on the full
   dataset (3c) showed it returned a hidden record to its true owner only 45% of the time.
   *Fix:* every action of one deletion run shares the run's timing fingerprint: its time modulo
   30 s matches the run's account deletion after a fixed per-step drift (0.5 s) and a per-kind
   offset. The offsets are learned from the 332 run actions whose records carry identifiers, so
   nothing is hand-labelled. Records are matched best-first to runs whose anchors they fit, records
   no request claims are grouped into runs by fingerprint, and runs that belong to people with no
   request are left out instead of guessed.
10. **Three identifier fields were never read.** A scan of every field in the JSON, SQL and log
   evidence for values shaped like e-mails, phones, user refs or hashes found `uploaded_by` on
   ticket attachments, `owner_key` on export-download lookups and `lookup_value` in snapshot
   bundles (118 values). *Fix:* all three are read as identifiers or link keys. Six attachments now
   link to their uploader directly, 20 retained audit records are now verified, and four accounts
   moved to the request whose e-mail looked them up.
11. **Reports could be missed by a strict packaging check.** `code.zip` held the reports only under
   `code/evaluation/`. *Fix:* `code/package.py` also writes them to `evaluation/` at the top level.

### Known remaining errors on the samples (3 of 32 not exact)

- case_18, case_20: the answer key verifies `audit_record_6645` / `audit_record_5569`, which
  appear in no evidence file, no activity row and no PDF text layer, so no participant-visible
  data can recover them.
- case_04: `account_004` has no evidence, no activity and no catalog entry at all.

## 5. Full-dataset sanity checks

- 518 rows, sorted by `case_id`, header `case_id,deletion_status,steps_json`. Every `steps_json`
  is a JSON array with consecutive `step_no` from 1.
- Status mix: 355 INCOMPLETE, 153 COMPLETE, 10 NOT_APPLICABLE. The 10 not-applicable requests are
  exactly the 10 accounts the privacy intake classified as non-deletion in the activity stream.
  The agent reaches this from the request text alone, and the activity rows confirm it.
- No cited evidence is captured after its case's `audit_at` (checked by `behaviour_checks.py` and
  by construction in `audit._visible`). A full scan of the 518 rows confirms all 1,210 cited ids
  exist, none predates its request, COMPLETE cases cite evidence on every step, and remediation and
  post-remediation checks carry an empty `evidence_id`.
- The ±3 h run window was tuned on September samples; on the full year, every worker action on an
  identifier-linked record falls inside it (75 min before to 2 h 10 min after `reported_at`).
