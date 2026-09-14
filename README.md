# renfield-mcp-paperless

MCP server for [Paperless-NGX](https://docs.paperless-ngx.com/) document search with compact responses and ID resolution.

Replaces other MCP Servers which might return full document objects including OCR content (~2KB each), causing token budget issues when the LLM processes search results.

## What it does

- Uses Paperless `?fields=id,title,correspondent,document_type,storage_path` for server-side field selection
- Resolves integer IDs to human-readable names (e.g. correspondent `178` → `"IONOS SE"`)
- Returns compact results (~100-200 bytes per document vs ~2-3KB)
- Supports pagination

## Installation

```bash
pip install git+https://github.com/ebongard/renfield-mcp-paperless.git
```

## Configuration

Set environment variables:

```bash
PAPERLESS_API_URL=http://your-paperless-url
PAPERLESS_API_TOKEN=your-api-token
```

Get the API token from: Paperless-NGX → Profile → Auth Tokens.

## Usage

### As a CLI

```bash
renfield-mcp-paperless
```

### As a Python module

```bash
python -m renfield_mcp_paperless
```

### MCP server config (stdio transport)

```yaml
- name: paperless
  transport: stdio
  command: python
  args: ["-m", "renfield_mcp_paperless"]
```

## Tool: `search_documents`

| Parameter   | Type | Default | Description                    |
|-------------|------|---------|--------------------------------|
| `query`     | str  | —       | Full-text search query         |
| `page`      | int  | 1       | Page number                    |
| `page_size` | int  | 25      | Results per page (max 100)     |

**Response:**

```json
{
  "count": 53,
  "page": 1,
  "page_size": 25,
  "results": [
    {
      "id": 1,
      "title": "COMPANY 1 Rechnung 2030-01",
      "correspondent": "COMPANNY 1",
      "document_type": "Rechnung",
      "storage_path": "rechnungen/company1"
    }
  ]
}
```

## Tool: `search_index_health`

Checks whether the Paperless full-text **search index** contains the documents in the
database, and optionally re-indexes the ones it lacks.

Paperless has no REST endpoint for a full reindex — that is the `document_index reindex`
management command on the Paperless host. `/api/status/` only reports whether the index
can be opened. A document `PATCH` re-indexes that one document, which is what healing
uses.

| Parameter         | Type | Default | Description |
|-------------------|------|---------|-------------|
| `sample_size`     | int  | 50      | Documents per page to probe (1–200) |
| `page`            | int  | 1       | Page of the id-descending document list; use `next_page` to walk the archive |
| `heal`            | bool | false   | Re-save (unchanged title) + re-probe missing documents |
| `max_touch`       | int  | 25      | Max documents re-saved per call (0–100) |
| `min_age_seconds` | int  | 900     | Skip documents added more recently (indexing may still run) |
| `probe_proven`    | bool | false   | The caller proved in an earlier call that the id probe works |
| `exclude_ids`     | list | —       | Documents that must never be re-saved (e.g. given up after repeated failures) |
| `allow_workflows` | bool | false   | Heal even while "Document Updated" workflows are active |

How it decides: the page of ids comes from the database (no `query`, index-independent);
each id is probed with `query=id:<n>`. Misses only count once the probe is **proven** to
work on this Paperless: a document on the same page was found, a **positive control** (a
recent document from page 1) was found in this call, or the caller passed
`probe_proven`. The control is what catches an index that lost its *old* documents,
where every old page is entirely missing. The output `probe_proven` reports only this
call's evidence, so a caller's stored proof can expire.

| `verdict`      | Meaning |
|----------------|---------|
| `healthy`      | every sampled document is in the index |
| `degraded`     | documents are missing and the probe is proven |
| `inconclusive` | misses with an unproven probe, a rejected probe (HTTP 400), or a sample cut short. With `heal`, ONE canary is re-saved; if it then appears the verdict becomes `degraded` |
| `index_error`  | `/api/status/` reports the index cannot be opened — run `document_index reindex` |
| `empty`        | nothing old enough to judge on this page |

**Healing and its side effects.** A heal sends an **empty** partial update
(`PATCH {}`). paperless-ngx's `DocumentViewSet.update` re-indexes the document on every
update, so no field has to be sent; nothing is overwritten, nothing is deleted or
re-OCR'd. It is still **not side-effect free**: Paperless bumps `modified` and sends
`document_updated`, which runs **every enabled workflow with a "Document Updated"
trigger** once per healed document. Such workflows may assign tags, owner or
permissions, send e-mail or call webhooks. The tool therefore reads `/api/workflows/`
first and **refuses to heal** (`heal_blocked="workflows"`, or
`"workflows_unverifiable"` when the list cannot be read — fail closed) unless
`allow_workflows=true`.

Every request retries on HTTP 429. One call has a 120 s budget: documents the budget
leaves untouched stay in `missing_ids`, re-saved ones it leaves un-probed are reported in
`unverified_ids` with `complete=false` — never as a failed heal.

**Response** (abridged): `index_check` (contract marker), `verdict`, `db_total`, `page`,
`next_page`, `sampled`, `found`, `missing`, `found_ids`, `missing_ids`, `control_id`,
`control_found`, `probe_proven`, `index_status`, `index_error`, `heal_attempted`,
`heal_blocked`, `blocking_workflows`, `touched`, `healed`, `healed_ids`,
`still_missing_ids` (re-saved and still absent), `skipped_ids` (could not be re-saved,
e.g. deleted meanwhile), `excluded_ids`, `unverified_ids`, `complete`, `message`.

Requires renfield-mcp-paperless ≥ 1.13.0.

## License

MIT
