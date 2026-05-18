import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fastapi.testclient import TestClient

from main import build_app, get_all_doc_files


SAMPLE_INDEX = """Welcome to Sample Docs
======================

This is the main index page.

See the API reference for Browser and Tab details.
"""

SAMPLE_API = """API Reference
=============

.. autoclass:: samplepkg.api.Browser
   :members:
   :undoc-members:
   :exclude-members: _hidden

.. autoclass:: samplepkg.api.Element
   :members:
   :undoc-members:

.. automodule:: samplepkg.api
   :members: connect, Tab, Element

Usage Example
-------------

.. code-block:: python

   browser = Browser.create(headless=False)
   tab = browser.open_tab("https://example.com")
"""

SAMPLE_BROKEN = """Broken API
==========

.. autoclass:: samplepkg.api.MissingBrowser
"""

SAMPLE_QUICKSTART = """Quickstart Guide
================

Installation
------------

Install samplepkg with pip.

Usage Example
-------------

.. code-block:: python

   browser = Browser.create(headless=False)
   element = browser.open_tab("https://example.com").find("h1")
"""

PACKAGE_INIT = '''"""Sample package used by tests."""

from .api import Browser, Element, Tab, connect

__all__ = ["Browser", "Element", "Tab", "connect"]
__version__ = "1.2.3"
'''

PACKAGE_API = '''from __future__ import annotations


class Element:
    """Represents a DOM element."""

    def __init__(self, selector: str) -> None:
        self.selector = selector

    @property
    def tab(self) -> str:
        """'"""
        return self.selector


class Tab:
    """Represents a :class:`Browser` tab."""

    def __init__(self, url: str) -> None:
        self.url = url

    def find(self, selector: str) -> Element:
        """Find an element in the current tab."""
        return Element(selector)


class Browser:
    """High-level :class:`Browser` controller."""

    def __init__(self, headless: bool = True) -> None:
        self.headless = headless
        self._tabs = [Tab("about:blank")]

    @classmethod
    def create(cls, headless: bool = True) -> "Browser":
        """Create a :class:`Browser` instance."""
        return cls(headless=headless)

    def open_tab(self, url: str) -> Tab:
        """Open a new tab and return it."""
        tab = Tab(url)
        self._tabs.append(tab)
        return tab

    @property
    def tabs(self) -> list[Tab]:
        """Currently attached tabs."""
        return list(self._tabs)

    def _hidden(self) -> str:
        """Internal helper not intended for docs."""
        return "secret"


def connect(endpoint: str) -> Browser:
    """Connect to a remote :class:`Browser` endpoint."""
    return Browser.create(headless=False)
'''

PYPROJECT = '''[project]
name = "samplepkg-docs"
version = "1.2.3"
requires-python = ">=3.10"
'''


def build_docs_tree(tmp_path: Path) -> Path:
    package_dir = tmp_path / "samplepkg"
    package_dir.mkdir()
    (package_dir / "__init__.py").write_text(PACKAGE_INIT, encoding="utf-8")
    (package_dir / "api.py").write_text(PACKAGE_API, encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8")

    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "index.rst").write_text(SAMPLE_INDEX, encoding="utf-8")
    (docs_dir / "api.rst").write_text(SAMPLE_API, encoding="utf-8")
    (docs_dir / "broken.rst").write_text(SAMPLE_BROKEN, encoding="utf-8")
    nested_dir = docs_dir / "guides"
    nested_dir.mkdir()
    (nested_dir / "quickstart.rst").write_text(SAMPLE_QUICKSTART, encoding="utf-8")
    (docs_dir / "ignore.md").write_text("This should be ignored.", encoding="utf-8")
    return docs_dir


def test_get_all_doc_files_only_returns_rst_files(tmp_path: Path) -> None:
    docs_dir = build_docs_tree(tmp_path)

    doc_map = get_all_doc_files(docs_dir)

    assert sorted(doc_map) == ["api.rst", "broken.rst", "guides/quickstart.rst", "index.rst"]


def test_toc_lists_available_documents(tmp_path: Path) -> None:
    client = TestClient(build_app(build_docs_tree(tmp_path)))

    response = client.get("/docs/toc")

    assert response.status_code == 200
    payload = response.json()
    assert [item["path"] for item in payload] == ["api.rst", "broken.rst", "guides/quickstart.rst", "index.rst"]
    assert payload[0]["title"] == "API Reference"
    assert payload[0]["symbols"] >= 4
    assert payload[0]["anchors"] >= 2


def test_view_document_supports_rendered_and_structured_autodoc_formats(tmp_path: Path) -> None:
    client = TestClient(build_app(build_docs_tree(tmp_path)))

    raw_response = client.get("/docs/view", params={"file_path": "api.rst"})
    text_response = client.get(
        "/docs/view",
        params={"file_path": "api.rst", "content_format": "text", "line_limit": 5},
    )
    rendered_response = client.get(
        "/docs/view",
        params={"file_path": "api.rst", "content_format": "rendered"},
    )
    structured_response = client.get(
        "/docs/view",
        params={"file_path": "api.rst", "content_format": "structured"},
    )

    assert raw_response.status_code == 200
    assert ".. autoclass:: samplepkg.api.Browser" in raw_response.json()["content"]

    assert text_response.status_code == 200
    text_payload = text_response.json()
    assert text_payload["content_format"] == "text"
    assert text_payload["line_start"] == 1
    assert text_payload["line_end"] == 5
    assert "Browser [class]" in text_payload["content"]

    assert rendered_response.status_code == 200
    rendered_payload = rendered_response.json()
    assert rendered_payload["content_format"] == "rendered"
    assert "Signature: Browser(" in rendered_payload["content"]
    assert "open_tab(" in rendered_payload["content"]
    assert all("Unknown directive" not in line for line in rendered_payload["content"].splitlines())
    assert any(anchor["anchor"].endswith("browser") for anchor in rendered_payload["anchors"])
    assert rendered_payload["code_blocks"]

    assert structured_response.status_code == 200
    structured_payload = structured_response.json()
    assert structured_payload["content"] is None
    assert structured_payload["structured"]
    entries = structured_payload["structured"]["entries"]
    assert any(entry["target"] == "samplepkg.api.Browser" for entry in entries)
    flattened_symbols = structured_payload["structured"]["symbols"]
    assert any(symbol["qualname"] == "samplepkg.api.Browser.create" for symbol in flattened_symbols)
    assert structured_payload["metadata"]["project_version"] == "1.2.3"


def test_import_failure_snippets_are_clean_and_human_readable(tmp_path: Path) -> None:
    client = TestClient(build_app(build_docs_tree(tmp_path)))

    response = client.get(
        "/docs/view",
        params={"file_path": "broken.rst", "content_format": "rendered"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert "Autodoc import failed: Attribute 'MissingBrowser' not found while resolving 'samplepkg.api.MissingBrowser'." in payload["content"]
    assert "<module" not in payload["content"]
    assert "object at 0x" not in payload["content"]


def test_view_document_returns_not_found_for_unknown_path(tmp_path: Path) -> None:
    client = TestClient(build_app(build_docs_tree(tmp_path)))

    response = client.get("/docs/view", params={"file_path": "missing.rst"})

    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


def test_search_supports_exact_symbol_lookup_and_path_prefix_filtering(tmp_path: Path) -> None:
    client = TestClient(build_app(build_docs_tree(tmp_path)))

    exact_symbol_response = client.get(
        "/docs/search",
        params={"query": "Browser.create", "exact_symbol": True},
    )
    filtered_response = client.get(
        "/docs/search",
        params={"query": "Browser.create", "path_prefix": "guides/"},
    )

    assert exact_symbol_response.status_code == 200
    exact_payload = exact_symbol_response.json()
    assert exact_payload
    assert exact_payload[0]["path"] == "api.rst"
    assert exact_payload[0]["exact_symbol_match"] is True
    assert exact_payload[0]["matched_symbols"] == ["samplepkg.api.Browser.create"]
    assert exact_payload[0]["anchor"].endswith("create")

    assert filtered_response.status_code == 200
    filtered_payload = filtered_response.json()
    assert filtered_payload
    assert all(item["path"].startswith("guides/") for item in filtered_payload)
    assert filtered_payload[0]["exact_symbol_match"] is False

    short_name_response = client.get(
        "/docs/search",
        params={"query": "Tab", "exact_symbol": True},
    )

    assert short_name_response.status_code == 200
    short_payload = short_name_response.json()
    assert short_payload
    assert short_payload[0]["matched_symbols"] == ["samplepkg.api.Tab"]
    assert short_payload[0]["anchor"].endswith("tab")
    assert short_payload[0]["line_start"] <= short_payload[0]["line_end"]
    assert all(result["matched_symbols"] != ["samplepkg.api.Element.tab"] for result in short_payload[:1])


def test_search_demotes_autodoc_import_failure_noise(tmp_path: Path) -> None:
    client = TestClient(build_app(build_docs_tree(tmp_path)))

    response = client.get("/docs/search", params={"query": "Browser"})

    assert response.status_code == 200
    payload = response.json()
    assert payload
    assert payload[0]["path"] == "api.rst"
    assert "Autodoc import failed" not in payload[0]["snippet"]
    if any(item["path"] == "broken.rst" for item in payload):
        broken_index = next(index for index, item in enumerate(payload) if item["path"] == "broken.rst")
        assert broken_index > 0


def test_search_results_include_anchor_line_ranges_and_code_blocks(tmp_path: Path) -> None:
    client = TestClient(build_app(build_docs_tree(tmp_path)))

    response = client.get("/docs/search", params={"query": "browser.open_tab"})

    assert response.status_code == 200
    payload = response.json()
    assert payload
    top = payload[0]
    assert top["anchor"]
    assert top["line_start"] <= top["line_end"]
    assert top["snippet"]
    assert top["code_blocks"]
    assert any("Browser.create" in block["content"] for block in top["code_blocks"])


def test_metadata_and_members_endpoint_expose_normalized_api(tmp_path: Path) -> None:
    client = TestClient(build_app(build_docs_tree(tmp_path)))

    metadata_response = client.get("/docs/metadata")
    members_response = client.get("/docs/members", params={"symbol": "Browser"})

    assert metadata_response.status_code == 200
    metadata = metadata_response.json()
    assert metadata["project_name"] == "samplepkg-docs"
    assert metadata["project_version"] == "1.2.3"
    assert metadata["python_compatibility"] == ">=3.10"

    assert members_response.status_code == 200
    members_payload = members_response.json()
    assert members_payload["resolved_symbol"] == "samplepkg.api.Browser"
    assert members_payload["owner_kind"] == "class"
    names = {member["name"] for member in members_payload["members"]}
    assert {"create", "open_tab", "tabs"}.issubset(names)
    assert "_hidden" not in names
    open_tab = next(member for member in members_payload["members"] if member["name"] == "open_tab")
    assert "url" in open_tab["signature"]
    assert "'" not in open_tab["signature"]
    assert open_tab["anchor"].endswith("open-tab")
    assert open_tab["line_start"] <= open_tab["line_end"]
    assert ":class:" not in members_payload["owner_summary"]

    tab_members_response = client.get("/docs/members", params={"symbol": "Tab"})

    assert tab_members_response.status_code == 200
    tab_payload = tab_members_response.json()
    assert tab_payload["resolved_symbol"] == "samplepkg.api.Tab"
    assert tab_payload["owner_kind"] == "class"
    assert tab_payload["path"] == "api.rst"
    assert tab_payload["anchor"].endswith("tab")
    assert tab_payload["line_start"] <= tab_payload["line_end"]
    assert ":class:" not in tab_payload["owner_summary"]
    find_member = next(member for member in tab_payload["members"] if member["name"] == "find")
    assert "selector: str" in find_member["signature"]
    assert "'" not in find_member["signature"]

    element_members_response = client.get("/docs/members", params={"symbol": "Element"})

    assert element_members_response.status_code == 200
    element_payload = element_members_response.json()
    tab_property = next(member for member in element_payload["members"] if member["name"] == "tab")
    assert tab_property["summary"] is None
    assert tab_property["anchor"].endswith("tab")
    assert tab_property["line_start"] <= tab_property["line_end"]


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
    assert "/docs/metadata" in schema["paths"]
    assert "/docs/members" in schema["paths"]
    search_parameters = {parameter["name"] for parameter in schema["paths"]["/docs/search"]["get"]["parameters"]}
    assert {"query", "limit", "path_prefix", "exact_symbol"}.issubset(search_parameters)

    assert docs_response.status_code == 200
    assert "Swagger UI" in docs_response.text

    assert health_response.status_code == 200
    health_payload = health_response.json()
    assert health_payload["indexed_files"] == 4
    assert health_payload["metadata"]["project_version"] == "1.2.3"

