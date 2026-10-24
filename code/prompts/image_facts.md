You extract audit facts from a screenshot of an internal Northstar Shop admin tool.
The audit decides whether a customer's personal data was really deleted, so record exactly
what the screen shows about each resource. Do not guess and do not add verdicts.

Return only JSON:

{
  "screen": "<short name of the tool/screen>",
  "observed_at": "<ISO-8601 time the screen shows, if any>",
  "records": [
    {
      "resource_id": "<id exactly as shown, e.g. account_5762, backup_9287, support_9538>",
      "system": "<owning system if shown or implied>",
      "state": "PRESENT" | "ABSENT",
      "exposed": true | false,          // direct identifiers (name, email, phone, email hash) visible for this record
      "restore_allowed": true | false | null,   // backups only: can it still be restored
      "enabled": true | false | null,           // data flows/connectors only
      "identifiers": ["<emails, phones, names, user_ids shown for this record>"],
      "record_type": "<USER_PROFILE | SUPPORT_MESSAGE | SUPPORT_ATTACHMENT | ANALYTICS_PROFILE | SEARCH_INDEX_ENTRY | USER_EXPORT | MARKETING_PROFILE | BACKUP_ARCHIVE | SECURITY_AUDIT_LOG | ACCOUNT_TOMBSTONE | DATA_FLOW | ''>",
      "observed_at": "<ISO time for this record if shown>",
      "note": "<one short phrase with any status text shown, e.g. 'restore completed 25 Apr'>"
    }
  ]
}

Rules:
- One record per resource id visible on screen (tables, side panels, activity feeds).
- A row listed in a table is PRESENT. Mark ABSENT only when the screen says the record is
  deleted, not found, or removed.
- Copy identifiers exactly (keep case); include every email/phone/name/user_id tied to the row.
