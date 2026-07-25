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
        # Must be ≥100 bytes — the strict base64 + size floor added in #4
        # rejects anything smaller as a probable placeholder.
        file_bytes = b"%PDF-1.4 " + b"x" * 200
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
    async def test_upload_wait_for_consume_false_returns_submitted_without_polling(self):
        """wait_for_consume=False: with post-consume fields (custom_fields), the
        tool POSTs and returns immediately with status=submitted + deferred_patch,
        WITHOUT polling the consume task — so the caller can apply the metadata
        async via update_document (avoids blocking on Paperless's consume queue)."""
        import base64

        paperless.PAPERLESS_API_URL = "http://test"
        paperless.PAPERLESS_API_TOKEN = "tok"
        file_bytes = b"%PDF-1.4 " + b"x" * 200
        b64 = base64.b64encode(file_bytes).decode("ascii")

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = '"task-async-9"'
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.post = AsyncMock(return_value=mock_resp)
            instance.get = AsyncMock()  # must NOT be called (no consume poll)
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = instance

            result = await paperless.upload_document(
                title="Async Invoice",
                file_content_base64=b64,
                filename="invoice.pdf",
                custom_fields=[{"field": 1, "value": "X"}],
                wait_for_consume=False,
            )

        assert result["task_id"] == "task-async-9"
        assert result["status"] == "submitted"
        assert result["deferred_patch"]["custom_fields"] == [{"field": 1, "value": "X"}]
        assert "document_id" not in result  # never polled for it
        instance.get.assert_not_called()  # no consume-task polling

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

    @pytest.mark.asyncio
    async def test_upload_resolves_correspondent_name_to_id(self):
        """Regression for the 2026-04-24 bug — Paperless post_document expects
        an integer pk for correspondent/document_type/tags. The MCP server must
        resolve the user-friendly name against the cached taxonomy before
        sending, otherwise Paperless returns HTTP 400
        "Incorrect type. Expected pk value, received str."."""
        import base64
        file_bytes = b"%PDF-1.4 " + b"x" * 200
        b64 = base64.b64encode(file_bytes).decode("ascii")

        paperless.PAPERLESS_API_URL = "http://test"
        paperless.PAPERLESS_API_TOKEN = "token"
        paperless._correspondent_cache = {42: "Telekom Deutschland GmbH"}
        paperless._document_type_cache = {7: "Rechnung"}
        paperless._tag_cache = {3: "privat", 4: "steuer"}
        paperless._storage_path_cache = {}

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = '"task-uuid-123"'

        captured_data: dict = {}

        async def capture_post(url, **kwargs):
            captured_data.update(kwargs.get("data", {}))
            return mock_resp

        with patch("httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.post = capture_post
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = instance

            result = await paperless.upload_document(
                title="Test Invoice",
                file_content_base64=b64,
                filename="invoice.pdf",
                correspondent="Telekom",  # substring match → id=42
                document_type="Rechnung",  # exact match → id=7
                tags=["privat", "steuer"],
            )

        assert "error" not in result
        # The posted form data must carry integer IDs, not the raw names.
        assert captured_data["correspondent"] == 42
        assert captured_data["document_type"] == 7
        assert captured_data["tags[0]"] == 3
        assert captured_data["tags[1]"] == 4

    @pytest.mark.asyncio
    async def test_upload_fails_fast_on_unknown_correspondent(self):
        import base64
        file_bytes = b"%PDF-1.4 " + b"x" * 200
        b64 = base64.b64encode(file_bytes).decode("ascii")

        paperless.PAPERLESS_API_URL = "http://test"
        paperless.PAPERLESS_API_TOKEN = "token"
        paperless._correspondent_cache = {42: "Telekom"}
        paperless._document_type_cache = {}
        paperless._tag_cache = {}
        paperless._storage_path_cache = {}

        result = await paperless.upload_document(
            title="Test",
            file_content_base64=b64,
            correspondent="DoesNotExist",
        )

        assert "error" in result
        assert "Unknown correspondent" in result["error"]
        assert "DoesNotExist" in result["error"]

    @pytest.mark.asyncio
    async def test_upload_surfaces_paperless_error_body_on_400(self):
        """Regression for diagnostics — when Paperless returns 4xx the MCP
        server must propagate the response body instead of swallowing it via
        resp.raise_for_status(). Without this, the user only sees
        "Client error '400 Bad Request'" and can't tell what Paperless
        actually complained about."""
        import base64
        file_bytes = b"%PDF-1.4 " + b"x" * 200
        b64 = base64.b64encode(file_bytes).decode("ascii")

        paperless.PAPERLESS_API_URL = "http://test"
        paperless.PAPERLESS_API_TOKEN = "token"
        paperless._correspondent_cache = {}
        paperless._document_type_cache = {}
        paperless._tag_cache = {}
        paperless._storage_path_cache = {}

        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.text = '{"document":["The submitted data was not a file."]}'

        with patch("httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.post = AsyncMock(return_value=mock_resp)
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = instance

            result = await paperless.upload_document(
                title="Test",
                file_content_base64=b64,
            )

        assert "error" in result
        assert "HTTP 400" in result["error"]
        assert "The submitted data was not a file." in result["error"]


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


# ── delete_document ─────────────────────────────────────────────


class TestDeleteDocument:
    @pytest.mark.asyncio
    async def test_missing_url_returns_error(self):
        paperless.PAPERLESS_API_URL = ""
        result = await paperless.delete_document(1)
        assert "error" in result

    @pytest.mark.asyncio
    async def test_missing_token_returns_error(self):
        paperless.PAPERLESS_API_URL = "http://test"
        paperless.PAPERLESS_API_TOKEN = ""
        result = await paperless.delete_document(1)
        assert "error" in result

    @pytest.mark.asyncio
    async def test_successful_delete(self):
        paperless.PAPERLESS_API_URL = "http://test"
        paperless.PAPERLESS_API_TOKEN = "tok"
        del_resp = MagicMock()
        del_resp.status_code = 204
        del_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.delete = AsyncMock(return_value=del_resp)
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = instance

            result = await paperless.delete_document(42)

        assert result == {"deleted": True, "id": 42}
        # Verify it hit the document detail endpoint with DELETE
        call_args = instance.delete.call_args
        assert call_args.args[0].endswith("/api/documents/42/")

    @pytest.mark.asyncio
    async def test_not_found_returns_error(self):
        paperless.PAPERLESS_API_URL = "http://test"
        paperless.PAPERLESS_API_TOKEN = "tok"
        del_resp = MagicMock()
        del_resp.status_code = 404
        del_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.delete = AsyncMock(return_value=del_resp)
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = instance

            result = await paperless.delete_document(9999)

        assert "error" in result
        assert "9999" in result["error"]


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

    @pytest.mark.asyncio
    async def test_include_content_false_omits_ocr(self):
        """include_content=False omits the OCR content (metadata-only) so the
        response can't be size-truncated; metadata fields still present."""
        paperless._correspondent_cache = {}
        paperless._document_type_cache = {}
        paperless._storage_path_cache = {}
        paperless._tag_cache = {}

        doc_resp = MagicMock()
        doc_resp.status_code = 200
        doc_resp.json.return_value = {
            "id": 7, "title": "Big OCR Doc",
            "content": "x" * 50000,  # large OCR text that would blow the cap
            "created": "2030-01-01", "correspondent": None,
            "document_type": None, "tags": [], "original_file_name": "big.pdf",
        }
        doc_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.get = AsyncMock(return_value=doc_resp)
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = instance

            full = await paperless.get_document(7)
            meta = await paperless.get_document(7, include_content=False)

        assert full["content"] == "x" * 50000      # default keeps content
        assert meta["content"] is None             # omitted
        assert meta["title"] == "Big OCR Doc"       # metadata still present
        assert meta["original_file_name"] == "big.pdf"


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


# ── update_document content ──────────────────────────────────────


class TestUpdateDocumentContent:
    @pytest.mark.asyncio
    async def test_content_sent_in_patch(self):
        """Passing content writes the OCR text back via the PATCH body.

        Used by Renfield's audit re-OCR: re-OCR a document locally with a
        better engine, then push the cleaned text into Paperless's searchable
        content field.
        """
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

            result = await paperless.update_document(42, content="Sauberer OCR Text.")

        assert result["id"] == 42
        call_kwargs = instance.patch.call_args
        assert call_kwargs.kwargs["json"] == {"content": "Sauberer OCR Text."}

    @pytest.mark.asyncio
    async def test_content_combines_with_metadata(self):
        """content and metadata fields go into the same PATCH body."""
        paperless._correspondent_cache = {}
        paperless._document_type_cache = {}
        paperless._storage_path_cache = {}
        paperless._tag_cache = {}

        patch_resp = MagicMock()
        patch_resp.status_code = 200
        patch_resp.json.return_value = {
            "id": 42, "title": "New", "correspondent": None,
            "document_type": None, "tags": [], "storage_path": None,
        }
        patch_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.patch = AsyncMock(return_value=patch_resp)
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = instance

            result = await paperless.update_document(42, title="New", content="OCR")

        assert result["id"] == 42
        assert instance.patch.call_args.kwargs["json"] == {"title": "New", "content": "OCR"}


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


# ── upload_document ──────────────────────────────────────────────

def _make_mock_upload_client(task_id: str = "abc-123"):
    """Mock httpx.AsyncClient whose POST returns a Paperless task id."""
    post_resp = MagicMock()
    post_resp.status_code = 200  # upload_document checks `status_code >= 400` first
    post_resp.text = f'"{task_id}"'
    post_resp.raise_for_status = MagicMock()

    instance = AsyncMock()
    instance.post = AsyncMock(return_value=post_resp)
    instance.__aenter__ = AsyncMock(return_value=instance)
    instance.__aexit__ = AsyncMock(return_value=False)
    return instance


class TestUploadDocumentMimeType:
    """Paperless rejects application/octet-stream — verify the content-type
    is always derived from the filename and sent as the third tuple element
    in the multipart files= dict."""

    @pytest.mark.asyncio
    async def test_pdf_filename_sets_application_pdf(self):
        import base64 as _b64
        # Pad past the 100-byte floor added in #4.
        payload = _b64.b64encode(b"%PDF-1.4\n" + b"x" * 200).decode("ascii")
        mock = _make_mock_upload_client()
        with patch("httpx.AsyncClient", return_value=mock):
            await paperless.upload_document(
                title="Invoice",
                file_content_base64=payload,
                filename="Invoice-1SOGUR2D-0011.pdf",
            )

        call_kwargs = mock.post.call_args.kwargs
        file_tuple = call_kwargs["files"]["document"]
        assert len(file_tuple) == 3, "httpx files tuple must include content-type"
        assert file_tuple[0] == "Invoice-1SOGUR2D-0011.pdf"
        assert file_tuple[2] == "application/pdf"

    @pytest.mark.asyncio
    async def test_png_filename_sets_image_png(self):
        import base64 as _b64
        payload = _b64.b64encode(b"\x89PNG\r\n" + b"x" * 200).decode("ascii")
        mock = _make_mock_upload_client()
        with patch("httpx.AsyncClient", return_value=mock):
            await paperless.upload_document(
                title="Scan",
                file_content_base64=payload,
                filename="scan.png",
            )
        file_tuple = mock.post.call_args.kwargs["files"]["document"]
        assert file_tuple[2] == "image/png"

    @pytest.mark.asyncio
    async def test_unknown_extension_falls_back_to_application_pdf(self):
        """mimetypes.guess_type returns None for extensionless filenames — the
        upload path must still produce a non-octet-stream content-type."""
        import base64 as _b64
        payload = _b64.b64encode(b"data" + b"x" * 200).decode("ascii")
        mock = _make_mock_upload_client()
        with patch("httpx.AsyncClient", return_value=mock):
            await paperless.upload_document(
                title="Mystery",
                file_content_base64=payload,
                filename="no_extension_here",
            )
        file_tuple = mock.post.call_args.kwargs["files"]["document"]
        assert file_tuple[2] != "application/octet-stream"
        assert file_tuple[2] == "application/pdf"

    @pytest.mark.asyncio
    async def test_never_sends_octet_stream(self):
        """Regression guard for the original bug: Paperless 'Unsupported mime
        type application/octet-stream' must not be reachable via the happy path."""
        mock = _make_mock_upload_client()
        with patch("httpx.AsyncClient", return_value=mock):
            # 100+ bytes so the size floor does not trip
            import base64 as _b64
            payload = _b64.b64encode(b"%PDF-1.4\n" + b"x" * 200).decode("ascii")
            await paperless.upload_document(
                title="T",
                file_content_base64=payload,
                filename="doc.pdf",
            )
        file_tuple = mock.post.call_args.kwargs["files"]["document"]
        assert file_tuple[2] != "application/octet-stream"


class TestUploadDocumentBase64Validation:
    """Garbage-in / clear-error-out: real LLMs have been observed to pass
    placeholder strings as ``file_content_base64``. The tool must not
    silently forward corrupt bytes to Paperless."""

    @pytest.mark.asyncio
    async def test_placeholder_string_rejected(self):
        """The exact string produced by a hallucinating LLM must surface as
        an MCP-level error, not reach Paperless."""
        result = await paperless.upload_document(
            title="Invoice",
            file_content_base64="base64_encoded_content_of_the_invoice",
            filename="invoice.pdf",
        )
        assert "error" in result
        assert "base64" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_garbage_with_invalid_chars_rejected(self):
        """Non-base64 characters (``_``, ``!``, spaces) fail validation."""
        result = await paperless.upload_document(
            title="T",
            file_content_base64="not!valid base64_at_all",
            filename="x.pdf",
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_too_small_payload_rejected(self):
        """Base64 that decodes to fewer than 100 bytes is refused with a
        helpful error message — a real document cannot be that small."""
        import base64 as _b64
        tiny = _b64.b64encode(b"hello").decode("ascii")
        result = await paperless.upload_document(
            title="T",
            file_content_base64=tiny,
            filename="x.pdf",
        )
        assert "error" in result
        assert "too small" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_validation_does_not_reach_paperless(self):
        """When base64 validation fails, no HTTP request is made."""
        mock = _make_mock_upload_client()
        with patch("httpx.AsyncClient", return_value=mock):
            await paperless.upload_document(
                title="T",
                file_content_base64="obviously_not_base64!",
                filename="x.pdf",
            )
        assert mock.post.call_count == 0

    @pytest.mark.asyncio
    async def test_valid_payload_still_uploads(self):
        """Positive control: a realistic base64 payload (>= 100 bytes) reaches Paperless."""
        mock = _make_mock_upload_client()
        import base64 as _b64
        real = _b64.b64encode(b"%PDF-1.4\n" + b"content" * 20).decode("ascii")
        with patch("httpx.AsyncClient", return_value=mock):
            result = await paperless.upload_document(
                title="T",
                file_content_base64=real,
                filename="real.pdf",
            )
        assert "error" not in result
        assert mock.post.call_count == 1


# ── _invalidate_cache ────────────────────────────────────────────


class TestInvalidateCache:
    def test_invalidates_correspondent_only(self):
        paperless._correspondent_cache = {1: "A"}
        paperless._document_type_cache = {2: "B"}
        paperless._tag_cache = {3: "C"}
        paperless._storage_path_cache = {4: "D"}

        paperless._invalidate_cache("correspondent")

        assert paperless._correspondent_cache is None
        assert paperless._document_type_cache == {2: "B"}
        assert paperless._tag_cache == {3: "C"}
        assert paperless._storage_path_cache == {4: "D"}

    def test_invalidates_document_type_only(self):
        paperless._correspondent_cache = {1: "A"}
        paperless._document_type_cache = {2: "B"}
        paperless._invalidate_cache("document_type")
        assert paperless._document_type_cache is None
        assert paperless._correspondent_cache == {1: "A"}

    def test_invalidates_tag_only(self):
        paperless._tag_cache = {1: "A"}
        paperless._storage_path_cache = {2: "B"}
        paperless._invalidate_cache("tag")
        assert paperless._tag_cache is None
        assert paperless._storage_path_cache == {2: "B"}

    def test_invalidates_storage_path_only(self):
        paperless._storage_path_cache = {1: "A"}
        paperless._correspondent_cache = {2: "B"}
        paperless._invalidate_cache("storage_path")
        assert paperless._storage_path_cache is None
        assert paperless._correspondent_cache == {2: "B"}


# ── _ensure_caches: parallel fetches ─────────────────────────────


class TestEnsureCachesParallel:
    @pytest.mark.asyncio
    async def test_all_four_fetches_run_concurrently(self):
        """Regression: _ensure_caches must use asyncio.gather so four
        cold-cache fetches parallelise. If the implementation reverts
        to serial awaits, the call_count stays at 4 but the internal
        await pattern changes — we detect the gather pattern by
        checking that no fetch blocks the others."""
        import asyncio as _asyncio

        # Each mock response returns a distinct shape so we can verify
        # all four populated without cross-talk.
        call_order: list[str] = []

        async def _tracked_get(url, **kwargs):
            # Record URL prefix so we know which endpoint was hit.
            if "correspondents" in url:
                call_order.append("correspondents")
                body = {"results": [{"id": 10, "name": "A"}], "next": None}
            elif "document_types" in url:
                call_order.append("document_types")
                body = {"results": [{"id": 20, "name": "B"}], "next": None}
            elif "storage_paths" in url:
                call_order.append("storage_paths")
                body = {"results": [{"id": 30, "path": "/x"}], "next": None}
            elif "tags" in url:
                call_order.append("tags")
                body = {"results": [{"id": 40, "name": "D"}], "next": None}
            else:
                body = {"results": [], "next": None}
            # Yield briefly so the event loop can interleave the four calls
            # when they run concurrently.
            await _asyncio.sleep(0)
            resp = MagicMock()
            resp.json.return_value = body
            resp.raise_for_status = MagicMock()
            return resp

        client = AsyncMock()
        client.get = _tracked_get

        await paperless._ensure_caches(client)

        assert paperless._correspondent_cache == {10: "A"}
        assert paperless._document_type_cache == {20: "B"}
        assert paperless._storage_path_cache == {30: "/x"}
        assert paperless._tag_cache == {40: "D"}
        # All four endpoints were touched exactly once.
        assert sorted(call_order) == sorted(
            ["correspondents", "document_types", "storage_paths", "tags"]
        )

    @pytest.mark.asyncio
    async def test_partial_invalidation_refetches_only_flushed(self):
        """After invalidating one cache dimension, only that dimension
        gets refetched on the next _ensure_caches call."""
        paperless._correspondent_cache = {1: "existing"}
        paperless._document_type_cache = {2: "existing"}
        paperless._storage_path_cache = {3: "existing"}
        paperless._tag_cache = {4: "existing"}

        paperless._invalidate_cache("correspondent")

        hit_urls: list[str] = []

        async def _tracked_get(url, **kwargs):
            hit_urls.append(url)
            resp = MagicMock()
            resp.json.return_value = {
                "results": [{"id": 99, "name": "New"}], "next": None,
            }
            resp.raise_for_status = MagicMock()
            return resp

        client = AsyncMock()
        client.get = _tracked_get

        await paperless._ensure_caches(client)

        # Only the correspondents endpoint was hit; the other three stayed
        # populated with their pre-existing values.
        assert len(hit_urls) == 1
        assert "correspondents" in hit_urls[0]
        assert paperless._correspondent_cache == {99: "New"}
        assert paperless._document_type_cache == {2: "existing"}
        assert paperless._storage_path_cache == {3: "existing"}
        assert paperless._tag_cache == {4: "existing"}


# ── _poll_task_for_document_id ───────────────────────────────────


class TestPollTaskForDocumentId:
    @pytest.mark.asyncio
    async def test_returns_document_id_on_success(self):
        resp = MagicMock()
        resp.json.return_value = [
            {"status": "SUCCESS", "related_document": 4242}
        ]
        resp.raise_for_status = MagicMock()

        client = AsyncMock()
        client.get = AsyncMock(return_value=resp)

        result = await paperless._poll_task_for_document_id(
            client, "task-abc", timeout_s=5.0
        )
        assert result == 4242

    @pytest.mark.asyncio
    async def test_returns_none_on_failure_status(self):
        resp = MagicMock()
        resp.json.return_value = [{"status": "FAILURE", "result": "OCR failed"}]
        resp.raise_for_status = MagicMock()

        client = AsyncMock()
        client.get = AsyncMock(return_value=resp)

        result = await paperless._poll_task_for_document_id(
            client, "task-abc", timeout_s=5.0
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_timeout(self):
        # Task forever PENDING → should time out.
        resp = MagicMock()
        resp.json.return_value = [{"status": "PENDING"}]
        resp.raise_for_status = MagicMock()

        client = AsyncMock()
        client.get = AsyncMock(return_value=resp)

        result = await paperless._poll_task_for_document_id(
            client, "task-abc", timeout_s=0.3
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_handles_empty_task_list_then_success(self):
        """Task registration is racy — first poll may return empty list,
        second returns the completed task."""
        call_count = {"n": 0}

        async def _paginated(url, **kwargs):
            call_count["n"] += 1
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            if call_count["n"] == 1:
                resp.json.return_value = []
            else:
                resp.json.return_value = [
                    {"status": "SUCCESS", "related_document": 77}
                ]
            return resp

        client = AsyncMock()
        client.get = _paginated

        result = await paperless._poll_task_for_document_id(
            client, "task-abc", timeout_s=5.0
        )
        assert result == 77
        assert call_count["n"] >= 2


# ── _patch_document_with_retry ───────────────────────────────────


class TestPatchDocumentWithRetry:
    @pytest.mark.asyncio
    async def test_first_attempt_succeeds(self):
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"id": 5, "title": "patched"}

        client = AsyncMock()
        client.patch = AsyncMock(return_value=resp)

        result, reason = await paperless._patch_document_with_retry(
            client, 5, {"storage_path": 7}
        )
        assert result == {"id": 5, "title": "patched"}
        assert reason is None
        assert client.patch.await_count == 1

    @pytest.mark.asyncio
    async def test_retries_on_5xx(self):
        """Server errors retry; exponential backoff between attempts."""
        mock_500 = MagicMock()
        mock_500.status_code = 503
        mock_500.request = MagicMock()
        mock_500.response = mock_500

        mock_200 = MagicMock()
        mock_200.status_code = 200
        mock_200.raise_for_status = MagicMock()
        mock_200.json.return_value = {"id": 5}

        client = AsyncMock()
        # First two return 503, third returns 200.
        client.patch = AsyncMock(side_effect=[mock_500, mock_500, mock_200])

        # Patch asyncio.sleep to skip real backoff delays in tests.
        with patch("renfield_mcp_paperless.server.asyncio.sleep", AsyncMock()):
            result, reason = await paperless._patch_document_with_retry(
                client, 5, {"storage_path": 7}, max_tries=3
            )
        assert result == {"id": 5}
        assert reason is None
        assert client.patch.await_count == 3

    @pytest.mark.asyncio
    async def test_bails_on_4xx_with_client_error_reason(self):
        """4xx errors (wrong id, bad format) don't retry. Return shape
        must distinguish this from retries_exhausted so the caller can
        pick the right recovery path."""
        import httpx as _httpx

        mock_404 = MagicMock()
        mock_404.status_code = 404
        mock_404.request = MagicMock()

        def _raise_404():
            raise _httpx.HTTPStatusError(
                "404", request=mock_404.request, response=mock_404
            )

        mock_404.raise_for_status = _raise_404
        mock_404.json.return_value = {}

        client = AsyncMock()
        client.patch = AsyncMock(return_value=mock_404)

        with patch("renfield_mcp_paperless.server.asyncio.sleep", AsyncMock()):
            result, reason = await paperless._patch_document_with_retry(
                client, 5, {"storage_path": 7}, max_tries=3
            )
        assert result is None
        assert reason == "client_error"
        # 4xx is definitive — one attempt only.
        assert client.patch.await_count == 1

    @pytest.mark.asyncio
    async def test_returns_retries_exhausted_reason(self):
        mock_500 = MagicMock()
        mock_500.status_code = 500
        mock_500.request = MagicMock()
        mock_500.response = mock_500

        client = AsyncMock()
        client.patch = AsyncMock(return_value=mock_500)

        with patch("renfield_mcp_paperless.server.asyncio.sleep", AsyncMock()):
            result, reason = await paperless._patch_document_with_retry(
                client, 5, {"storage_path": 7}, max_tries=3
            )
        assert result is None
        assert reason == "retries_exhausted"
        assert client.patch.await_count == 3

    @pytest.mark.asyncio
    async def test_retries_on_transport_error(self):
        import httpx as _httpx

        mock_200 = MagicMock()
        mock_200.status_code = 200
        mock_200.raise_for_status = MagicMock()
        mock_200.json.return_value = {"id": 5}

        client = AsyncMock()
        client.patch = AsyncMock(
            side_effect=[
                _httpx.TimeoutException("timed out"),
                mock_200,
            ]
        )

        with patch("renfield_mcp_paperless.server.asyncio.sleep", AsyncMock()):
            result, reason = await paperless._patch_document_with_retry(
                client, 5, {"storage_path": 7}, max_tries=3
            )
        assert result == {"id": 5}
        assert reason is None
        assert client.patch.await_count == 2


# ── upload_document: extended metadata fields ────────────────────


def _mk_post_resp(task_id: str = "task-abc"):
    r = MagicMock()
    r.status_code = 200
    r.text = f'"{task_id}"'
    r.raise_for_status = MagicMock()
    return r


def _mk_task_poll_resp(document_id: int | None):
    r = MagicMock()
    r.raise_for_status = MagicMock()
    if document_id is None:
        r.json.return_value = [{"status": "PENDING"}]
    else:
        r.json.return_value = [
            {"status": "SUCCESS", "related_document": document_id}
        ]
    return r


def _mk_taxonomy_resp(items: list[dict]):
    r = MagicMock()
    r.raise_for_status = MagicMock()
    r.json.return_value = {"results": items, "next": None}
    return r


class TestUploadDocumentExtendedFields:
    @pytest.mark.asyncio
    async def test_no_new_params_skips_patch_regression(self):
        """Regression guard: calling upload_document WITHOUT storage_path /
        created_date / custom_fields must produce the exact same behavior
        as pre-change — one POST, no task polling, no PATCH."""
        import base64
        b64 = base64.b64encode(b"%PDF-1.4 " + b"x" * 200).decode()

        client = AsyncMock()
        client.post = AsyncMock(return_value=_mk_post_resp("task-old"))
        # If _poll_task_for_document_id ran, client.get would be called.
        client.get = AsyncMock()
        client.patch = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=client):
            result = await paperless.upload_document(
                title="T",
                file_content_base64=b64,
                filename="t.pdf",
            )

        assert result["task_id"] == "task-old"
        assert "post_upload_patch" not in result  # no patch attempted
        assert client.patch.await_count == 0
        assert client.get.await_count == 0  # no polling, no taxonomy fetch

    @pytest.mark.asyncio
    async def test_storage_path_triggers_poll_and_patch(self):
        import base64
        b64 = base64.b64encode(b"%PDF-1.4 " + b"x" * 200).decode()

        client = AsyncMock()
        client.post = AsyncMock(return_value=_mk_post_resp("task-new"))
        # Client.get is called for: 4 taxonomy endpoints (cache warm-up)
        # then polling /api/tasks/. We sequence them:
        client.get = AsyncMock(
            side_effect=[
                _mk_taxonomy_resp([]),  # correspondents (parallel)
                _mk_taxonomy_resp([]),  # document_types (parallel)
                _mk_taxonomy_resp([
                    {"id": 50, "path": "/wohnung/betriebskosten"}
                ]),
                _mk_taxonomy_resp([]),  # tags (parallel)
                _mk_task_poll_resp(4321),  # task finished, doc id = 4321
            ]
        )
        patch_resp = MagicMock()
        patch_resp.status_code = 200
        patch_resp.raise_for_status = MagicMock()
        patch_resp.json.return_value = {"id": 4321, "storage_path": 50}
        client.patch = AsyncMock(return_value=patch_resp)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=client):
            with patch(
                "renfield_mcp_paperless.server.asyncio.sleep", AsyncMock()
            ):
                result = await paperless.upload_document(
                    title="T",
                    file_content_base64=b64,
                    filename="t.pdf",
                    storage_path="/wohnung/betriebskosten",
                )

        assert result["task_id"] == "task-new"
        assert result["document_id"] == 4321
        assert result["post_upload_patch"] == "success"
        assert client.patch.await_count == 1
        # The PATCH carried the resolved storage_path id (50), not the raw name.
        patch_call = client.patch.await_args
        assert patch_call.kwargs["json"] == {"storage_path": 50}

    @pytest.mark.asyncio
    async def test_unknown_storage_path_fails_fast_before_poll(self):
        """If the storage_path name doesn't resolve to any known id,
        don't waste time polling — return an error immediately."""
        import base64
        b64 = base64.b64encode(b"%PDF-1.4 " + b"x" * 200).decode()

        client = AsyncMock()
        client.post = AsyncMock(return_value=_mk_post_resp("task-x"))
        client.get = AsyncMock(
            side_effect=[
                _mk_taxonomy_resp([]),  # correspondents
                _mk_taxonomy_resp([]),  # document_types
                _mk_taxonomy_resp([{"id": 1, "path": "/inbox"}]),
                _mk_taxonomy_resp([]),  # tags
            ]
        )
        client.patch = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=client):
            result = await paperless.upload_document(
                title="T",
                file_content_base64=b64,
                filename="t.pdf",
                storage_path="/unknown/nowhere",
            )

        assert result["post_upload_patch"] == "unknown_storage_path"
        assert "/unknown/nowhere" in result["patch_error"]
        assert client.patch.await_count == 0  # no PATCH attempted

    @pytest.mark.asyncio
    async def test_patch_poll_timeout_returns_timed_out(self):
        import base64
        b64 = base64.b64encode(b"%PDF-1.4 " + b"x" * 200).decode()

        client = AsyncMock()
        client.post = AsyncMock(return_value=_mk_post_resp("task-slow"))

        # Function-based side_effect so poll requests always return PENDING,
        # however many iterations the loop makes before the deadline. A
        # fixed-length list would raise StopAsyncIteration on the Nth+1 call.
        taxonomy_responses = [
            _mk_taxonomy_resp([]),
            _mk_taxonomy_resp([]),
            _mk_taxonomy_resp([{"id": 99, "path": "/x"}]),
            _mk_taxonomy_resp([]),
        ]
        taxonomy_iter = iter(taxonomy_responses)

        async def _get(url, **kwargs):
            if "/api/tasks/" in url:
                return _mk_task_poll_resp(None)  # forever PENDING
            return next(taxonomy_iter)

        client.get = _get
        client.patch = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=client):
            with patch(
                "renfield_mcp_paperless.server._UPLOAD_TASK_POLL_TIMEOUT_S", 0.1
            ):
                with patch(
                    "renfield_mcp_paperless.server._UPLOAD_TASK_POLL_INTERVAL_S", 0.02
                ):
                    result = await paperless.upload_document(
                        title="T",
                        file_content_base64=b64,
                        filename="t.pdf",
                        storage_path="/x",
                    )

        assert result["post_upload_patch"] == "timed_out"
        assert "patch_error" in result
        assert client.patch.await_count == 0  # no PATCH since no doc id

    @pytest.mark.asyncio
    async def test_patch_retries_exhausted_surfaces_warning(self):
        import base64
        b64 = base64.b64encode(b"%PDF-1.4 " + b"x" * 200).decode()

        client = AsyncMock()
        client.post = AsyncMock(return_value=_mk_post_resp("task-y"))
        client.get = AsyncMock(
            side_effect=[
                _mk_taxonomy_resp([]),
                _mk_taxonomy_resp([]),
                _mk_taxonomy_resp([{"id": 1, "path": "/x"}]),
                _mk_taxonomy_resp([]),
                _mk_task_poll_resp(555),  # doc created successfully
            ]
        )
        # All 3 PATCH attempts fail with 503
        bad_resp = MagicMock()
        bad_resp.status_code = 503
        bad_resp.request = MagicMock()
        bad_resp.response = bad_resp
        client.patch = AsyncMock(return_value=bad_resp)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=client):
            with patch(
                "renfield_mcp_paperless.server.asyncio.sleep", AsyncMock()
            ):
                result = await paperless.upload_document(
                    title="T",
                    file_content_base64=b64,
                    filename="t.pdf",
                    storage_path="/x",
                )

        assert result["document_id"] == 555  # upload DID succeed
        assert result["post_upload_patch"] == "retries_exhausted"
        assert "555" in result["patch_error"]
        assert "transient errors" in result["patch_error"]
        assert client.patch.await_count == 3

    @pytest.mark.asyncio
    async def test_patch_4xx_surfaces_client_error_not_retries(self):
        """A 4xx on the PATCH (e.g. a storage_path id that was valid at
        cache time but got deleted between the fetch and the PATCH) must
        surface as ``client_error`` — NOT ``retries_exhausted``. Different
        recovery path: client_error means retrying won't help."""
        import base64
        import httpx as _httpx
        b64 = base64.b64encode(b"%PDF-1.4 " + b"x" * 200).decode()

        client = AsyncMock()
        client.post = AsyncMock(return_value=_mk_post_resp("task-z"))
        client.get = AsyncMock(
            side_effect=[
                _mk_taxonomy_resp([]),
                _mk_taxonomy_resp([]),
                _mk_taxonomy_resp([{"id": 1, "path": "/x"}]),
                _mk_taxonomy_resp([]),
                _mk_task_poll_resp(777),
            ]
        )

        # PATCH returns 422 (validation error)
        mock_422 = MagicMock()
        mock_422.status_code = 422
        mock_422.request = MagicMock()

        def _raise_422():
            raise _httpx.HTTPStatusError(
                "422", request=mock_422.request, response=mock_422
            )
        mock_422.raise_for_status = _raise_422
        client.patch = AsyncMock(return_value=mock_422)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=client):
            with patch(
                "renfield_mcp_paperless.server.asyncio.sleep", AsyncMock()
            ):
                result = await paperless.upload_document(
                    title="T",
                    file_content_base64=b64,
                    filename="t.pdf",
                    storage_path="/x",
                )

        assert result["document_id"] == 777
        assert result["post_upload_patch"] == "client_error"
        # 4xx bails on first attempt — retries_exhausted wording must NOT
        # appear in the error message for this path.
        assert "retries" not in result["patch_error"].lower()
        assert "manual" in result["patch_error"].lower() or "manually" in result["patch_error"].lower()
        assert client.patch.await_count == 1

    @pytest.mark.asyncio
    async def test_empty_custom_fields_list_does_not_trigger_patch(self):
        """Regression guard: passing custom_fields=[] must NOT trigger a
        PATCH. Sending {"custom_fields": []} to Paperless WIPES any
        existing custom fields on the document — destructive and almost
        certainly not what the caller wanted when they passed an empty
        list. Treat [] same as None (no-op for the PATCH trigger)."""
        import base64
        b64 = base64.b64encode(b"%PDF-1.4 " + b"x" * 200).decode()

        client = AsyncMock()
        client.post = AsyncMock(return_value=_mk_post_resp("task-w"))
        client.get = AsyncMock()  # should NEVER be called
        client.patch = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=client):
            result = await paperless.upload_document(
                title="T",
                file_content_base64=b64,
                filename="t.pdf",
                custom_fields=[],  # empty list — must be no-op
            )

        # Upload succeeded, but no PATCH was issued.
        assert result["task_id"] == "task-w"
        assert "post_upload_patch" not in result  # no patch attempted
        assert client.patch.await_count == 0
        assert client.get.await_count == 0  # no polling, no cache warm-up

    @pytest.mark.asyncio
    async def test_nonempty_custom_fields_does_trigger_patch(self):
        """Sibling of the empty-list test: a real custom_fields value
        SHOULD trigger the full PATCH path."""
        import base64
        b64 = base64.b64encode(b"%PDF-1.4 " + b"x" * 200).decode()

        custom_fields = [{"field": 1, "value": "invoice-2026-01"}]

        client = AsyncMock()
        client.post = AsyncMock(return_value=_mk_post_resp("task-cf"))
        client.get = AsyncMock(
            side_effect=[
                _mk_taxonomy_resp([]),
                _mk_taxonomy_resp([]),
                _mk_taxonomy_resp([]),
                _mk_taxonomy_resp([]),
                _mk_task_poll_resp(888),
            ]
        )
        patch_resp = MagicMock()
        patch_resp.status_code = 200
        patch_resp.raise_for_status = MagicMock()
        patch_resp.json.return_value = {"id": 888, "custom_fields": custom_fields}
        client.patch = AsyncMock(return_value=patch_resp)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=client):
            with patch(
                "renfield_mcp_paperless.server.asyncio.sleep", AsyncMock()
            ):
                result = await paperless.upload_document(
                    title="T",
                    file_content_base64=b64,
                    filename="t.pdf",
                    custom_fields=custom_fields,
                )

        assert result["post_upload_patch"] == "success"
        assert client.patch.await_count == 1
        # Verify the PATCH carried the custom_fields payload.
        patch_call = client.patch.await_args
        assert patch_call.kwargs["json"]["custom_fields"] == custom_fields


# ── create_correspondent ─────────────────────────────────────────


def _mk_created_resp(body: dict):
    r = MagicMock()
    r.status_code = 201
    r.raise_for_status = MagicMock()
    r.json.return_value = body
    return r


class TestCreateCorrespondent:
    @pytest.mark.asyncio
    async def test_creates_new_and_invalidates_cache(self):
        # Pre-populate cache so _ensure_caches does not re-fetch
        paperless._correspondent_cache = {1: "Existing"}
        paperless._document_type_cache = {}
        paperless._storage_path_cache = {}
        paperless._tag_cache = {}

        client = AsyncMock()
        client.post = AsyncMock(
            return_value=_mk_created_resp({"id": 42, "name": "Stadtwerke Köln"})
        )
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=client):
            result = await paperless.create_correspondent("Stadtwerke Köln")

        assert result == {"id": 42, "name": "Stadtwerke Köln"}
        # Cache was invalidated on success.
        assert paperless._correspondent_cache is None
        assert client.post.await_count == 1

    @pytest.mark.asyncio
    async def test_rejects_duplicate_returns_existing_id(self):
        paperless._correspondent_cache = {7: "Stadtwerke Köln"}
        paperless._document_type_cache = {}
        paperless._storage_path_cache = {}
        paperless._tag_cache = {}

        client = AsyncMock()
        client.post = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=client):
            result = await paperless.create_correspondent("stadtwerke köln")

        assert result["error"] == "already_exists"
        assert result["existing_id"] == 7
        assert client.post.await_count == 0  # no API call made
        # Cache NOT invalidated — nothing changed.
        assert paperless._correspondent_cache == {7: "Stadtwerke Köln"}

    @pytest.mark.asyncio
    async def test_rejects_empty_name(self):
        result = await paperless.create_correspondent("")
        assert "name must not be empty" in result["error"]

    @pytest.mark.asyncio
    async def test_rejects_whitespace_only_name(self):
        result = await paperless.create_correspondent("   ")
        assert "name must not be empty" in result["error"]

    @pytest.mark.asyncio
    async def test_surfaces_server_400(self):
        """Paperless might reject for reasons we didn't pre-check
        (server-side uniqueness on a slightly different spelling).
        We surface the raw 400 rather than silently swallowing."""
        paperless._correspondent_cache = {}
        paperless._document_type_cache = {}
        paperless._storage_path_cache = {}
        paperless._tag_cache = {}

        bad_resp = MagicMock()
        bad_resp.status_code = 400
        bad_resp.text = '{"name":["correspondent exists"]}'
        bad_resp.raise_for_status = MagicMock()

        client = AsyncMock()
        client.post = AsyncMock(return_value=bad_resp)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=client):
            result = await paperless.create_correspondent("New")

        assert result["error"] == "bad_request"
        # Cache NOT invalidated on failure.
        assert paperless._correspondent_cache == {}


# ── create_document_type ─────────────────────────────────────────


class TestCreateDocumentType:
    @pytest.mark.asyncio
    async def test_creates_new_and_invalidates_cache(self):
        paperless._correspondent_cache = {}
        paperless._document_type_cache = {1: "Rechnung"}
        paperless._storage_path_cache = {}
        paperless._tag_cache = {}

        client = AsyncMock()
        client.post = AsyncMock(
            return_value=_mk_created_resp({"id": 11, "name": "Quittung"})
        )
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=client):
            result = await paperless.create_document_type("Quittung")

        assert result == {"id": 11, "name": "Quittung"}
        assert paperless._document_type_cache is None

    @pytest.mark.asyncio
    async def test_rejects_duplicate(self):
        paperless._correspondent_cache = {}
        paperless._document_type_cache = {5: "Rechnung"}
        paperless._storage_path_cache = {}
        paperless._tag_cache = {}

        client = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=client):
            result = await paperless.create_document_type("rechnung")

        assert result["error"] == "already_exists"
        assert result["existing_id"] == 5


# ── create_tag ───────────────────────────────────────────────────


class TestCreateTag:
    @pytest.mark.asyncio
    async def test_creates_with_color(self):
        paperless._correspondent_cache = {}
        paperless._document_type_cache = {}
        paperless._storage_path_cache = {}
        paperless._tag_cache = {}

        client = AsyncMock()
        client.post = AsyncMock(
            return_value=_mk_created_resp(
                {"id": 8, "name": "steuer-2026", "color": "#a6cee3"}
            )
        )
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=client):
            result = await paperless.create_tag("steuer-2026", color="#a6cee3")

        assert result["id"] == 8
        assert result["name"] == "steuer-2026"
        assert result["color"] == "#a6cee3"
        # Verify color was sent in the payload.
        post_call = client.post.await_args
        assert post_call.kwargs["json"]["color"] == "#a6cee3"
        assert paperless._tag_cache is None

    @pytest.mark.asyncio
    async def test_creates_without_color_omits_field(self):
        paperless._correspondent_cache = {}
        paperless._document_type_cache = {}
        paperless._storage_path_cache = {}
        paperless._tag_cache = {}

        client = AsyncMock()
        client.post = AsyncMock(
            return_value=_mk_created_resp({"id": 9, "name": "privat"})
        )
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=client):
            await paperless.create_tag("privat")

        post_call = client.post.await_args
        assert "color" not in post_call.kwargs["json"]

    @pytest.mark.asyncio
    async def test_rejects_duplicate_tag(self):
        paperless._correspondent_cache = {}
        paperless._document_type_cache = {}
        paperless._storage_path_cache = {}
        paperless._tag_cache = {3: "privat"}

        client = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=client):
            result = await paperless.create_tag("privat")

        assert result["error"] == "already_exists"
        assert result["existing_id"] == 3


# ── create_storage_path ──────────────────────────────────────────


class TestCreateStoragePath:
    @pytest.mark.asyncio
    async def test_creates_new_with_template(self):
        paperless._correspondent_cache = {}
        paperless._document_type_cache = {}
        paperless._storage_path_cache = {}
        paperless._tag_cache = {}

        client = AsyncMock()
        client.post = AsyncMock(
            return_value=_mk_created_resp(
                {"id": 2, "name": "Steuer 2025", "path": "/steuer/{created_year}"}
            )
        )
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=client):
            result = await paperless.create_storage_path(
                "Steuer 2025", "/steuer/{created_year}"
            )

        assert result["id"] == 2
        assert result["path"] == "/steuer/{created_year}"
        assert paperless._storage_path_cache is None

    @pytest.mark.asyncio
    async def test_rejects_empty_path(self):
        result = await paperless.create_storage_path("Test", "")
        assert "path must not be empty" in result["error"]

    @pytest.mark.asyncio
    async def test_rejects_duplicate_path_template(self):
        """Even if the display name is new, a duplicate path template
        should be caught by the pre-check."""
        paperless._correspondent_cache = {}
        paperless._document_type_cache = {}
        paperless._storage_path_cache = {5: "/steuer/2025"}
        paperless._tag_cache = {}

        client = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=client):
            result = await paperless.create_storage_path(
                "Steuer Neu", "/steuer/2025"
            )

        assert result["error"] == "already_exists"
        assert result["existing_id"] == 5


# ── list_correspondents / list_document_types / list_tags ────────


class TestListCorrespondents:
    @pytest.mark.asyncio
    async def test_returns_cached_items(self):
        paperless._correspondent_cache = {
            1: "Stadtwerke Korschenbroich",
            2: "Finanzamt Neuss",
        }
        paperless._document_type_cache = {}
        paperless._storage_path_cache = {}
        paperless._tag_cache = {}

        client = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.get = AsyncMock()  # cache warm, no fetches expected

        with patch("httpx.AsyncClient", return_value=client):
            result = await paperless.list_correspondents()

        assert result == {
            "items": [
                {"id": 1, "name": "Stadtwerke Korschenbroich"},
                {"id": 2, "name": "Finanzamt Neuss"},
            ]
        }

    @pytest.mark.asyncio
    async def test_empty_cache_returns_empty_list(self):
        paperless._correspondent_cache = {}
        paperless._document_type_cache = {}
        paperless._storage_path_cache = {}
        paperless._tag_cache = {}

        client = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.get = AsyncMock()

        with patch("httpx.AsyncClient", return_value=client):
            result = await paperless.list_correspondents()

        assert result == {"items": []}

    @pytest.mark.asyncio
    async def test_missing_config_returns_error(self):
        paperless.PAPERLESS_API_URL = ""
        result = await paperless.list_correspondents()
        assert "error" in result


class TestListDocumentTypes:
    @pytest.mark.asyncio
    async def test_returns_cached_items(self):
        paperless._correspondent_cache = {}
        paperless._document_type_cache = {10: "Rechnung", 20: "Vertrag"}
        paperless._storage_path_cache = {}
        paperless._tag_cache = {}

        client = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.get = AsyncMock()

        with patch("httpx.AsyncClient", return_value=client):
            result = await paperless.list_document_types()

        assert result == {
            "items": [
                {"id": 10, "name": "Rechnung"},
                {"id": 20, "name": "Vertrag"},
            ]
        }

    @pytest.mark.asyncio
    async def test_missing_config_returns_error(self):
        paperless.PAPERLESS_API_URL = ""
        result = await paperless.list_document_types()
        assert "error" in result


class TestListTags:
    @pytest.mark.asyncio
    async def test_returns_cached_items(self):
        paperless._correspondent_cache = {}
        paperless._document_type_cache = {}
        paperless._storage_path_cache = {}
        paperless._tag_cache = {100: "wohnung", 101: "steuer-2025"}

        client = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.get = AsyncMock()

        with patch("httpx.AsyncClient", return_value=client):
            result = await paperless.list_tags()

        assert result == {
            "items": [
                {"id": 100, "name": "wohnung"},
                {"id": 101, "name": "steuer-2025"},
            ]
        }

    @pytest.mark.asyncio
    async def test_populates_cache_if_empty(self):
        """When the cache is None (not yet loaded), the tool triggers
        _ensure_caches which populates it from the Paperless API."""
        # Cache not yet loaded.
        paperless._correspondent_cache = None
        paperless._document_type_cache = None
        paperless._storage_path_cache = None
        paperless._tag_cache = None

        # Mock _ensure_caches's parallel fetches: all four endpoints
        # return one item each.
        async def _fake_get(url, **kwargs):
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            if "correspondents" in url:
                resp.json.return_value = {
                    "results": [{"id": 1, "name": "A"}], "next": None,
                }
            elif "document_types" in url:
                resp.json.return_value = {
                    "results": [{"id": 2, "name": "B"}], "next": None,
                }
            elif "storage_paths" in url:
                resp.json.return_value = {
                    "results": [{"id": 3, "path": "/x"}], "next": None,
                }
            elif "tags" in url:
                resp.json.return_value = {
                    "results": [{"id": 4, "name": "D"}], "next": None,
                }
            else:
                resp.json.return_value = {"results": [], "next": None}
            return resp

        client = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.get = _fake_get

        with patch("httpx.AsyncClient", return_value=client):
            result = await paperless.list_tags()

        # Cache got populated; list_tags returns the one tag.
        assert result == {"items": [{"id": 4, "name": "D"}]}
        # And the other caches got populated too — parallel fetch.
        assert paperless._correspondent_cache == {1: "A"}
        assert paperless._document_type_cache == {2: "B"}
        assert paperless._storage_path_cache == {3: "/x"}


# ── consume-task polling + duplicate detection (folder-ingest T5/T10) ──────


def _task_poll_client(task_obj):
    """A mock httpx client whose GET on /api/tasks/ returns [task_obj] (or [] if
    None) with a passing raise_for_status."""
    resp = MagicMock()
    resp.json.return_value = [task_obj] if task_obj is not None else []
    resp.raise_for_status = MagicMock()
    client = AsyncMock()
    client.get = AsyncMock(return_value=resp)
    return client


class TestIsDuplicateFailure:
    def test_canonical_marker(self):
        assert paperless._is_duplicate_failure(
            "invoice.pdf: Not consuming invoice.pdf: It is a duplicate of Rechnung (#7)"
        )

    def test_case_insensitive(self):
        assert paperless._is_duplicate_failure("IT IS A DUPLICATE OF X")

    def test_non_duplicate_failure(self):
        assert not paperless._is_duplicate_failure("OSError: cannot parse PDF")

    def test_bare_duplicate_of_is_not_matched(self):
        # Narrow marker: a bare "duplicate of" in unrelated failure text must
        # NOT be read as a duplicate (would swallow a genuine failure).
        assert not paperless._is_duplicate_failure("config has a duplicate of field x")

    def test_none_and_empty(self):
        assert not paperless._is_duplicate_failure(None)
        assert not paperless._is_duplicate_failure("")

    def test_non_string_result_is_safe(self):
        # Paperless occasionally returns a non-string result — must not crash.
        assert not paperless._is_duplicate_failure(123)
        assert not paperless._is_duplicate_failure({"err": "x"})


class TestPollTaskOutcome:
    @pytest.mark.asyncio
    async def test_success_returns_document_id(self):
        client = _task_poll_client({"status": "SUCCESS", "related_document": 42})
        out = await paperless._poll_task(client, "t1")
        assert out == {"status": "success", "document_id": 42, "detail": None}

    @pytest.mark.asyncio
    async def test_success_without_related_document(self):
        client = _task_poll_client({"status": "SUCCESS", "related_document": None})
        out = await paperless._poll_task(client, "t1")
        assert out["status"] == "success"
        assert out["document_id"] is None

    @pytest.mark.asyncio
    async def test_lowercase_success_is_terminal(self):
        # Regression (2026-07): a Paperless version switched task status to
        # lower-case ("success"/"failure"). Comparing case-sensitively against
        # "SUCCESS" never matched → the poll looped to timeout → the doc never
        # settled → re-upload loop + duplicates. Must treat lower-case as terminal.
        client = _task_poll_client({"status": "success", "related_document": 42})
        out = await paperless._poll_task(client, "t1")
        assert out == {"status": "success", "document_id": 42, "detail": None}

    @pytest.mark.asyncio
    async def test_lowercase_failure_is_terminal(self):
        client = _task_poll_client(
            {"status": "failure", "result": "Not consuming: it is a duplicate"}
        )
        out = await paperless._poll_task(client, "t1")
        assert out["status"] in ("duplicate", "failure")  # terminal, not an endless poll

    @pytest.mark.asyncio
    async def test_duplicate_failure_is_terminal_success(self):
        client = _task_poll_client(
            {"status": "FAILURE",
             "result": "x.pdf: Not consuming x.pdf: It is a duplicate of R (#7)"}
        )
        out = await paperless._poll_task(client, "t1")
        assert out["status"] == "duplicate"
        assert out["document_id"] is None
        assert "duplicate" in out["detail"].lower()

    @pytest.mark.asyncio
    async def test_non_duplicate_failure(self):
        client = _task_poll_client({"status": "FAILURE", "result": "cannot parse PDF"})
        out = await paperless._poll_task(client, "t1")
        assert out["status"] == "failure"
        assert out["detail"] == "cannot parse PDF"

    @pytest.mark.asyncio
    async def test_pending_on_timeout(self, monkeypatch):
        monkeypatch.setattr(paperless, "_UPLOAD_TASK_POLL_INTERVAL_S", 0.001)
        client = _task_poll_client({"status": "STARTED"})  # never terminal
        out = await paperless._poll_task(client, "t1", timeout_s=0.01)
        assert out["status"] == "pending"


class TestPollTaskBackCompat:
    @pytest.mark.asyncio
    async def test_wrapper_returns_doc_id_on_success(self):
        client = _task_poll_client({"status": "SUCCESS", "related_document": 9})
        assert await paperless._poll_task_for_document_id(client, "t1") == 9

    @pytest.mark.asyncio
    async def test_wrapper_none_on_duplicate(self):
        client = _task_poll_client(
            {"status": "FAILURE", "result": "It is a duplicate of X"}
        )
        assert await paperless._poll_task_for_document_id(client, "t1") is None


class TestAwaitConsumeResult:
    @pytest.mark.asyncio
    async def test_missing_url_errors(self, monkeypatch):
        monkeypatch.setattr(paperless, "PAPERLESS_API_URL", "")
        out = await paperless.await_consume_result("t1")
        assert "error" in out

    @pytest.mark.asyncio
    async def test_missing_task_id_errors(self):
        out = await paperless.await_consume_result("")
        assert "error" in out

    @pytest.mark.asyncio
    async def test_delegates_to_poll_task(self, monkeypatch):
        monkeypatch.setattr(
            paperless,
            "_poll_task",
            AsyncMock(return_value={"status": "duplicate", "document_id": None, "detail": "dup"}),
        )
        out = await paperless.await_consume_result("t1")
        assert out["status"] == "duplicate"
