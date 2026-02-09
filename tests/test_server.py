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
    yield
    paperless._correspondent_cache = None
    paperless._document_type_cache = None
    paperless._storage_path_cache = None


@pytest.fixture(autouse=True)
def _set_env(monkeypatch):
    """Ensure env vars are set for all tests."""
    monkeypatch.setenv("PAPERLESS_API_URL", "http://your-paperless-url")
    monkeypatch.setenv("PAPERLESS_API_TOKEN", "test-token-abc")
    # Reload module-level constants
    monkeypatch.setattr(paperless, "PAPERLESS_API_URL", "http://your-paperless-url")
    monkeypatch.setattr(paperless, "PAPERLESS_API_TOKEN", "test-token-abc")


# ── _resolve_document ────────────────────────────────────────────


class TestResolveDocument:
    def test_resolves_all_ids(self):
        paperless._correspondent_cache = {1: "COMPANY 1"}
        paperless._document_type_cache = {2: "Rechnung"}
        paperless._storage_path_cache = {3: "rechnungen/company1"}

        result = paperless._resolve_document({
            "id": 1,
            "title": "COMPANY 1 Rechnung 2030-01",
            "created": "2030-01-15",
            "correspondent": 1,
            "document_type": 2,
            "storage_path": 3,
        })

        assert result == {
            "id": 1,
            "title": "COMPANY 1 Rechnung 2030-01",
            "created": "2030-01-15",
            "correspondent": "COMPANY 1",
            "document_type": "Rechnung",
            "storage_path": "rechnungen/company1",
        }

    def test_null_ids_return_none(self):
        paperless._correspondent_cache = {}
        paperless._document_type_cache = {}
        paperless._storage_path_cache = {}

        result = paperless._resolve_document({
            "id": 1, "title": "Test", "created": None,
            "correspondent": None, "document_type": None, "storage_path": None,
        })

        assert result["correspondent"] is None
        assert result["document_type"] is None
        assert result["storage_path"] is None
        assert result["created"] is None

    def test_unknown_id_returns_none(self):
        paperless._correspondent_cache = {1: "Known"}
        paperless._document_type_cache = {}
        paperless._storage_path_cache = {}

        result = paperless._resolve_document({
            "id": 1, "title": "Test", "created": "2030-06-01",
            "correspondent": 999, "document_type": None, "storage_path": None,
        })

        assert result["correspondent"] is None


# ── _ensure_caches ───────────────────────────────────────────────


class TestEnsureCaches:
    @pytest.mark.asyncio
    async def test_fetches_all_three_caches(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"results": [{"id": 1, "name": "Test"}], "next": None}
        mock_resp.raise_for_status = MagicMock()

        client = AsyncMock()
        client.get = AsyncMock(return_value=mock_resp)

        await paperless._ensure_caches(client)

        assert client.get.call_count == 3
        assert paperless._correspondent_cache == {1: "Test"}
        assert paperless._document_type_cache == {1: "Test"}

    @pytest.mark.asyncio
    async def test_skips_when_already_populated(self):
        paperless._correspondent_cache = {1: "Cached"}
        paperless._document_type_cache = {2: "Cached"}
        paperless._storage_path_cache = {3: "Cached"}

        client = AsyncMock()
        await paperless._ensure_caches(client)

        client.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_dict_not_refetched(self):
        """Empty dict (no correspondents) should not trigger re-fetch."""
        paperless._correspondent_cache = {}
        paperless._document_type_cache = {}
        paperless._storage_path_cache = {}

        client = AsyncMock()
        await paperless._ensure_caches(client)

        client.get.assert_not_called()


# ── search_documents ─────────────────────────────────────────────


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

        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "count": 2,
            "next": None,
            "results": [
                {"id": 10, "title": "COMPANY 1 RE 2030-01", "correspondent": 1,
                 "document_type": 1, "storage_path": 1},
                {"id": 11, "title": "COMPANY 1 RE 2030-02", "correspondent": 1,
                 "document_type": 1, "storage_path": None},
            ],
        }
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.get = AsyncMock(return_value=mock_resp)
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = instance

            result = await paperless.search_documents("COMPANY 1 Rechnung")

        assert result["count"] == 2
        assert result["page"] == 1
        assert result["page_size"] == 25
        assert len(result["results"]) == 2
        assert result["results"][0]["correspondent"] == "COMPANY 1"
        assert result["results"][0]["document_type"] == "Rechnung"
        assert result["results"][1]["storage_path"] is None

    @pytest.mark.asyncio
    async def test_page_size_clamped(self):
        """page_size should be clamped to 1-100."""
        paperless._correspondent_cache = {}
        paperless._document_type_cache = {}
        paperless._storage_path_cache = {}

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"count": 0, "next": None, "results": []}
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.get = AsyncMock(return_value=mock_resp)
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = instance

            result = await paperless.search_documents("test", page_size=200)

        assert result["page_size"] == 100


# ── Response Size ────────────────────────────────────────────────


class TestSearchDocumentsOrdering:
    @pytest.mark.asyncio
    async def test_default_ordering_is_newest_first(self):
        """Default ordering sends -created to the API."""
        paperless._correspondent_cache = {}
        paperless._document_type_cache = {}
        paperless._storage_path_cache = {}

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"count": 0, "next": None, "results": []}
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.get = AsyncMock(return_value=mock_resp)
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = instance

            await paperless.search_documents("test")

        call_kwargs = instance.get.call_args
        params = call_kwargs.kwargs.get("params") or call_kwargs[1].get("params")
        assert params["ordering"] == "-created"

    @pytest.mark.asyncio
    async def test_custom_ordering_forwarded(self):
        """Custom ordering parameter is forwarded to API."""
        paperless._correspondent_cache = {}
        paperless._document_type_cache = {}
        paperless._storage_path_cache = {}

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"count": 0, "next": None, "results": []}
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.get = AsyncMock(return_value=mock_resp)
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = instance

            await paperless.search_documents("test", ordering="title")

        call_kwargs = instance.get.call_args
        params = call_kwargs.kwargs.get("params") or call_kwargs[1].get("params")
        assert params["ordering"] == "title"

    @pytest.mark.asyncio
    async def test_created_field_in_results(self):
        """Results include the created date field."""
        paperless._correspondent_cache = {}
        paperless._document_type_cache = {}
        paperless._storage_path_cache = {}

        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "count": 1,
            "next": None,
            "results": [
                {"id": 1, "title": "Invoice", "created": "2030-06-15",
                 "correspondent": None, "document_type": None, "storage_path": None},
            ],
        }
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.get = AsyncMock(return_value=mock_resp)
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = instance

            result = await paperless.search_documents("Invoice")

        assert result["results"][0]["created"] == "2030-06-15"


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


# ── Response Size ────────────────────────────────────────────────


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
    def test_25_results_under_10kb(self):
        """25 results with resolved names must fit under 10KB."""
        results = []
        for i in range(25):
            results.append({
                "id": 1700 + i,
                "title": f"2030_{i:02d}_02 RE COMPANY 1 IN_10015{i}23473",
                "created": "2030-01-15",
                "correspondent": "COMPANY 1",
                "document_type": "Rechnung",
                "storage_path": "rechnungen/company1/2030",
            })
        response = {"count": 53, "page": 1, "page_size": 25, "results": results}
        size = len(json.dumps(response).encode("utf-8"))
        assert size < 10240, f"Response is {size} bytes, exceeds 10KB"
