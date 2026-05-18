# Githubio2OpenAPI

Wrap local documentation repositories into OpenAPI-compatible endpoints.

This project serves a local documentation tree as a small FastAPI service so an LLM can:

- discover available documents
- search across them
- fetch only the file or line range it needs
- inspect autodoc pages as rendered or structured API data
- discover normalized class/module members without runtime scraping

The service focuses on Sphinx-style `.rst` documentation and now includes autodoc-aware rendering for directives such as `autoclass` and `automodule`.

## Features

- CLI entrypoint: `python main.py --port=7771 /path/to/docs`
- OpenAPI schema at `/openapi.json`
- Swagger UI at `/docs`
- Documentation TOC endpoint at `/docs/toc`
- Document reader endpoint at `/docs/view`
- Full-text and exact-symbol search endpoint at `/docs/search`
- Project metadata endpoint at `/docs/metadata`
- Normalized members endpoint at `/docs/members`
- Health endpoint at `/health`

## Install

```bash
python3 -m pip install -r requirements.txt
```

## Run

Example with the local nodriver docs tree:

```bash
python3 main.py --port=7771 /Users/ekimtco2/PycharmProjects/nodriver/docs
```

Then open:

- `http://127.0.0.1:7771/docs` for Swagger UI
- `http://127.0.0.1:7771/openapi.json` for the OpenAPI schema

## API Overview

### `GET /docs/toc`

Returns the list of available `.rst` documents with titles, anchor counts, and discovered symbol counts.

### `GET /docs/view`

Query parameters:

- `file_path` – relative path from the docs root
- `content_format` – `raw`, `text`, `rendered`, or `structured`
- `line_start` – optional 1-based start line
- `line_limit` – optional maximum number of lines to return

`rendered` expands autodoc directives into stable human-readable sections with signatures, methods, and properties.

`structured` returns flattened symbols and autodoc entries that tools can consume directly.

### `GET /docs/search`

Query parameters:

- `query` – word, phrase, or symbol to search for
- `limit` – optional maximum results
- `path_prefix` – optional relative path prefix filter
- `exact_symbol` – set to `true` to match only normalized symbols such as `Browser.create`

Search results include anchors, line ranges, matched symbols, and nearby extracted code blocks.

Exact-symbol queries also resolve documented short names such as `Browser`, `Tab`, or `Element` when they are unambiguous in the indexed docs.

### `GET /docs/metadata`

Returns best-effort project metadata such as project version, installed package version, source commit, and Python compatibility.

### `GET /docs/members`

Query parameters:

- `symbol` – class or module symbol to inspect, for example `nodriver.Browser`

Returns normalized members with stable kinds, signatures, summaries, anchors, and line ranges so external tooling can avoid runtime introspection fallback.

Short-name lookups such as `Browser` and `Tab` are supported when those symbols can be resolved from the rendered documentation corpus.

## Test

```bash
pytest
```
