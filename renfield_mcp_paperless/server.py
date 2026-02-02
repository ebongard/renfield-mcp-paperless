#!/usr/bin/env python3
"""
renfield-mcp-paperless — MCP server for Paperless-NGX document search.

Returns compact search results (id, title, correspondent, document_type,
storage_path) with IDs resolved to human-readable names.

Environment variables:
    PAPERLESS_API_URL    — Base URL (e.g. http://your-paperpess-url)
    PAPERLESS_API_TOKEN  — API authentication token
"""

import base64
import logging
import os
import re
import sys
from typing import Optional

import httpx
from mcp.server.fastmcp import FastMCP

# MCP stdio servers must NEVER write to stdout — log to stderr only.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("renfield-mcp-paperless")

# --- Configuration from environment ---
PAPERLESS_API_URL = os.environ.get("PAPERLESS_API_URL", "").rstrip("/")
PAPERLESS_API_TOKEN = os.environ.get("PAPERLESS_API_TOKEN", "")

# --- ID-to-name caches (None = not yet loaded) ---
_correspondent_cache: Optional[dict[int, str]] = None
_document_type_cache: Optional[dict[int, str]] = None
_storage_path_cache: Optional[dict[int, str]] = None


def _headers() -> dict[str, str]:
    return {"Authorization": f"Token {PAPERLESS_API_TOKEN}"}


async def _fetch_all_pages(client: httpx.AsyncClient, url: str) -> list[dict]:
    """Fetch all pages of a paginated Paperless endpoint."""
    results: list[dict] = []
    while url:
        resp = await client.get(url, headers=_headers())
        resp.raise_for_status()
        data = resp.json()
        results.extend(data.get("results", []))
        url = data.get("next")
    return results


async def _ensure_caches(client: httpx.AsyncClient) -> None:
    """Populate correspondent/document_type/storage_path caches once."""
    global _correspondent_cache, _document_type_cache, _storage_path_cache

    if _correspondent_cache is None:
        items = await _fetch_all_pages(
            client, f"{PAPERLESS_API_URL}/api/correspondents/?fields=id,name"
        )
        _correspondent_cache = {it["id"]: it["name"] for it in items}
        logger.info("Cached %d correspondents", len(_correspondent_cache))

    if _document_type_cache is None:
        items = await _fetch_all_pages(
            client, f"{PAPERLESS_API_URL}/api/document_types/?fields=id,name"
        )
        _document_type_cache = {it["id"]: it["name"] for it in items}
        logger.info("Cached %d document types", len(_document_type_cache))

    if _storage_path_cache is None:
        items = await _fetch_all_pages(
            client, f"{PAPERLESS_API_URL}/api/storage_paths/?fields=id,path,name"
        )
        _storage_path_cache = {
            it["id"]: it.get("path", it.get("name", "")) for it in items
        }
        logger.info("Cached %d storage paths", len(_storage_path_cache))


def _resolve_document(doc: dict) -> dict:
    """Map a raw Paperless document to compact format with resolved names."""
    corr_id = doc.get("correspondent")
    dtype_id = doc.get("document_type")
    spath_id = doc.get("storage_path")

    return {
        "id": doc["id"],
        "title": doc.get("title", ""),
        "created": doc.get("created"),
        "correspondent": (_correspondent_cache or {}).get(corr_id) if corr_id else None,
        "document_type": (_document_type_cache or {}).get(dtype_id) if dtype_id else None,
        "storage_path": (_storage_path_cache or {}).get(spath_id) if spath_id else None,
    }


# --- MCP Server ---
mcp = FastMCP("renfield-paperless")


@mcp.tool()
async def search_documents(
    query: str,
    page: int = 1,
    page_size: int = 25,
    ordering: str = "-created",
) -> dict:
    """Search documents in Paperless-NGX by full-text query.

    Returns a compact list with id, title, created date, correspondent,
    document_type, and storage_path (names resolved from IDs).
    Results are sorted by date (newest first) by default.

    Args:
        query: Full-text search query
        page: Page number (default: 1)
        page_size: Results per page (default: 25, max: 100)
        ordering: Sort order (default: "-created" for newest first)
    """
    if not PAPERLESS_API_URL:
        return {"error": "PAPERLESS_API_URL not configured"}
    if not PAPERLESS_API_TOKEN:
        return {"error": "PAPERLESS_API_TOKEN not configured"}

    page_size = max(1, min(page_size, 100))

    async with httpx.AsyncClient(timeout=15.0) as client:
        await _ensure_caches(client)

        resp = await client.get(
            f"{PAPERLESS_API_URL}/api/documents/",
            params={
                "query": query,
                "page": page,
                "page_size": page_size,
                "ordering": ordering,
                "fields": "id,title,created,correspondent,document_type,storage_path",
            },
            headers=_headers(),
        )
        resp.raise_for_status()
        data = resp.json()

    results = [_resolve_document(doc) for doc in data.get("results", [])]

    return {
        "count": data.get("count", 0),
        "page": page,
        "page_size": page_size,
        "results": results,
    }


@mcp.tool()
async def download_document(document_id: int) -> dict:
    """Download a document from Paperless-NGX by ID.

    Returns the file as base64-encoded content with filename and MIME type.
    Use the document IDs from search_documents results.

    Args:
        document_id: Paperless document ID
    """
    if not PAPERLESS_API_URL:
        return {"error": "PAPERLESS_API_URL not configured"}
    if not PAPERLESS_API_TOKEN:
        return {"error": "PAPERLESS_API_TOKEN not configured"}

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            f"{PAPERLESS_API_URL}/api/documents/{document_id}/download/",
            headers=_headers(),
        )
        if resp.status_code == 404:
            return {"error": f"Document {document_id} not found"}
        resp.raise_for_status()

    # Extract filename from Content-Disposition header
    filename = f"document_{document_id}.pdf"
    cd = resp.headers.get("content-disposition", "")
    match = re.search(r'filename="?([^";\n]+)"?', cd)
    if match:
        filename = match.group(1).strip()

    mime_type = resp.headers.get("content-type", "application/pdf").split(";")[0].strip()
    content_b64 = base64.b64encode(resp.content).decode("ascii")

    return {
        "id": document_id,
        "filename": filename,
        "mime_type": mime_type,
        "content_base64": content_b64,
    }


def main():
    """Entry point for console script and python -m."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
