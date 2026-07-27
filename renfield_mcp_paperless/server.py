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

import asyncio
import base64
import logging
import mimetypes
import os
import re
import sys
from collections import Counter
from typing import Literal, Optional

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
    """Populate correspondent/document_type/storage_path/tag caches once.

    Fetches run in parallel via ``asyncio.gather`` — typical cold-cache
    cost drops from ~500 ms (four serial calls) to ~150 ms (one
    round-trip's worth). Only dimensions whose cache is ``None`` get
    fetched, so a partial invalidation (one dimension flushed by a
    create_* tool) re-populates just that dimension.
    """
    global _correspondent_cache, _document_type_cache, _storage_path_cache, _tag_cache

    async def _load_correspondents():
        global _correspondent_cache
        if _correspondent_cache is None:
            items = await _fetch_all_pages(
                client, f"{PAPERLESS_API_URL}/api/correspondents/?fields=id,name"
            )
            _correspondent_cache = {it["id"]: it["name"] for it in items}
            logger.info("Cached %d correspondents", len(_correspondent_cache))

    async def _load_document_types():
        global _document_type_cache
        if _document_type_cache is None:
            items = await _fetch_all_pages(
                client, f"{PAPERLESS_API_URL}/api/document_types/?fields=id,name"
            )
            _document_type_cache = {it["id"]: it["name"] for it in items}
            logger.info("Cached %d document types", len(_document_type_cache))

    async def _load_storage_paths():
        global _storage_path_cache
        if _storage_path_cache is None:
            items = await _fetch_all_pages(
                client, f"{PAPERLESS_API_URL}/api/storage_paths/?fields=id,path,name"
            )
            _storage_path_cache = {
                it["id"]: it.get("path", it.get("name", "")) for it in items
            }
            logger.info("Cached %d storage paths", len(_storage_path_cache))

    async def _load_tags():
        global _tag_cache
        if _tag_cache is None:
            items = await _fetch_all_pages(
                client, f"{PAPERLESS_API_URL}/api/tags/?fields=id,name"
            )
            _tag_cache = {it["id"]: it["name"] for it in items}
            logger.info("Cached %d tags", len(_tag_cache))

    await asyncio.gather(
        _load_correspondents(),
        _load_document_types(),
        _load_storage_paths(),
        _load_tags(),
    )


_CacheDimension = Literal["correspondent", "document_type", "storage_path", "tag"]


def _invalidate_cache(dimension: _CacheDimension) -> None:
    """Flush one taxonomy cache so the next ``_ensure_caches`` reloads it.

    Called by each ``create_*`` tool after a successful create so that
    Renfield's cache (which mirrors Paperless) picks up the new entry
    immediately instead of waiting for the process-level TTL (10 min
    in the Renfield-side extractor).
    """
    global _correspondent_cache, _document_type_cache, _storage_path_cache, _tag_cache
    if dimension == "correspondent":
        _correspondent_cache = None
    elif dimension == "document_type":
        _document_type_cache = None
    elif dimension == "storage_path":
        _storage_path_cache = None
    elif dimension == "tag":
        _tag_cache = None


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
        # page_count lets callers establish document identity (e.g. the dedupe tool's
        # metadata match) straight from search, without a per-document get_document.
        "page_count": doc.get("page_count"),
        "snippet": snippet,
        "storage_path": _storage_path_cache.get(doc.get("storage_path")) if doc.get("storage_path") else None,
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
    tags, page_count, and a content snippet showing WHY the document matched.

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
            "fields": "id,title,created,correspondent,document_type,tags,content,page_count",
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


# --- Upload helpers: task polling + PATCH with retry ---

# How long to wait for Paperless's consume queue to produce a document id
# before giving up and returning a "pending" result. Keep in line with the
# LLM extraction latency budget — typical Paperless consume is < 10 s for
# text PDFs, 15-30 s for image-PDFs with OCR. Exceed this and the post-upload
# PATCH (storage_path, custom_fields) cannot run synchronously.
_UPLOAD_TASK_POLL_TIMEOUT_S = 30.0
_UPLOAD_TASK_POLL_INTERVAL_S = 1.0

# Paperless-ngx rejects a duplicate document ASYNCHRONOUSLY inside the consume
# task: the task ends in status=FAILURE with a ``result`` string, with no
# structured "duplicate" field. The canonical message is
# ``"<file>: Not consuming <file>: It is a duplicate of <title> (#<pk>)"``.
# We pin the SPECIFIC phrase so a duplicate reads as a TERMINAL SUCCESS to the
# caller (the document is already in Paperless — nothing to retry). Matched
# case-insensitively. Kept narrow on purpose: a bare "duplicate of" can appear
# in unrelated failure text, and misreading a genuine failure as a duplicate
# would silently swallow it — a miss (treated as a normal failure) is the safe
# direction, a false positive is not.
_PAPERLESS_DUPLICATE_MARKERS = ("it is a duplicate",)


def _is_duplicate_failure(result_text) -> bool:
    """True when a consume FAILURE ``result`` is Paperless's duplicate rejection.
    Non-string / empty results (Paperless occasionally returns null) are not
    duplicates — and must not crash on ``.lower()``."""
    if not isinstance(result_text, str) or not result_text:
        return False
    low = result_text.lower()
    return any(marker in low for marker in _PAPERLESS_DUPLICATE_MARKERS)


async def _poll_task(
    client: httpx.AsyncClient, task_id: str, timeout_s: float | None = None
) -> dict:
    """Poll Paperless's task endpoint until the consume task reaches a terminal
    state, and report the outcome structurally.

    Returns a dict with ``status`` one of:
      - ``"success"``   — consumed; ``document_id`` is the new Paperless id.
      - ``"duplicate"`` — Paperless rejected it as a duplicate (already filed);
                          terminal SUCCESS for our purposes (nothing to retry).
      - ``"failure"``   — consume failed for a non-duplicate reason; ``detail``
                          carries Paperless's failure result string.
      - ``"pending"``   — the timeout elapsed before the task became terminal.

    ``timeout_s`` defaults to ``_UPLOAD_TASK_POLL_TIMEOUT_S`` when omitted (the
    default resolves at call time so tests can patch the module constant).
    """
    import json as _json
    if timeout_s is None:
        timeout_s = _UPLOAD_TASK_POLL_TIMEOUT_S
    loop = asyncio.get_running_loop()
    url = f"{PAPERLESS_API_URL}/api/tasks/?task_id={task_id}"
    deadline = loop.time() + timeout_s

    while loop.time() < deadline:
        try:
            resp = await client.get(url, headers=_headers())
            resp.raise_for_status()
            data = resp.json()

            # Paperless's /api/tasks/?task_id= returns a list (usually
            # length 0 while the task is still being registered, length 1
            # once it's in the DB).
            tasks = data if isinstance(data, list) else data.get("results", [])
            if tasks:
                task = tasks[0]
                # Normalise case: Paperless returned UPPER-CASE task status
                # ("SUCCESS"/"FAILURE") historically but switched to lower-case
                # ("success"/"failure") in a later version. Comparing case-sensitively
                # against "SUCCESS" then NEVER matched → the poll looped to timeout →
                # the doc never settled → re-upload loop + duplicates (2026-07). Upper()
                # here matches both spellings.
                status = (task.get("status") or "").upper()
                if status == "SUCCESS":
                    related = task.get("related_document")
                    if related is not None:
                        return {"status": "success", "document_id": int(related), "detail": None}
                    logger.warning(
                        "Task %s completed SUCCESS but has no related_document",
                        task_id,
                    )
                    return {"status": "success", "document_id": None, "detail": None}
                if status == "FAILURE":
                    result_text = task.get("result")
                    is_dup = _is_duplicate_failure(result_text)
                    logger.warning(
                        "Task %s failed in Paperless (%s): %s",
                        task_id,
                        "duplicate" if is_dup else "non-duplicate",
                        result_text or "no details",
                    )
                    return {
                        "status": "duplicate" if is_dup else "failure",
                        "document_id": None,
                        "detail": result_text,
                    }
        except (httpx.HTTPError, _json.JSONDecodeError, ValueError) as e:
            # Narrow catch so programming errors (AttributeError, KeyError in
            # our own code) don't get silently swallowed into a 30 s hang.
            # Transient Paperless errors on /api/tasks/ do get retried.
            logger.warning("Error polling task %s: %s", task_id, e)

        await asyncio.sleep(_UPLOAD_TASK_POLL_INTERVAL_S)

    logger.warning("Task %s did not complete within %.1fs", task_id, timeout_s)
    return {"status": "pending", "document_id": None, "detail": "timeout"}


async def _poll_task_for_document_id(
    client: httpx.AsyncClient, task_id: str, timeout_s: float | None = None
) -> int | None:
    """Back-compat thin wrapper over :func:`_poll_task` for the upload PATCH
    path: returns the document id on success, else None (duplicate / failure /
    timeout all map to None — "PATCH cannot run now")."""
    outcome = await _poll_task(client, task_id, timeout_s)
    return outcome.get("document_id")


async def _patch_document_with_retry(
    client: httpx.AsyncClient, document_id: int, patch_data: dict, max_tries: int = 3
) -> tuple[dict | None, str | None]:
    """PATCH /api/documents/{id}/ with exponential backoff.

    Returns ``(result_dict, None)`` on success, or ``(None, reason)`` on
    failure where ``reason`` is one of:
      - ``"client_error"`` — 4xx from Paperless (unknown id, invalid
        format). Did not retry; retrying wouldn't help.
      - ``"retries_exhausted"`` — all ``max_tries`` attempts failed with
        transport errors or 5xx responses.

    Backoff schedule: 0.5 s, 1.5 s, 4.5 s between attempts (≈ 6.5 s total
    wall-clock worst case). Retries on httpx transport errors and any
    5xx response; 4xx errors bail immediately.
    """
    url = f"{PAPERLESS_API_URL}/api/documents/{document_id}/"
    last_exc: Exception | None = None
    for attempt in range(max_tries):
        try:
            resp = await client.patch(
                url,
                headers={**_headers(), "Content-Type": "application/json"},
                json=patch_data,
            )
            if 500 <= resp.status_code < 600:
                last_exc = httpx.HTTPStatusError(
                    f"{resp.status_code} on PATCH", request=resp.request, response=resp
                )
                logger.warning(
                    "PATCH document %d got %d on attempt %d",
                    document_id,
                    resp.status_code,
                    attempt + 1,
                )
            else:
                resp.raise_for_status()
                return resp.json(), None
        except (httpx.TimeoutException, httpx.TransportError) as e:
            last_exc = e
            logger.warning(
                "PATCH document %d transport error on attempt %d: %s",
                document_id,
                attempt + 1,
                e,
            )
        except httpx.HTTPStatusError as e:
            # 4xx → don't retry; the bind params are wrong (unknown id,
            # invalid format), and waiting won't help. Return a distinct
            # reason so the caller can distinguish "agent passed bad
            # inputs" from "Paperless was overloaded."
            logger.warning(
                "PATCH document %d got non-retryable %d: %s",
                document_id,
                e.response.status_code,
                e,
            )
            return None, "client_error"

        # Exponential backoff: 0.5 s, 1.5 s, 4.5 s
        if attempt < max_tries - 1:
            await asyncio.sleep(0.5 * (3 ** attempt))

    logger.warning(
        "PATCH document %d exhausted %d attempts: %s",
        document_id,
        max_tries,
        last_exc,
    )
    return None, "retries_exhausted"


@mcp.tool()
async def upload_document(
    title: str,
    file_content_base64: str,
    filename: str = "document.pdf",
    correspondent: str | None = None,
    document_type: str | None = None,
    tags: list[str] | None = None,
    storage_path: str | None = None,
    created_date: str | None = None,
    custom_fields: list[dict] | None = None,
    wait_for_consume: bool = True,
) -> dict:
    """Upload a document to Paperless-NGX for OCR and archiving.

    Accepts a base64-encoded file and submits it to Paperless for processing.
    Returns a task ID that can be used to track the import status.

    The fields that Paperless's ``post_document`` endpoint accepts directly
    (``correspondent``, ``document_type``, ``tags``) are sent with the
    initial POST. Fields that endpoint does NOT accept (``storage_path``,
    ``created_date``, ``custom_fields``) trigger a second-step PATCH once
    Paperless's consume queue produces the document id — see
    ``§ renfield-mcp-paperless additions`` in
    ``docs/design/paperless-llm-metadata.md``.

    The PATCH waits up to 30 s for the consume task to complete. If the
    task hasn't finished in that window, the response carries
    ``post_upload_patch: "timed_out"`` and the caller should surface the
    warning to the user — the document is still uploaded, but the extra
    metadata couldn't be attached synchronously.

    Args:
        title: Document title
        file_content_base64: Base64-encoded file content
        filename: Original filename (default: document.pdf)
        correspondent: Optional correspondent name (must exist in Paperless)
        document_type: Optional document type name (must exist in Paperless)
        tags: Optional list of tag names (must exist in Paperless)
        storage_path: Optional storage path name. Applied via post-upload PATCH.
        created_date: Optional document creation date (YYYY-MM-DD). Applied via PATCH.
        custom_fields: Optional list of ``[{field: id, value: ...}]``. Applied via PATCH.
        wait_for_consume: When True (default), block to apply the post-consume
            PATCH (storage_path/created_date/custom_fields) before returning —
            the historical behaviour. When False, return immediately after the
            POST with ``status="submitted"`` and a ``deferred_patch`` dict; the
            caller polls the consume task itself and applies those fields via
            ``update_document`` once the document id exists. Use False to avoid
            blocking the tool call on Paperless's consume queue, which can
            exceed the caller's tool-call timeout for large / OCR-heavy docs.
    """
    if not PAPERLESS_API_URL:
        return {"error": "PAPERLESS_API_URL not configured"}
    if not PAPERLESS_API_TOKEN:
        return {"error": "PAPERLESS_API_TOKEN not configured"}

    # Strict decoding: reject non-base64 characters (``_``, spaces, etc.) up
    # front instead of silently producing garbage bytes. LLM agents have been
    # observed to pass placeholder strings like
    # ``"base64_encoded_content_of_the_invoice"`` — those would decode without
    # error under the default (lenient) setting and reach Paperless as a
    # corrupt file, producing an opaque HTTP 400 with no useful error chain.
    try:
        file_bytes = base64.b64decode(file_content_base64, validate=True)
    except Exception:
        return {
            "error": (
                "Invalid base64 content. The ``file_content_base64`` parameter "
                "must be real base64-encoded file bytes, not a placeholder "
                "string or description."
            )
        }

    # Size floor: a real document cannot realistically be smaller than a PDF
    # magic-byte header + one object. If the caller passed something short
    # enough to be a placeholder that happened to be valid base64, fail fast
    # with a clear message rather than forwarding obvious garbage to Paperless.
    if len(file_bytes) < 100:
        return {
            "error": (
                f"Decoded file is only {len(file_bytes)} bytes, which is too "
                "small to be a real document. Make sure real file content is "
                "being passed, not a placeholder."
            )
        }

    # Paperless rejects application/octet-stream (httpx's default when the
    # content-type tuple element is omitted). Derive the real MIME from the
    # filename extension so the multipart upload carries a type Paperless'
    # consumer will accept (application/pdf, image/png, ...).
    content_type, _ = mimetypes.guess_type(filename)
    if not content_type:
        content_type = "application/pdf"  # sensible default for the common case

    # Whether we need a post-upload PATCH. Empty list for custom_fields
    # must NOT trigger a PATCH — doing so sends ``{"custom_fields": []}``
    # which would WIPE any existing custom fields on the document. Treat
    # empty list as "no-op, same as None" for the trigger check.
    needs_patch = (
        storage_path is not None
        or created_date is not None
        or (custom_fields is not None and len(custom_fields) > 0)
    )

    async with httpx.AsyncClient(timeout=60.0) as client:
        # Paperless's post_document endpoint uses DRF PrimaryKeyRelatedField
        # for correspondent / document_type / tags — sending raw name strings
        # produces HTTP 400 "Incorrect type. Expected pk value, received str."
        # Resolve name → ID up-front against the cached taxonomy the same way
        # the post-upload PATCH path does for storage_path / custom_fields.
        data: dict[str, str | int] = {"title": title}
        needs_caches = bool(correspondent or document_type or tags)
        if needs_caches:
            await _ensure_caches(client)

        if correspondent:
            cid = _resolve_name_to_id(correspondent, _correspondent_cache or {})
            if cid is None:
                return {
                    "error": (
                        f"Unknown correspondent: {correspondent!r}. Create it "
                        "first via create_correspondent, or omit the field."
                    ),
                }
            data["correspondent"] = cid
        if document_type:
            dtid = _resolve_name_to_id(document_type, _document_type_cache or {})
            if dtid is None:
                return {
                    "error": (
                        f"Unknown document_type: {document_type!r}. Create it "
                        "first via create_document_type, or omit the field."
                    ),
                }
            data["document_type"] = dtid
        if tags:
            tag_ids = _resolve_tags_to_ids(tags, _tag_cache or {})
            unresolved = [t for t in tags if _resolve_name_to_id(t, _tag_cache or {}) is None]
            if unresolved:
                return {
                    "error": (
                        f"Unknown tag(s): {unresolved!r}. Create them first "
                        "via create_tag, or omit them from the upload."
                    ),
                }
            for i, tid in enumerate(tag_ids):
                data[f"tags[{i}]"] = tid

        resp = await client.post(
            f"{PAPERLESS_API_URL}/api/documents/post_document/",
            headers=_headers(),
            files={"document": (filename, file_bytes, content_type)},
            data=data,
        )
        # Surface Paperless's own error body on 4xx/5xx instead of the
        # generic httpx message — the original bug report hit a plain
        # "Client error '400 Bad Request'" that hid the real reason
        # ("Incorrect type. Expected pk value, received str.").
        if resp.status_code >= 400:
            return {
                "error": (
                    f"Paperless rejected the upload with HTTP {resp.status_code}: "
                    f"{resp.text.strip() or '(empty response body)'}"
                ),
            }

        # Paperless returns the task ID as plain text (sometimes quoted)
        task_id = resp.text.strip().strip('"')

        result: dict = {
            "task_id": task_id,
            "title": title,
            "filename": filename,
        }

        if not needs_patch:
            return result

        if not wait_for_consume:
            # Submit-only: hand the post-consume metadata back to the caller to
            # apply asynchronously (via update_document once the consume task
            # produces a document id). Avoids blocking this tool call on
            # Paperless's consume queue. Field values are returned by NAME (as
            # received) — update_document resolves them the same way the
            # synchronous PATCH path does.
            result["status"] = "submitted"
            result["deferred_patch"] = {
                "storage_path": storage_path,
                "created_date": created_date,
                "custom_fields": custom_fields,
            }
            return result

        # Phase 2: poll the task endpoint to get the document id, then
        # issue a PATCH with the extra metadata fields. If polling times
        # out or the PATCH all retries exhausted, return the upload
        # success AND a patch-status flag so the caller can warn the user.
        await _ensure_caches(client)

        patch_data: dict = {}
        if storage_path is not None:
            sp_id = _resolve_name_to_id(storage_path, _storage_path_cache or {})
            if sp_id is None:
                result["post_upload_patch"] = "unknown_storage_path"
                result["patch_error"] = f"Unknown storage path: {storage_path!r}"
                return result
            patch_data["storage_path"] = sp_id
        if created_date is not None:
            patch_data["created"] = created_date
        if custom_fields:
            # Empty list drops through intentionally — see `needs_patch`
            # check above. Passing ``[]`` to Paperless wipes existing
            # custom fields, which is destructive and almost certainly
            # not what the caller wanted.
            patch_data["custom_fields"] = custom_fields

        document_id = await _poll_task_for_document_id(client, task_id)
        if document_id is None:
            result["post_upload_patch"] = "timed_out"
            result["patch_error"] = (
                f"Document was uploaded (task {task_id}) but Paperless's "
                f"consume queue did not produce a document id within "
                f"{_UPLOAD_TASK_POLL_TIMEOUT_S:.0f}s. The extra metadata "
                "(storage_path / created_date / custom_fields) was NOT "
                "attached. Set them manually in Paperless once the "
                "document is consumed."
            )
            return result

        result["document_id"] = document_id

        patched, patch_reason = await _patch_document_with_retry(
            client, document_id, patch_data
        )
        if patched is not None:
            result["post_upload_patch"] = "success"
        elif patch_reason == "client_error":
            # 4xx from Paperless — usually means one of the resolved ids
            # was stale (taxonomy changed between our cache warm-up and
            # the PATCH) or the payload shape was malformed. Different
            # recovery path than a transient 5xx: the caller shouldn't
            # retry blindly.
            result["post_upload_patch"] = "client_error"
            result["patch_error"] = (
                f"Document {document_id} was uploaded but the post-upload "
                "PATCH failed with a 4xx response (check Paperless logs "
                "for the specific reason). Retrying won't help — set the "
                "extra metadata manually via update_document or in the "
                "Paperless UI."
            )
        else:
            # retries_exhausted (5xx / transport errors across all attempts)
            result["post_upload_patch"] = "retries_exhausted"
            result["patch_error"] = (
                f"Document {document_id} was uploaded but the post-upload "
                "PATCH failed after 3 retries on transient errors. The "
                "extra metadata was NOT attached. Retry via update_document "
                "or set it manually in Paperless."
            )

    return result


@mcp.tool()
async def await_consume_result(task_id: str, timeout_s: float | None = None) -> dict:
    """Poll a Paperless consume task to a terminal state and report the outcome.

    Use after ``upload_document(wait_for_consume=False)`` to learn whether the
    document was filed, was rejected as a DUPLICATE (already in Paperless),
    failed to consume, or is still pending — Paperless decides this
    asynchronously, so the upload call returns before the verdict is known.

    Returns ``{"status": ..., "document_id": int|None, "detail": str|None}``
    where ``status`` is one of:
      - ``"success"``   — consumed; ``document_id`` is the new Paperless id.
      - ``"duplicate"`` — already filed (terminal success; nothing to retry).
      - ``"failure"``   — consume failed (non-duplicate); ``detail`` has why.
      - ``"pending"``   — still running after ``timeout_s`` (poll again later).

    ``timeout_s`` defaults to ~30 s. Errors (config / unreachable Paperless)
    surface as ``{"error": ...}``.
    """
    if not PAPERLESS_API_URL:
        return {"error": "PAPERLESS_API_URL not configured"}
    if not PAPERLESS_API_TOKEN:
        return {"error": "PAPERLESS_API_TOKEN not configured"}
    if not task_id:
        return {"error": "task_id is required"}
    async with httpx.AsyncClient(timeout=60.0) as client:
        return await _poll_task(client, task_id, timeout_s)


@mcp.tool()
async def get_document(document_id: int, include_content: bool = True) -> dict:
    """Get full details of a single document from Paperless-NGX by ID.

    Returns the complete document including full OCR text content,
    resolved correspondent/document_type/tag names, and original filename.
    Use the document IDs from search_documents results.

    Args:
        document_id: Paperless document ID
        include_content: When False, omit the (often large) OCR `content`
            field — callers that only need metadata avoid the response being
            truncated by the client's size cap. `content` is then None.
    """
    if not PAPERLESS_API_URL:
        return {"error": "PAPERLESS_API_URL not configured"}
    if not PAPERLESS_API_TOKEN:
        return {"error": "PAPERLESS_API_TOKEN not configured"}

    async with httpx.AsyncClient(timeout=30.0) as client:
        await _ensure_caches(client)

        resp = await client.get(
            f"{PAPERLESS_API_URL}/api/documents/{document_id}/",
            headers=_headers(),
        )
        if resp.status_code == 404:
            return {"error": f"Document {document_id} not found"}
        resp.raise_for_status()

    doc = resp.json()

    corr_id = doc.get("correspondent")
    dtype_id = doc.get("document_type")
    tag_ids = doc.get("tags") or []

    resolved_tags = []
    if tag_ids and _tag_cache:
        resolved_tags = [_tag_cache[tid] for tid in tag_ids if tid in _tag_cache]

    return {
        "id": doc["id"],
        "title": doc.get("title", ""),
        "content": doc.get("content") if include_content else None,
        "created": doc.get("created"),
        "correspondent": (_correspondent_cache or {}).get(corr_id) if corr_id else None,
        "document_type": (_document_type_cache or {}).get(dtype_id) if dtype_id else None,
        "tags": resolved_tags if resolved_tags else None,
        "original_file_name": doc.get("original_file_name"),
        "storage_path": _storage_path_cache.get(doc.get("storage_path")) if doc.get("storage_path") else None,
        "custom_fields": doc.get("custom_fields"),   # [{field: id, value: ...}]
        "page_count": doc.get("page_count"),
        "added": doc.get("added"),
    }


@mcp.tool()
async def update_document(
    document_id: int,
    title: str | None = None,
    correspondent: str | None = None,
    document_type: str | None = None,
    tags: list[str] | None = None,
    created_date: str | None = None,
    storage_path: str | None = None,
    custom_fields: list[dict] | None = None,
    content: str | None = None,
) -> dict:
    """Update metadata of a document in Paperless-NGX.

    Only the fields you provide will be updated (PATCH semantics).
    Use human-readable names for correspondent, document_type, tags, and
    storage_path — they are resolved to IDs automatically.

    Args:
        document_id: Paperless document ID
        title: New document title
        correspondent: Correspondent name (must exist in Paperless)
        document_type: Document type name (must exist in Paperless)
        tags: List of tag names (must exist in Paperless)
        created_date: Document creation date (YYYY-MM-DD)
        storage_path: Storage path name (must exist in Paperless)
        custom_fields: List of custom field dicts [{field: id, value: ...}]
        content: Replace the document's OCR/text content. Use to write back
            text re-OCR'd by a better engine. Note: this updates the searchable
            content only; the stored archive PDF is not regenerated.
    """
    if not PAPERLESS_API_URL:
        return {"error": "PAPERLESS_API_URL not configured"}
    if not PAPERLESS_API_TOKEN:
        return {"error": "PAPERLESS_API_TOKEN not configured"}

    async with httpx.AsyncClient(timeout=30.0) as client:
        await _ensure_caches(client)

        patch_data: dict = {}

        if title is not None:
            patch_data["title"] = title

        if correspondent is not None:
            corr_id = _resolve_name_to_id(correspondent, _correspondent_cache or {})
            if corr_id is None:
                return {"error": f"Unknown correspondent: '{correspondent}'"}
            patch_data["correspondent"] = corr_id

        if document_type is not None:
            dt_id = _resolve_name_to_id(document_type, _document_type_cache or {})
            if dt_id is None:
                return {"error": f"Unknown document type: '{document_type}'"}
            patch_data["document_type"] = dt_id

        if tags is not None:
            tag_ids = _resolve_tags_to_ids(tags, _tag_cache or {})
            unresolved = [t for t in tags if _resolve_name_to_id(t, _tag_cache or {}) is None]
            if unresolved:
                return {"error": f"Unknown tags: {unresolved}"}
            patch_data["tags"] = tag_ids

        if created_date is not None:
            patch_data["created"] = created_date

        if storage_path is not None:
            sp_id = _resolve_name_to_id(storage_path, _storage_path_cache or {})
            if sp_id is None:
                return {"error": f"Unknown storage path: '{storage_path}'"}
            patch_data["storage_path"] = sp_id

        if custom_fields is not None:
            patch_data["custom_fields"] = custom_fields

        if content is not None:
            patch_data["content"] = content

        if not patch_data:
            return {"error": "No fields to update"}

        resp = await client.patch(
            f"{PAPERLESS_API_URL}/api/documents/{document_id}/",
            headers={**_headers(), "Content-Type": "application/json"},
            json=patch_data,
        )
        if resp.status_code == 404:
            return {"error": f"Document {document_id} not found"}
        resp.raise_for_status()

    doc = resp.json()

    corr_id = doc.get("correspondent")
    dtype_id = doc.get("document_type")
    tag_ids = doc.get("tags") or []

    resolved_tags = []
    if tag_ids and _tag_cache:
        resolved_tags = [_tag_cache[tid] for tid in tag_ids if tid in _tag_cache]

    return {
        "id": doc["id"],
        "title": doc.get("title", ""),
        "correspondent": (_correspondent_cache or {}).get(corr_id) if corr_id else None,
        "document_type": (_document_type_cache or {}).get(dtype_id) if dtype_id else None,
        "tags": resolved_tags if resolved_tags else None,
        "storage_path": _storage_path_cache.get(doc.get("storage_path")) if doc.get("storage_path") else None,
    }


@mcp.tool()
async def delete_document(document_id: int) -> dict:
    """Delete a document from Paperless-NGX by ID.

    On Paperless-ngx 2.x this moves the document to the **trash** (a soft
    delete, recoverable for the instance's trash-retention window), not an
    immediate hard delete — so an over-eager de-duplication can still be
    undone from the Paperless UI. Use only after you have confirmed the
    document is a true duplicate (identical content) of one you are keeping.

    Args:
        document_id: Paperless document ID to delete.

    Returns ``{"deleted": true, "id": document_id}`` on success (HTTP 204),
    ``{"error": ...}`` on a missing id (404) or transport failure.
    """
    if not PAPERLESS_API_URL:
        return {"error": "PAPERLESS_API_URL not configured"}
    if not PAPERLESS_API_TOKEN:
        return {"error": "PAPERLESS_API_TOKEN not configured"}

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.delete(
            f"{PAPERLESS_API_URL}/api/documents/{document_id}/",
            headers=_headers(),
        )
        if resp.status_code == 404:
            return {"error": f"Document {document_id} not found"}
        resp.raise_for_status()

    return {"deleted": True, "id": document_id}


@mcp.tool()
async def reprocess_document(document_id: int) -> dict:
    """Trigger reprocessing of a document in Paperless-NGX.

    This re-runs OCR and content extraction on the document.
    Useful after changing OCR settings or when content was not extracted correctly.

    Args:
        document_id: Paperless document ID
    """
    if not PAPERLESS_API_URL:
        return {"error": "PAPERLESS_API_URL not configured"}
    if not PAPERLESS_API_TOKEN:
        return {"error": "PAPERLESS_API_TOKEN not configured"}

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{PAPERLESS_API_URL}/api/documents/bulk_edit/",
            headers={**_headers(), "Content-Type": "application/json"},
            json={"documents": [document_id], "method": "reprocess"},
        )
        resp.raise_for_status()

    return {"id": document_id, "status": "reprocessing"}


@mcp.tool()
async def list_custom_fields() -> dict:
    """List all custom field definitions from Paperless-NGX.

    Returns field IDs, names, data types (string, url, date, boolean, integer, float, monetary, document_link, select).
    Use this to discover available custom fields before auditing documents.
    """
    api_url = os.environ.get("PAPERLESS_API_URL", "")
    token = os.environ.get("PAPERLESS_API_TOKEN", "")
    if not api_url or not token:
        return {"error": "PAPERLESS_API_URL and PAPERLESS_API_TOKEN must be set"}

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            f"{api_url}/api/custom_fields/",
            headers=_headers(),
        )
        resp.raise_for_status()
        data = resp.json()

    fields = []
    for f in data.get("results", data if isinstance(data, list) else []):
        fields.append({
            "id": f["id"],
            "name": f["name"],
            "data_type": f.get("data_type", "string"),
            "extra_data": f.get("extra_data"),
        })

    return {"fields": fields}


@mcp.tool()
async def list_storage_paths() -> dict:
    """List all storage paths from Paperless-NGX.

    Returns path IDs and path strings. Use to discover available storage paths
    for document organization.
    """
    api_url = os.environ.get("PAPERLESS_API_URL", "")
    token = os.environ.get("PAPERLESS_API_TOKEN", "")
    if not api_url or not token:
        return {"error": "PAPERLESS_API_URL and PAPERLESS_API_TOKEN must be set"}

    async with httpx.AsyncClient(timeout=30.0) as client:
        await _ensure_caches(client)
    paths = [{"id": pid, "path": pname} for pid, pname in (_storage_path_cache or {}).items()]
    return {"paths": paths}


# --- Taxonomy list tools (siblings of list_storage_paths) ---
#
# These three exist so Renfield's LLM-metadata extractor can fetch the
# current Paperless taxonomy in one round-trip per dimension. The
# underlying caches are already populated by ``_ensure_caches``; these
# tools are thin read-only accessors that shape the response as
# ``{"items": [{"id": ..., "name": ...}]}``, matching the convention
# consumer code expects.


@mcp.tool()
async def list_correspondents() -> dict:
    """List all correspondents from Paperless-NGX.

    Returns correspondent IDs and names. Use to discover which
    correspondents already exist before creating a new one via
    ``create_correspondent`` and as the taxonomy input for LLM-driven
    metadata extraction.
    """
    if not PAPERLESS_API_URL or not PAPERLESS_API_TOKEN:
        return {"error": "PAPERLESS_API_URL and PAPERLESS_API_TOKEN must be set"}

    async with httpx.AsyncClient(timeout=30.0) as client:
        await _ensure_caches(client)
    items = [
        {"id": cid, "name": cname}
        for cid, cname in (_correspondent_cache or {}).items()
    ]
    return {"items": items}


@mcp.tool()
async def list_document_types() -> dict:
    """List all document types from Paperless-NGX.

    Returns document-type IDs and names. Use to discover which types
    already exist before creating a new one via ``create_document_type``
    and as the taxonomy input for LLM-driven metadata extraction.
    """
    if not PAPERLESS_API_URL or not PAPERLESS_API_TOKEN:
        return {"error": "PAPERLESS_API_URL and PAPERLESS_API_TOKEN must be set"}

    async with httpx.AsyncClient(timeout=30.0) as client:
        await _ensure_caches(client)
    items = [
        {"id": did, "name": dname}
        for did, dname in (_document_type_cache or {}).items()
    ]
    return {"items": items}


@mcp.tool()
async def list_tags() -> dict:
    """List all tags from Paperless-NGX.

    Returns tag IDs and names. Use to discover which tags already exist
    before creating a new one via ``create_tag`` and as the taxonomy
    input for LLM-driven metadata extraction.
    """
    if not PAPERLESS_API_URL or not PAPERLESS_API_TOKEN:
        return {"error": "PAPERLESS_API_URL and PAPERLESS_API_TOKEN must be set"}

    async with httpx.AsyncClient(timeout=30.0) as client:
        await _ensure_caches(client)
    items = [
        {"id": tid, "name": tname}
        for tid, tname in (_tag_cache or {}).items()
    ]
    return {"items": items}


# --- Taxonomy create tools ---
#
# These four tools exist so Renfield's LLM-metadata extractor can propose
# new correspondents / document_types / tags / storage_paths at confirm
# time, and on user approval create them via a single tool call rather
# than asking the user to open the Paperless admin UI.
#
# Every create_* tool:
#   1. Pre-checks the name against the taxonomy cache — if the name is
#      already present (exact, case-insensitive, or substring match via
#      the same logic as _resolve_name_to_id), returns an ``already_exists``
#      error with the existing id rather than creating a duplicate.
#   2. POSTs to the Paperless API endpoint.
#   3. Invalidates the corresponding in-process cache so the next
#      ``_ensure_caches`` reflects the new entry.


@mcp.tool()
async def create_correspondent(name: str) -> dict:
    """Create a new correspondent in Paperless-NGX.

    Rejects duplicates (exact or near-match against existing cache). On
    success, invalidates the correspondent cache so subsequent reads see
    the new entry.

    Args:
        name: Correspondent name (must not be empty or already exist).
    """
    if not PAPERLESS_API_URL:
        return {"error": "PAPERLESS_API_URL not configured"}
    if not PAPERLESS_API_TOKEN:
        return {"error": "PAPERLESS_API_TOKEN not configured"}
    if not name or not name.strip():
        return {"error": "name must not be empty"}

    async with httpx.AsyncClient(timeout=30.0) as client:
        await _ensure_caches(client)

        existing_id = _resolve_name_to_id(name, _correspondent_cache or {})
        if existing_id is not None:
            return {
                "error": "already_exists",
                "existing_id": existing_id,
                "existing_name": (_correspondent_cache or {}).get(existing_id),
            }

        resp = await client.post(
            f"{PAPERLESS_API_URL}/api/correspondents/",
            headers={**_headers(), "Content-Type": "application/json"},
            json={"name": name.strip()},
        )
        if resp.status_code == 400:
            # Paperless rejects duplicates server-side too (case-sensitive).
            # Surface the server's response rather than retrying.
            return {"error": "bad_request", "details": resp.text}
        resp.raise_for_status()

    created = resp.json()
    _invalidate_cache("correspondent")
    return {"id": created["id"], "name": created["name"]}


@mcp.tool()
async def create_document_type(name: str) -> dict:
    """Create a new document type in Paperless-NGX.

    Rejects duplicates and invalidates the document_type cache on success.

    Args:
        name: Document type name (must not be empty or already exist).
    """
    if not PAPERLESS_API_URL:
        return {"error": "PAPERLESS_API_URL not configured"}
    if not PAPERLESS_API_TOKEN:
        return {"error": "PAPERLESS_API_TOKEN not configured"}
    if not name or not name.strip():
        return {"error": "name must not be empty"}

    async with httpx.AsyncClient(timeout=30.0) as client:
        await _ensure_caches(client)

        existing_id = _resolve_name_to_id(name, _document_type_cache or {})
        if existing_id is not None:
            return {
                "error": "already_exists",
                "existing_id": existing_id,
                "existing_name": (_document_type_cache or {}).get(existing_id),
            }

        resp = await client.post(
            f"{PAPERLESS_API_URL}/api/document_types/",
            headers={**_headers(), "Content-Type": "application/json"},
            json={"name": name.strip()},
        )
        if resp.status_code == 400:
            return {"error": "bad_request", "details": resp.text}
        resp.raise_for_status()

    created = resp.json()
    _invalidate_cache("document_type")
    return {"id": created["id"], "name": created["name"]}


@mcp.tool()
async def create_tag(name: str, color: str | None = None) -> dict:
    """Create a new tag in Paperless-NGX.

    Rejects duplicates and invalidates the tag cache on success.

    Args:
        name: Tag name (must not be empty or already exist).
        color: Optional hex color like ``"#a6cee3"``. Paperless assigns
               a default if omitted.
    """
    if not PAPERLESS_API_URL:
        return {"error": "PAPERLESS_API_URL not configured"}
    if not PAPERLESS_API_TOKEN:
        return {"error": "PAPERLESS_API_TOKEN not configured"}
    if not name or not name.strip():
        return {"error": "name must not be empty"}

    async with httpx.AsyncClient(timeout=30.0) as client:
        await _ensure_caches(client)

        existing_id = _resolve_name_to_id(name, _tag_cache or {})
        if existing_id is not None:
            return {
                "error": "already_exists",
                "existing_id": existing_id,
                "existing_name": (_tag_cache or {}).get(existing_id),
            }

        payload: dict = {"name": name.strip()}
        if color:
            payload["color"] = color

        resp = await client.post(
            f"{PAPERLESS_API_URL}/api/tags/",
            headers={**_headers(), "Content-Type": "application/json"},
            json=payload,
        )
        if resp.status_code == 400:
            return {"error": "bad_request", "details": resp.text}
        resp.raise_for_status()

    created = resp.json()
    _invalidate_cache("tag")
    return {
        "id": created["id"],
        "name": created["name"],
        "color": created.get("color"),
    }


@mcp.tool()
async def create_storage_path(name: str, path: str) -> dict:
    """Create a new storage path in Paperless-NGX.

    Storage paths are templates for where Paperless saves the original
    file on disk. Typical shapes:
        ``{created_year}/{correspondent}/{title}``
        ``/steuer/{created_year}``

    Rejects duplicates (by name match) and invalidates the storage_path
    cache on success.

    Args:
        name: Display name shown in Paperless UI (must not be empty).
        path: Path template. See Paperless docs for available placeholders.
    """
    if not PAPERLESS_API_URL:
        return {"error": "PAPERLESS_API_URL not configured"}
    if not PAPERLESS_API_TOKEN:
        return {"error": "PAPERLESS_API_TOKEN not configured"}
    if not name or not name.strip():
        return {"error": "name must not be empty"}
    if not path or not path.strip():
        return {"error": "path must not be empty"}

    async with httpx.AsyncClient(timeout=30.0) as client:
        await _ensure_caches(client)

        # Storage-path cache values are the path strings themselves (see
        # _ensure_caches). We resolve against both the name and the path
        # to avoid creating a second entry that duplicates a path.
        existing_id = _resolve_name_to_id(name, _storage_path_cache or {})
        if existing_id is None:
            existing_id = _resolve_name_to_id(path, _storage_path_cache or {})
        if existing_id is not None:
            return {
                "error": "already_exists",
                "existing_id": existing_id,
                "existing_path": (_storage_path_cache or {}).get(existing_id),
            }

        resp = await client.post(
            f"{PAPERLESS_API_URL}/api/storage_paths/",
            headers={**_headers(), "Content-Type": "application/json"},
            json={"name": name.strip(), "path": path.strip()},
        )
        if resp.status_code == 400:
            return {"error": "bad_request", "details": resp.text}
        resp.raise_for_status()

    created = resp.json()
    _invalidate_cache("storage_path")
    return {
        "id": created["id"],
        "name": created["name"],
        "path": created.get("path"),
    }


def main():
    """Entry point for console script and python -m."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
