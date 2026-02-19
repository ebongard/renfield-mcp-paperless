#!/usr/bin/env python3
"""
renfield-mcp-paperless — MCP server for Paperless-NGX document search.

Returns compact search results (id, title, correspondent, document_type,
tags, snippet) with IDs resolved to human-readable names.
Supports structured filters, auto-pagination, and content snippets.

Environment variables:
    PAPERLESS_API_URL    — Base URL (e.g. http://your-paperpess-url)
    PAPERLESS_API_TOKEN  — API authentication token
"""

import base64
import logging
import os
import re
import sys
from collections import Counter
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
_tag_cache: Optional[dict[int, str]] = None

# --- Constants ---
_MAX_RESULTS_CAP = 500
_PAGE_SIZE = 100


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
    """Populate correspondent/document_type/storage_path/tag caches once."""
    global _correspondent_cache, _document_type_cache, _storage_path_cache, _tag_cache

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

    if _tag_cache is None:
        items = await _fetch_all_pages(
            client, f"{PAPERLESS_API_URL}/api/tags/?fields=id,name"
        )
        _tag_cache = {it["id"]: it["name"] for it in items}
        logger.info("Cached %d tags", len(_tag_cache))


# --- Name → ID resolution helpers ---


def _resolve_name_to_id(name: str, cache: dict[int, str]) -> int | None:
    """Resolve a human-readable name to a cache ID.

    Priority: exact match → case-insensitive → substring (bidirectional).
    """
    name_lower = name.lower()

    # 1. Exact match
    for id_, cached_name in cache.items():
        if cached_name == name:
            return id_

    # 2. Case-insensitive
    for id_, cached_name in cache.items():
        if cached_name.lower() == name_lower:
            return id_

    # 3. Substring (bidirectional): "Telekom" matches "Telekom Deutschland GmbH"
    #    and "Telekom Deutschland GmbH" matches "Telekom"
    for id_, cached_name in cache.items():
        if name_lower in cached_name.lower() or cached_name.lower() in name_lower:
            return id_

    return None


def _resolve_tags_to_ids(tag_names: list[str], cache: dict[int, str]) -> list[int]:
    """Resolve a list of tag names to IDs, skipping unresolved ones."""
    ids = []
    for name in tag_names:
        id_ = _resolve_name_to_id(name, cache)
        if id_ is not None:
            ids.append(id_)
    return ids


# --- Auto-pagination helper ---


async def _fetch_documents(
    client: httpx.AsyncClient, params: dict, max_results: int
) -> tuple[list[dict], int]:
    """Fetch documents with auto-pagination up to max_results.

    Returns (results, total_count).
    """
    params = {**params, "page": 1, "page_size": _PAGE_SIZE}
    all_results: list[dict] = []
    total_count = 0

    resp = await client.get(
        f"{PAPERLESS_API_URL}/api/documents/",
        params=params,
        headers=_headers(),
    )
    resp.raise_for_status()
    data = resp.json()

    total_count = data.get("count", 0)
    all_results.extend(data.get("results", []))

    next_url = data.get("next")
    while next_url and len(all_results) < max_results:
        resp = await client.get(next_url, headers=_headers())
        resp.raise_for_status()
        data = resp.json()
        all_results.extend(data.get("results", []))
        next_url = data.get("next")

    return all_results[:max_results], total_count


# --- Snippet extraction ---


def _extract_snippet(
    content: str | None, query: str | None, max_length: int = 200
) -> str | None:
    """Extract a relevant snippet from document content around the query match.

    Tries full phrase first, then individual words (longest first).
    Centers the snippet around the match and avoids breaking mid-word.
    """
    if not content:
        return None

    content = content.strip()
    if not content:
        return None

    match_pos = None

    if query:
        # Try full phrase first
        idx = content.lower().find(query.lower())
        if idx >= 0:
            match_pos = idx
        else:
            # Try individual words, longest first (most specific)
            words = [w for w in query.split() if len(w) >= 2]
            words.sort(key=len, reverse=True)
            for word in words:
                idx = content.lower().find(word.lower())
                if idx >= 0:
                    match_pos = idx
                    break

    if match_pos is None:
        # No match or no query — return beginning of content
        if len(content) <= max_length:
            return content
        # Find word boundary
        end = content.rfind(" ", 0, max_length)
        if end < max_length // 2:
            end = max_length
        return content[:end] + "..."

    # Center snippet around match
    half = max_length // 2
    start = max(0, match_pos - half)
    end = min(len(content), start + max_length)

    # Adjust start to not break mid-word
    if start > 0:
        space = content.find(" ", start)
        if space >= 0 and space < start + 20:
            start = space + 1

    # Adjust end to not break mid-word
    if end < len(content):
        space = content.rfind(" ", start, end)
        if space > start + max_length // 2:
            end = space

    snippet = content[start:end]
    if start > 0:
        snippet = "..." + snippet
    if end < len(content):
        snippet = snippet + "..."

    return snippet


# --- Summary builder ---


def _build_summary(
    results: list[dict],
    total_count: int,
    max_results: int,
    filters_applied: dict,
) -> dict:
    """Build an aggregation summary for the search results."""
    # Count correspondents and document types
    corr_counter: Counter = Counter()
    dtype_counter: Counter = Counter()
    for r in results:
        if r.get("correspondent"):
            corr_counter[r["correspondent"]] += 1
        if r.get("document_type"):
            dtype_counter[r["document_type"]] += 1

    summary: dict = {
        "total_matching": total_count,
        "returned": len(results),
    }

    if filters_applied:
        summary["filters"] = filters_applied

    if len(results) < total_count:
        summary["note"] = f"Showing first {len(results)} of {total_count} matches."

    if corr_counter:
        summary["top_correspondents"] = [
            {"name": name, "count": count}
            for name, count in corr_counter.most_common(5)
        ]

    if dtype_counter:
        summary["top_document_types"] = [
            {"name": name, "count": count}
            for name, count in dtype_counter.most_common(5)
        ]

    return summary


def _resolve_document(doc: dict, query: str | None = None) -> dict:
    """Map a raw Paperless document to compact format with resolved names."""
    corr_id = doc.get("correspondent")
    dtype_id = doc.get("document_type")
    tag_ids = doc.get("tags") or []

    # Resolve tag IDs to names
    resolved_tags = []
    if tag_ids and _tag_cache:
        resolved_tags = [_tag_cache[tid] for tid in tag_ids if tid in _tag_cache]

    # Extract snippet from content
    snippet = _extract_snippet(doc.get("content"), query)

    result = {
        "id": doc["id"],
        "title": doc.get("title", ""),
        "created": doc.get("created"),
        "correspondent": (_correspondent_cache or {}).get(corr_id) if corr_id else None,
        "document_type": (_document_type_cache or {}).get(dtype_id) if dtype_id else None,
        "tags": resolved_tags if resolved_tags else None,
        "snippet": snippet,
    }

    return result


# --- MCP Server ---
mcp = FastMCP("renfield-paperless")


@mcp.tool()
async def search_documents(
    query: str | None = None,
    document_type: str | None = None,
    correspondent: str | None = None,
    tags: list[str] | None = None,
    storage_path: str | None = None,
    ordering: str = "-created",
    created_after: str | None = None,
    created_before: str | None = None,
    max_results: int = 100,
) -> dict:
    """Search documents in Paperless-NGX with filters, snippets, and auto-pagination.

    Returns a summary (total count, top correspondents/types) followed by
    compact results with id, title, created date, correspondent, document_type,
    tags, and a content snippet showing WHY the document matched.

    The summary is always at the top so it survives response truncation.

    Args:
        query: Full-text search query (optional — omit for filter-only searches)
        document_type: Filter by document type name (e.g. "Rechnung")
        correspondent: Filter by correspondent name (e.g. "Telekom")
        tags: Filter by tag names (OR logic — matches any of the given tags)
        storage_path: Filter by storage path name
        ordering: Sort order (default: "-created" for newest first)
        created_after: Only documents created on or after this date (YYYY-MM-DD)
        created_before: Only documents created on or before this date (YYYY-MM-DD)
        max_results: Maximum results to return (default: 100, max: 500)
    """
    if not PAPERLESS_API_URL:
        return {"error": "PAPERLESS_API_URL not configured"}
    if not PAPERLESS_API_TOKEN:
        return {"error": "PAPERLESS_API_TOKEN not configured"}

    max_results = max(1, min(max_results, _MAX_RESULTS_CAP))

    async with httpx.AsyncClient(timeout=30.0) as client:
        await _ensure_caches(client)

        params: dict = {
            "ordering": ordering,
            "fields": "id,title,created,correspondent,document_type,tags,content",
        }

        if query:
            params["query"] = query
        if created_after:
            params["created__date__gte"] = created_after
        if created_before:
            params["created__date__lte"] = created_before

        # Resolve structured filters to IDs
        filters_applied: dict = {}

        if document_type:
            dt_id = _resolve_name_to_id(document_type, _document_type_cache or {})
            if dt_id is None:
                return {"error": f"Unknown document type: '{document_type}'"}
            params["document_type__id"] = dt_id
            filters_applied["document_type"] = document_type

        if correspondent:
            corr_id = _resolve_name_to_id(correspondent, _correspondent_cache or {})
            if corr_id is None:
                return {"error": f"Unknown correspondent: '{correspondent}'"}
            params["correspondent__id"] = corr_id
            filters_applied["correspondent"] = correspondent

        if tags:
            tag_ids = _resolve_tags_to_ids(tags, _tag_cache or {})
            if tag_ids:
                params["tags__id__in"] = ",".join(str(tid) for tid in tag_ids)
                filters_applied["tags"] = tags

        if storage_path:
            sp_id = _resolve_name_to_id(storage_path, _storage_path_cache or {})
            if sp_id is None:
                return {"error": f"Unknown storage path: '{storage_path}'"}
            params["storage_path__id"] = sp_id
            filters_applied["storage_path"] = storage_path

        if created_after:
            filters_applied["created_after"] = created_after
        if created_before:
            filters_applied["created_before"] = created_before

        raw_results, total_count = await _fetch_documents(client, params, max_results)

    results = [_resolve_document(doc, query) for doc in raw_results]
    summary = _build_summary(results, total_count, max_results, filters_applied)

    return {
        "summary": summary,
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


@mcp.tool()
async def upload_document(
    title: str,
    file_content_base64: str,
    filename: str = "document.pdf",
    correspondent: str | None = None,
    document_type: str | None = None,
    tags: list[str] | None = None,
) -> dict:
    """Upload a document to Paperless-NGX for OCR and archiving.

    Accepts a base64-encoded file and submits it to Paperless for processing.
    Returns a task ID that can be used to track the import status.

    Args:
        title: Document title
        file_content_base64: Base64-encoded file content
        filename: Original filename (default: document.pdf)
        correspondent: Optional correspondent name
        document_type: Optional document type name
        tags: Optional list of tag names
    """
    if not PAPERLESS_API_URL:
        return {"error": "PAPERLESS_API_URL not configured"}
    if not PAPERLESS_API_TOKEN:
        return {"error": "PAPERLESS_API_TOKEN not configured"}

    try:
        file_bytes = base64.b64decode(file_content_base64)
    except Exception:
        return {"error": "Invalid base64 content"}

    data: dict[str, str] = {"title": title}
    if correspondent:
        data["correspondent"] = correspondent
    if document_type:
        data["document_type"] = document_type
    if tags:
        for i, tag in enumerate(tags):
            data[f"tags[{i}]"] = tag

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            f"{PAPERLESS_API_URL}/api/documents/post_document/",
            headers=_headers(),
            files={"document": (filename, file_bytes)},
            data=data,
        )
        resp.raise_for_status()

    # Paperless returns the task ID as plain text
    task_id = resp.text.strip().strip('"')

    return {
        "task_id": task_id,
        "title": title,
        "filename": filename,
    }


def main():
    """Entry point for console script and python -m."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
