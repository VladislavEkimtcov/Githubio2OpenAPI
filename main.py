from __future__ import annotations

import argparse
import configparser
import contextlib
import importlib
import importlib.metadata
import inspect
import re
import subprocess
import sys
import textwrap
import tomllib
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Sequence

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
APP_VERSION = "0.2.0"
SUPPORTED_EXTENSIONS = {".rst"}
HEADING_CHARS = set("=-~^\"#*+`")
AUTODOC_DIRECTIVE_RE = re.compile(
	r"^\.\.\s+(auto(?:class|module|function|method|attribute|property|data))::\s*(?P<target>[^\n]+?)\s*$"
)
CODE_BLOCK_DIRECTIVE_RE = re.compile(r"^\.\.\s+code(?:-block)?::\s*(?P<language>[\w.+-]+)?\s*$")
INLINE_ROLE_RE = re.compile(r":[a-zA-Z0-9_.-]+:`~?([^`]+)`")
LINK_RE = re.compile(r"`([^`<]+?)\s*<[^>]+>`_")
SIMPLE_LINK_RE = re.compile(r"`([^`]+)`_")
DOUBLE_BACKTICK_RE = re.compile(r"``([^`]+)``")
STRONG_RE = re.compile(r"\*\*([^*]+)\*\*")
EMPHASIS_RE = re.compile(r"(?<!\*)\*([^*]+)\*(?!\*)")
QUOTED_ANNOTATION_RE = re.compile(r"(:\s*|->\s*)(['\"])([^'\"]+)\2")


class ContentFormat(str, Enum):
	raw = "raw"
	text = "text"
	rendered = "rendered"
	structured = "structured"


class DocAnchor(BaseModel):
	anchor: str = Field(description="Stable anchor for a section or symbol within a rendered document.")
	title: str = Field(description="Human-readable heading or symbol title for the anchor.")
	line_start: int = Field(description="1-based starting line within the rendered document.")
	line_end: int = Field(description="1-based ending line within the rendered document.")


class CodeBlock(BaseModel):
	language: str | None = Field(default=None, description="Language name extracted from a code-block directive.")
	content: str = Field(description="Code block body with indentation normalized.")
	line_start: int = Field(description="1-based start line in the rendered document.")
	line_end: int = Field(description="1-based end line in the rendered document.")


class NormalizedMember(BaseModel):
	name: str = Field(description="Short member name.")
	qualname: str = Field(description="Fully qualified member name when available.")
	kind: str = Field(description="Normalized member kind such as class, method, property, function, or module.")
	signature: str | None = Field(default=None, description="Call signature or constructor signature when available.")
	summary: str | None = Field(default=None, description="First sentence or first line of the member docstring.")
	module: str | None = Field(default=None, description="Owning Python module for the member.")
	owner: str | None = Field(default=None, description="Owning class or module symbol, if applicable.")
	anchor: str | None = Field(default=None, description="Stable anchor for linking to the member in rendered docs.")
	line_start: int | None = Field(default=None, description="Rendered-document start line for the member.")
	line_end: int | None = Field(default=None, description="Rendered-document end line for the member.")


class AutodocEntry(BaseModel):
	target: str = Field(description="Autodoc target, typically a fully-qualified Python symbol.")
	kind: str = Field(description="Resolved object kind such as class, module, or function.")
	title: str = Field(description="Display title for the autodoc entry.")
	anchor: str = Field(description="Stable anchor for the autodoc entry.")
	signature: str | None = Field(default=None, description="Resolved signature for the entry when available.")
	summary: str | None = Field(default=None, description="Resolved docstring summary when available.")
	line_start: int | None = Field(default=None, description="Rendered-document start line for the entry.")
	line_end: int | None = Field(default=None, description="Rendered-document end line for the entry.")
	members: List[NormalizedMember] = Field(default_factory=list, description="Normalized members exposed by the entry.")


class StructuredDocument(BaseModel):
	entries: List[AutodocEntry] = Field(default_factory=list, description="Autodoc-derived structured entries found in the document.")
	symbols: List[NormalizedMember] = Field(default_factory=list, description="Flattened normalized symbols, including owners and members.")


class ProjectMetadata(BaseModel):
	project_name: str | None = Field(default=None, description="Project name when discoverable from packaging metadata.")
	project_version: str | None = Field(default=None, description="Project version from source metadata such as pyproject.toml.")
	package_version: str | None = Field(default=None, description="Installed distribution version when available.")
	source_commit: str | None = Field(default=None, description="Git commit SHA for the nearest repository, if available.")
	python_compatibility: str | None = Field(default=None, description="Python version requirement or compatibility marker.")
	metadata_sources: List[str] = Field(default_factory=list, description="Files or sources used to populate metadata.")


class DocFile(BaseModel):
	path: str = Field(description="Path of the document relative to the configured docs root.")
	title: str = Field(description="Best-effort title extracted from the document.")
	source_format: str = Field(description="Underlying documentation source format.")
	anchors: int = Field(description="Number of rendered anchors discovered in the document.")
	symbols: int = Field(description="Number of normalized symbols discovered in the document.")


class DocContent(BaseModel):
	path: str
	title: str
	source_format: str
	content_format: ContentFormat
	content: str | None = None
	total_lines: int
	line_start: int
	line_end: int
	anchors: List[DocAnchor] = Field(default_factory=list)
	code_blocks: List[CodeBlock] = Field(default_factory=list)
	structured: StructuredDocument | None = None
	metadata: ProjectMetadata | None = None


class SearchResult(BaseModel):
	path: str
	title: str
	snippet: str
	matches: int
	anchor: str | None = None
	line_start: int | None = None
	line_end: int | None = None
	code_blocks: List[CodeBlock] = Field(default_factory=list)
	matched_symbols: List[str] = Field(default_factory=list)
	exact_symbol_match: bool = False


class HealthResponse(BaseModel):
	status: str
	docs_root: str | None
	indexed_files: int
	metadata: ProjectMetadata | None = None


class MembersResponse(BaseModel):
	query: str
	resolved_symbol: str
	owner_kind: str
	owner_signature: str | None = None
	owner_summary: str | None = None
	path: str | None = None
	anchor: str | None = None
	line_start: int | None = None
	line_end: int | None = None
	members: List[NormalizedMember] = Field(default_factory=list)
	metadata: ProjectMetadata | None = None


@dataclass(slots=True)
class DocumentRecord:
	path: str
	title: str
	source_path: Path
	source_format: str
	raw_content: str
	text_content: str
	rendered_content: str
	rendered_lines: List[str]
	anchors: List[DocAnchor]
	code_blocks: List[CodeBlock]
	structured: StructuredDocument


@dataclass(slots=True)
class SymbolLocation:
	record: DocumentRecord
	member: NormalizedMember
	entry: AutodocEntry | None


def normalize_docs_root(docs_dir: Path) -> Path:
	return docs_dir.expanduser().resolve()


def unique_paths(paths: Iterable[Path]) -> list[Path]:
	seen: set[Path] = set()
	unique: list[Path] = []
	for path in paths:
		resolved = path.expanduser().resolve()
		if resolved not in seen:
			seen.add(resolved)
			unique.append(resolved)
	return unique


@contextlib.contextmanager
def temporary_sys_path(paths: Iterable[Path]) -> Iterator[None]:
	added: list[str] = []
	for path in reversed(unique_paths(paths)):
		path_str = str(path)
		if path_str not in sys.path:
			sys.path.insert(0, path_str)
			added.append(path_str)
	try:
		yield
	finally:
		for path_str in added:
			with contextlib.suppress(ValueError):
				sys.path.remove(path_str)


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


def clean_inline_markup(line: str) -> str:
	cleaned = line.rstrip("\n")
	cleaned = INLINE_ROLE_RE.sub(r"\1", cleaned)
	cleaned = LINK_RE.sub(r"\1", cleaned)
	cleaned = SIMPLE_LINK_RE.sub(r"\1", cleaned)
	cleaned = DOUBLE_BACKTICK_RE.sub(r"\1", cleaned)
	cleaned = STRONG_RE.sub(r"\1", cleaned)
	cleaned = EMPHASIS_RE.sub(r"\1", cleaned)
	return cleaned


def sanitize_extracted_text(text: str | None, *, preserve_newlines: bool = False) -> str | None:
	if text is None:
		return None

	cleaned = clean_inline_markup(text.replace("\r\n", "\n"))
	if preserve_newlines:
		cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
		cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
	else:
		cleaned = re.sub(r"\s*\n\s*", " ", cleaned)
	cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
	cleaned = cleaned.strip()
	if not cleaned:
		return None
	if re.fullmatch(r"['\"`]+", cleaned):
		return None
	if not re.search(r"[A-Za-z0-9]", cleaned) and re.search(r"['\"`.,:;!?()\[\]{}\-_/\\]+", cleaned):
		return None
	return cleaned


def is_import_failure_text(text: str | None) -> bool:
	if not text:
		return False
	normalized = sanitize_extracted_text(text) or ""
	return normalized.startswith("Autodoc import failed:") or normalized.startswith("Unable to resolve '") or "not found while resolving" in normalized


def is_noise_summary(text: str | None) -> bool:
	if not text:
		return True
	normalized = sanitize_extracted_text(text)
	if not normalized:
		return True
	return is_import_failure_text(normalized)


def extract_title(content: str, fallback: str) -> str:
	text = extract_rst_text(content)
	for line in text.splitlines():
		candidate = line.strip()
		if candidate:
			return candidate[:200]
	return fallback


def first_nonempty_line(text: str | None) -> str | None:
	if not text:
		return None
	for line in text.splitlines():
		candidate = sanitize_extracted_text(line)
		if candidate:
			return candidate[:300]
	return None


def slugify(value: str) -> str:
	slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower())
	return slug.strip("-") or "section"


def safe_signature(obj: Any) -> str | None:
	for eval_str in (True, False):
		try:
			signature = str(inspect.signature(obj, eval_str=eval_str))
		except (NameError, TypeError, ValueError):
			continue
		cleaned = sanitize_extracted_text(signature)
		if cleaned:
			cleaned = QUOTED_ANNOTATION_RE.sub(r"\1\3", cleaned)
		return cleaned
	return None


def safe_doc_summary(obj: Any) -> str | None:
	try:
		summary = sanitize_extracted_text(first_nonempty_line(inspect.getdoc(obj)))
		return None if is_noise_summary(summary) else summary
	except Exception:
		return None


def object_kind(obj: Any) -> str:
	if inspect.ismodule(obj):
		return "module"
	if inspect.isclass(obj):
		return "class"
	if isinstance(obj, property):
		return "property"
	if inspect.ismethod(obj) or inspect.isfunction(obj) or inspect.isbuiltin(obj) or inspect.ismethoddescriptor(obj):
		return "function"
	return "attribute"


def normalize_member_name(qualname: str) -> str:
	return qualname.rsplit(".", 1)[-1]


def set_line_range(model: Any, start: int, end: int) -> None:
	model.line_start = start
	model.line_end = end


def build_symbol_keys(symbol: str) -> list[str]:
	parts = [part for part in symbol.split(".") if part]
	keys: list[str] = []
	for index in range(len(parts)):
		keys.append(".".join(parts[index:]).lower())
	return keys or [symbol.lower()]


def format_import_error(target: str, *, missing_attribute: str | None = None) -> str:
	if missing_attribute:
		return f"Attribute '{sanitize_extracted_text(missing_attribute) or missing_attribute}' not found while resolving '{target}'."
	return f"Unable to resolve '{target}' as an importable symbol."


def parse_option_members(value: str | bool | None) -> set[str] | None:
	if value is None:
		return None
	if value is True or value == "":
		return None
	return {item.strip() for item in str(value).split(",") if item.strip()}


def option_enabled(options: dict[str, str | bool], name: str) -> bool:
	return name in options


def should_include_member(
	name: str,
	options: dict[str, str | bool],
	*,
	has_docstring: bool,
	members_requested: bool,
) -> bool:
	if not members_requested:
		return False

	excluded = parse_option_members(options.get("exclude-members")) or set()
	if name in excluded:
		return False

	specified_members = parse_option_members(options.get("members"))
	if specified_members is not None:
		return name in specified_members

	is_special = name.startswith("__") and name.endswith("__")
	is_private = name.startswith("_") and not is_special
	if is_special:
		special_members = parse_option_members(options.get("special-members"))
		return option_enabled(options, "special-members") and (special_members is None or name in special_members)
	if is_private and not option_enabled(options, "private-members"):
		return False
	if not has_docstring and not option_enabled(options, "undoc-members"):
		return False
	return True


def normalize_class_member(owner: type[Any], name: str, value: Any, kind: str) -> NormalizedMember:
	module_name = getattr(owner, "__module__", None)
	qualname = f"{owner.__module__}.{owner.__qualname__}.{name}" if module_name else f"{owner.__qualname__}.{name}"
	member_kind = {
		"method": "method",
		"class method": "classmethod",
		"static method": "staticmethod",
		"property": "property",
		"data": "attribute",
	}.get(kind, kind.replace(" ", "-"))

	signature_source = value
	if isinstance(value, staticmethod):
		signature_source = value.__func__
	elif isinstance(value, classmethod):
		signature_source = value.__func__
	elif isinstance(value, property):
		signature_source = value.fget

	return NormalizedMember(
		name=name,
		qualname=qualname,
		kind=member_kind,
		signature=safe_signature(signature_source) if member_kind not in {"property", "attribute"} else None,
		summary=safe_doc_summary(signature_source),
		module=module_name,
		owner=f"{owner.__module__}.{owner.__qualname__}",
	)


def collect_class_members(owner: type[Any], options: dict[str, str | bool]) -> list[NormalizedMember]:
	if not any(key in options for key in {"members", "private-members", "special-members"}):
		return []

	include_inherited = option_enabled(options, "inherited-members")
	members: list[NormalizedMember] = []
	for attr in inspect.classify_class_attrs(owner):
		if attr.name in {"__dict__", "__weakref__", "__module__", "__doc__"}:
			continue
		if not include_inherited and attr.defining_class is not owner:
			continue
		try:
			static_value = inspect.getattr_static(attr.defining_class, attr.name)
		except AttributeError:
			static_value = attr.object
		has_docstring = bool(safe_doc_summary(static_value))
		if not should_include_member(
			attr.name,
			options,
			has_docstring=has_docstring,
			members_requested=True,
		):
			continue
		members.append(normalize_class_member(owner, attr.name, static_value, attr.kind))

	members.sort(key=lambda member: member.name)
	return members


def normalize_module_member(owner: Any, name: str, value: Any) -> NormalizedMember:
	kind = object_kind(value)
	module_name = getattr(value, "__module__", owner.__name__) if not inspect.ismodule(value) else value.__name__
	qualname = getattr(value, "__qualname__", name)
	fully_qualified = f"{module_name}.{qualname}" if module_name and not inspect.ismodule(value) else f"{owner.__name__}.{name}"
	return NormalizedMember(
		name=name,
		qualname=fully_qualified,
		kind=kind,
		signature=safe_signature(value) if kind in {"class", "function"} else None,
		summary=safe_doc_summary(value),
		module=module_name,
		owner=owner.__name__,
	)


def collect_module_members(owner: Any, options: dict[str, str | bool]) -> list[NormalizedMember]:
	if not any(key in options for key in {"members", "private-members", "special-members"}):
		return []

	members: list[NormalizedMember] = []
	for name, value in inspect.getmembers(owner):
		if name in {"__builtins__", "__cached__", "__doc__", "__file__", "__loader__", "__package__", "__spec__"}:
			continue
		if inspect.ismodule(value):
			continue
		if inspect.isroutine(value) or inspect.isclass(value):
			module_name = getattr(value, "__module__", None)
			if module_name and module_name != owner.__name__ and not option_enabled(options, "imported-members"):
				continue
		has_docstring = bool(safe_doc_summary(value))
		if not should_include_member(name, options, has_docstring=has_docstring, members_requested=True):
			continue
		members.append(normalize_module_member(owner, name, value))

	members.sort(key=lambda member: member.name)
	return members


def resolve_import_target(target: str, search_roots: Iterable[Path]) -> tuple[Any | None, str | None]:
	parts = [part for part in target.split(".") if part]
	with temporary_sys_path(search_roots):
		for index in range(len(parts), 0, -1):
			module_name = ".".join(parts[:index])
			try:
				module = importlib.import_module(module_name)
			except Exception:
				continue

			current: Any = module
			for attr in parts[index:]:
				try:
					current = getattr(current, attr)
				except AttributeError:
					return None, format_import_error(target, missing_attribute=attr)
			return current, None
	return None, format_import_error(target)


def wrap_summary(summary: str | None) -> list[str]:
	if not summary:
		return []
	cleaned = sanitize_extracted_text(summary)
	if not cleaned or is_noise_summary(cleaned):
		return []
	return textwrap.wrap(cleaned, width=100) or [cleaned]


def build_owner_member(target: str, obj: Any) -> NormalizedMember:
	kind = object_kind(obj)
	module_name = obj.__name__ if inspect.ismodule(obj) else getattr(obj, "__module__", None)
	return NormalizedMember(
		name=normalize_member_name(target),
		qualname=target,
		kind=kind,
		signature=safe_signature(obj) if kind in {"class", "function"} else None,
		summary=safe_doc_summary(obj),
		module=module_name,
		owner=getattr(obj, "__module__", None) if kind != "module" else None,
		anchor=slugify(target),
	)


def build_member_line(member: NormalizedMember) -> str:
	display = member.name
	if member.signature:
		display = f"{display}{member.signature}"
	label = f"- {display} [{member.kind}]"
	if member.summary:
		label = f"{label} — {member.summary}"
	return label


def build_autodoc_entry(target: str, directive: str, options: dict[str, str | bool], search_roots: Iterable[Path]) -> tuple[AutodocEntry, NormalizedMember, list[NormalizedMember], list[str]]:
	resolved, error = resolve_import_target(target, search_roots)
	if resolved is None:
		failure = sanitize_extracted_text(error) or format_import_error(target)
		entry = AutodocEntry(
			target=target,
			kind=directive.removeprefix("auto"),
			title=normalize_member_name(target),
			anchor=slugify(target),
			summary=failure,
		)
		owner_member = NormalizedMember(
			name=normalize_member_name(target),
			qualname=target,
			kind=entry.kind,
			summary=failure,
			anchor=entry.anchor,
		)
		lines = [f"{entry.title} [{entry.kind}]", f"Target: {target}", f"Autodoc import failed: {failure}"]
		return entry, owner_member, [], lines

	owner_member = build_owner_member(target, resolved)
	entry = AutodocEntry(
		target=target,
		kind=owner_member.kind,
		title=owner_member.name,
		anchor=owner_member.anchor or slugify(target),
		signature=owner_member.signature,
		summary=owner_member.summary,
	)

	if inspect.isclass(resolved):
		members = collect_class_members(resolved, options)
	elif inspect.ismodule(resolved):
		members = collect_module_members(resolved, options)
	else:
		members = []

	entry.members = members
	lines = [f"{entry.title} [{entry.kind}]", f"Target: {target}"]
	if entry.signature:
		lines.append(f"Signature: {entry.title}{sanitize_extracted_text(entry.signature) or entry.signature}")
	lines.extend(wrap_summary(entry.summary))
	if entry.members:
		lines.append("Members:")
		for member in entry.members:
			lines.append(build_member_line(member))
	return entry, owner_member, members, lines


def heading_level(line: str, underline: str) -> bool:
	stripped_line = line.strip()
	stripped_underline = underline.strip()
	return bool(
		stripped_line
		and stripped_underline
		and len(stripped_underline) >= len(stripped_line)
		and len(set(stripped_underline)) == 1
		and stripped_underline[0] in HEADING_CHARS
	)


def parse_directive_options(lines: list[str], start_index: int) -> tuple[dict[str, str | bool], int]:
	options: dict[str, str | bool] = {}
	directive_indent = len(lines[start_index]) - len(lines[start_index].lstrip())
	index = start_index + 1
	while index < len(lines):
		current = lines[index]
		if not current.strip():
			index += 1
			continue
		current_indent = len(current) - len(current.lstrip())
		if current_indent <= directive_indent:
			break
		option_match = re.match(r"\s*:([a-zA-Z0-9_-]+):\s*(.*)$", current)
		if option_match:
			name, value = option_match.groups()
			options[name] = value if value else True
			index += 1
			continue
		break
	return options, index


def parse_code_block(lines: list[str], start_index: int) -> tuple[CodeBlock | None, list[str], int]:
	match = CODE_BLOCK_DIRECTIVE_RE.match(lines[start_index].strip())
	if not match:
		return None, [], start_index + 1

	language = match.group("language") or None
	directive_indent = len(lines[start_index]) - len(lines[start_index].lstrip())
	index = start_index + 1
	while index < len(lines) and (not lines[index].strip() or re.match(r"\s*:[a-zA-Z0-9_-]+:", lines[index])):
		index += 1

	block_lines: list[str] = []
	while index < len(lines):
		line = lines[index]
		if not line.strip():
			block_lines.append("")
			index += 1
			continue
		indent = len(line) - len(line.lstrip())
		if indent <= directive_indent:
			break
		block_lines.append(line[indent:])
		index += 1

	while block_lines and not block_lines[-1].strip():
		block_lines.pop()

	if not block_lines:
		return None, [], index

	rendered = [f"```{language or ''}".rstrip(), *block_lines, "```"]
	return CodeBlock(language=language, content="\n".join(block_lines), line_start=0, line_end=0), rendered, index


def find_anchor_for_line(anchors: list[DocAnchor], line_number: int) -> DocAnchor | None:
	anchor: DocAnchor | None = None
	for candidate in anchors:
		if candidate.line_start <= line_number:
			anchor = candidate
		else:
			break
	return anchor


def finalize_anchor_ranges(anchors: list[DocAnchor], total_lines: int) -> None:
	for index, anchor in enumerate(anchors):
		next_start = anchors[index + 1].line_start if index + 1 < len(anchors) else total_lines + 1
		anchor.line_end = max(anchor.line_start, next_start - 1)


def apply_anchor_ranges_to_members(
	*,
	anchors: list[DocAnchor],
	entries: list[AutodocEntry],
	symbols: list[NormalizedMember],
) -> None:
	anchor_map = {anchor.anchor: anchor for anchor in anchors}
	for entry in entries:
		if entry.anchor in anchor_map:
			anchor = anchor_map[entry.anchor]
			entry.line_start = anchor.line_start
			entry.line_end = anchor.line_end
	for symbol in symbols:
		if symbol.anchor and symbol.anchor in anchor_map:
			anchor = anchor_map[symbol.anchor]
			symbol.line_start = anchor.line_start
			symbol.line_end = anchor.line_end


def build_line_snippet(lines: list[str], center_line: int, radius: int = 2) -> tuple[str, int, int]:
	if not lines:
		return "", 0, 0
	index = min(max(center_line - 1, 0), len(lines) - 1)
	start = max(0, index - radius)
	end = min(len(lines), index + radius + 1)
	snippet = "\n".join(line.rstrip() for line in lines[start:end]).strip()
	return snippet, start + 1, end


def project_file_candidates(docs_root: Path) -> list[Path]:
	parents = [docs_root, *docs_root.parents[:6]]
	return unique_paths(parents)


def discover_source_commit(candidates: Iterable[Path]) -> str | None:
	for candidate in candidates:
		git_dir = candidate / ".git"
		if not git_dir.exists():
			continue
		result = subprocess.run(
			["git", "-C", str(candidate), "rev-parse", "HEAD"],
			capture_output=True,
			text=True,
			check=False,
		)
		commit = result.stdout.strip()
		if result.returncode == 0 and commit:
			return commit
	return None


def discover_metadata(docs_root: Path) -> ProjectMetadata:
	metadata = ProjectMetadata()
	for candidate in project_file_candidates(docs_root):
		pyproject_path = candidate / "pyproject.toml"
		if pyproject_path.is_file():
			try:
				data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
			except Exception:
				data = {}
			project_data = data.get("project", {}) if isinstance(data, dict) else {}
			if not metadata.project_name:
				metadata.project_name = project_data.get("name")
			if not metadata.project_version:
				metadata.project_version = project_data.get("version")
			if not metadata.python_compatibility:
				metadata.python_compatibility = project_data.get("requires-python")
			metadata.metadata_sources.append(str(pyproject_path))
			break

	for candidate in project_file_candidates(docs_root):
		setup_cfg_path = candidate / "setup.cfg"
		if not setup_cfg_path.is_file():
			continue
		parser = configparser.ConfigParser()
		try:
			parser.read(setup_cfg_path, encoding="utf-8")
		except Exception:
			continue
		if parser.has_section("metadata"):
			if not metadata.project_name:
				metadata.project_name = parser.get("metadata", "name", fallback=None)
			if not metadata.project_version:
				metadata.project_version = parser.get("metadata", "version", fallback=None)
		if parser.has_section("options") and not metadata.python_compatibility:
			metadata.python_compatibility = parser.get("options", "python_requires", fallback=None)
		metadata.metadata_sources.append(str(setup_cfg_path))
		break

	if metadata.project_name and not metadata.package_version:
		for candidate_name in {metadata.project_name, metadata.project_name.replace("_", "-"), metadata.project_name.replace("-", "_")}:
			with contextlib.suppress(importlib.metadata.PackageNotFoundError):
				metadata.package_version = importlib.metadata.version(candidate_name)
				break

	metadata.source_commit = discover_source_commit(project_file_candidates(docs_root))
	if not metadata.metadata_sources:
		metadata.metadata_sources = []
	return metadata


def build_document_record(
	*,
	path: str,
	source_path: Path,
	raw_content: str,
	title: str,
	search_roots: Iterable[Path],
) -> DocumentRecord:
	raw_lines = raw_content.replace("\r\n", "\n").splitlines()
	rendered_lines: list[str] = []
	anchors: list[DocAnchor] = []
	code_blocks: list[CodeBlock] = []
	entries: list[AutodocEntry] = []
	symbols: list[NormalizedMember] = []
	index = 0
	while index < len(raw_lines):
		line = raw_lines[index]
		stripped = line.strip()

		if index + 1 < len(raw_lines) and heading_level(line, raw_lines[index + 1]):
			heading = clean_inline_markup(stripped)
			anchor = DocAnchor(
				anchor=slugify(f"{path}-{heading}"),
				title=heading,
				line_start=len(rendered_lines) + 1,
				line_end=len(rendered_lines) + 1,
			)
			anchors.append(anchor)
			rendered_lines.append(heading)
			rendered_lines.append("")
			index += 2
			continue

		autodoc_match = AUTODOC_DIRECTIVE_RE.match(stripped)
		if autodoc_match:
			options, next_index = parse_directive_options(raw_lines, index)
			entry, owner_member, members, block_lines = build_autodoc_entry(
				target=autodoc_match.group("target"),
				directive=autodoc_match.group(1),
				options=options,
				search_roots=search_roots,
			)
			block_start = len(rendered_lines) + 1
			rendered_lines.extend(block_lines)
			rendered_lines.append("")
			block_end = len(rendered_lines) - 1
			set_line_range(entry, block_start, max(block_start, block_end))
			set_line_range(owner_member, block_start, block_start + max(0, min(len(block_lines), 3) - 1))
			anchors.append(DocAnchor(anchor=entry.anchor, title=entry.title, line_start=block_start, line_end=max(block_start, block_end)))
			offset = block_start
			member_line = offset + len(block_lines) - len(members)
			for member in members:
				member.anchor = f"{entry.anchor}-{slugify(member.name)}"
				set_line_range(member, member_line, member_line)
				anchors.append(DocAnchor(anchor=member.anchor, title=f"{entry.title}.{member.name}", line_start=member_line, line_end=member_line))
				member_line += 1
			entries.append(entry)
			symbols.append(owner_member)
			symbols.extend(members)
			index = next_index
			continue

		code_block, rendered_block, next_index = parse_code_block(raw_lines, index)
		if code_block is not None:
			block_start = len(rendered_lines) + 1
			rendered_lines.extend(rendered_block)
			rendered_lines.append("")
			code_block.line_start = block_start + 1
			code_block.line_end = block_start + len(rendered_block) - 2
			code_blocks.append(code_block)
			index = next_index
			continue

		rendered_lines.append(clean_inline_markup(line))
		index += 1

	while rendered_lines and not rendered_lines[-1].strip():
		rendered_lines.pop()

	total_lines = len(rendered_lines)
	finalize_anchor_ranges(anchors, total_lines)
	apply_anchor_ranges_to_members(anchors=anchors, entries=entries, symbols=symbols)
	rendered_content = "\n".join(rendered_lines).strip()
	text_content = extract_rst_text(rendered_content)
	structured = StructuredDocument(entries=entries, symbols=symbols)
	return DocumentRecord(
		path=path,
		title=title,
		source_path=source_path,
		source_format=source_path.suffix.lower().lstrip("."),
		raw_content=raw_content,
		text_content=text_content,
		rendered_content=rendered_content,
		rendered_lines=rendered_lines,
		anchors=anchors,
		code_blocks=code_blocks,
		structured=structured,
	)


class DocumentationLibrary:
	def __init__(self, docs_root: Path):
		self.docs_root = normalize_docs_root(docs_root)
		self.search_roots = unique_paths([self.docs_root.parent, *self.docs_root.parents[:3]])
		self.metadata = discover_metadata(self.docs_root)
		self.records: Dict[str, DocumentRecord] = {}
		self.symbol_index: Dict[str, list[SymbolLocation]] = {}
		self.refresh()

	def refresh(self) -> None:
		records: Dict[str, DocumentRecord] = {}
		for relative_path, source_path in get_all_doc_files(self.docs_root).items():
			raw_content = source_path.read_text(encoding="utf-8", errors="replace")
			title = extract_title(raw_content, fallback=source_path.stem.replace("_", " ").title())
			records[relative_path] = build_document_record(
				path=relative_path,
				source_path=source_path,
				raw_content=raw_content,
				title=title,
				search_roots=self.search_roots,
			)
		self.records = records
		self.symbol_index = {}
		for record in self.records.values():
			for entry in record.structured.entries:
				for symbol in [member for member in record.structured.symbols if member.qualname == entry.target] + entry.members:
					location = SymbolLocation(record=record, member=symbol, entry=entry)
					for key in build_symbol_keys(symbol.qualname):
						self.symbol_index.setdefault(key, []).append(location)
					self.symbol_index.setdefault(symbol.name.lower(), []).append(location)

	def _candidate_locations(self, query: str, *, owner_only: bool = False) -> list[SymbolLocation]:
		query = query.strip()
		if not query:
			return []

		query_lower = query.lower()
		seen: set[tuple[str, str, str | None]] = set()
		candidates: list[SymbolLocation] = []
		for key in [query_lower, *build_symbol_keys(query)]:
			for location in self.symbol_index.get(key, []):
				candidate_key = (location.record.path, location.member.qualname, location.member.anchor)
				if candidate_key in seen:
					continue
				seen.add(candidate_key)
				candidates.append(location)

		if owner_only:
			candidates = [candidate for candidate in candidates if candidate.member.kind in {"class", "module"}]

		def priority(location: SymbolLocation) -> tuple[int, int, int, int, str, str]:
			qualname = location.member.qualname.lower()
			name = location.member.name.lower()
			is_short_query = "." not in query_lower
			if qualname == query_lower:
				match_rank = 0
			elif name == query_lower:
				match_rank = 1
			elif query_lower in build_symbol_keys(location.member.qualname):
				match_rank = 2
			else:
				match_rank = 3
			owner_rank = 0 if location.member.kind in {"class", "module"} else 1
			short_name_owner_rank = 0 if is_short_query and name == query_lower and location.member.kind in {"class", "module"} else 1
			dedicated_entry_rank = 0 if location.entry and location.entry.target.lower() == qualname else 1
			anchor_rank = 0 if location.member.anchor else 1
			return (
				match_rank,
				short_name_owner_rank,
				owner_rank,
				dedicated_entry_rank,
				anchor_rank,
				location.record.path,
				location.member.qualname,
			)

		candidates.sort(key=priority)
		return candidates

	def _best_documented_location(self, query: str, *, owner_only: bool = False) -> SymbolLocation | None:
		locations = self._candidate_locations(query, owner_only=owner_only)
		return locations[0] if locations else None

	def _apply_documented_member_metadata(self, members: list[NormalizedMember]) -> list[NormalizedMember]:
		enriched: list[NormalizedMember] = []
		for member in members:
			copy = member.model_copy(deep=True)
			location = self._best_documented_location(copy.qualname)
			if location and location.member.qualname.lower() == copy.qualname.lower():
				if location.member.anchor:
					copy.anchor = location.member.anchor
				copy.line_start = location.member.line_start
				copy.line_end = location.member.line_end
				copy.summary = location.member.summary or copy.summary
				copy.signature = location.member.signature or copy.signature
			enriched.append(copy)
		return enriched

	def toc(self) -> List[DocFile]:
		return [
			DocFile(
				path=record.path,
				title=record.title,
				source_format=record.source_format,
				anchors=len(record.anchors),
				symbols=len(record.structured.symbols),
			)
			for record in self.records.values()
		]

	def get_record(self, file_path: str) -> DocumentRecord | None:
		return self.records.get(file_path)

	def find_member_owner(self, query: str) -> tuple[DocumentRecord | None, AutodocEntry | None, list[NormalizedMember]]:
		location = self._best_documented_location(query, owner_only=True)
		if location is not None:
			if location.entry and location.entry.target.lower() == location.member.qualname.lower():
				return location.record, location.entry, location.entry.members
			return location.record, None, []

		query_lower = query.strip().lower()
		for locations in [self.symbol_index.get(query_lower, [])]:
			for location in locations:
				if location.entry and location.entry.target.lower() == query_lower:
					return location.record, location.entry, location.entry.members
		for record in self.records.values():
			for entry in record.structured.entries:
				if query_lower in {entry.target.lower(), entry.title.lower(), entry.anchor.lower()}:
					return record, entry, entry.members
				short_target = normalize_member_name(entry.target).lower()
				if short_target == query_lower:
					return record, entry, entry.members
		return None, None, []

	def members_for_symbol(self, query: str) -> MembersResponse:
		owner_location = self._best_documented_location(query, owner_only=True)
		if owner_location is not None:
			if owner_location.entry and owner_location.entry.target.lower() == owner_location.member.qualname.lower():
				members = [member.model_copy(deep=True) for member in owner_location.entry.members]
			else:
				resolved, error = resolve_import_target(owner_location.member.qualname, self.search_roots)
				if resolved is None:
					raise HTTPException(status_code=404, detail=error or f"Symbol '{query}' not found.")
				if inspect.isclass(resolved):
					members = collect_class_members(resolved, {"members": True, "undoc-members": True})
				elif inspect.ismodule(resolved):
					members = collect_module_members(resolved, {"members": True, "undoc-members": True})
				else:
					members = []
				members = self._apply_documented_member_metadata(members)

			return MembersResponse(
				query=query,
				resolved_symbol=owner_location.member.qualname,
				owner_kind=owner_location.member.kind,
				owner_signature=owner_location.member.signature,
				owner_summary=owner_location.member.summary,
				path=owner_location.record.path,
				anchor=owner_location.member.anchor,
				line_start=owner_location.member.line_start,
				line_end=owner_location.member.line_end,
				members=members,
				metadata=self.metadata,
			)

		record, entry, members = self.find_member_owner(query)
		if entry is not None:
			return MembersResponse(
				query=query,
				resolved_symbol=entry.target,
				owner_kind=entry.kind,
				owner_signature=entry.signature,
				owner_summary=entry.summary,
				path=record.path if record else None,
				anchor=entry.anchor,
				line_start=entry.line_start,
				line_end=entry.line_end,
				members=members,
				metadata=self.metadata,
			)

		resolved, error = resolve_import_target(query, self.search_roots)
		if resolved is None:
			raise HTTPException(status_code=404, detail=error or f"Symbol '{query}' not found.")
		owner_member = build_owner_member(query, resolved)
		if inspect.isclass(resolved):
			members = collect_class_members(resolved, {"members": True, "undoc-members": True})
		elif inspect.ismodule(resolved):
			members = collect_module_members(resolved, {"members": True, "undoc-members": True})
		else:
			members = []
		members = self._apply_documented_member_metadata(members)
		return MembersResponse(
			query=query,
			resolved_symbol=owner_member.qualname,
			owner_kind=owner_member.kind,
			owner_signature=owner_member.signature,
			owner_summary=owner_member.summary,
			anchor=owner_member.anchor,
			line_start=owner_member.line_start,
			line_end=owner_member.line_end,
			members=members,
			metadata=self.metadata,
		)

	def _record_matches_path_prefix(self, record: DocumentRecord, path_prefix: str | None) -> bool:
		if not path_prefix:
			return True
		return record.path.startswith(path_prefix.strip())

	def _nearby_code_blocks(self, record: DocumentRecord, line_number: int) -> list[CodeBlock]:
		matches = [
			block
			for block in record.code_blocks
			if block.line_start - 6 <= line_number <= block.line_end + 6
		]
		return matches[:2]

	def _search_text_record(self, record: DocumentRecord, query: str, terms: list[str]) -> tuple[int, SearchResult] | None:
		query_lower = query.lower()
		lines = record.rendered_lines or record.text_content.splitlines()
		matching_lines: list[int] = []
		for index, line in enumerate(lines, start=1):
			line_lower = line.lower()
			if query_lower in line_lower or (terms and all(term in line_lower for term in terms)):
				matching_lines.append(index)
		if not matching_lines:
			return None

		non_failure_matching_lines = [line_number for line_number in matching_lines if not is_import_failure_text(lines[line_number - 1])]
		best_line = non_failure_matching_lines[0] if non_failure_matching_lines else matching_lines[0]
		matched_symbols = [
			symbol.qualname
			for symbol in record.structured.symbols
			if query_lower in symbol.qualname.lower() or (symbol.signature and query_lower in symbol.signature.lower())
		]
		matched_symbols = [symbol for symbol in matched_symbols if symbol]
		snippet, line_start, line_end = build_line_snippet(lines, best_line)
		anchor = find_anchor_for_line(record.anchors, best_line)
		phrase_matches = record.rendered_content.lower().count(query_lower)
		term_matches = sum(record.rendered_content.lower().count(term) for term in terms)
		failure_penalty = 0
		if any(is_import_failure_text(lines[line_number - 1]) for line_number in matching_lines):
			failure_penalty += 12
		if is_import_failure_text(snippet):
			failure_penalty += 8
		if any(is_import_failure_text(symbol.summary) for symbol in record.structured.symbols):
			failure_penalty += 6
		score = phrase_matches * 10 + term_matches + len(matched_symbols) * 4 - failure_penalty
		return (
			score,
			SearchResult(
				path=record.path,
				title=record.title,
				snippet=snippet,
				matches=max(len(matching_lines), phrase_matches, term_matches, len(matched_symbols)),
				anchor=anchor.anchor if anchor else None,
				line_start=line_start,
				line_end=line_end,
				code_blocks=self._nearby_code_blocks(record, best_line),
				matched_symbols=matched_symbols[:10],
				exact_symbol_match=False,
			),
		)

	def search(
		self,
		query: str,
		*,
		limit: int = 20,
		path_prefix: str | None = None,
		exact_symbol: bool = False,
	) -> List[SearchResult]:
		query = query.strip()
		if not query:
			return []

		if exact_symbol:
			results: list[SearchResult] = []
			seen: set[tuple[str, str | None]] = set()
			for location in self._candidate_locations(query):
				if not self._record_matches_path_prefix(location.record, path_prefix):
					continue
				key = (location.record.path, location.member.anchor)
				if key in seen:
					continue
				seen.add(key)
				line_number = location.member.line_start or (location.entry.line_start if location.entry else 1) or 1
				snippet, snippet_start, snippet_end = build_line_snippet(location.record.rendered_lines, line_number, radius=1)
				results.append(
					SearchResult(
						path=location.record.path,
						title=location.record.title,
						snippet=snippet,
						matches=1,
						anchor=location.member.anchor or (location.entry.anchor if location.entry else None),
						line_start=location.member.line_start or snippet_start,
						line_end=location.member.line_end or snippet_end,
						code_blocks=self._nearby_code_blocks(location.record, line_number),
						matched_symbols=[location.member.qualname],
						exact_symbol_match=True,
					)
				)
			return results[:limit]

		terms = [term for term in re.split(r"\s+", query.lower()) if term]
		ranked: list[tuple[int, SearchResult]] = []
		for record in self.records.values():
			if not self._record_matches_path_prefix(record, path_prefix):
				continue
			result = self._search_text_record(record, query, terms)
			if result is not None:
				ranked.append(result)

		ranked.sort(key=lambda item: (-item[0], item[1].path, item[1].anchor or ""))
		return [result for _, result in ranked[:limit]]


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
			metadata=library.metadata if library else None,
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

	@app.get("/docs/metadata", response_model=ProjectMetadata, summary="Get Project Metadata")
	async def get_metadata(request: Request) -> ProjectMetadata:
		library = require_library(request)
		return library.metadata

	@app.get("/docs/view", response_model=DocContent, summary="Read a Specific Documentation File")
	async def view_doc(
		request: Request,
		file_path: str = Query(..., description="Relative path from the docs root, e.g. 'index.rst'."),
		content_format: ContentFormat = Query(
			default=ContentFormat.raw,
			description="Return original source, normalized text, rendered autodoc-aware text, or structured symbol data.",
		),
		line_start: int = Query(1, ge=1, description="1-based starting line number to return."),
		line_limit: int | None = Query(
			None,
			ge=1,
			le=5000,
			description="Optional maximum number of lines to return.",
		),
	) -> DocContent:
		"""Return a specific documentation file in raw, text, rendered, or structured form."""
		library = require_library(request)
		record = library.get_record(file_path)
		if record is None:
			raise HTTPException(status_code=404, detail=f"Documentation file '{file_path}' not found.")

		if content_format == ContentFormat.raw:
			source = record.raw_content
		elif content_format == ContentFormat.text:
			source = record.text_content
		else:
			source = record.rendered_content

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
			content=None if content_format == ContentFormat.structured else "\n".join(selected_lines),
			total_lines=total_lines,
			line_start=line_start,
			line_end=line_end,
			anchors=record.anchors,
			code_blocks=record.code_blocks,
			structured=record.structured if content_format in {ContentFormat.rendered, ContentFormat.structured} else None,
			metadata=library.metadata,
		)

	@app.get("/docs/search", response_model=List[SearchResult], summary="Search Across All Documentation")
	async def search_docs(
		request: Request,
		query: str = Query(..., min_length=1, description="Keyword, phrase, or exact symbol to search for."),
		limit: int = Query(20, ge=1, le=100, description="Maximum number of matches to return."),
		path_prefix: str | None = Query(
			default=None,
			description="Optional relative path prefix to scope the search, such as 'api/' or 'guides/'.",
		),
		exact_symbol: bool = Query(
			default=False,
			description="When true, only exact normalized symbol matches are returned.",
		),
	) -> List[SearchResult]:
		"""Search the documentation corpus and return ranked snippets with anchors and line ranges."""
		library = require_library(request)
		return library.search(query=query, limit=limit, path_prefix=path_prefix, exact_symbol=exact_symbol)

	@app.get("/docs/members", response_model=MembersResponse, summary="Get Normalized API Members")
	async def get_members(
		request: Request,
		symbol: str = Query(..., min_length=1, description="Class or module symbol to inspect, e.g. 'nodriver.Browser'."),
	) -> MembersResponse:
		"""Return normalized members for a class or module so tooling can rely on stable signatures."""
		library = require_library(request)
		return library.members_for_symbol(symbol)

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

