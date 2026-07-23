"""
Markdown rendering for the code map: the small `overview.md` entry point and the
per-source-file module files. All output is deterministic (no timestamps, no absolute
paths, stable sort orders).
"""

from serena.code_map.model import TYPE_SYMBOL_KINDS, CodeMap, CodeMapSymbol

DEFAULT_OVERVIEW_MAX_CHARS = 32768

_TYPE_KIND_NAMES = {kind.name for kind in TYPE_SYMBOL_KINDS}
_MAX_DESCRIPTION_CHARS = 200
_MAX_TYPE_DEPENDENCIES = 50
_MAX_CALL_ROOTS = 30
_MAX_DETAIL_LINKS = 25


def module_markdown_path(relative_source_path: str) -> str:
    """Returns the output path (relative to the code map directory) of the module file for a source file."""
    return f"modules/{relative_source_path}.md"


def _first_paragraph(documentation: str | None) -> str | None:
    """Extracts the first paragraph of a documentation string, shortened to a display-friendly length."""
    if not documentation:
        return None
    paragraph = documentation.strip().split("\n\n")[0].strip()
    paragraph = " ".join(line.strip() for line in paragraph.split("\n") if line.strip())
    if not paragraph:
        return None
    if len(paragraph) > _MAX_DESCRIPTION_CHARS:
        paragraph = paragraph[: _MAX_DESCRIPTION_CHARS - 1].rstrip() + "…"
    return paragraph


def _symbol_label(code_map: CodeMap, symbol_id: str) -> str:
    symbol = code_map.symbols_by_id.get(symbol_id)
    if symbol is None:
        return symbol_id
    return symbol.name_path


def render_overview(code_map: CodeMap, max_chars: int = DEFAULT_OVERVIEW_MAX_CHARS) -> str:
    """
    Renders the small overview document that serves as the entry point into the code map.
    The result is guaranteed not to exceed max_chars characters.
    """
    lines: list[str] = ["# Code Map Overview", ""]

    # coverage summary
    lines.append("## Coverage")
    lines.append("")
    if code_map.coverage:
        for ls_id in sorted(code_map.coverage):
            coverage = code_map.coverage[ls_id]
            lines.append(
                f"- {ls_id}: symbols {coverage.document_symbols}, hover {coverage.hover}, "
                f"calls {coverage.call_hierarchy}, type hierarchy {coverage.type_hierarchy}"
            )
    else:
        lines.append("- no language servers produced symbols")
    lines.append("")

    internal_symbols = [s for s in code_map.sorted_symbols() if not s.is_external]

    # source areas: directories with their top-level type symbols
    directories: dict[str, list[CodeMapSymbol]] = {}
    for symbol in internal_symbols:
        if symbol.kind in _TYPE_KIND_NAMES and symbol.parent_id is None and symbol.relative_path:
            directory = symbol.relative_path.rsplit("/", 1)[0] + "/" if "/" in symbol.relative_path else "./"
            directories.setdefault(directory, []).append(symbol)

    lines.append("## Source areas")
    lines.append("")
    for directory in sorted(directories):
        lines.append(f"- {directory}")
        for symbol in directories[directory]:
            description = _first_paragraph(symbol.documentation)
            if description:
                lines.append(f"  - {symbol.name} — {description}")
            else:
                lines.append(f"  - {symbol.name}")
    if not directories:
        lines.append("- no type symbols found")
    lines.append("")

    # type dependencies (CLASS_DEPENDS_ON), most-called first
    depends_edges = sorted(code_map.edges_by_type("CLASS_DEPENDS_ON"), key=lambda e: (-e.count, e.source, e.target))
    if depends_edges:
        lines.append("## Type dependencies")
        lines.append("")
        for edge in depends_edges[:_MAX_TYPE_DEPENDENCIES]:
            calls = "call" if edge.count == 1 else "calls"
            lines.append(f"- {_symbol_label(code_map, edge.source)} -> {_symbol_label(code_map, edge.target)} ({edge.count} {calls})")
        if len(depends_edges) > _MAX_TYPE_DEPENDENCIES:
            lines.append(f"- … {len(depends_edges) - _MAX_TYPE_DEPENDENCIES} more (see edges.jsonl)")
        lines.append("")

    # call root candidates: internal callables with outgoing calls but no incoming calls
    outgoing = code_map.outgoing_edges("CALLS")
    incoming = code_map.incoming_edges("CALLS")
    call_roots = [
        symbol_id for symbol_id in sorted(outgoing) if symbol_id not in incoming and not code_map.symbols_by_id[symbol_id].is_external
    ]
    if call_roots:
        lines.append("## Call root candidates")
        lines.append("")
        for symbol_id in call_roots[:_MAX_CALL_ROOTS]:
            lines.append(f"- {_symbol_label(code_map, symbol_id)}")
        if len(call_roots) > _MAX_CALL_ROOTS:
            lines.append(f"- … {len(call_roots) - _MAX_CALL_ROOTS} more")
        lines.append("")

    # pointers to module files (largest files first)
    files_with_counts: dict[str, int] = {}
    for symbol in internal_symbols:
        if symbol.relative_path:
            files_with_counts[symbol.relative_path] = files_with_counts.get(symbol.relative_path, 0) + 1
    detail_files = sorted(files_with_counts, key=lambda path: (-files_with_counts[path], path))
    if detail_files:
        lines.append("## Details")
        lines.append("")
        for path in detail_files[:_MAX_DETAIL_LINKS]:
            lines.append(f"- {module_markdown_path(path)}")
        if len(detail_files) > _MAX_DETAIL_LINKS:
            lines.append(f"- … {len(detail_files) - _MAX_DETAIL_LINKS} more under modules/")
        lines.append("")

    text = "\n".join(lines).rstrip("\n") + "\n"
    if len(text) <= max_chars:
        return text

    # shrink deterministically by removing list items from the end until the limit is met
    truncation_notice = "\n(truncated to fit the size limit)\n"
    while lines and len("\n".join(lines)) + len(truncation_notice) > max_chars:
        # find the last list item line and remove it; if none remain, drop the last line
        last_item_index = None
        for i in range(len(lines) - 1, -1, -1):
            if lines[i].lstrip().startswith("- "):
                last_item_index = i
                break
        if last_item_index is None:
            lines.pop()
        else:
            lines.pop(last_item_index)
    return ("\n".join(lines)).rstrip("\n") + truncation_notice


def render_module_markdown(code_map: CodeMap, relative_source_path: str) -> str:
    """
    Renders the per-source-file Markdown document with symbol details,
    including calls and called-by relations derived from the stored CALLS edges.
    """
    symbols_in_file = [s for s in code_map.sorted_symbols() if s.relative_path == relative_source_path and not s.is_external]
    # preserve source order within the file
    symbols_in_file.sort(key=lambda s: (s.selection_range.start.line if s.selection_range else 0, s.id))

    outgoing = code_map.outgoing_edges("CALLS")
    incoming = code_map.incoming_edges("CALLS")
    supertypes = code_map.outgoing_edges("TYPE_SUPERTYPE")

    lines: list[str] = [f"# {relative_source_path}", ""]
    for symbol in symbols_in_file:
        heading_level = "##" if symbol.parent_id is None else "###"
        heading = symbol.signature if symbol.signature and symbol.parent_id is not None else symbol.name_path
        lines.append(f"{heading_level} {heading}")
        lines.append("")
        lines.append(f"Kind: {symbol.kind}")
        lines.append(f"ID: `{symbol.id}`")
        lines.append("")
        description = _first_paragraph(symbol.documentation)
        if description:
            lines.append(description)
            lines.append("")

        supertype_edges = supertypes.get(symbol.id, [])
        if supertype_edges:
            lines.append("Supertypes:")
            for edge in supertype_edges:
                lines.append(f"- `{_symbol_label(code_map, edge.target)}`")
            lines.append("")

        call_edges = outgoing.get(symbol.id, [])
        if call_edges:
            lines.append("Calls:")
            for edge in call_edges:
                lines.append(f"- `{_symbol_label(code_map, edge.target)}`")
            lines.append("")

        called_by_edges = incoming.get(symbol.id, [])
        if called_by_edges:
            lines.append("Called by:")
            for edge in called_by_edges:
                lines.append(f"- `{_symbol_label(code_map, edge.source)}`")
            lines.append("")

    return "\n".join(lines).rstrip("\n") + "\n"
