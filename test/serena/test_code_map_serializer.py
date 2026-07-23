"""
Unit tests for the code map serializer and Markdown rendering: deterministic output,
write-if-changed behavior and failure safety.
"""

import re
from pathlib import Path

import pytest

from serena.code_map.model import (
    CodeMap,
    CodeMapDiagnostic,
    CodeMapEdge,
    CodeMapSymbol,
    LanguageServerCoverage,
    SourcePosition,
    SourceRange,
)
from serena.code_map.serializer import (
    CodeMapSerializationError,
    render_code_map_files,
    write_code_map,
)

ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _range(line: int) -> SourceRange:
    return SourceRange(start=SourcePosition(line, 4), end=SourcePosition(line, 20))


def _symbol(
    symbol_id: str,
    name: str,
    kind: str,
    path: str | None,
    line: int = 1,
    parent_id: str | None = None,
    documentation: str | None = None,
    signature: str | None = None,
    is_external: bool = False,
) -> CodeMapSymbol:
    return CodeMapSymbol(
        id=symbol_id,
        language_server="external" if is_external else "mock",
        name=name,
        name_path=symbol_id.split("|")[2] if not is_external else name,
        kind=kind,
        relative_path=path,
        selection_range=_range(line) if not is_external else None,
        body_range=None,
        parent_id=parent_id,
        documentation=documentation,
        signature=signature,
        is_external=is_external,
    )


def make_code_map(symbol_order: list[str] | None = None, edge_order: list[int] | None = None) -> CodeMap:
    """Creates a small fixture code map; insertion order of symbols/edges can be permuted."""
    service_id = "mock|src/OrderService.java|OrderService|Class"
    cancel_id = "mock|src/OrderService.java|OrderService/cancelOrder|Method"
    refund_class_id = "mock|src/RefundService.java|RefundService|Class"
    refund_id = "mock|src/RefundService.java|RefundService/requestRefund|Method"

    symbols = {
        service_id: _symbol(service_id, "OrderService", "Class", "src/OrderService.java", 1, documentation="Manages order use cases."),
        cancel_id: _symbol(
            cancel_id,
            "cancelOrder",
            "Method",
            "src/OrderService.java",
            3,
            parent_id=service_id,
            documentation="Cancels an order.",
            signature="CancelResult cancelOrder(OrderId id)",
        ),
        refund_class_id: _symbol(refund_class_id, "RefundService", "Class", "src/RefundService.java", 1),
        refund_id: _symbol(refund_id, "requestRefund", "Method", "src/RefundService.java", 3, parent_id=refund_class_id),
    }
    symbols[service_id].child_ids = [cancel_id]
    symbols[refund_class_id].child_ids = [refund_id]

    edges = [
        CodeMapEdge(type="CONTAINS", source=service_id, target=cancel_id, resolution="documentSymbol"),
        CodeMapEdge(type="CONTAINS", source=refund_class_id, target=refund_id, resolution="documentSymbol"),
        CodeMapEdge(type="CALLS", source=cancel_id, target=refund_id, source_ranges=[_range(5)], count=2, resolution="callHierarchy"),
        CodeMapEdge(type="CLASS_DEPENDS_ON", source=service_id, target=refund_class_id, count=2, resolution="derived"),
    ]

    code_map = CodeMap(project_name="test-project")
    for key in symbol_order or list(symbols):
        code_map.symbols_by_id[key] = symbols[key]
    for index in edge_order or range(len(edges)):
        code_map.edges.append(edges[index])
    code_map.coverage["mock"] = LanguageServerCoverage(
        document_symbols="supported", hover="supported", call_hierarchy="supported", type_hierarchy="supported"
    )
    code_map.diagnostics.append(CodeMapDiagnostic(level="info", phase="callHierarchy", message="all good", language_server="mock"))
    return code_map


class TestDeterminism:
    def test_two_renders_are_byte_identical(self) -> None:
        files_a = render_code_map_files(make_code_map())
        files_b = render_code_map_files(make_code_map())
        assert files_a == files_b

    def test_output_is_independent_of_insertion_order(self) -> None:
        default_order = make_code_map()
        permuted = make_code_map(
            symbol_order=[
                "mock|src/RefundService.java|RefundService/requestRefund|Method",
                "mock|src/OrderService.java|OrderService|Class",
                "mock|src/RefundService.java|RefundService|Class",
                "mock|src/OrderService.java|OrderService/cancelOrder|Method",
            ],
            edge_order=[3, 2, 1, 0],
        )
        assert render_code_map_files(default_order) == render_code_map_files(permuted)

    def test_no_absolute_paths_in_output(self) -> None:
        for path, content in render_code_map_files(make_code_map()).items():
            assert "/Users/" not in content, path
            assert "C:\\" not in content, path

    def test_no_timestamps_in_output(self) -> None:
        for path, content in render_code_map_files(make_code_map()).items():
            assert not ISO_DATE_RE.search(content), path

    def test_all_required_files_present(self) -> None:
        files = render_code_map_files(make_code_map())
        for required in ["overview.md", "manifest.json", "symbols.jsonl", "edges.jsonl", "diagnostics.jsonl", "AGENTS_SNIPPET.md"]:
            assert required in files
        assert "modules/src/OrderService.java.md" in files
        assert "modules/src/RefundService.java.md" in files

    def test_files_end_with_single_newline(self) -> None:
        for path, content in render_code_map_files(make_code_map()).items():
            assert content.endswith("\n") and not content.endswith("\n\n"), path


class TestMarkdownContent:
    def test_overview_contains_coverage_and_dependencies(self) -> None:
        overview = render_code_map_files(make_code_map())["overview.md"]
        assert "## Coverage" in overview
        assert "- mock: symbols supported, hover supported, calls supported, type hierarchy supported" in overview
        assert "OrderService -> RefundService (2 calls)" in overview
        assert "## Call root candidates" in overview
        assert "OrderService/cancelOrder" in overview

    def test_overview_respects_max_chars(self) -> None:
        files = render_code_map_files(make_code_map(), overview_max_chars=300)
        assert len(files["overview.md"]) <= 300
        assert "(truncated to fit the size limit)" in files["overview.md"]

    def test_module_markdown_contains_calls_and_called_by(self) -> None:
        files = render_code_map_files(make_code_map())
        order_module = files["modules/src/OrderService.java.md"]
        assert order_module.startswith("# src/OrderService.java")
        assert "CancelResult cancelOrder(OrderId id)" in order_module
        assert "Calls:" in order_module
        assert "`RefundService/requestRefund`" in order_module

        refund_module = files["modules/src/RefundService.java.md"]
        assert "Called by:" in refund_module
        assert "`OrderService/cancelOrder`" in refund_module

    def test_description_comes_from_documentation_only(self) -> None:
        code_map = make_code_map()
        refund_module = render_code_map_files(code_map)["modules/src/RefundService.java.md"]
        # RefundService has no documentation: nothing may be fabricated
        assert "Manages order" not in refund_module


class TestWriteBehavior:
    def test_write_and_rewrite_unchanged(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "code-map"
        result_first = write_code_map(make_code_map(), output_dir)
        assert "overview.md" in result_first.files_written
        overview_mtime = (output_dir / "overview.md").stat().st_mtime_ns

        result_second = write_code_map(make_code_map(), output_dir)
        assert result_second.files_written == []
        assert "overview.md" in result_second.files_unchanged
        assert (output_dir / "overview.md").stat().st_mtime_ns == overview_mtime

    def test_changed_file_is_rewritten(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "code-map"
        write_code_map(make_code_map(), output_dir)

        changed = make_code_map()
        changed.symbols_by_id["mock|src/OrderService.java|OrderService|Class"].documentation = "Updated documentation."
        result = write_code_map(changed, output_dir)
        assert "modules/src/OrderService.java.md" in result.files_written
        assert "modules/src/RefundService.java.md" in result.files_unchanged

    def test_stale_module_files_are_deleted(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "code-map"
        write_code_map(make_code_map(), output_dir)
        stale = output_dir / "modules" / "src" / "Removed.java.md"
        stale.write_text("# stale\n", encoding="utf-8")

        result = write_code_map(make_code_map(), output_dir)
        assert not stale.exists()
        assert "modules/src/Removed.java.md" in result.files_deleted

    def test_failure_keeps_previous_output(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "code-map"
        write_code_map(make_code_map(), output_dir)
        overview_before = (output_dir / "overview.md").read_text(encoding="utf-8")

        broken = make_code_map()
        broken.edges.append(CodeMapEdge(type="CALLS", source="mock|nonexistent", target="mock|also-nonexistent"))
        with pytest.raises(CodeMapSerializationError):
            write_code_map(broken, output_dir)

        assert (output_dir / "overview.md").read_text(encoding="utf-8") == overview_before

    def test_inconsistent_edge_raises(self) -> None:
        broken = make_code_map()
        broken.edges.append(CodeMapEdge(type="CALLS", source="mock|nonexistent", target="mock|src/OrderService.java|OrderService|Class"))
        with pytest.raises(CodeMapSerializationError):
            render_code_map_files(broken)
