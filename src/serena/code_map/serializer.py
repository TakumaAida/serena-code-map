"""
Serialization of a CodeMap to the static on-disk representation under `.serena/code-map/`.

All output is deterministic: stable sort orders, sorted JSON keys, LF line endings,
no timestamps in overview/module files, and no absolute paths. Files are only rewritten
when their content actually changed (write-if-changed), and each file replacement is atomic,
so a failed export leaves the previous code map intact.
"""

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from serena.code_map.model import CodeMap
from serena.code_map.overview import DEFAULT_OVERVIEW_MAX_CHARS, module_markdown_path, render_module_markdown, render_overview

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
GENERATOR_NAME = "serena-code-map"
UPSTREAM_BASELINE = "ac256f36309dd01153389eb3828ae08d2ab9d705"

AGENTS_SNIPPET = """# Serena Code Map Instructions

At the start of repository work, read `.serena/code-map/overview.md`.
Before performing a broad repository search, check the relevant file under
`.serena/code-map/modules/`.

Treat the code map as a static snapshot. Use Serena MCP for exact live symbol,
reference, implementation, and editing operations.

Do not load `symbols.jsonl` or `edges.jsonl` in full unless the task requires it.
Prefer the overview and one relevant module file.
"""


class CodeMapSerializationError(Exception):
    """Raised on internal consistency violations of the code map (these abort the export)."""


@dataclass
class CodeMapWriteResult:
    files_written: list[str] = field(default_factory=list)
    files_unchanged: list[str] = field(default_factory=list)
    files_deleted: list[str] = field(default_factory=list)


def _jsonl(records: list[dict]) -> str:
    lines = [json.dumps(record, sort_keys=True, ensure_ascii=False) for record in records]
    return "\n".join(lines) + "\n" if lines else ""


def render_symbols_jsonl(code_map: CodeMap) -> str:
    return _jsonl([symbol.to_dict() for symbol in code_map.sorted_symbols()])


def render_edges_jsonl(code_map: CodeMap) -> str:
    return _jsonl([edge.to_dict() for edge in code_map.sorted_edges()])


def render_diagnostics_jsonl(code_map: CodeMap) -> str:
    return _jsonl([diagnostic.to_dict() for diagnostic in code_map.diagnostics])


def render_manifest_json(code_map: CodeMap) -> str:
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "generator": GENERATOR_NAME,
        "upstream_baseline": UPSTREAM_BASELINE,
        "project_name": code_map.project_name,
        "source_root": ".",
        "language_servers": sorted(code_map.coverage),
        "symbol_count": len(code_map.symbols_by_id),
        "edge_count": len(code_map.edges),
        "diagnostic_count": len(code_map.diagnostics),
        "dropped_diagnostics": code_map.dropped_diagnostics,
        "unresolved_internal_targets": code_map.unresolved_internal_targets,
        "coverage": {ls_id: coverage.to_dict() for ls_id, coverage in sorted(code_map.coverage.items())},
    }
    return json.dumps(manifest, sort_keys=True, ensure_ascii=False, indent=2) + "\n"


def _validate(code_map: CodeMap) -> None:
    for edge in code_map.edges:
        if edge.source not in code_map.symbols_by_id:
            raise CodeMapSerializationError(f"Edge {edge.type} references unknown source symbol '{edge.source}'")
        if edge.target not in code_map.symbols_by_id:
            raise CodeMapSerializationError(f"Edge {edge.type} references unknown target symbol '{edge.target}'")
    for symbol_id, symbol in code_map.symbols_by_id.items():
        if symbol.id != symbol_id:
            raise CodeMapSerializationError(f"Symbol id mismatch: key '{symbol_id}' vs symbol.id '{symbol.id}'")
        if symbol.relative_path is not None and os.path.isabs(symbol.relative_path):
            raise CodeMapSerializationError(f"Symbol '{symbol_id}' has an absolute path: {symbol.relative_path}")


def render_code_map_files(code_map: CodeMap, overview_max_chars: int = DEFAULT_OVERVIEW_MAX_CHARS) -> dict[str, str]:
    """
    Renders the complete set of code map files as a mapping from the path relative to
    the code map output directory to the file content.
    """
    _validate(code_map)
    files: dict[str, str] = {
        "overview.md": render_overview(code_map, max_chars=overview_max_chars),
        "manifest.json": render_manifest_json(code_map),
        "symbols.jsonl": render_symbols_jsonl(code_map),
        "edges.jsonl": render_edges_jsonl(code_map),
        "diagnostics.jsonl": render_diagnostics_jsonl(code_map),
        "AGENTS_SNIPPET.md": AGENTS_SNIPPET,
    }
    source_paths = sorted({s.relative_path for s in code_map.symbols_by_id.values() if s.relative_path and not s.is_external})
    for relative_source_path in source_paths:
        files[module_markdown_path(relative_source_path)] = render_module_markdown(code_map, relative_source_path)
    return files


def write_code_map(
    code_map: CodeMap,
    output_dir: str | Path,
    overview_max_chars: int = DEFAULT_OVERVIEW_MAX_CHARS,
) -> CodeMapWriteResult:
    """
    Writes the code map to the given output directory.

    All file contents are rendered in memory before anything is written, so a rendering
    failure leaves an existing code map untouched. Individual files are then replaced
    atomically and only if their content changed; module files that no longer correspond
    to a source file are deleted.

    :return: statistics about written/unchanged/deleted files
    """
    output_path = Path(output_dir)
    files = render_code_map_files(code_map, overview_max_chars=overview_max_chars)

    result = CodeMapWriteResult()
    output_path.mkdir(parents=True, exist_ok=True)
    for relative_path in sorted(files):
        target = output_path / relative_path
        content = files[relative_path]
        if target.exists():
            try:
                existing_content = target.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                existing_content = None
            if existing_content == content:
                result.files_unchanged.append(relative_path)
                continue
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = target.parent / f"{target.name}.tmp-{os.getpid()}"
        try:
            tmp_path.write_text(content, encoding="utf-8", newline="\n")
            os.replace(tmp_path, target)
        finally:
            if tmp_path.exists():
                tmp_path.unlink()
        result.files_written.append(relative_path)

    # remove stale module files (and now-empty directories) from previous exports
    modules_dir = output_path / "modules"
    if modules_dir.is_dir():
        expected = {output_path / relative_path for relative_path in files}
        for existing_file in sorted(modules_dir.rglob("*.md"), reverse=True):
            if existing_file not in expected:
                existing_file.unlink()
                result.files_deleted.append(str(existing_file.relative_to(output_path)))
        for directory in sorted((p for p in modules_dir.rglob("*") if p.is_dir()), reverse=True):
            if not any(directory.iterdir()):
                directory.rmdir()

    return result
