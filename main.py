from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Dict, Iterable, List, Sequence

import uvicorn
from docutils.core import publish_doctree
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field


APP_TITLE = "Github.io Documentation API"
APP_DESCRIPTION = (
	"Expose a local documentation tree as OpenAPI-compatible endpoints so LLMs can "
	"browse, search, and read only the relevant parts of the docs."
)
APP_VERSION = "0.1.0"
SUPPORTED_EXTENSIONS = {".rst"}


class ContentFormat(str, Enum):
	raw = "raw"
	text = "text"


class DocFile(BaseModel):
	path: str = Field(description="Path of the document relative to the configured docs root.")
	title: str = Field(description="Best-effort title extracted from the document.")
	source_format: str = Field(description="Underlying documentation source format.")


class DocContent(BaseModel):
	path: str
	title: str
	source_format: str
	content_format: ContentFormat
	content: str
	total_lines: int
	line_start: int
	line_end: int


class SearchResult(BaseModel):
	path: str
	title: str
	snippet: str
	matches: int


class HealthResponse(BaseModel):
	status: str
	docs_root: str | None
	indexed_files: int


@dataclass(slots=True)
class DocumentRecord:
	path: str
	title: str
	source_path: Path
	source_format: str
	raw_content: str
	text_content: str


def normalize_docs_root(docs_dir: Path) -> Path:
	return docs_dir.expanduser().resolve()


def get_all_doc_files(docs_dir: Path) -> Dict[str, Path]:
	"""Crawl the docs directory and return supported documentation files."""
	docs_root = normalize_docs_root(docs_dir)
	if not docs_root.exists() or not docs_root.is_dir():
		return {}

	doc_map: Dict[str, Path] = {}
	for file_path in sorted(docs_root.rglob("*")):
		if file_path.is_file() and file_path.suffix.lower() in SUPPORTED_EXTENSIONS:
			relative_key = file_path.relative_to(docs_root).as_posix()
			doc_map[relative_key] = file_path
	return doc_map


def extract_rst_text(content: str) -> str:
	try:
		text = publish_doctree(content).astext()
	except Exception:
		text = content

	text = text.replace("\r\n", "\n")
	text = re.sub(r"\n{3,}", "\n\n", text)
	return text.strip()


def extract_title(content: str, fallback: str) -> str:
	text = extract_rst_text(content)
	for line in text.splitlines():
		candidate = line.strip()
		if candidate:
			return candidate[:200]
	return fallback


def build_snippet(content: str, query: str, radius: int = 180) -> str:
	normalized_content = re.sub(r"\s+", " ", content).strip()
	if not normalized_content:
		return ""

	query_lower = query.lower()
	content_lower = normalized_content.lower()
	index = content_lower.find(query_lower)
	if index == -1:
		terms = [term for term in re.split(r"\s+", query_lower) if term]
		for term in terms:
			index = content_lower.find(term)
			if index != -1:
				break

	if index == -1:
		snippet = normalized_content[: radius * 2]
		return snippet + ("..." if len(normalized_content) > len(snippet) else "")

	start = max(0, index - radius)
	end = min(len(normalized_content), index + max(len(query), 1) + radius)
	snippet = normalized_content[start:end].strip()
	prefix = "..." if start > 0 else ""
	suffix = "..." if end < len(normalized_content) else ""
	return f"{prefix}{snippet}{suffix}"


class DocumentationLibrary:
	def __init__(self, docs_root: Path):
		self.docs_root = normalize_docs_root(docs_root)
		self.records: Dict[str, DocumentRecord] = {}
		self.refresh()

	def refresh(self) -> None:
		records: Dict[str, DocumentRecord] = {}
		for relative_path, source_path in get_all_doc_files(self.docs_root).items():
			raw_content = source_path.read_text(encoding="utf-8", errors="replace")
			text_content = extract_rst_text(raw_content)
			title = extract_title(raw_content, fallback=source_path.stem.replace("_", " ").title())
			records[relative_path] = DocumentRecord(
				path=relative_path,
				title=title,
				source_path=source_path,
				source_format=source_path.suffix.lower().lstrip("."),
				raw_content=raw_content,
				text_content=text_content,
			)
		self.records = records

	def toc(self) -> List[DocFile]:
		return [
			DocFile(path=record.path, title=record.title, source_format=record.source_format)
			for record in self.records.values()
		]

	def get_record(self, file_path: str) -> DocumentRecord | None:
		return self.records.get(file_path)

	def search(self, query: str, limit: int = 20) -> List[SearchResult]:
		query = query.strip()
		if not query:
			return []

		terms = [term for term in re.split(r"\s+", query.lower()) if term]
		results: list[tuple[int, SearchResult]] = []

		for record in self.records.values():
			haystack = record.text_content.lower()
			phrase_matches = haystack.count(query.lower())
			term_matches = sum(haystack.count(term) for term in terms)

			if phrase_matches == 0 and (not terms or not all(term in haystack for term in terms)):
				continue

			score = phrase_matches * 10 + term_matches
			results.append(
				(
					score,
					SearchResult(
						path=record.path,
						title=record.title,
						snippet=build_snippet(record.text_content, query),
						matches=max(phrase_matches, term_matches),
					),
				)
			)

		results.sort(key=lambda item: (-item[0], item[1].path))
		return [result for _, result in results[:limit]]


def build_app(docs_dir: Path | None = None) -> FastAPI:
	app = FastAPI(title=APP_TITLE, description=APP_DESCRIPTION, version=APP_VERSION)
	app.state.library = DocumentationLibrary(docs_dir) if docs_dir else None

	@app.get("/", include_in_schema=False)
	async def root() -> RedirectResponse:
		return RedirectResponse(url="/docs")

	@app.get("/health", response_model=HealthResponse, summary="Health Check")
	async def health(request: Request) -> HealthResponse:
		library = request.app.state.library
		return HealthResponse(
			status="ok",
			docs_root=str(library.docs_root) if library else None,
			indexed_files=len(library.records) if library else 0,
		)

	def require_library(request: Request) -> DocumentationLibrary:
		library = request.app.state.library
		if library is None:
			raise HTTPException(
				status_code=500,
				detail="Documentation directory is not configured. Start the server with a docs path.",
			)
		if not library.records:
			raise HTTPException(
				status_code=500,
				detail="Documentation directory is empty or no supported .rst files were found.",
			)
		return library

	@app.get("/docs/toc", response_model=List[DocFile], summary="Get Documentation Table of Contents")
	async def get_toc(request: Request) -> List[DocFile]:
		"""Return all available documentation files for the configured docs tree."""
		library = require_library(request)
		return library.toc()

	@app.get("/docs/view", response_model=DocContent, summary="Read a Specific Documentation File")
	async def view_doc(
		request: Request,
		file_path: str = Query(..., description="Relative path from the docs root, e.g. 'index.rst'."),
		content_format: ContentFormat = Query(
			default=ContentFormat.raw,
			description="Return either the original source text or normalized plain text.",
		),
		line_start: int = Query(1, ge=1, description="1-based starting line number to return."),
		line_limit: int | None = Query(
			None,
			ge=1,
			le=5000,
			description="Optional maximum number of lines to return.",
		),
	) -> DocContent:
		"""Return a specific documentation file in raw or normalized text form."""
		library = require_library(request)
		record = library.get_record(file_path)
		if record is None:
			raise HTTPException(status_code=404, detail=f"Documentation file '{file_path}' not found.")

		source = record.raw_content if content_format == ContentFormat.raw else record.text_content
		lines = source.splitlines()
		total_lines = len(lines)
		if total_lines and line_start > total_lines:
			raise HTTPException(
				status_code=416,
				detail=f"line_start {line_start} exceeds document length of {total_lines} lines.",
			)

		start_index = line_start - 1
		end_index = total_lines if line_limit is None else min(total_lines, start_index + line_limit)
		selected_lines = lines[start_index:end_index]
		line_end = start_index + len(selected_lines)

		return DocContent(
			path=record.path,
			title=record.title,
			source_format=record.source_format,
			content_format=content_format,
			content="\n".join(selected_lines),
			total_lines=total_lines,
			line_start=line_start,
			line_end=line_end,
		)

	@app.get("/docs/search", response_model=List[SearchResult], summary="Search Across All Documentation")
	async def search_docs(
		request: Request,
		query: str = Query(..., min_length=1, description="Keyword or phrase to search for."),
		limit: int = Query(20, ge=1, le=100, description="Maximum number of matches to return."),
	) -> List[SearchResult]:
		"""Search the documentation corpus and return ranked snippets."""
		library = require_library(request)
		return library.search(query=query, limit=limit)

	return app


app = build_app()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
	parser = argparse.ArgumentParser(
		description="Serve a local .rst documentation tree as an OpenAPI-compatible API."
	)
	parser.add_argument("docs_dir", type=Path, help="Path to the local documentation directory.")
	parser.add_argument("--host", default="127.0.0.1", help="Host interface to bind. Default: 127.0.0.1")
	parser.add_argument("--port", type=int, default=7771, help="Port to bind. Default: 7771")
	return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
	args = parse_args(argv)
	docs_dir = normalize_docs_root(args.docs_dir)
	if not docs_dir.exists() or not docs_dir.is_dir():
		raise SystemExit(f"Documentation directory does not exist or is not a directory: {docs_dir}")

	configured_app = build_app(docs_dir)
	uvicorn.run(configured_app, host=args.host, port=args.port)
	return 0


if __name__ == "__main__":
	raise SystemExit(main())

