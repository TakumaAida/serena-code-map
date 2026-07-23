"""
Unit tests for the code map builder and the quick info parser, using mocked language servers.
"""

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from serena.code_map.builder import CodeMapBuilder, CodeMapBuildOptions
from serena.code_map.model import CodeMap
from serena.code_map.quick_info import parse_quick_info
from solidlsp.ls import DocumentSymbols
from solidlsp.ls_exceptions import SolidLSPException
from solidlsp.lsp_protocol_handler.lsp_types import ErrorCodes, SymbolKind
from solidlsp.lsp_protocol_handler.server import LSPError

PROJECT_ROOT = "/repo"


# region fixtures


def _range(line: int, start_col: int = 0, end_col: int = 80, end_line: int | None = None) -> dict:
    return {
        "start": {"line": line, "character": start_col},
        "end": {"line": end_line if end_line is not None else line, "character": end_col},
    }


def make_symbol(
    name: str,
    kind: SymbolKind,
    path: str,
    line: int,
    end_line: int | None = None,
    children: list[dict] | None = None,
    overload_idx: int | None = None,
) -> dict:
    symbol: dict[str, Any] = {
        "name": name,
        "kind": int(kind),
        "selectionRange": _range(line, 4, 4 + len(name)),
        "location": {
            "uri": f"file://{PROJECT_ROOT}/{path}",
            "range": _range(line, 0, 0, end_line if end_line is not None else line + 1),
            "relativePath": path,
            "absolutePath": f"{PROJECT_ROOT}/{path}",
        },
        "children": children or [],
        "parent": None,
    }
    if overload_idx is not None:
        symbol["overload_idx"] = overload_idx
    for child in symbol["children"]:
        child["parent"] = symbol
    return symbol


def hierarchy_item(name: str, kind: SymbolKind, path: str | None, line: int, external_uri: str | None = None) -> dict:
    uri = external_uri if external_uri is not None else f"file://{PROJECT_ROOT}/{path}"
    return {
        "name": name,
        "kind": int(kind),
        "uri": uri,
        "range": _range(line, 0, 0, line + 1),
        "selectionRange": _range(line, 4, 4 + len(name)),
    }


def outgoing_call(to_item: dict, from_lines: list[int]) -> dict:
    return {"to": to_item, "fromRanges": [_range(line, 8, 20) for line in from_lines]}


class FakeLanguageServer:
    def __init__(self, ls_id: str = "mock") -> None:
        self.ls_id = SimpleNamespace(value=ls_id)
        self.document_symbols_by_file: dict[str, list[dict]] = {}
        self.outgoing_calls_by_location: dict[tuple[str, int, int], Any] = {}
        self.supertypes_by_location: dict[tuple[str, int, int], Any] = {}
        # spies for methods that must never be invoked by the exporter
        self.request_call_hierarchy_incoming = MagicMock(name="request_call_hierarchy_incoming")
        self.request_type_hierarchy_subtypes = MagicMock(name="request_type_hierarchy_subtypes")

    def request_document_symbols(self, relative_file_path: str) -> DocumentSymbols:
        return DocumentSymbols(self.document_symbols_by_file.get(relative_file_path, []))

    def open_file(self, relative_file_path: str):
        from contextlib import contextmanager

        @contextmanager
        def cm():
            yield MagicMock(name="file_buffer")

        return cm()

    def request_call_hierarchy_outgoing(self, relative_file_path: str, line: int, column: int, file_buffer: Any = None) -> list[dict]:
        result = self.outgoing_calls_by_location.get((relative_file_path, line, column), [])
        if isinstance(result, Exception):
            raise result
        return result

    def request_type_hierarchy_supertypes(self, relative_file_path: str, line: int, column: int, file_buffer: Any = None) -> list[dict]:
        result = self.supertypes_by_location.get((relative_file_path, line, column), [])
        if isinstance(result, Exception):
            raise result
        return result


class FakeLanguageServerManager:
    def __init__(self, language_server: FakeLanguageServer) -> None:
        self._language_server = language_server

    def get_language_server(self, relative_path: str) -> FakeLanguageServer:
        return self._language_server


class FakeProject:
    def __init__(self, source_files: list[str]) -> None:
        self.project_name = "test-project"
        self.project_root = PROJECT_ROOT
        self._source_files = source_files

    def gather_source_files(self) -> list[str]:
        return list(self._source_files)


class FakeSymbolRetriever:
    def __init__(self, info_by_symbol_name: dict[str, str] | None = None) -> None:
        self.info_by_symbol_name = info_by_symbol_name or {}
        self.received_budget_override: float | None = None

    def request_info_for_symbol_batch(self, symbols: list, *, budget_seconds_override: float | None = None) -> dict:
        self.received_budget_override = budget_seconds_override
        return {symbol: self.info_by_symbol_name.get(symbol.name) for symbol in symbols}


def build_code_map(
    files: dict[str, list[dict]],
    ls: FakeLanguageServer | None = None,
    options: CodeMapBuildOptions | None = None,
    retriever: FakeSymbolRetriever | None = None,
) -> tuple[CodeMap, FakeLanguageServer]:
    ls = ls or FakeLanguageServer()
    ls.document_symbols_by_file.update(files)
    project = FakeProject(sorted(files.keys()))
    manager = FakeLanguageServerManager(ls)
    builder = CodeMapBuilder(
        project,
        manager,
        options=options or CodeMapBuildOptions(show_progress=False),
        symbol_retriever=retriever or FakeSymbolRetriever(),
    )
    return builder.build(), ls


def method_not_found_error() -> SolidLSPException:
    return SolidLSPException("not supported", cause=LSPError(ErrorCodes.MethodNotFound, "method not found"))


# endregion


class TestSymbolEnumeration:
    def test_stable_id_format(self) -> None:
        method = make_symbol("cancelOrder", SymbolKind.Method, "src/OrderService.java", 3)
        clazz = make_symbol("OrderService", SymbolKind.Class, "src/OrderService.java", 1, children=[method])
        code_map, _ = build_code_map({"src/OrderService.java": [clazz]})

        assert "mock|src/OrderService.java|OrderService|Class" in code_map.symbols_by_id
        assert "mock|src/OrderService.java|OrderService/cancelOrder|Method" in code_map.symbols_by_id

    def test_overload_index_is_part_of_id(self) -> None:
        m0 = make_symbol("calc", SymbolKind.Method, "src/A.java", 2, overload_idx=0)
        m1 = make_symbol("calc", SymbolKind.Method, "src/A.java", 5, overload_idx=1)
        clazz = make_symbol("A", SymbolKind.Class, "src/A.java", 1, children=[m0, m1])
        code_map, _ = build_code_map({"src/A.java": [clazz]})

        assert "mock|src/A.java|A/calc[0]|Method" in code_map.symbols_by_id
        assert "mock|src/A.java|A/calc[1]|Method" in code_map.symbols_by_id

    def test_id_collision_gets_position_suffix(self) -> None:
        # two symbols producing an identical name path and kind (no overload index assigned)
        m0 = make_symbol("dup", SymbolKind.Function, "src/a.py", 2)
        m1 = make_symbol("dup", SymbolKind.Function, "src/a.py", 7)
        code_map, _ = build_code_map({"src/a.py": [m0, m1]})

        assert "mock|src/a.py|dup|Function" in code_map.symbols_by_id
        assert "mock|src/a.py|dup|Function|7:4" in code_map.symbols_by_id

    def test_low_level_symbols_are_excluded(self) -> None:
        variable = make_symbol("x", SymbolKind.Variable, "src/a.py", 3)
        field = make_symbol("f", SymbolKind.Field, "src/a.py", 2)
        clazz = make_symbol("A", SymbolKind.Class, "src/a.py", 1, children=[field])
        code_map, _ = build_code_map({"src/a.py": [clazz, variable]})

        assert len(code_map.symbols_by_id) == 1
        assert next(iter(code_map.symbols_by_id.values())).name == "A"

    def test_parent_and_child_ids(self) -> None:
        method = make_symbol("m", SymbolKind.Method, "src/a.py", 2)
        clazz = make_symbol("A", SymbolKind.Class, "src/a.py", 1, children=[method])
        code_map, _ = build_code_map({"src/a.py": [clazz]})

        class_symbol = code_map.symbols_by_id["mock|src/a.py|A|Class"]
        method_symbol = code_map.symbols_by_id["mock|src/a.py|A/m|Method"]
        assert method_symbol.parent_id == class_symbol.id
        assert class_symbol.child_ids == [method_symbol.id]

    def test_contains_edges(self) -> None:
        method = make_symbol("m", SymbolKind.Method, "src/a.py", 2)
        clazz = make_symbol("A", SymbolKind.Class, "src/a.py", 1, children=[method])
        code_map, _ = build_code_map({"src/a.py": [clazz]})

        contains = code_map.edges_by_type("CONTAINS")
        assert len(contains) == 1
        assert contains[0].source == "mock|src/a.py|A|Class"
        assert contains[0].target == "mock|src/a.py|A/m|Method"
        assert contains[0].resolution == "documentSymbol"


class TestQuickInfo:
    def test_quick_info_is_parsed_and_stored(self) -> None:
        func = make_symbol("calculate", SymbolKind.Function, "src/a.py", 1)
        retriever = FakeSymbolRetriever({"calculate": "```python\ndef calculate(left, right)\n```\nAdds two operands."})
        code_map, _ = build_code_map({"src/a.py": [func]}, retriever=retriever)

        symbol = code_map.symbols_by_id["mock|src/a.py|calculate|Function"]
        assert symbol.quick_info_raw is not None and "calculate" in symbol.quick_info_raw
        assert symbol.signature == "def calculate(left, right)"
        assert symbol.documentation == "Adds two operands."

    def test_budget_override_is_passed(self) -> None:
        func = make_symbol("f", SymbolKind.Function, "src/a.py", 1)
        retriever = FakeSymbolRetriever()
        build_code_map(
            {"src/a.py": [func]}, options=CodeMapBuildOptions(show_progress=False, hover_budget_seconds=42.0), retriever=retriever
        )

        assert retriever.received_budget_override == 42.0

    def test_no_docs_option_skips_hover(self) -> None:
        func = make_symbol("f", SymbolKind.Function, "src/a.py", 1)
        retriever = FakeSymbolRetriever({"f": "info"})
        code_map, _ = build_code_map(
            {"src/a.py": [func]}, options=CodeMapBuildOptions(show_progress=False, include_docs=False), retriever=retriever
        )

        assert retriever.received_budget_override is None  # never called
        assert code_map.symbols_by_id["mock|src/a.py|f|Function"].quick_info_raw is None

    def test_absolute_project_paths_are_stripped_from_hover(self) -> None:
        func = make_symbol("f", SymbolKind.Function, "src/a.py", 1)
        raw = f"```python\ndef f()\n```\nDocs with a link: [src](file://{PROJECT_ROOT}/src/a.py#1) and path {PROJECT_ROOT}/src/a.py."
        retriever = FakeSymbolRetriever({"f": raw})
        code_map, _ = build_code_map({"src/a.py": [func]}, retriever=retriever)

        symbol = code_map.symbols_by_id["mock|src/a.py|f|Function"]
        assert symbol.quick_info_raw is not None
        assert PROJECT_ROOT not in symbol.quick_info_raw
        assert "src/a.py" in symbol.quick_info_raw

    def test_missing_hover_does_not_fabricate(self) -> None:
        func = make_symbol("f", SymbolKind.Function, "src/a.py", 1)
        code_map, _ = build_code_map({"src/a.py": [func]}, retriever=FakeSymbolRetriever())

        symbol = code_map.symbols_by_id["mock|src/a.py|f|Function"]
        assert symbol.quick_info_raw is None
        assert symbol.signature is None
        assert symbol.documentation is None


class TestCallEdges:
    def _two_class_fixture(self) -> dict[str, list[dict]]:
        cancel = make_symbol("cancelOrder", SymbolKind.Method, "src/OrderService.java", 3)
        order_service = make_symbol("OrderService", SymbolKind.Class, "src/OrderService.java", 1, children=[cancel])
        refund = make_symbol("requestRefund", SymbolKind.Method, "src/RefundService.java", 3)
        refund_service = make_symbol("RefundService", SymbolKind.Class, "src/RefundService.java", 1, children=[refund])
        return {"src/OrderService.java": [order_service], "src/RefundService.java": [refund_service]}

    def test_calls_edge_resolved_to_internal_symbol(self) -> None:
        ls = FakeLanguageServer()
        target = hierarchy_item("requestRefund", SymbolKind.Method, "src/RefundService.java", 3)
        ls.outgoing_calls_by_location[("src/OrderService.java", 3, 4)] = [outgoing_call(target, [5])]
        code_map, _ = build_code_map(self._two_class_fixture(), ls=ls)

        calls = code_map.edges_by_type("CALLS")
        assert len(calls) == 1
        assert calls[0].source == "mock|src/OrderService.java|OrderService/cancelOrder|Method"
        assert calls[0].target == "mock|src/RefundService.java|RefundService/requestRefund|Method"
        assert calls[0].count == 1
        assert len(calls[0].source_ranges) == 1
        assert calls[0].resolution == "callHierarchy"

    def test_duplicate_callsites_are_aggregated(self) -> None:
        ls = FakeLanguageServer()
        target = hierarchy_item("requestRefund", SymbolKind.Method, "src/RefundService.java", 3)
        ls.outgoing_calls_by_location[("src/OrderService.java", 3, 4)] = [
            outgoing_call(target, [5, 8]),
            outgoing_call(target, [12]),
        ]
        code_map, _ = build_code_map(self._two_class_fixture(), ls=ls)

        calls = code_map.edges_by_type("CALLS")
        assert len(calls) == 1
        assert calls[0].count == 3
        assert len(calls[0].source_ranges) == 3

    def test_called_by_is_available_via_reverse_lookup(self) -> None:
        ls = FakeLanguageServer()
        target = hierarchy_item("requestRefund", SymbolKind.Method, "src/RefundService.java", 3)
        ls.outgoing_calls_by_location[("src/OrderService.java", 3, 4)] = [outgoing_call(target, [5])]
        code_map, _ = build_code_map(self._two_class_fixture(), ls=ls)

        incoming = code_map.incoming_edges("CALLS")
        callers = incoming["mock|src/RefundService.java|RefundService/requestRefund|Method"]
        assert [e.source for e in callers] == ["mock|src/OrderService.java|OrderService/cancelOrder|Method"]

    def test_external_target_creates_minimal_symbol(self) -> None:
        ls = FakeLanguageServer()
        external = hierarchy_item("println", SymbolKind.Method, None, 10, external_uri="file:///jdk/java/io/PrintStream.java")
        ls.outgoing_calls_by_location[("src/OrderService.java", 3, 4)] = [outgoing_call(external, [5])]
        code_map, _ = build_code_map(self._two_class_fixture(), ls=ls)

        calls = code_map.edges_by_type("CALLS")
        assert len(calls) == 1
        target_id = calls[0].target
        assert target_id == "external|file:PrintStream.java|println|Method"
        external_symbol = code_map.symbols_by_id[target_id]
        assert external_symbol.is_external
        assert external_symbol.relative_path is None

    def test_class_depends_on_aggregation(self) -> None:
        ls = FakeLanguageServer()
        target = hierarchy_item("requestRefund", SymbolKind.Method, "src/RefundService.java", 3)
        ls.outgoing_calls_by_location[("src/OrderService.java", 3, 4)] = [outgoing_call(target, [5, 8])]
        code_map, _ = build_code_map(self._two_class_fixture(), ls=ls)

        depends = code_map.edges_by_type("CLASS_DEPENDS_ON")
        assert len(depends) == 1
        assert depends[0].source == "mock|src/OrderService.java|OrderService|Class"
        assert depends[0].target == "mock|src/RefundService.java|RefundService|Class"
        assert depends[0].count == 2
        assert depends[0].resolution == "derived"

    def test_class_depends_on_excludes_same_owner(self) -> None:
        helper = make_symbol("helper", SymbolKind.Method, "src/A.java", 5)
        main = make_symbol("main", SymbolKind.Method, "src/A.java", 2)
        clazz = make_symbol("A", SymbolKind.Class, "src/A.java", 1, children=[main, helper])
        ls = FakeLanguageServer()
        target = hierarchy_item("helper", SymbolKind.Method, "src/A.java", 5)
        ls.outgoing_calls_by_location[("src/A.java", 2, 4)] = [outgoing_call(target, [3])]
        code_map, _ = build_code_map({"src/A.java": [clazz]}, ls=ls)

        assert code_map.edges_by_type("CALLS")  # the call edge itself exists
        assert code_map.edges_by_type("CLASS_DEPENDS_ON") == []

    def test_unsupported_call_hierarchy_short_circuits(self) -> None:
        m1 = make_symbol("m1", SymbolKind.Method, "src/A.java", 2)
        m2 = make_symbol("m2", SymbolKind.Method, "src/A.java", 5)
        clazz = make_symbol("A", SymbolKind.Class, "src/A.java", 1, children=[m1, m2])
        ls = FakeLanguageServer()
        ls.outgoing_calls_by_location[("src/A.java", 2, 4)] = method_not_found_error()
        ls.outgoing_calls_by_location[("src/A.java", 5, 4)] = method_not_found_error()

        attempted_locations: list[tuple] = []
        original = ls.request_call_hierarchy_outgoing

        def spy(path: str, line: int, column: int, file_buffer: Any = None) -> list[dict]:
            attempted_locations.append((path, line, column))
            return original(path, line, column, file_buffer)

        ls.request_call_hierarchy_outgoing = spy  # type: ignore[method-assign]
        code_map, _ = build_code_map({"src/A.java": [clazz]}, ls=ls)

        assert attempted_locations == [("src/A.java", 2, 4)]  # second symbol is not attempted
        assert code_map.coverage["mock"].call_hierarchy == "unsupported"
        assert code_map.edges_by_type("CALLS") == []

    def test_single_symbol_failure_does_not_stop_export(self) -> None:
        m1 = make_symbol("m1", SymbolKind.Method, "src/A.java", 2)
        m2 = make_symbol("m2", SymbolKind.Method, "src/A.java", 5)
        m3 = make_symbol("m3", SymbolKind.Method, "src/A.java", 8)
        clazz = make_symbol("A", SymbolKind.Class, "src/A.java", 1, children=[m1, m2, m3])
        ls = FakeLanguageServer()
        ls.outgoing_calls_by_location[("src/A.java", 2, 4)] = SolidLSPException("boom")
        target = hierarchy_item("m3", SymbolKind.Method, "src/A.java", 8)
        ls.outgoing_calls_by_location[("src/A.java", 5, 4)] = [outgoing_call(target, [6])]
        code_map, _ = build_code_map({"src/A.java": [clazz]}, ls=ls)

        calls = code_map.edges_by_type("CALLS")
        assert len(calls) == 1
        assert calls[0].source.endswith("A/m2|Method")
        assert code_map.coverage["mock"].errors == 1
        assert code_map.coverage["mock"].call_hierarchy == "partial"
        assert any(d.level == "warning" and d.phase == "callHierarchy" for d in code_map.diagnostics)

    def test_incoming_calls_is_never_invoked_by_exporter(self) -> None:
        ls = FakeLanguageServer()
        target = hierarchy_item("requestRefund", SymbolKind.Method, "src/RefundService.java", 3)
        ls.outgoing_calls_by_location[("src/OrderService.java", 3, 4)] = [outgoing_call(target, [5])]
        _, ls = build_code_map(self._two_class_fixture(), ls=ls)

        ls.request_call_hierarchy_incoming.assert_not_called()


class TestTypeEdges:
    def test_supertype_edge(self) -> None:
        base = make_symbol("Base", SymbolKind.Class, "src/base.py", 1)
        derived = make_symbol("Derived", SymbolKind.Class, "src/derived.py", 1)
        ls = FakeLanguageServer()
        ls.supertypes_by_location[("src/derived.py", 1, 4)] = [hierarchy_item("Base", SymbolKind.Class, "src/base.py", 1)]
        code_map, _ = build_code_map({"src/base.py": [base], "src/derived.py": [derived]}, ls=ls)

        supertypes = code_map.edges_by_type("TYPE_SUPERTYPE")
        assert len(supertypes) == 1
        assert supertypes[0].source == "mock|src/derived.py|Derived|Class"
        assert supertypes[0].target == "mock|src/base.py|Base|Class"

    def test_subtypes_is_never_invoked_by_exporter(self) -> None:
        derived = make_symbol("Derived", SymbolKind.Class, "src/derived.py", 1)
        _, ls = build_code_map({"src/derived.py": [derived]})

        ls.request_type_hierarchy_subtypes.assert_not_called()

    def test_unsupported_type_hierarchy_is_recorded(self) -> None:
        derived = make_symbol("Derived", SymbolKind.Class, "src/derived.py", 1)
        ls = FakeLanguageServer()
        ls.supertypes_by_location[("src/derived.py", 1, 4)] = method_not_found_error()
        code_map, _ = build_code_map({"src/derived.py": [derived]}, ls=ls)

        assert code_map.coverage["mock"].type_hierarchy == "unsupported"


class TestQuickInfoParser:
    def test_fenced_java_signature_and_javadoc(self) -> None:
        raw = "```java\npublic int calculate(int left, int right)\n```\nPerforms a calculation.\n\n@param left left operand\n@return the value"
        parts = parse_quick_info(raw, "calculate")
        assert parts is not None
        assert parts.signature == "public int calculate(int left, int right)"
        assert parts.documentation is not None
        assert "@param left" in parts.documentation
        assert parts.raw == raw

    def test_fenced_python_signature_and_docstring(self) -> None:
        raw = "```python\ndef foo(a: int) -> str\n```\n---\nDoes foo things."
        parts = parse_quick_info(raw, "foo")
        assert parts is not None
        assert parts.signature == "def foo(a: int) -> str"
        assert "Does foo things." in (parts.documentation or "")

    def test_plain_signature_with_description(self) -> None:
        parts = parse_quick_info("def bar(x)\nSome description", "bar")
        assert parts is not None
        assert parts.signature == "def bar(x)"
        assert parts.documentation == "Some description"

    def test_description_only(self) -> None:
        parts = parse_quick_info("Just a plain description without any declaration syntax", "unrelated_symbol")
        assert parts is not None
        assert parts.signature is None
        assert parts.documentation == "Just a plain description without any declaration syntax"

    def test_none_input(self) -> None:
        assert parse_quick_info(None, "x") is None
        assert parse_quick_info("   \n  ", "x") is None

    def test_crlf_normalization(self) -> None:
        parts = parse_quick_info("```java\r\nvoid m()\r\n```\r\nDocs here.", "m")
        assert parts is not None
        assert parts.signature == "void m()"
        assert parts.documentation == "Docs here."
        assert "\r" not in parts.raw

    def test_multiple_code_blocks_uses_first_as_signature(self) -> None:
        raw = "```python\ndef first()\n```\nSome docs.\n```python\nexample_usage()\n```"
        parts = parse_quick_info(raw, "first")
        assert parts is not None
        assert parts.signature == "def first()"
        assert "Some docs." in (parts.documentation or "")
        assert "example_usage" not in (parts.documentation or "")

    def test_raw_is_never_lost(self) -> None:
        raw = "some ambiguous content"
        parts = parse_quick_info(raw, "sym")
        assert parts is not None
        assert parts.raw == raw
