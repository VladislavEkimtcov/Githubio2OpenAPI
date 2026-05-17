# Githubio2OpenAPI

Wrap local documentation repositories into OpenAPI-compatible endpoints.

This project serves a local documentation tree as a small FastAPI service so an LLM can:

- discover available documents
- search across them
- fetch only the file or line range it needs

The current MVP focuses on Sphinx-style `.rst` documentation such as the local `nodriver/docs` tree.

## Features

- CLI entrypoint: `python main.py --port=7771 /path/to/docs`
- OpenAPI schema at `/openapi.json`
- Swagger UI at `/docs`
- Documentation TOC endpoint at `/docs/toc`
- Document reader endpoint at `/docs/view`
- Full-text search endpoint at `/docs/search`
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

Returns the list of available `.rst` documents with titles.

### `GET /docs/view`

Query parameters:

- `file_path` – relative path from the docs root
- `content_format` – `raw` or `text`
- `line_start` – optional 1-based start line
- `line_limit` – optional maximum number of lines to return

### `GET /docs/search`

Query parameters:

- `query` – word or phrase to search for
- `limit` – optional maximum results

## Test

```bash
pytest
```
