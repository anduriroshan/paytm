# Connected sources

This folder tells Munim **what it can reach, and when**. Each `.yaml` file describes one system.
Munim re-reads the folder before every case, so you don't need to restart.

Munim has no vector database or knowledge base. This folder is the list of systems it can reach.

| file | kind | reached when | fixes it carries out |
|---|---|---|---|
| `ledger.yaml` | database | always | REFUND_DEBITS |
| `devices.yaml` | database | rent complaint, device-returned / duplicate / one-device claim, words like *soundbox* | CANCEL_SUBSCRIPTION, SCHEDULE_PICKUP |
| `loans.yaml` | database | loan complaint, words like *EDI*, *EMI* | none |
| `payouts.yaml` | database | settlement complaint, short / missing payout | none |
| `holds-kyc.yaml` | database | settlement complaint, payout not received, words like *hold*, *KYC* | SEND_KYC_LINK, RELEASE_HOLD |
| `courier-scans.yaml` | file (CSV) | device-returned claim, words like *pickup*, *courier* | none |
| `lender-api.yaml` | API | loan complaint (**turned off**: example only) | none |

The data in `files/courier_scans.csv` is fictional. `lender-api.yaml` points at an example URL.

## Add a source

1. Copy `_template.yaml` to a new file, for example `courier-api.yaml`. Files starting with `_` are skipped.
2. Fill in `id`, `name`, `kind`, `holds` (one sentence; the AI reads it) and `when`.
3. Add the details for its kind:
   - **file:** `file.path` (inside this folder) and `file.merchant_column`. CSV or JSON.
   - **api:** `api.url` (use `{merchant_id}`), `api.headers`, `api.rows_at`, `api.merchant_column`. GET only.
   - **database:** `tables`, each with a `name` and an `about` text for the AI. Only Munim's own tables.
4. Open the **Sources** page in the app to check it. Errors are listed there and in `GET /api/sources`.

**API keys:** put the key in `backend/.env`, and write `${NAME}` in the header, like this:
```yaml
api:
  headers:
    Authorization: Bearer ${PARTNER_API_KEY}
```
Use this block style. The one-line `{...}` style breaks on the `${...}`. A header that looks like a secret but has no `${...}` is refused.

## When Munim reaches a source

A source is reached when **any** of its `when` rules match the complaint:

| rule | matches when |
|---|---|
| `always: true` | every case |
| `topics: [rent \| loan \| settlement]` | the complaint's topic, from the intake step |
| `claims: [device_returned, ...]` | the merchant makes that claim (one of the 8 claim kinds in `_template.yaml`) |
| `words: [pickup, ...]` | a word starting with one of these appears in the complaint or its English translation |

There are also two automatic cases:
- If the topic is unclear, every database source is reached.
- If the money trail follows a link into a database source, that source counts as reached.

## What reaching a source does

| kind | what happens |
|---|---|
| database | The AI sees its tables (the schema card), and its read-only probes may query them. |
| file / api | Munim reads only this merchant's rows (at most `max_rows`) and shows them under **Proof › Where it looked**. The AI gets them as untrusted data, never as instructions. |

A failing file or API never stops a case. It shows as an error on that source.

## Turning a source off

Set `enabled: false`.
- **Database:** its tables leave the schema card, and probes are refused.
- **Fixes:** the Action Firewall refuses the fixes listed under its `actions`. The message names the file.

Each table and each fix can belong to only one source. If two files claim the same one, the newer file is skipped and listed under Problems.

## What this does not change

The seven known-cause checks (the suspect lineup), the money trail and the statement checks are code. They read Munim's own tables directly, whatever this folder says. This folder controls three things:
- what the AI can see and query
- which outside files and APIs are read
- which fixes are available
