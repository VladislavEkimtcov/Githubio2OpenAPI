import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fastapi.testclient import TestClient

from main import build_app, get_all_doc_files


SAMPLE_INDEX = """Welcome to Nodriver
===================

This is the main index page.

See the quickstart guide for installation details.
"""

SAMPLE_QUICKSTART = """Quickstart Guide
================

Installation
------------

Install nodriver with pip.

Usage Example
-------------

Use Browser.create() to launch a browser.
"""


def build_docs_tree(tmp_path: Path) -> Path:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "index.rst").write_text(SAMPLE_INDEX, encoding="utf-8")
    nested_dir = docs_dir / "nodriver"
    nested_dir.mkdir()
    (nested_dir / "quickstart.rst").write_text(SAMPLE_QUICKSTART, encoding="utf-8")
    (docs_dir / "ignore.md").write_text("This should be ignored.", encoding="utf-8")
    return docs_dir


def test_get_all_doc_files_only_returns_rst_files(tmp_path: Path) -> None:
    docs_dir = build_docs_tree(tmp_path)

    doc_map = get_all_doc_files(docs_dir)

    assert sorted(doc_map) == ["index.rst", "nodriver/quickstart.rst"]


def test_toc_lists_available_documents(tmp_path: Path) -> None:
    client = TestClient(build_app(build_docs_tree(tmp_path)))

    response = client.get("/docs/toc")

    assert response.status_code == 200
    payload = response.json()
    assert [item["path"] for item in payload] == ["index.rst", "nodriver/quickstart.rst"]
    assert payload[0]["title"] == "Welcome to Nodriver"


def test_view_document_supports_raw_and_text_formats(tmp_path: Path) -> None:
    client = TestClient(build_app(build_docs_tree(tmp_path)))

    raw_response = client.get("/docs/view", params={"file_path": "index.rst"})
    text_response = client.get(
        "/docs/view",
        params={"file_path": "index.rst", "content_format": "text", "line_limit": 2},
    )

    assert raw_response.status_code == 200
    assert "===================" in raw_response.json()["content"]

    assert text_response.status_code == 200
    text_payload = text_response.json()
    assert text_payload["content_format"] == "text"
    assert text_payload["line_start"] == 1
    assert text_payload["line_end"] == 2
    assert "Welcome to Nodriver" in text_payload["content"]


def test_view_document_returns_not_found_for_unknown_path(tmp_path: Path) -> None:
    client = TestClient(build_app(build_docs_tree(tmp_path)))

    response = client.get("/docs/view", params={"file_path": "missing.rst"})

    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


def test_search_is_case_insensitive_and_ranked(tmp_path: Path) -> None:
    client = TestClient(build_app(build_docs_tree(tmp_path)))

    response = client.get("/docs/search", params={"query": "browser.create"})

    assert response.status_code == 200
    payload = response.json()
    assert payload
    assert payload[0]["path"] == "nodriver/quickstart.rst"
    assert payload[0]["matches"] >= 1
    assert "Browser.create()" in payload[0]["snippet"]


def test_openapi_and_swagger_ui_are_available(tmp_path: Path) -> None:
    client = TestClient(build_app(build_docs_tree(tmp_path)))

    openapi_response = client.get("/openapi.json")
    docs_response = client.get("/docs")
    health_response = client.get("/health")

    assert openapi_response.status_code == 200
    schema = openapi_response.json()
    assert "/docs/toc" in schema["paths"]
    assert "/docs/view" in schema["paths"]
    assert "/docs/search" in schema["paths"]

    assert docs_response.status_code == 200
    assert "Swagger UI" in docs_response.text

    assert health_response.status_code == 200
    assert health_response.json()["indexed_files"] == 2

