"""
Tests for renfield-mcp-paperless MCP server.
"""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from renfield_mcp_paperless import server as paperless


@pytest.fixture(autouse=True)
def _reset_caches():
    """Reset module-level caches between tests."""
    paperless._correspondent_cache = None
    paperless._document_type_cache = None
    paperless._storage_path_cache = None
    paperless._tag_cache = None
    yield
    paperless._correspondent_cache = None
    paperless._document_type_cache = None
    paperless._storage_path_cache = None
    paperless._tag_cache = None


@pytest.fixture(autouse=True)
def _set_env(monkeypatch):
    """Ensure env vars are set for all tests."""
    monkeypatch.setenv("PAPERLESS_API_URL", "http://your-paperless-url")
    monkeypatch.setenv("PAPERLESS_API_TOKEN", "test-token-abc")
    # Reload module-level constants
    monkeypatch.setattr(paperless, "PAPERLESS_API_URL", "http://your-paperless-url")
    monkeypatch.setattr(paperless, "PAPERLESS_API_TOKEN", "test-token-abc")


# ── _resolve_name_to_id ─────────────────────────────────────────


class TestResolveNameToId:
    def test_exact_match(self):
        cache = {1: "Rechnung", 2: "Vertrag"}
        assert paperless._resolve_name_to_id("Rechnung", cache) == 1

    def test_case_insensitive(self):
        cache = {1: "Rechnung", 2: "Vertrag"}
        assert paperless._resolve_name_to_id("rechnung", cache) == 1

    def test_substring_input_in_cache(self):
        """'Telekom' matches 'Telekom Deutschland GmbH'."""
        cache = {1: "Telekom Deutschland GmbH"}
        assert paperless._resolve_name_to_id("Telekom", cache) == 1

    def test_substring_cache_in_input(self):
        """'Telekom Deutschland GmbH' matches cache entry 'Telekom'."""
        cache = {1: "Telekom"}
        assert paperless._resolve_name_to_id("Telekom Deutschland GmbH", cache) == 1

    def test_no_match(self):
        cache = {1: "Rechnung", 2: "Vertrag"}
        assert paperless._resolve_name_to_id("Quittung", cache) is None

    def test_exact_match_priority_over_case_insensitive(self):
        """Exact match takes priority when both would match."""
        cache = {1: "rechnung", 2: "Rechnung"}
        # "Rechnung" should match id=2 (exact), not id=1 (case-insensitive)
        assert paperless._resolve_name_to_id("Rechnung", cache) == 2

    def test_empty_cache(self):
        assert paperless._resolve_name_to_id("Test", {}) is None


# ── _resolve_tags_to_ids ────────────────────────────────────────


class TestResolveTagsToIds:
    def test_multiple_tags(self):
        cache = {1: "privat", 2: "Rechnung", 3: "steuer"}
        result = paperless._resolve_tags_to_ids(["privat", "Rechnung"], cache)
        assert result == [1, 2]

    def test_skips_unresolved(self):
        cache = {1: "privat", 2: "Rechnung"}
        result = paperless._resolve_tags_to_ids(["privat", "nonexistent"], cache)
        assert result == [1]

    def test_empty_list(self):
        cache = {1: "privat"}
        result = paperless._resolve_tags_to_ids([], cache)
        assert result == []

    def test_all_unresolved(self):
        cache = {1: "privat"}
        result = paperless._resolve_tags_to_ids(["nope", "nada"], cache)
        assert result == []


# ── _extract_snippet ────────────────────────────────────────────


class TestExtractSnippet:
    def test_phrase_match(self):
        content = "X" * 100 + "Am Stirkenbend 20, 40489 Düsseldorf" + "Y" * 100
        snippet = paperless._extract_snippet(content, "Am Stirkenbend 20", max_length=80)
        assert "Am Stirkenbend" in snippet

    def test_word_fallback(self):
        content = "Herr Müller hat die Rechnung über 49,99 EUR bezahlt."
        snippet = paperless._extract_snippet(content, "Rechnung bezahlt", max_length=200)
        assert "Rechnung" in snippet

    def test_no_match_returns_beginning(self):
        content = "Dies ist ein langer Text über verschiedene Themen." * 5
        snippet = paperless._extract_snippet(content, "Xylophon", max_length=50)
        assert snippet.startswith("Dies ist")

    def test_no_content(self):
        assert paperless._extract_snippet(None, "test") is None
        assert paperless._extract_snippet("", "test") is None
        assert paperless._extract_snippet("   ", "test") is None

    def test_no_query_returns_beginning(self):
        content = "First sentence. Second sentence. Third sentence."
        snippet = paperless._extract_snippet(content, None, max_length=200)
        assert snippet == content

    def test_short_content_returned_fully(self):
        content = "Short text."
        snippet = paperless._extract_snippet(content, None, max_length=200)
        assert snippet == "Short text."

    def test_word_boundary_not_broken(self):
        """Snippet should not cut mid-word at the end."""
        content = "Word1 Word2 Word3 Word4 Word5 Word6 Word7 Word8 Word9 Word10"
        snippet = paperless._extract_snippet(content, None, max_length=30)
        # Should not end with a partial word
        assert not snippet.rstrip("...").endswith("Wor")

    def test_short_words_ignored(self):
        """Words shorter than 2 chars should be skipped."""
        content = "This is a long text with various content pieces here."
        snippet = paperless._extract_snippet(content, "a", max_length=200)
        # "a" is too short (< 2 chars), so no word match — returns beginning
        assert snippet == content


# ── _build_summary ──────────────────────────────────────────────


class TestBuildSummary:
    def test_basic_summary(self):
        results = [
            {"correspondent": "Telekom", "document_type": "Rechnung"},
            {"correspondent": "Telekom", "document_type": "Rechnung"},
            {"correspondent": "Vodafone", "document_type": "Rechnung"},
        ]
        summary = paperless._build_summary(results, 3, 100, {})
        assert summary["total_matching"] == 3
        assert summary["returned"] == 3
        assert "note" not in summary  # all results returned

    def test_capped_results_show_note(self):
        results = [{"correspondent": "A", "document_type": "B"}] * 100
        summary = paperless._build_summary(results, 250, 100, {})
        assert summary["note"] == "Showing first 100 of 250 matches."

    def test_filters_included(self):
        filters = {"document_type": "Rechnung", "created_after": "2022-01-01"}
        summary = paperless._build_summary([], 0, 100, filters)
        assert summary["filters"] == filters

    def test_no_filters_no_key(self):
        summary = paperless._build_summary([], 0, 100, {})
        assert "filters" not in summary

    def test_top_5_limit(self):
        results = [
            {"correspondent": f"Company{i}", "document_type": "Rechnung"}
            for i in range(10)
        ]
        summary = paperless._build_summary(results, 10, 100, {})
        assert len(summary["top_correspondents"]) == 5

    def test_top_correspondents_sorted_by_count(self):
        results = (
            [{"correspondent": "Telekom", "document_type": None}] * 5
            + [{"correspondent": "Vodafone", "document_type": None}] * 3
            + [{"correspondent": "O2", "document_type": None}] * 1
        )
        summary = paperless._build_summary(results, 9, 100, {})
        names = [c["name"] for c in summary["top_correspondents"]]
        assert names[0] == "Telekom"
        assert names[1] == "Vodafone"

    def test_empty_results(self):
        summary = paperless._build_summary([], 0, 100, {})
        assert summary["total_matching"] == 0
        assert summary["returned"] == 0
        assert "top_correspondents" not in summary
        assert "top_document_types" not in summary


# ── _resolve_document ────────────────────────────────────────────


class TestResolveDocument:
    def test_resolves_all_ids(self):
        paperless._correspondent_cache = {1: "COMPANY 1"}
        paperless._document_type_cache = {2: "Rechnung"}
        paperless._tag_cache = {10: "privat", 11: "steuer"}

        result = paperless._resolve_document({
            "id": 1,
            "title": "COMPANY 1 Rechnung 2030-01",
            "created": "2030-01-15",
            "correspondent": 1,
            "document_type": 2,
            "tags": [10, 11],
            "content": "Rechnungsbetrag: 49,99 EUR",
        }, query="Rechnung")

        assert result["id"] == 1
        assert result["correspondent"] == "COMPANY 1"
        assert result["document_type"] == "Rechnung"
        assert result["tags"] == ["privat", "steuer"]
        assert result["snippet"] is not None
        assert result["storage_path"] is None

    def test_null_ids_return_none(self):
        paperless._correspondent_cache = {}
        paperless._document_type_cache = {}
        paperless._tag_cache = {}

        result = paperless._resolve_document({
            "id": 1, "title": "Test", "created": None,
            "correspondent": None, "document_type": None, "tags": [],
            "content": None,
        })

        assert result["correspondent"] is None
        assert result["document_type"] is None
        assert result["tags"] is None
        assert result["snippet"] is None
        assert result["created"] is None

    def test_unknown_id_returns_none(self):
        paperless._correspondent_cache = {1: "Known"}
        paperless._document_type_cache = {}
        paperless._tag_cache = {}

        result = paperless._resolve_document({
            "id": 1, "title": "Test", "created": "2030-06-01",
            "correspondent": 999, "document_type": None, "tags": [],
            "content": None,
        })

        assert result["correspondent"] is None

    def test_tags_resolved_from_cache(self):
        paperless._correspondent_cache = {}
        paperless._document_type_cache = {}
        paperless._tag_cache = {5: "inbox", 8: "important"}

        result = paperless._resolve_document({
            "id": 1, "title": "Test", "created": None,
            "correspondent": None, "document_type": None,
            "tags": [5, 8, 999],  # 999 not in cache
            "content": None,
        })

        assert result["tags"] == ["inbox", "important"]

    def test_snippet_from_content(self):
        paperless._correspondent_cache = {}
        paperless._document_type_cache = {}
        paperless._tag_cache = {}

        result = paperless._resolve_document({
            "id": 1, "title": "Test", "created": None,
            "correspondent": None, "document_type": None,
            "tags": [],
            "content": "This document contains important information about invoices.",
        }, query="invoices")

        assert result["snippet"] is not None
        assert "invoices" in result["snippet"]


# ── _ensure_caches ───────────────────────────────────────────────


class TestEnsureCaches:
    @pytest.mark.asyncio
    async def test_fetches_all_four_caches(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"results": [{"id": 1, "name": "Test"}], "next": None}
        mock_resp.raise_for_status = MagicMock()

        client = AsyncMock()
        client.get = AsyncMock(return_value=mock_resp)

        await paperless._ensure_caches(client)

        assert client.get.call_count == 4
        assert paperless._correspondent_cache == {1: "Test"}
        assert paperless._document_type_cache == {1: "Test"}
        assert paperless._tag_cache == {1: "Test"}

    @pytest.mark.asyncio
    async def test_skips_when_already_populated(self):
        paperless._correspondent_cache = {1: "Cached"}
        paperless._document_type_cache = {2: "Cached"}
        paperless._storage_path_cache = {3: "Cached"}
        paperless._tag_cache = {4: "Cached"}

        client = AsyncMock()
        await paperless._ensure_caches(client)

        client.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_dict_not_refetched(self):
        """Empty dict (no correspondents) should not trigger re-fetch."""
        paperless._correspondent_cache = {}
        paperless._document_type_cache = {}
        paperless._storage_path_cache = {}
        paperless._tag_cache = {}

        client = AsyncMock()
        await paperless._ensure_caches(client)

        client.get.assert_not_called()


# ── search_documents ─────────────────────────────────────────────


def _make_mock_client(search_response):
    """Create a mock httpx.AsyncClient that returns search_response on GET."""
    cache_resp = MagicMock()
    cache_resp.json.return_value = {"results": [], "next": None}
    cache_resp.raise_for_status = MagicMock()

    search_resp = MagicMock()
    search_resp.json.return_value = search_response
    search_resp.raise_for_status = MagicMock()

    instance = AsyncMock()

    async def _side_effect_get(url, **kwargs):
        if "/api/documents/" in str(url):
            return search_resp
        return cache_resp

    instance.get = AsyncMock(side_effect=_side_effect_get)
    instance.__aenter__ = AsyncMock(return_value=instance)
    instance.__aexit__ = AsyncMock(return_value=False)

    return instance


class TestSearchDocuments:
    @pytest.mark.asyncio
    async def test_missing_url_returns_error(self):
        paperless.PAPERLESS_API_URL = ""
        result = await paperless.search_documents("test")
        assert "error" in result

    @pytest.mark.asyncio
    async def test_missing_token_returns_error(self):
        paperless.PAPERLESS_API_URL = "http://test"
        paperless.PAPERLESS_API_TOKEN = ""
        result = await paperless.search_documents("test")
        assert "error" in result

    @pytest.mark.asyncio
    async def test_successful_search(self):
        paperless._correspondent_cache = {1: "COMPANY 1"}
        paperless._document_type_cache = {1: "Rechnung"}
        paperless._storage_path_cache = {1: "rechnungen"}
        paperless._tag_cache = {10: "privat"}

        mock_instance = _make_mock_client({
            "count": 2,
            "next": None,
            "results": [
                {"id": 10, "title": "COMPANY 1 RE 2030-01", "created": "2030-01-15",
                 "correspondent": 1, "document_type": 1, "tags": [10],
                 "content": "Invoice content here"},
                {"id": 11, "title": "COMPANY 1 RE 2030-02", "created": "2030-02-15",
                 "correspondent": 1, "document_type": 1, "tags": [],
                 "content": None},
            ],
        })

        with patch("httpx.AsyncClient") as MockClient:
            MockClient.return_value = mock_instance
            result = await paperless.search_documents("COMPANY 1 Rechnung")

        assert "summary" in result
        assert result["summary"]["total_matching"] == 2
        assert result["summary"]["returned"] == 2
        assert len(result["results"]) == 2
        assert result["results"][0]["correspondent"] == "COMPANY 1"
        assert result["results"][0]["document_type"] == "Rechnung"
        assert result["results"][0]["tags"] == ["privat"]
        assert result["results"][0]["snippet"] is not None

    @pytest.mark.asyncio
    async def test_query_optional_for_filter_only(self):
        """query=None should work when filters are provided."""
        paperless._correspondent_cache = {1: "COMPANY 1"}
        paperless._document_type_cache = {1: "Rechnung"}
        paperless._storage_path_cache = {}
        paperless._tag_cache = {}

        mock_instance = _make_mock_client({
            "count": 1,
            "next": None,
            "results": [
                {"id": 10, "title": "Test", "created": "2030-01-15",
                 "correspondent": 1, "document_type": 1, "tags": [],
                 "content": "Some content"},
            ],
        })

        with patch("httpx.AsyncClient") as MockClient:
            MockClient.return_value = mock_instance
            result = await paperless.search_documents(
                query=None, document_type="Rechnung"
            )

        assert "error" not in result
        assert result["summary"]["total_matching"] == 1

    @pytest.mark.asyncio
    async def test_document_type_filter(self):
        """document_type filter resolves name to ID and sends to API."""
        paperless._correspondent_cache = {}
        paperless._document_type_cache = {5: "Rechnung"}
        paperless._storage_path_cache = {}
        paperless._tag_cache = {}

        mock_instance = _make_mock_client({"count": 0, "next": None, "results": []})

        with patch("httpx.AsyncClient") as MockClient:
            MockClient.return_value = mock_instance
            result = await paperless.search_documents(
                query="test", document_type="Rechnung"
            )

        # Check the API call params
        for call in mock_instance.get.call_args_list:
            url = str(call.args[0]) if call.args else str(call.kwargs.get("url", ""))
            params = call.kwargs.get("params", {})
            if "/api/documents/" in url and params:
                assert params["document_type__id"] == 5
                break

        assert result["summary"]["filters"]["document_type"] == "Rechnung"

    @pytest.mark.asyncio
    async def test_correspondent_filter(self):
        paperless._correspondent_cache = {3: "Telekom"}
        paperless._document_type_cache = {}
        paperless._storage_path_cache = {}
        paperless._tag_cache = {}

        mock_instance = _make_mock_client({"count": 0, "next": None, "results": []})

        with patch("httpx.AsyncClient") as MockClient:
            MockClient.return_value = mock_instance
            result = await paperless.search_documents(
                query="test", correspondent="Telekom"
            )

        for call in mock_instance.get.call_args_list:
            params = call.kwargs.get("params", {})
            if "correspondent__id" in params:
                assert params["correspondent__id"] == 3
                break

        assert result["summary"]["filters"]["correspondent"] == "Telekom"

    @pytest.mark.asyncio
    async def test_tags_filter(self):
        paperless._correspondent_cache = {}
        paperless._document_type_cache = {}
        paperless._storage_path_cache = {}
        paperless._tag_cache = {1: "privat", 2: "steuer"}

        mock_instance = _make_mock_client({"count": 0, "next": None, "results": []})

        with patch("httpx.AsyncClient") as MockClient:
            MockClient.return_value = mock_instance
            result = await paperless.search_documents(
                query="test", tags=["privat", "steuer"]
            )

        for call in mock_instance.get.call_args_list:
            params = call.kwargs.get("params", {})
            if "tags__id__in" in params:
                assert "1" in params["tags__id__in"]
                assert "2" in params["tags__id__in"]
                break

        assert result["summary"]["filters"]["tags"] == ["privat", "steuer"]

    @pytest.mark.asyncio
    async def test_unknown_document_type_returns_error(self):
        paperless._correspondent_cache = {}
        paperless._document_type_cache = {1: "Rechnung"}
        paperless._storage_path_cache = {}
        paperless._tag_cache = {}

        mock_instance = _make_mock_client({"count": 0, "next": None, "results": []})

        with patch("httpx.AsyncClient") as MockClient:
            MockClient.return_value = mock_instance
            result = await paperless.search_documents(
                query="test", document_type="NonExistent"
            )

        assert "error" in result
        assert "NonExistent" in result["error"]

    @pytest.mark.asyncio
    async def test_unknown_correspondent_returns_error(self):
        paperless._correspondent_cache = {1: "Telekom"}
        paperless._document_type_cache = {}
        paperless._storage_path_cache = {}
        paperless._tag_cache = {}

        mock_instance = _make_mock_client({"count": 0, "next": None, "results": []})

        with patch("httpx.AsyncClient") as MockClient:
            MockClient.return_value = mock_instance
            result = await paperless.search_documents(
                query="test", correspondent="Sparkasse"
            )

        assert "error" in result
        assert "Sparkasse" in result["error"]

    @pytest.mark.asyncio
    async def test_max_results_capped(self):
        """max_results > 500 is capped to 500."""
        paperless._correspondent_cache = {}
        paperless._document_type_cache = {}
        paperless._storage_path_cache = {}
        paperless._tag_cache = {}

        mock_instance = _make_mock_client({"count": 0, "next": None, "results": []})

        with patch("httpx.AsyncClient") as MockClient:
            MockClient.return_value = mock_instance
            result = await paperless.search_documents("test", max_results=1000)

        # Should not error — just cap internally
        assert "error" not in result

    @pytest.mark.asyncio
    async def test_content_not_in_results(self):
        """Full content should not be in the response — only snippets."""
        paperless._correspondent_cache = {}
        paperless._document_type_cache = {}
        paperless._storage_path_cache = {}
        paperless._tag_cache = {}

        content = "This is the full document content that should not appear in results."
        mock_instance = _make_mock_client({
            "count": 1,
            "next": None,
            "results": [
                {"id": 1, "title": "Test", "created": "2030-01-01",
                 "correspondent": None, "document_type": None, "tags": [],
                 "content": content},
            ],
        })

        with patch("httpx.AsyncClient") as MockClient:
            MockClient.return_value = mock_instance
            result = await paperless.search_documents("test")

        assert "content" not in result["results"][0]
        assert "snippet" in result["results"][0]


class TestSearchDocumentsOrdering:
    @pytest.mark.asyncio
    async def test_default_ordering_is_newest_first(self):
        """Default ordering sends -created to the API."""
        paperless._correspondent_cache = {}
        paperless._document_type_cache = {}
        paperless._storage_path_cache = {}
        paperless._tag_cache = {}

        mock_instance = _make_mock_client({"count": 0, "next": None, "results": []})

        with patch("httpx.AsyncClient") as MockClient:
            MockClient.return_value = mock_instance
            await paperless.search_documents("test")

        # Find the documents API call
        for call in mock_instance.get.call_args_list:
            params = call.kwargs.get("params", {})
            if "ordering" in params:
                assert params["ordering"] == "-created"
                break

    @pytest.mark.asyncio
    async def test_custom_ordering_forwarded(self):
        """Custom ordering parameter is forwarded to API."""
        paperless._correspondent_cache = {}
        paperless._document_type_cache = {}
        paperless._storage_path_cache = {}
        paperless._tag_cache = {}

        mock_instance = _make_mock_client({"count": 0, "next": None, "results": []})

        with patch("httpx.AsyncClient") as MockClient:
            MockClient.return_value = mock_instance
            await paperless.search_documents("test", ordering="title")

        for call in mock_instance.get.call_args_list:
            params = call.kwargs.get("params", {})
            if "ordering" in params:
                assert params["ordering"] == "title"
                break

    @pytest.mark.asyncio
    async def test_created_after_forwarded(self):
        """created_after sends created__date__gte to API."""
        paperless._correspondent_cache = {}
        paperless._document_type_cache = {}
        paperless._storage_path_cache = {}
        paperless._tag_cache = {}

        mock_instance = _make_mock_client({"count": 0, "next": None, "results": []})

        with patch("httpx.AsyncClient") as MockClient:
            MockClient.return_value = mock_instance
            await paperless.search_documents("Rechnung", created_after="2022-01-01")

        for call in mock_instance.get.call_args_list:
            params = call.kwargs.get("params", {})
            if "created__date__gte" in params:
                assert params["created__date__gte"] == "2022-01-01"
                break

    @pytest.mark.asyncio
    async def test_created_before_forwarded(self):
        """created_before sends created__date__lte to API."""
        paperless._correspondent_cache = {}
        paperless._document_type_cache = {}
        paperless._storage_path_cache = {}
        paperless._tag_cache = {}

        mock_instance = _make_mock_client({"count": 0, "next": None, "results": []})

        with patch("httpx.AsyncClient") as MockClient:
            MockClient.return_value = mock_instance
            await paperless.search_documents("Rechnung", created_before="2022-12-31")

        for call in mock_instance.get.call_args_list:
            params = call.kwargs.get("params", {})
            if "created__date__lte" in params:
                assert params["created__date__lte"] == "2022-12-31"
                break

    @pytest.mark.asyncio
    async def test_both_date_filters_forwarded(self):
        """Both date filters are sent to API together."""
        paperless._correspondent_cache = {}
        paperless._document_type_cache = {}
        paperless._storage_path_cache = {}
        paperless._tag_cache = {}

        mock_instance = _make_mock_client({"count": 0, "next": None, "results": []})

        with patch("httpx.AsyncClient") as MockClient:
            MockClient.return_value = mock_instance
            await paperless.search_documents(
                "Rechnung",
                created_after="2022-01-01",
                created_before="2022-12-31",
            )

        for call in mock_instance.get.call_args_list:
            params = call.kwargs.get("params", {})
            if "created__date__gte" in params:
                assert params["created__date__gte"] == "2022-01-01"
                assert params["created__date__lte"] == "2022-12-31"
                break

    @pytest.mark.asyncio
    async def test_created_field_in_results(self):
        """Results include the created date field."""
        paperless._correspondent_cache = {}
        paperless._document_type_cache = {}
        paperless._storage_path_cache = {}
        paperless._tag_cache = {}

        mock_instance = _make_mock_client({
            "count": 1,
            "next": None,
            "results": [
                {"id": 1, "title": "Invoice", "created": "2030-06-15",
                 "correspondent": None, "document_type": None, "tags": [],
                 "content": None},
            ],
        })

        with patch("httpx.AsyncClient") as MockClient:
            MockClient.return_value = mock_instance
            result = await paperless.search_documents("Invoice")

        assert result["results"][0]["created"] == "2030-06-15"


# ── Auto-Pagination ──────────────────────────────────────────────


class TestFetchDocuments:
    @pytest.mark.asyncio
    async def test_single_page(self):
        """Single page of results — no pagination needed."""
        resp = MagicMock()
        resp.json.return_value = {
            "count": 2,
            "next": None,
            "results": [{"id": 1}, {"id": 2}],
        }
        resp.raise_for_status = MagicMock()

        client = AsyncMock()
        client.get = AsyncMock(return_value=resp)

        results, total = await paperless._fetch_documents(client, {"query": "test"}, 100)
        assert len(results) == 2
        assert total == 2

    @pytest.mark.asyncio
    async def test_multi_page(self):
        """Multiple pages — follows next URLs."""
        page1_resp = MagicMock()
        page1_resp.json.return_value = {
            "count": 150,
            "next": "http://paperless/api/documents/?page=2",
            "results": [{"id": i} for i in range(100)],
        }
        page1_resp.raise_for_status = MagicMock()

        page2_resp = MagicMock()
        page2_resp.json.return_value = {
            "count": 150,
            "next": None,
            "results": [{"id": i} for i in range(100, 150)],
        }
        page2_resp.raise_for_status = MagicMock()

        client = AsyncMock()
        call_count = 0

        async def _get_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return page1_resp
            return page2_resp

        client.get = AsyncMock(side_effect=_get_side_effect)

        results, total = await paperless._fetch_documents(client, {"query": "test"}, 200)
        assert len(results) == 150
        assert total == 150

    @pytest.mark.asyncio
    async def test_max_results_limits_pagination(self):
        """Stops fetching when max_results is reached."""
        page1_resp = MagicMock()
        page1_resp.json.return_value = {
            "count": 300,
            "next": "http://paperless/api/documents/?page=2",
            "results": [{"id": i} for i in range(100)],
        }
        page1_resp.raise_for_status = MagicMock()

        page2_resp = MagicMock()
        page2_resp.json.return_value = {
            "count": 300,
            "next": "http://paperless/api/documents/?page=3",
            "results": [{"id": i} for i in range(100, 200)],
        }
        page2_resp.raise_for_status = MagicMock()

        client = AsyncMock()
        call_count = 0

        async def _get_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return page1_resp
            return page2_resp

        client.get = AsyncMock(side_effect=_get_side_effect)

        # max_results=50 — should stop after first page and truncate
        results, total = await paperless._fetch_documents(client, {"query": "test"}, 50)
        assert len(results) == 50
        assert total == 300


# ── download_document ───────────────────────────────────────────


class TestDownloadDocument:
    @pytest.mark.asyncio
    async def test_missing_url_returns_error(self):
        paperless.PAPERLESS_API_URL = ""
        result = await paperless.download_document(1)
        assert "error" in result

    @pytest.mark.asyncio
    async def test_missing_token_returns_error(self):
        paperless.PAPERLESS_API_URL = "http://test"
        paperless.PAPERLESS_API_TOKEN = ""
        result = await paperless.download_document(1)
        assert "error" in result

    @pytest.mark.asyncio
    async def test_successful_download(self):
        pdf_bytes = b"%PDF-1.4 fake content"

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = pdf_bytes
        mock_resp.headers = {
            "content-disposition": 'attachment; filename="invoice_2030.pdf"',
            "content-type": "application/pdf",
        }
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.get = AsyncMock(return_value=mock_resp)
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = instance

            result = await paperless.download_document(42)

        assert result["id"] == 42
        assert result["filename"] == "invoice_2030.pdf"
        assert result["mime_type"] == "application/pdf"
        import base64
        assert base64.b64decode(result["content_base64"]) == pdf_bytes

    @pytest.mark.asyncio
    async def test_document_not_found(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 404

        with patch("httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.get = AsyncMock(return_value=mock_resp)
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = instance

            result = await paperless.download_document(9999)

        assert "error" in result
        assert "9999" in result["error"]

    @pytest.mark.asyncio
    async def test_fallback_filename_without_content_disposition(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b"data"
        mock_resp.headers = {"content-type": "application/pdf"}
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.get = AsyncMock(return_value=mock_resp)
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = instance

            result = await paperless.download_document(7)

        assert result["filename"] == "document_7.pdf"

    @pytest.mark.asyncio
    async def test_content_disposition_without_quotes(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b"data"
        mock_resp.headers = {
            "content-disposition": "attachment; filename=report.pdf",
            "content-type": "application/pdf",
        }
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.get = AsyncMock(return_value=mock_resp)
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = instance

            result = await paperless.download_document(5)

        assert result["filename"] == "report.pdf"


# ── upload_document ─────────────────────────────────────────────


class TestUploadDocument:
    @pytest.mark.asyncio
    async def test_upload_success(self):
        import base64
        file_bytes = b"%PDF-1.4 test content"
        b64 = base64.b64encode(file_bytes).decode("ascii")

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = '"task-uuid-123"'
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.post = AsyncMock(return_value=mock_resp)
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = instance

            result = await paperless.upload_document(
                title="Test Invoice",
                file_content_base64=b64,
                filename="invoice.pdf",
            )

        assert result["task_id"] == "task-uuid-123"
        assert result["title"] == "Test Invoice"
        assert result["filename"] == "invoice.pdf"

    @pytest.mark.asyncio
    async def test_upload_missing_url(self):
        paperless.PAPERLESS_API_URL = ""
        result = await paperless.upload_document(
            title="Test", file_content_base64="dGVzdA=="
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_upload_missing_token(self):
        paperless.PAPERLESS_API_URL = "http://test"
        paperless.PAPERLESS_API_TOKEN = ""
        result = await paperless.upload_document(
            title="Test", file_content_base64="dGVzdA=="
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_upload_invalid_base64(self):
        result = await paperless.upload_document(
            title="Test", file_content_base64="not-valid-base64!!!"
        )
        assert "error" in result


# ── Response Size ────────────────────────────────────────────────


class TestResponseSize:
    def test_10_results_with_snippets_under_10kb(self):
        """10 results with snippets and summary must fit under 10KB."""
        results = []
        for i in range(10):
            results.append({
                "id": 1700 + i,
                "title": f"2030_{i:02d}_02 RE COMPANY 1 IN_10015{i}23473",
                "created": "2030-01-15",
                "correspondent": "COMPANY 1",
                "document_type": "Rechnung",
                "tags": ["privat", "steuer"],
                "snippet": f"...Rechnungsbetrag: {49.99 + i} EUR, Am Stirkenbend 20, 40489 Düsseldorf...",
            })
        response = {
            "summary": {
                "total_matching": 143,
                "returned": 10,
                "filters": {"document_type": "Rechnung"},
                "note": "Showing first 10 of 143 matches.",
                "top_correspondents": [
                    {"name": "Telekom", "count": 15},
                    {"name": "Vodafone", "count": 10},
                ],
                "top_document_types": [
                    {"name": "Rechnung", "count": 100},
                ],
            },
            "results": results,
        }
        size = len(json.dumps(response).encode("utf-8"))
        assert size < 10240, f"Response is {size} bytes, exceeds 10KB"


# ── get_document ────────────────────────────────────────────────


class TestGetDocument:
    @pytest.mark.asyncio
    async def test_missing_url_returns_error(self):
        paperless.PAPERLESS_API_URL = ""
        result = await paperless.get_document(1)
        assert "error" in result

    @pytest.mark.asyncio
    async def test_missing_token_returns_error(self):
        paperless.PAPERLESS_API_URL = "http://test"
        paperless.PAPERLESS_API_TOKEN = ""
        result = await paperless.get_document(1)
        assert "error" in result

    @pytest.mark.asyncio
    async def test_successful_get(self):
        paperless._correspondent_cache = {1: "Telekom"}
        paperless._document_type_cache = {2: "Rechnung"}
        paperless._storage_path_cache = {}
        paperless._tag_cache = {10: "privat", 11: "steuer"}

        doc_resp = MagicMock()
        doc_resp.status_code = 200
        doc_resp.json.return_value = {
            "id": 42,
            "title": "Telekom Rechnung Januar",
            "content": "Rechnungsbetrag: 49,99 EUR",
            "created": "2030-01-15",
            "correspondent": 1,
            "document_type": 2,
            "tags": [10, 11],
            "original_file_name": "telekom_jan.pdf",
        }
        doc_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.get = AsyncMock(return_value=doc_resp)
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = instance

            result = await paperless.get_document(42)

        assert result["id"] == 42
        assert result["title"] == "Telekom Rechnung Januar"
        assert result["content"] == "Rechnungsbetrag: 49,99 EUR"
        assert result["created"] == "2030-01-15"
        assert result["correspondent"] == "Telekom"
        assert result["document_type"] == "Rechnung"
        assert result["tags"] == ["privat", "steuer"]
        assert result["original_file_name"] == "telekom_jan.pdf"

    @pytest.mark.asyncio
    async def test_document_not_found(self):
        paperless._correspondent_cache = {}
        paperless._document_type_cache = {}
        paperless._storage_path_cache = {}
        paperless._tag_cache = {}

        doc_resp = MagicMock()
        doc_resp.status_code = 404

        with patch("httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.get = AsyncMock(return_value=doc_resp)
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = instance

            result = await paperless.get_document(9999)

        assert "error" in result
        assert "9999" in result["error"]

    @pytest.mark.asyncio
    async def test_null_fields(self):
        paperless._correspondent_cache = {}
        paperless._document_type_cache = {}
        paperless._storage_path_cache = {}
        paperless._tag_cache = {}

        doc_resp = MagicMock()
        doc_resp.status_code = 200
        doc_resp.json.return_value = {
            "id": 1,
            "title": "Untitled",
            "content": None,
            "created": None,
            "correspondent": None,
            "document_type": None,
            "tags": [],
            "original_file_name": None,
        }
        doc_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.get = AsyncMock(return_value=doc_resp)
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = instance

            result = await paperless.get_document(1)

        assert result["correspondent"] is None
        assert result["document_type"] is None
        assert result["tags"] is None
        assert result["content"] is None


# ── update_document ─────────────────────────────────────────────


class TestUpdateDocument:
    @pytest.mark.asyncio
    async def test_missing_url_returns_error(self):
        paperless.PAPERLESS_API_URL = ""
        result = await paperless.update_document(1, title="New Title")
        assert "error" in result

    @pytest.mark.asyncio
    async def test_missing_token_returns_error(self):
        paperless.PAPERLESS_API_URL = "http://test"
        paperless.PAPERLESS_API_TOKEN = ""
        result = await paperless.update_document(1, title="New Title")
        assert "error" in result

    @pytest.mark.asyncio
    async def test_no_fields_returns_error(self):
        """Calling with no fields to update should return an error."""
        paperless._correspondent_cache = {}
        paperless._document_type_cache = {}
        paperless._storage_path_cache = {}
        paperless._tag_cache = {}

        with patch("httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = instance

            result = await paperless.update_document(1)

        assert "error" in result
        assert "No fields" in result["error"]

    @pytest.mark.asyncio
    async def test_update_title_only(self):
        """PATCH semantics: only title is sent."""
        paperless._correspondent_cache = {}
        paperless._document_type_cache = {}
        paperless._storage_path_cache = {}
        paperless._tag_cache = {}

        patch_resp = MagicMock()
        patch_resp.status_code = 200
        patch_resp.json.return_value = {
            "id": 42,
            "title": "New Title",
            "correspondent": None,
            "document_type": None,
            "tags": [],
        }
        patch_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.get = AsyncMock()  # for _ensure_caches (won't be called with pre-set caches)
            instance.patch = AsyncMock(return_value=patch_resp)
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = instance

            result = await paperless.update_document(42, title="New Title")

        assert result["id"] == 42
        assert result["title"] == "New Title"

        # Verify only title was sent in PATCH body
        call_kwargs = instance.patch.call_args
        assert call_kwargs.kwargs["json"] == {"title": "New Title"}

    @pytest.mark.asyncio
    async def test_update_correspondent(self):
        paperless._correspondent_cache = {5: "Telekom"}
        paperless._document_type_cache = {}
        paperless._storage_path_cache = {}
        paperless._tag_cache = {}

        patch_resp = MagicMock()
        patch_resp.status_code = 200
        patch_resp.json.return_value = {
            "id": 42,
            "title": "Doc",
            "correspondent": 5,
            "document_type": None,
            "tags": [],
        }
        patch_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.patch = AsyncMock(return_value=patch_resp)
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = instance

            result = await paperless.update_document(42, correspondent="Telekom")

        assert result["correspondent"] == "Telekom"
        call_kwargs = instance.patch.call_args
        assert call_kwargs.kwargs["json"] == {"correspondent": 5}

    @pytest.mark.asyncio
    async def test_update_document_type(self):
        paperless._correspondent_cache = {}
        paperless._document_type_cache = {3: "Rechnung"}
        paperless._storage_path_cache = {}
        paperless._tag_cache = {}

        patch_resp = MagicMock()
        patch_resp.status_code = 200
        patch_resp.json.return_value = {
            "id": 42,
            "title": "Doc",
            "correspondent": None,
            "document_type": 3,
            "tags": [],
        }
        patch_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.patch = AsyncMock(return_value=patch_resp)
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = instance

            result = await paperless.update_document(42, document_type="Rechnung")

        assert result["document_type"] == "Rechnung"
        call_kwargs = instance.patch.call_args
        assert call_kwargs.kwargs["json"] == {"document_type": 3}

    @pytest.mark.asyncio
    async def test_update_tags(self):
        paperless._correspondent_cache = {}
        paperless._document_type_cache = {}
        paperless._storage_path_cache = {}
        paperless._tag_cache = {1: "privat", 2: "steuer"}

        patch_resp = MagicMock()
        patch_resp.status_code = 200
        patch_resp.json.return_value = {
            "id": 42,
            "title": "Doc",
            "correspondent": None,
            "document_type": None,
            "tags": [1, 2],
        }
        patch_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.patch = AsyncMock(return_value=patch_resp)
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = instance

            result = await paperless.update_document(42, tags=["privat", "steuer"])

        assert result["tags"] == ["privat", "steuer"]
        call_kwargs = instance.patch.call_args
        assert call_kwargs.kwargs["json"] == {"tags": [1, 2]}

    @pytest.mark.asyncio
    async def test_unknown_correspondent_returns_error(self):
        paperless._correspondent_cache = {1: "Telekom"}
        paperless._document_type_cache = {}
        paperless._storage_path_cache = {}
        paperless._tag_cache = {}

        with patch("httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = instance

            result = await paperless.update_document(42, correspondent="NonExistent")

        assert "error" in result
        assert "NonExistent" in result["error"]

    @pytest.mark.asyncio
    async def test_unknown_document_type_returns_error(self):
        paperless._correspondent_cache = {}
        paperless._document_type_cache = {1: "Rechnung"}
        paperless._storage_path_cache = {}
        paperless._tag_cache = {}

        with patch("httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = instance

            result = await paperless.update_document(42, document_type="NonExistent")

        assert "error" in result
        assert "NonExistent" in result["error"]

    @pytest.mark.asyncio
    async def test_unknown_tags_returns_error(self):
        paperless._correspondent_cache = {}
        paperless._document_type_cache = {}
        paperless._storage_path_cache = {}
        paperless._tag_cache = {1: "privat"}

        with patch("httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = instance

            result = await paperless.update_document(42, tags=["privat", "nonexistent"])

        assert "error" in result
        assert "nonexistent" in result["error"]

    @pytest.mark.asyncio
    async def test_document_not_found(self):
        paperless._correspondent_cache = {}
        paperless._document_type_cache = {}
        paperless._storage_path_cache = {}
        paperless._tag_cache = {}

        patch_resp = MagicMock()
        patch_resp.status_code = 404

        with patch("httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.patch = AsyncMock(return_value=patch_resp)
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = instance

            result = await paperless.update_document(9999, title="New")

        assert "error" in result
        assert "9999" in result["error"]

    @pytest.mark.asyncio
    async def test_multiple_fields_sent_together(self):
        """Multiple fields in one PATCH call."""
        paperless._correspondent_cache = {5: "Telekom"}
        paperless._document_type_cache = {3: "Rechnung"}
        paperless._storage_path_cache = {}
        paperless._tag_cache = {}

        patch_resp = MagicMock()
        patch_resp.status_code = 200
        patch_resp.json.return_value = {
            "id": 42,
            "title": "Updated",
            "correspondent": 5,
            "document_type": 3,
            "tags": [],
        }
        patch_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.patch = AsyncMock(return_value=patch_resp)
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = instance

            result = await paperless.update_document(
                42, title="Updated", correspondent="Telekom", document_type="Rechnung"
            )

        call_kwargs = instance.patch.call_args
        sent_json = call_kwargs.kwargs["json"]
        assert sent_json == {"title": "Updated", "correspondent": 5, "document_type": 3}
        assert result["title"] == "Updated"
        assert result["correspondent"] == "Telekom"
        assert result["document_type"] == "Rechnung"


# ── reprocess_document ──────────────────────────────────────────


class TestReprocessDocument:
    @pytest.mark.asyncio
    async def test_missing_url_returns_error(self):
        paperless.PAPERLESS_API_URL = ""
        result = await paperless.reprocess_document(1)
        assert "error" in result

    @pytest.mark.asyncio
    async def test_missing_token_returns_error(self):
        paperless.PAPERLESS_API_URL = "http://test"
        paperless.PAPERLESS_API_TOKEN = ""
        result = await paperless.reprocess_document(1)
        assert "error" in result

    @pytest.mark.asyncio
    async def test_successful_reprocess(self):
        post_resp = MagicMock()
        post_resp.status_code = 200
        post_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.post = AsyncMock(return_value=post_resp)
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = instance

            result = await paperless.reprocess_document(42)

        assert result["id"] == 42
        assert result["status"] == "reprocessing"

        # Verify the correct API call
        call_args = instance.post.call_args
        assert "/api/documents/bulk_edit/" in call_args.args[0]
        assert call_args.kwargs["json"] == {"documents": [42], "method": "reprocess"}

    @pytest.mark.asyncio
    async def test_reprocess_sends_correct_body(self):
        """Verify the exact POST body sent to the bulk_edit endpoint."""
        post_resp = MagicMock()
        post_resp.status_code = 200
        post_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.post = AsyncMock(return_value=post_resp)
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = instance

            await paperless.reprocess_document(123)

        call_kwargs = instance.post.call_args.kwargs
        assert call_kwargs["json"]["documents"] == [123]
        assert call_kwargs["json"]["method"] == "reprocess"


# ── get_document extended fields ─────────────────────────────────


class TestGetDocumentExtendedFields:
    @pytest.mark.asyncio
    async def test_returns_storage_path_custom_fields_page_count_added(self):
        """get_document returns storage_path, custom_fields, page_count, added."""
        paperless._correspondent_cache = {1: "Telekom"}
        paperless._document_type_cache = {2: "Rechnung"}
        paperless._storage_path_cache = {7: "archive/invoices"}
        paperless._tag_cache = {10: "privat"}

        doc_resp = MagicMock()
        doc_resp.status_code = 200
        doc_resp.json.return_value = {
            "id": 42,
            "title": "Telekom Rechnung Januar",
            "content": "Rechnungsbetrag: 49,99 EUR",
            "created": "2030-01-15",
            "correspondent": 1,
            "document_type": 2,
            "tags": [10],
            "original_file_name": "telekom_jan.pdf",
            "storage_path": 7,
            "custom_fields": [{"field": 1, "value": "CF-001"}],
            "page_count": 3,
            "added": "2030-01-16T10:30:00Z",
        }
        doc_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.get = AsyncMock(return_value=doc_resp)
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = instance

            result = await paperless.get_document(42)

        assert result["storage_path"] == "archive/invoices"
        assert result["custom_fields"] == [{"field": 1, "value": "CF-001"}]
        assert result["page_count"] == 3
        assert result["added"] == "2030-01-16T10:30:00Z"

    @pytest.mark.asyncio
    async def test_null_extended_fields(self):
        """Extended fields return None when absent from API response."""
        paperless._correspondent_cache = {}
        paperless._document_type_cache = {}
        paperless._storage_path_cache = {}
        paperless._tag_cache = {}

        doc_resp = MagicMock()
        doc_resp.status_code = 200
        doc_resp.json.return_value = {
            "id": 1,
            "title": "Untitled",
            "content": None,
            "created": None,
            "correspondent": None,
            "document_type": None,
            "tags": [],
            "original_file_name": None,
        }
        doc_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.get = AsyncMock(return_value=doc_resp)
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = instance

            result = await paperless.get_document(1)

        assert result["storage_path"] is None
        assert result["custom_fields"] is None
        assert result["page_count"] is None
        assert result["added"] is None


# ── update_document created_date ─────────────────────────────────


class TestUpdateDocumentCreatedDate:
    @pytest.mark.asyncio
    async def test_created_date_sent_in_patch(self):
        """Passing created_date sends 'created' in PATCH body."""
        paperless._correspondent_cache = {}
        paperless._document_type_cache = {}
        paperless._storage_path_cache = {}
        paperless._tag_cache = {}

        patch_resp = MagicMock()
        patch_resp.status_code = 200
        patch_resp.json.return_value = {
            "id": 42,
            "title": "Doc",
            "correspondent": None,
            "document_type": None,
            "tags": [],
            "storage_path": None,
        }
        patch_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.patch = AsyncMock(return_value=patch_resp)
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = instance

            result = await paperless.update_document(42, created_date="2024-01-15")

        assert result["id"] == 42
        call_kwargs = instance.patch.call_args
        assert call_kwargs.kwargs["json"] == {"created": "2024-01-15"}


# ── update_document storage_path ─────────────────────────────────


class TestUpdateDocumentStoragePath:
    @pytest.mark.asyncio
    async def test_storage_path_resolved_and_sent(self):
        """Passing storage_path resolves to ID and includes in PATCH."""
        paperless._correspondent_cache = {}
        paperless._document_type_cache = {}
        paperless._storage_path_cache = {7: "archive/invoices"}
        paperless._tag_cache = {}

        patch_resp = MagicMock()
        patch_resp.status_code = 200
        patch_resp.json.return_value = {
            "id": 42,
            "title": "Doc",
            "correspondent": None,
            "document_type": None,
            "tags": [],
            "storage_path": 7,
        }
        patch_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.patch = AsyncMock(return_value=patch_resp)
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = instance

            result = await paperless.update_document(42, storage_path="archive/invoices")

        assert result["storage_path"] == "archive/invoices"
        call_kwargs = instance.patch.call_args
        assert call_kwargs.kwargs["json"] == {"storage_path": 7}

    @pytest.mark.asyncio
    async def test_unknown_storage_path_returns_error(self):
        """Unknown storage_path returns error."""
        paperless._correspondent_cache = {}
        paperless._document_type_cache = {}
        paperless._storage_path_cache = {7: "archive/invoices"}
        paperless._tag_cache = {}

        with patch("httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = instance

            result = await paperless.update_document(42, storage_path="nonexistent/path")

        assert "error" in result
        assert "nonexistent/path" in result["error"]


# ── update_document custom_fields ────────────────────────────────


class TestUpdateDocumentCustomFields:
    @pytest.mark.asyncio
    async def test_custom_fields_sent_in_patch(self):
        """Passing custom_fields includes them in the PATCH body as-is."""
        paperless._correspondent_cache = {}
        paperless._document_type_cache = {}
        paperless._storage_path_cache = {}
        paperless._tag_cache = {}

        custom = [{"field": 1, "value": "CF-001"}, {"field": 2, "value": True}]

        patch_resp = MagicMock()
        patch_resp.status_code = 200
        patch_resp.json.return_value = {
            "id": 42,
            "title": "Doc",
            "correspondent": None,
            "document_type": None,
            "tags": [],
            "storage_path": None,
        }
        patch_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.patch = AsyncMock(return_value=patch_resp)
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = instance

            result = await paperless.update_document(42, custom_fields=custom)

        assert result["id"] == 42
        call_kwargs = instance.patch.call_args
        assert call_kwargs.kwargs["json"] == {"custom_fields": custom}


# ── list_custom_fields ───────────────────────────────────────────


class TestListCustomFields:
    @pytest.mark.asyncio
    async def test_successful_return(self):
        """Returns fields list from API."""
        api_resp = MagicMock()
        api_resp.status_code = 200
        api_resp.json.return_value = {
            "results": [
                {"id": 1, "name": "Invoice Number", "data_type": "string", "extra_data": None},
                {"id": 2, "name": "Due Date", "data_type": "date", "extra_data": None},
            ]
        }
        api_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.get = AsyncMock(return_value=api_resp)
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = instance

            result = await paperless.list_custom_fields()

        assert len(result["fields"]) == 2
        assert result["fields"][0] == {
            "id": 1, "name": "Invoice Number", "data_type": "string", "extra_data": None
        }
        assert result["fields"][1] == {
            "id": 2, "name": "Due Date", "data_type": "date", "extra_data": None
        }

    @pytest.mark.asyncio
    async def test_missing_url_returns_error(self, monkeypatch):
        monkeypatch.setenv("PAPERLESS_API_URL", "")
        monkeypatch.setattr(paperless, "PAPERLESS_API_URL", "")
        result = await paperless.list_custom_fields()
        assert "error" in result

    @pytest.mark.asyncio
    async def test_missing_token_returns_error(self, monkeypatch):
        monkeypatch.setenv("PAPERLESS_API_TOKEN", "")
        monkeypatch.setattr(paperless, "PAPERLESS_API_TOKEN", "")
        result = await paperless.list_custom_fields()
        assert "error" in result


# ── list_storage_paths ───────────────────────────────────────────


class TestListStoragePaths:
    @pytest.mark.asyncio
    async def test_successful_return_from_cache(self):
        """Returns paths from cache after _ensure_caches."""
        paperless._correspondent_cache = {}
        paperless._document_type_cache = {}
        paperless._storage_path_cache = {1: "archive/invoices", 2: "archive/contracts"}
        paperless._tag_cache = {}

        with patch("httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = instance

            result = await paperless.list_storage_paths()

        assert len(result["paths"]) == 2
        path_ids = {p["id"] for p in result["paths"]}
        assert path_ids == {1, 2}
        path_names = {p["path"] for p in result["paths"]}
        assert path_names == {"archive/invoices", "archive/contracts"}

    @pytest.mark.asyncio
    async def test_missing_url_returns_error(self, monkeypatch):
        monkeypatch.setenv("PAPERLESS_API_URL", "")
        monkeypatch.setattr(paperless, "PAPERLESS_API_URL", "")
        result = await paperless.list_storage_paths()
        assert "error" in result

    @pytest.mark.asyncio
    async def test_missing_token_returns_error(self, monkeypatch):
        monkeypatch.setenv("PAPERLESS_API_TOKEN", "")
        monkeypatch.setattr(paperless, "PAPERLESS_API_TOKEN", "")
        result = await paperless.list_storage_paths()
        assert "error" in result


# ── _resolve_document storage_path ───────────────────────────────


class TestResolveDocumentStoragePath:
    def test_includes_storage_path_when_present(self):
        """_resolve_document includes storage_path in output."""
        paperless._correspondent_cache = {}
        paperless._document_type_cache = {}
        paperless._storage_path_cache = {7: "archive/invoices"}
        paperless._tag_cache = {}

        result = paperless._resolve_document({
            "id": 1,
            "title": "Test",
            "created": "2030-01-15",
            "correspondent": None,
            "document_type": None,
            "tags": [],
            "content": None,
            "storage_path": 7,
        })

        assert result["storage_path"] == "archive/invoices"

    def test_storage_path_none_when_absent(self):
        """_resolve_document returns None for storage_path when not set."""
        paperless._correspondent_cache = {}
        paperless._document_type_cache = {}
        paperless._storage_path_cache = {7: "archive/invoices"}
        paperless._tag_cache = {}

        result = paperless._resolve_document({
            "id": 1,
            "title": "Test",
            "created": "2030-01-15",
            "correspondent": None,
            "document_type": None,
            "tags": [],
            "content": None,
        })

        assert result["storage_path"] is None

    def test_storage_path_none_when_id_not_in_cache(self):
        """_resolve_document returns None when storage_path ID not in cache."""
        paperless._correspondent_cache = {}
        paperless._document_type_cache = {}
        paperless._storage_path_cache = {7: "archive/invoices"}
        paperless._tag_cache = {}

        result = paperless._resolve_document({
            "id": 1,
            "title": "Test",
            "created": "2030-01-15",
            "correspondent": None,
            "document_type": None,
            "tags": [],
            "content": None,
            "storage_path": 999,
        })

        assert result["storage_path"] is None
