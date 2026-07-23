"""
Builder that constructs a CodeMap from a project using only local LSP analysis
(document symbols, hover, outgoing call hierarchy and supertype hierarchy).
"""

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePath
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from tqdm import tqdm

from serena.code_map.model import (
    CALLABLE_SYMBOL_KINDS,
    DOCUMENTED_SYMBOL_KINDS,
    EXTERNAL_LANGUAGE_SERVER,
    INCLUDED_SYMBOL_KINDS,
    OWNER_SYMBOL_KINDS,
    TYPE_SYMBOL_KINDS,
    CodeMap,
    CodeMapDiagnostic,
    CodeMapEdge,
    CodeMapSymbol,
    LanguageServerCoverage,
    SourcePosition,
    SourceRange,
)
from serena.code_map.quick_info import parse_quick_info
from serena.symbol import LanguageServerSymbol, LanguageServerSymbolRetriever
from solidlsp import ls_types
from solidlsp.ls_exceptions import SolidLSPException
from solidlsp.ls_types import SymbolKind
from solidlsp.ls_utils import PathUtils

if TYPE_CHECKING:
    from serena.ls_manager import LanguageServerManager
    from serena.project import Project

log = logging.getLogger(__name__)


@dataclass
class CodeMapBuildOptions:
    include_docs: bool = True
    include_calls: bool = True
    include_type_hierarchy: bool = True
    hover_budget_seconds: float = 0.0
    """total time budget for hover requests in seconds; 0 means unlimited"""
    max_diagnostics: int = 1000
    show_progress: bool = True


def _symbol_kind_name(kind: int) -> str:
    try:
        return SymbolKind(kind).name
    except ValueError:
        return str(kind)


class CodeMapBuilder:
    def __init__(
        self,
        project: "Project",
        ls_manager: "LanguageServerManager",
        options: CodeMapBuildOptions | None = None,
        symbol_retriever: LanguageServerSymbolRetriever | None = None,
    ) -> None:
        """
        :param project: the project to build the code map for; its language server manager must already be initialized.
        :param ls_manager: the language server manager to use for LSP requests.
        :param options: build options; defaults are used if None.
        :param symbol_retriever: the symbol retriever used for hover requests; created from the project if None.
        """
        self._project = project
        self._ls_manager = ls_manager
        self._options = options or CodeMapBuildOptions()
        self._symbol_retriever = symbol_retriever

        self._code_map = CodeMap(project_name=project.project_name)
        self._edges_by_key: dict[tuple[str, str, str], CodeMapEdge] = {}
        self._symbol_id_by_location: dict[tuple[str, int, int], str] = {}
        self._symbol_ids_by_path_name_kind: dict[tuple[str, str, str], list[str]] = {}
        self._owner_by_symbol_id: dict[str, str | None] = {}
        self._ls_symbols_by_id: dict[str, LanguageServerSymbol] = {}
        self._symbol_ids_by_file: dict[str, list[str]] = {}
        self._call_hierarchy_unsupported: set[str] = set()
        self._type_hierarchy_unsupported: set[str] = set()

    # region helpers

    def _get_symbol_retriever(self) -> LanguageServerSymbolRetriever:
        if self._symbol_retriever is None:
            self._symbol_retriever = LanguageServerSymbolRetriever(self._project)
        return self._symbol_retriever

    def _coverage(self, ls_id: str) -> LanguageServerCoverage:
        if ls_id not in self._code_map.coverage:
            self._code_map.coverage[ls_id] = LanguageServerCoverage()
        return self._code_map.coverage[ls_id]

    def _add_diagnostic(
        self,
        level: str,
        phase: str,
        message: str,
        language_server: str | None = None,
        relative_path: str | None = None,
        symbol_id: str | None = None,
    ) -> None:
        if len(self._code_map.diagnostics) >= self._options.max_diagnostics:
            self._code_map.dropped_diagnostics += 1
            return
        self._code_map.diagnostics.append(
            CodeMapDiagnostic(
                level=level,  # type: ignore[arg-type]
                phase=phase,
                message=message,
                language_server=language_server,
                relative_path=relative_path,
                symbol_id=symbol_id,
            )
        )

    def _add_edge(
        self,
        edge_type: str,
        source: str,
        target: str,
        source_ranges: list[SourceRange] | None = None,
        count: int = 1,
        resolution: str = "derived",
    ) -> None:
        """Adds an edge, aggregating duplicates of the same (type, source, target) into a single edge."""
        key = (edge_type, source, target)
        existing = self._edges_by_key.get(key)
        if existing is None:
            edge = CodeMapEdge(
                type=edge_type,  # type: ignore[arg-type]
                source=source,
                target=target,
                source_ranges=list(source_ranges or []),
                count=count,
                resolution=resolution,  # type: ignore[arg-type]
            )
            self._edges_by_key[key] = edge
            self._code_map.edges.append(edge)
        else:
            existing.count += count
            if source_ranges:
                known_ranges = set(existing.source_ranges)
                existing.source_ranges.extend(r for r in source_ranges if r not in known_ranges)

    @staticmethod
    def _posix_path(relative_path: str) -> str:
        return PurePath(relative_path).as_posix()

    def _make_symbol_id(self, ls_id: str, posix_path: str, ls_symbol: LanguageServerSymbol) -> str:
        symbol_id = f"{ls_id}|{posix_path}|{ls_symbol.get_name_path()}|{ls_symbol.symbol_kind_name}"
        if symbol_id in self._code_map.symbols_by_id:
            line = ls_symbol.line if ls_symbol.line is not None else 0
            column = ls_symbol.column if ls_symbol.column is not None else 0
            symbol_id = f"{symbol_id}|{line}:{column}"
        return symbol_id

    def _relative_path_for_uri(self, uri: str) -> str | None:
        """Converts a URI to a posix-normalized project-relative path, or None if outside the project."""
        try:
            abs_path = PathUtils.uri_to_path(uri)
        except Exception:
            return None
        relative_path = PathUtils.get_relative_path(abs_path, str(self._project.project_root))
        if relative_path is None or relative_path == ".." or relative_path.startswith(".." + os.path.sep):
            return None
        return self._posix_path(relative_path)

    @staticmethod
    def _external_uri_fragment(uri: str) -> str:
        """Normalizes a URI of an external symbol to a machine-independent fragment (scheme + last path segment)."""
        parsed = urlparse(uri)
        path = parsed.path or uri
        last_segment = path.rstrip("/").split("/")[-1] or path
        scheme = parsed.scheme or "unknown"
        return f"{scheme}:{last_segment}"

    def _resolve_hierarchy_item(self, item: Mapping[str, Any], resolution_phase: str) -> tuple[str, bool]:
        """
        Resolves a CallHierarchyItem/TypeHierarchyItem to an internal symbol id if possible;
        otherwise registers (or reuses) an external symbol and returns its id.

        :param item: the hierarchy item as returned by the language server
        :param resolution_phase: the phase name for diagnostics
        :return: a tuple (symbol_id, is_internal); is_internal is False if the target was inside the project
            but could not be matched to a known symbol, or was outside the project entirely.
        """
        name = item.get("name", "")
        kind_name = _symbol_kind_name(item.get("kind", 0))
        uri = item.get("uri", "")
        relative_path = self._relative_path_for_uri(uri)

        if relative_path is not None:
            selection_range = item.get("selectionRange")
            if selection_range is not None:
                location_key = (relative_path, selection_range["start"]["line"], selection_range["start"]["character"])
                symbol_id = self._symbol_id_by_location.get(location_key)
                if symbol_id is not None:
                    return symbol_id, True
            candidates = self._symbol_ids_by_path_name_kind.get((relative_path, name, kind_name), [])
            if len(candidates) == 1:
                return candidates[0], True

        external_fragment = self._external_uri_fragment(uri)
        external_id = f"{EXTERNAL_LANGUAGE_SERVER}|{external_fragment}|{name}|{kind_name}"
        if external_id not in self._code_map.symbols_by_id:
            self._code_map.symbols_by_id[external_id] = CodeMapSymbol(
                id=external_id,
                language_server=EXTERNAL_LANGUAGE_SERVER,
                name=name,
                name_path=name,
                kind=kind_name,
                relative_path=None,
                selection_range=None,
                body_range=None,
                parent_id=None,
                is_external=True,
            )
            if relative_path is not None:
                self._add_diagnostic(
                    "info",
                    resolution_phase,
                    f"Could not resolve hierarchy item '{name}' ({kind_name}) in '{relative_path}' to a known symbol",
                    relative_path=relative_path,
                )
        return external_id, False

    # endregion

    def build(self) -> CodeMap:
        source_files = sorted(self._posix_path(f) for f in self._project.gather_source_files())
        self._build_symbols(source_files)
        if self._options.include_docs:
            self._build_quick_info()
        self._build_contains_edges()
        if self._options.include_calls:
            self._build_call_edges()
        if self._options.include_type_hierarchy:
            self._build_type_edges()
        self._build_class_depends_on_edges()
        self._finalize_coverage()
        return self._code_map

    # region Phase B: document symbols

    def _build_symbols(self, source_files: list[str]) -> None:
        for relative_path in tqdm(source_files, desc="Collecting symbols", disable=not self._options.show_progress):
            try:
                ls = self._ls_manager.get_language_server(relative_path)
            except Exception as e:
                self._add_diagnostic("warning", "documentSymbols", f"No language server for file: {e}", relative_path=relative_path)
                continue
            ls_id = ls.ls_id.value
            coverage = self._coverage(ls_id)
            try:
                document_symbols = ls.request_document_symbols(relative_path)
            except Exception as e:
                coverage.errors += 1
                self._add_diagnostic(
                    "error",
                    "documentSymbols",
                    f"Failed to retrieve document symbols: {e}",
                    language_server=ls_id,
                    relative_path=relative_path,
                )
                continue
            if coverage.document_symbols == "not_attempted":
                coverage.document_symbols = "supported"
            for root in document_symbols.root_symbols:
                self._process_symbol_tree(root, ls_id, relative_path, parent_id=None, owner_id=None)

    def _process_symbol_tree(
        self,
        symbol_dict: "ls_types.UnifiedSymbolInformation",
        ls_id: str,
        relative_path: str,
        parent_id: str | None,
        owner_id: str | None,
    ) -> None:
        ls_symbol = LanguageServerSymbol(symbol_dict)
        kind = ls_symbol.symbol_kind
        child_parent_id = parent_id
        child_owner_id = owner_id

        if kind in INCLUDED_SYMBOL_KINDS:
            symbol_id = self._make_symbol_id(ls_id, relative_path, ls_symbol)

            selection_range = None
            if "selectionRange" in symbol_dict:
                selection_range = SourceRange.from_lsp(symbol_dict["selectionRange"])
            body_range = None
            start_position = ls_symbol.body_start_position
            end_position = ls_symbol.body_end_position
            if start_position is not None and end_position is not None:
                body_range = SourceRange(
                    start=SourcePosition.from_lsp(start_position),
                    end=SourcePosition.from_lsp(end_position),
                )

            code_symbol = CodeMapSymbol(
                id=symbol_id,
                language_server=ls_id,
                name=ls_symbol.name,
                name_path=ls_symbol.get_name_path(),
                kind=ls_symbol.symbol_kind_name,
                relative_path=relative_path,
                selection_range=selection_range,
                body_range=body_range,
                parent_id=parent_id,
            )
            self._code_map.symbols_by_id[symbol_id] = code_symbol
            self._ls_symbols_by_id[symbol_id] = ls_symbol
            self._owner_by_symbol_id[symbol_id] = owner_id
            self._symbol_ids_by_file.setdefault(relative_path, []).append(symbol_id)
            self._symbol_ids_by_path_name_kind.setdefault((relative_path, ls_symbol.name, ls_symbol.symbol_kind_name), []).append(symbol_id)
            if selection_range is not None:
                location_key = (relative_path, selection_range.start.line, selection_range.start.character)
                self._symbol_id_by_location.setdefault(location_key, symbol_id)
            if parent_id is not None:
                self._code_map.symbols_by_id[parent_id].child_ids.append(symbol_id)

            child_parent_id = symbol_id
            child_owner_id = symbol_id if kind in OWNER_SYMBOL_KINDS else owner_id

        for child in symbol_dict.get("children", []):
            self._process_symbol_tree(child, ls_id, relative_path, parent_id=child_parent_id, owner_id=child_owner_id)

    # endregion

    # region Phase C/D: quick info

    def _build_quick_info(self) -> None:
        documented: list[tuple[str, LanguageServerSymbol]] = []
        for symbol_id, ls_symbol in self._ls_symbols_by_id.items():
            if ls_symbol.symbol_kind in DOCUMENTED_SYMBOL_KINDS:
                documented.append((symbol_id, ls_symbol))
        if not documented:
            return

        for ls_id in {self._code_map.symbols_by_id[symbol_id].language_server for symbol_id, _ in documented}:
            coverage = self._coverage(ls_id)
            coverage.hover_symbols_attempted += sum(
                1 for symbol_id, _ in documented if self._code_map.symbols_by_id[symbol_id].language_server == ls_id
            )

        try:
            retriever = self._get_symbol_retriever()
            info_by_symbol = retriever.request_info_for_symbol_batch(
                [ls_symbol for _, ls_symbol in documented],
                budget_seconds_override=self._options.hover_budget_seconds,
            )
        except Exception as e:
            self._add_diagnostic("error", "quickInfo", f"Hover batch request failed: {e}")
            return

        for symbol_id, ls_symbol in documented:
            raw_info = info_by_symbol.get(ls_symbol)
            if raw_info is None:
                continue
            code_symbol = self._code_map.symbols_by_id[symbol_id]
            parts = parse_quick_info(raw_info, ls_symbol.name)
            if parts is None:
                continue
            code_symbol.quick_info_raw = parts.raw
            code_symbol.signature = parts.signature
            code_symbol.documentation = parts.documentation
            self._coverage(code_symbol.language_server).hover_symbols_resolved += 1

    # endregion

    # region Phase E: CONTAINS edges

    def _build_contains_edges(self) -> None:
        for symbol_id, symbol in self._code_map.symbols_by_id.items():
            if symbol.parent_id is not None:
                self._add_edge("CONTAINS", symbol.parent_id, symbol_id, resolution="documentSymbol")

    # endregion

    # region Phase F: CALLS edges

    def _build_call_edges(self) -> None:
        callables_by_file = self._symbol_ids_by_file_with_kinds(CALLABLE_SYMBOL_KINDS)
        file_iterator = tqdm(sorted(callables_by_file.items()), desc="Call hierarchy", disable=not self._options.show_progress)
        for relative_path, symbol_ids in file_iterator:
            ls = self._ls_manager.get_language_server(relative_path)
            ls_id = ls.ls_id.value
            if ls_id in self._call_hierarchy_unsupported:
                continue
            coverage = self._coverage(ls_id)
            with ls.open_file(relative_path) as file_buffer:
                for symbol_id in symbol_ids:
                    if ls_id in self._call_hierarchy_unsupported:
                        break
                    code_symbol = self._code_map.symbols_by_id[symbol_id]
                    if code_symbol.selection_range is None:
                        continue
                    coverage.callable_symbols_attempted += 1
                    try:
                        outgoing_calls = ls.request_call_hierarchy_outgoing(
                            relative_path,
                            code_symbol.selection_range.start.line,
                            code_symbol.selection_range.start.character,
                            file_buffer=file_buffer,
                        )
                    except SolidLSPException as e:
                        if e.is_method_not_found():
                            self._call_hierarchy_unsupported.add(ls_id)
                            coverage.call_hierarchy = "unsupported"
                            self._add_diagnostic(
                                "info", "callHierarchy", "Call hierarchy is not supported by this language server", language_server=ls_id
                            )
                            break
                        coverage.errors += 1
                        self._add_diagnostic(
                            "warning",
                            "callHierarchy",
                            f"Outgoing call request failed: {e}",
                            language_server=ls_id,
                            relative_path=relative_path,
                            symbol_id=symbol_id,
                        )
                        continue
                    except Exception as e:
                        coverage.errors += 1
                        self._add_diagnostic(
                            "warning",
                            "callHierarchy",
                            f"Outgoing call request failed: {e}",
                            language_server=ls_id,
                            relative_path=relative_path,
                            symbol_id=symbol_id,
                        )
                        continue

                    coverage.callable_symbols_resolved += 1
                    for outgoing_call in outgoing_calls:
                        target_id, _is_internal = self._resolve_hierarchy_item(outgoing_call["to"], "callHierarchy")
                        from_ranges = [SourceRange.from_lsp(r) for r in outgoing_call.get("fromRanges", [])]
                        call_count = max(len(from_ranges), 1)
                        self._add_edge(
                            "CALLS", symbol_id, target_id, source_ranges=from_ranges, count=call_count, resolution="callHierarchy"
                        )
                        coverage.call_edges += 1

    # endregion

    # region Phase G: TYPE_SUPERTYPE edges

    def _build_type_edges(self) -> None:
        types_by_file = self._symbol_ids_by_file_with_kinds(TYPE_SYMBOL_KINDS)
        file_iterator = tqdm(sorted(types_by_file.items()), desc="Type hierarchy", disable=not self._options.show_progress)
        for relative_path, symbol_ids in file_iterator:
            ls = self._ls_manager.get_language_server(relative_path)
            ls_id = ls.ls_id.value
            if ls_id in self._type_hierarchy_unsupported:
                continue
            coverage = self._coverage(ls_id)
            with ls.open_file(relative_path) as file_buffer:
                for symbol_id in symbol_ids:
                    if ls_id in self._type_hierarchy_unsupported:
                        break
                    code_symbol = self._code_map.symbols_by_id[symbol_id]
                    if code_symbol.selection_range is None:
                        continue
                    coverage.type_symbols_attempted += 1
                    try:
                        supertypes = ls.request_type_hierarchy_supertypes(
                            relative_path,
                            code_symbol.selection_range.start.line,
                            code_symbol.selection_range.start.character,
                            file_buffer=file_buffer,
                        )
                    except SolidLSPException as e:
                        if e.is_method_not_found():
                            self._type_hierarchy_unsupported.add(ls_id)
                            coverage.type_hierarchy = "unsupported"
                            self._add_diagnostic(
                                "info", "typeHierarchy", "Type hierarchy is not supported by this language server", language_server=ls_id
                            )
                            break
                        coverage.errors += 1
                        self._add_diagnostic(
                            "warning",
                            "typeHierarchy",
                            f"Supertype request failed: {e}",
                            language_server=ls_id,
                            relative_path=relative_path,
                            symbol_id=symbol_id,
                        )
                        continue
                    except Exception as e:
                        coverage.errors += 1
                        self._add_diagnostic(
                            "warning",
                            "typeHierarchy",
                            f"Supertype request failed: {e}",
                            language_server=ls_id,
                            relative_path=relative_path,
                            symbol_id=symbol_id,
                        )
                        continue

                    coverage.type_symbols_resolved += 1
                    for supertype in supertypes:
                        supertype_id, _is_internal = self._resolve_hierarchy_item(supertype, "typeHierarchy")
                        self._add_edge("TYPE_SUPERTYPE", symbol_id, supertype_id, resolution="typeHierarchy")

    # endregion

    # region Phase H: CLASS_DEPENDS_ON edges

    def _build_class_depends_on_edges(self) -> None:
        for edge in list(self._code_map.edges):
            if edge.type != "CALLS":
                continue
            source_owner = self._owner_by_symbol_id.get(edge.source)
            target_owner = self._owner_by_symbol_id.get(edge.target)
            if source_owner is None or target_owner is None or source_owner == target_owner:
                continue
            self._add_edge("CLASS_DEPENDS_ON", source_owner, target_owner, count=edge.count, resolution="derived")

    # endregion

    def _symbol_ids_by_file_with_kinds(self, kinds: tuple[SymbolKind, ...]) -> dict[str, list[str]]:
        kind_names = {kind.name for kind in kinds}
        result: dict[str, list[str]] = {}
        for relative_path, symbol_ids in self._symbol_ids_by_file.items():
            selected = [s for s in symbol_ids if self._code_map.symbols_by_id[s].kind in kind_names]
            if selected:
                result[relative_path] = selected
        return result

    def _finalize_coverage(self) -> None:
        for ls_id, coverage in self._code_map.coverage.items():
            if self._options.include_docs:
                if coverage.hover_symbols_attempted > 0:
                    if coverage.hover_symbols_resolved == 0:
                        coverage.hover = "unsupported"
                    elif coverage.hover_symbols_resolved < coverage.hover_symbols_attempted:
                        coverage.hover = "partial"
                    else:
                        coverage.hover = "supported"
            if self._options.include_calls and ls_id not in self._call_hierarchy_unsupported:
                if coverage.callable_symbols_attempted > 0:
                    if coverage.callable_symbols_resolved == coverage.callable_symbols_attempted:
                        coverage.call_hierarchy = "supported"
                    elif coverage.callable_symbols_resolved > 0:
                        coverage.call_hierarchy = "partial"
                    else:
                        coverage.call_hierarchy = "unsupported"
            if self._options.include_type_hierarchy and ls_id not in self._type_hierarchy_unsupported:
                if coverage.type_symbols_attempted > 0:
                    if coverage.type_symbols_resolved == coverage.type_symbols_attempted:
                        coverage.type_hierarchy = "supported"
                    elif coverage.type_symbols_resolved > 0:
                        coverage.type_hierarchy = "partial"
                    else:
                        coverage.type_hierarchy = "unsupported"
