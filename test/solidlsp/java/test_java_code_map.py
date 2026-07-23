"""
Integration test for the code map builder against the real Java language server (Eclipse JDT LS),
using the CodeMapFixture/CodeMapHelper classes of the Java test repository.
"""

import pytest

from serena.code_map.builder import CodeMapBuilder, CodeMapBuildOptions
from serena.code_map.model import CodeMap
from serena.code_map.serializer import render_code_map_files
from serena.symbol import LanguageServerSymbolRetriever
from solidlsp import SolidLanguageServer
from solidlsp.ls_config import LanguageServerId
from test.conftest import language_server_tests_enabled

pytestmark = [
    pytest.mark.java,
    pytest.mark.skipif(not language_server_tests_enabled(LanguageServerId.JAVA), reason="Java tests disabled"),
]

FIXTURE_FILE = "src/main/java/test_repo/CodeMapFixture.java"
HELPER_FILE = "src/main/java/test_repo/CodeMapHelper.java"


class _SingleLanguageServerManager:
    def __init__(self, language_server: SolidLanguageServer) -> None:
        self._language_server = language_server

    def get_language_server(self, relative_path: str) -> SolidLanguageServer:
        return self._language_server


class _FixtureProject:
    def __init__(self, project_root: str) -> None:
        self.project_name = "java-code-map-test"
        self.project_root = project_root

    def gather_source_files(self) -> list[str]:
        return [FIXTURE_FILE, HELPER_FILE]


def _build_code_map(language_server: SolidLanguageServer) -> CodeMap:
    manager = _SingleLanguageServerManager(language_server)
    project = _FixtureProject(language_server.repository_root_path)
    # a minimally initialized retriever: only get_language_server/_ls_manager are used,
    # since the builder always passes an explicit hover budget override
    retriever = object.__new__(LanguageServerSymbolRetriever)
    retriever._ls_manager = manager
    builder = CodeMapBuilder(
        project,
        manager,
        options=CodeMapBuildOptions(show_progress=False),
        symbol_retriever=retriever,
    )
    return builder.build()


class TestJavaCodeMap:
    @pytest.mark.parametrize("language_server", [LanguageServerId.JAVA], indirect=True)
    def test_code_map_export(self, language_server: SolidLanguageServer) -> None:
        code_map = _build_code_map(language_server)

        # method symbols are present
        calculate_id = next((s.id for s in code_map.symbols_by_id.values() if s.name == "calculate"), None)
        normalize_id = next((s.id for s in code_map.symbols_by_id.values() if s.name == "normalize"), None)
        assert calculate_id is not None, "calculate method symbol not found"
        assert normalize_id is not None, "normalize method symbol not found"
        calculate = code_map.symbols_by_id[calculate_id]
        assert calculate.kind == "Method"
        assert calculate.relative_path == FIXTURE_FILE
        assert calculate.name_path == "CodeMapFixture/calculate"

        # quick info: signature and JavaDoc-derived documentation are preserved
        assert calculate.quick_info_raw is not None, "hover information was not retrieved"
        assert "calculate" in calculate.quick_info_raw
        assert calculate.signature is not None and "calculate" in calculate.signature
        combined_docs = (calculate.documentation or "") + calculate.quick_info_raw
        assert any(keyword in combined_docs for keyword in ("deterministic", "left operand", "calculated value")), (
            f"JavaDoc content missing from hover info: {calculate.quick_info_raw!r}"
        )

        # CONTAINS edge from class to method
        contains = code_map.edges_by_type("CONTAINS")
        assert any(e.target == calculate_id and "CodeMapFixture|Class" in e.source for e in contains)

        # CALLS edge calculate -> helper.normalize
        calls = code_map.edges_by_type("CALLS")
        calculate_calls = [e for e in calls if e.source == calculate_id]
        assert any(e.target == normalize_id for e in calculate_calls), (
            f"CALLS edge calculate -> normalize not found; outgoing edges: {[e.target for e in calculate_calls]}"
        )

        # called-by is available via reverse lookup
        incoming = code_map.incoming_edges("CALLS")
        assert any(e.source == calculate_id for e in incoming.get(normalize_id, []))

        # coverage reflects successful call hierarchy support
        coverage = code_map.coverage[LanguageServerId.JAVA.value]
        assert coverage.call_hierarchy == "supported"
        assert coverage.hover in ("supported", "partial")

    @pytest.mark.parametrize("language_server", [LanguageServerId.JAVA], indirect=True)
    def test_module_markdown_and_determinism(self, language_server: SolidLanguageServer) -> None:
        code_map = _build_code_map(language_server)
        files = render_code_map_files(code_map)

        module_md = files[f"modules/{FIXTURE_FILE}.md"]
        assert "calculate" in module_md
        assert "Calls:" in module_md
        assert "CodeMapHelper/normalize" in module_md

        helper_md = files[f"modules/{HELPER_FILE}.md"]
        assert "Called by:" in helper_md
        assert "CodeMapFixture/calculate" in helper_md

        # a second build from the same sources yields byte-identical output
        files_again = render_code_map_files(_build_code_map(language_server))
        assert files == files_again

    @pytest.mark.parametrize("language_server", [LanguageServerId.JAVA], indirect=True)
    def test_hierarchy_wrappers_against_real_ls(self, language_server: SolidLanguageServer) -> None:
        document_symbols = language_server.request_document_symbols(FIXTURE_FILE)
        calculate = next(s for s in document_symbols.iter_symbols() if s["name"] == "calculate")
        line = calculate["selectionRange"]["start"]["line"]
        column = calculate["selectionRange"]["start"]["character"]

        items = language_server.request_call_hierarchy_items(FIXTURE_FILE, line, column)
        assert items, "prepareCallHierarchy returned no items for calculate"
        outgoing = language_server.request_call_hierarchy_outgoing_from_items(items)
        # the exact item name is LS-specific (e.g. "normalize" or "normalize(int)"), so match loosely
        assert any(call["to"]["name"].startswith("normalize") or "CodeMapHelper" in call["to"]["uri"] for call in outgoing), (
            f"outgoing call to normalize not found: {[call['to']['name'] for call in outgoing]}"
        )
