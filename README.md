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

How it decides: the page of ids comes from the database (no `query`, index-independent);
each id is probed with `query=id:<n>`.

| `verdict`      | Meaning |
|----------------|---------|
| `healthy`      | every sampled document is in the index |
| `degraded`     | some are missing while others were found — proof the probe works |
| `inconclusive` | none found, the probe was rejected (HTTP 400), or the sample was cut short. With `heal`, ONE canary is re-saved; if it then appears the verdict becomes `degraded` |
| `index_error`  | `/api/status/` reports the index cannot be opened — run `document_index reindex` |
| `empty`        | nothing old enough to judge on this page |

Healing never deletes, reprocesses or changes a field value; Paperless does bump
`modified` and runs "document updated" workflows. Every request retries on HTTP 429 and
one call has a 120 s budget.

**Response** (abridged): `index_check` (contract marker), `verdict`, `db_total`, `page`,
`next_page`, `sampled`, `found`, `missing`, `missing_ids`, `index_status`, `index_error`,
`heal_attempted`, `touched`, `healed`, `still_missing_ids`, `complete`, `message`.

## License

MIT
