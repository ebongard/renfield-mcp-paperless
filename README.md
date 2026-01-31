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

## License

MIT
