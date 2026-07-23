"""
Unit tests for the high-level call hierarchy and type hierarchy API of SolidLanguageServer,
using a mocked language server (no LS process is started).
"""

from unittest.mock import MagicMock

import pytest

from solidlsp.ls import SolidLanguageServer
from solidlsp.ls_exceptions import SolidLSPException
from solidlsp.lsp_protocol_handler.lsp_types import ErrorCodes
from solidlsp.lsp_protocol_handler.server import LSPError


def _make_mock_ls(uri: str = "file:///repo/src/main.py") -> MagicMock:
    """Creates a mock standing in for a started SolidLanguageServer instance."""
    mock_ls = MagicMock()
    mock_ls.server_started = True
    mock_ls._resolve_file_uri.return_value = uri
    return mock_ls


def _call_hierarchy_item(name: str) -> dict:
    return {
        "name": name,
        "kind": 12,
        "uri": "file:///repo/src/main.py",
        "range": {"start": {"line": 0, "character": 0}, "end": {"line": 5, "character": 0}},
        "selectionRange": {"start": {"line": 0, "character": 4}, "end": {"line": 0, "character": 4 + len(name)}},
    }


class TestCallHierarchyApi:
    def test_prepare_is_called_with_position(self) -> None:
        mock_ls = _make_mock_ls()
        item = _call_hierarchy_item("foo")
        mock_ls.server.send.prepare_call_hierarchy.return_value = [item]

        result = SolidLanguageServer.request_call_hierarchy_items(mock_ls, "src/main.py", 3, 7)

        assert result == [item]
        mock_ls.server.send.prepare_call_hierarchy.assert_called_once_with(
            {
                "textDocument": {"uri": "file:///repo/src/main.py"},
                "position": {"line": 3, "character": 7},
            }
        )

    def test_prepare_null_response_yields_empty_list(self) -> None:
        mock_ls = _make_mock_ls()
        mock_ls.server.send.prepare_call_hierarchy.return_value = None

        result = SolidLanguageServer.request_call_hierarchy_items(mock_ls, "src/main.py", 3, 7)

        assert result == []

    def test_outgoing_receives_prepare_items(self) -> None:
        mock_ls = _make_mock_ls()
        item = _call_hierarchy_item("foo")
        outgoing_call = {"to": _call_hierarchy_item("bar"), "fromRanges": []}
        mock_ls.server.send.outgoing_calls.return_value = [outgoing_call]

        result = SolidLanguageServer.request_call_hierarchy_outgoing_from_items(mock_ls, [item])

        assert result == [outgoing_call]
        mock_ls.server.send.outgoing_calls.assert_called_once_with({"item": item})

    def test_outgoing_combines_results_of_multiple_items(self) -> None:
        mock_ls = _make_mock_ls()
        items = [_call_hierarchy_item("foo"), _call_hierarchy_item("foo2")]
        call1 = {"to": _call_hierarchy_item("a"), "fromRanges": []}
        call2 = {"to": _call_hierarchy_item("b"), "fromRanges": []}
        mock_ls.server.send.outgoing_calls.side_effect = [[call1], [call2]]

        result = SolidLanguageServer.request_call_hierarchy_outgoing_from_items(mock_ls, items)

        assert result == [call1, call2]
        assert mock_ls.server.send.outgoing_calls.call_count == 2

    def test_outgoing_null_and_empty_responses_yield_empty_list(self) -> None:
        mock_ls = _make_mock_ls()
        items = [_call_hierarchy_item("foo"), _call_hierarchy_item("foo2")]
        mock_ls.server.send.outgoing_calls.side_effect = [None, []]

        result = SolidLanguageServer.request_call_hierarchy_outgoing_from_items(mock_ls, items)

        assert result == []

    def test_incoming_receives_prepare_items(self) -> None:
        mock_ls = _make_mock_ls()
        item = _call_hierarchy_item("foo")
        incoming_call = {"from": _call_hierarchy_item("caller"), "fromRanges": []}
        mock_ls.server.send.incoming_calls.return_value = [incoming_call]

        result = SolidLanguageServer.request_call_hierarchy_incoming_from_items(mock_ls, [item])

        assert result == [incoming_call]
        mock_ls.server.send.incoming_calls.assert_called_once_with({"item": item})

    def test_convenience_method_chains_prepare_and_outgoing(self) -> None:
        mock_ls = _make_mock_ls()
        item = _call_hierarchy_item("foo")
        outgoing_call = {"to": _call_hierarchy_item("bar"), "fromRanges": []}
        mock_ls.request_call_hierarchy_items.return_value = [item]
        mock_ls.request_call_hierarchy_outgoing_from_items.return_value = [outgoing_call]

        result = SolidLanguageServer.request_call_hierarchy_outgoing(mock_ls, "src/main.py", 3, 7)

        assert result == [outgoing_call]
        mock_ls.request_call_hierarchy_items.assert_called_once_with("src/main.py", 3, 7, file_buffer=None)
        mock_ls.request_call_hierarchy_outgoing_from_items.assert_called_once_with([item])

    def test_raises_when_server_not_started(self) -> None:
        mock_ls = _make_mock_ls()
        mock_ls.server_started = False

        with pytest.raises(SolidLSPException):
            SolidLanguageServer.request_call_hierarchy_items(mock_ls, "src/main.py", 3, 7)


class TestTypeHierarchyApi:
    def test_prepare_is_called_with_position(self) -> None:
        mock_ls = _make_mock_ls()
        item = _call_hierarchy_item("MyClass")
        mock_ls.server.send.prepare_type_hierarchy.return_value = [item]

        result = SolidLanguageServer.request_type_hierarchy_items(mock_ls, "src/main.py", 1, 6)

        assert result == [item]
        mock_ls.server.send.prepare_type_hierarchy.assert_called_once_with(
            {
                "textDocument": {"uri": "file:///repo/src/main.py"},
                "position": {"line": 1, "character": 6},
            }
        )

    def test_supertypes_receives_prepare_items(self) -> None:
        mock_ls = _make_mock_ls()
        item = _call_hierarchy_item("MyClass")
        supertype = _call_hierarchy_item("MyBase")
        mock_ls.server.send.type_hierarchy_supertypes.return_value = [supertype]

        result = SolidLanguageServer.request_type_hierarchy_supertypes_from_items(mock_ls, [item])

        assert result == [supertype]
        mock_ls.server.send.type_hierarchy_supertypes.assert_called_once_with({"item": item})

    def test_subtypes_receives_prepare_items(self) -> None:
        mock_ls = _make_mock_ls()
        item = _call_hierarchy_item("MyBase")
        subtype = _call_hierarchy_item("MyClass")
        mock_ls.server.send.type_hierarchy_subtypes.return_value = [subtype]

        result = SolidLanguageServer.request_type_hierarchy_subtypes_from_items(mock_ls, [item])

        assert result == [subtype]

    def test_null_responses_yield_empty_list(self) -> None:
        mock_ls = _make_mock_ls()
        mock_ls.server.send.prepare_type_hierarchy.return_value = None
        assert SolidLanguageServer.request_type_hierarchy_items(mock_ls, "src/main.py", 1, 6) == []

        mock_ls.server.send.type_hierarchy_supertypes.return_value = None
        assert SolidLanguageServer.request_type_hierarchy_supertypes_from_items(mock_ls, [_call_hierarchy_item("C")]) == []

    def test_raises_when_server_not_started(self) -> None:
        mock_ls = _make_mock_ls()
        mock_ls.server_started = False

        with pytest.raises(SolidLSPException):
            SolidLanguageServer.request_type_hierarchy_items(mock_ls, "src/main.py", 1, 6)


class TestMethodNotFoundDetection:
    def test_method_not_found_is_detected(self) -> None:
        exc = SolidLSPException("request failed", cause=LSPError(ErrorCodes.MethodNotFound, "method not found"))
        assert exc.is_method_not_found()

    def test_other_lsp_errors_are_not_method_not_found(self) -> None:
        exc = SolidLSPException("request failed", cause=LSPError(ErrorCodes.InternalError, "boom"))
        assert not exc.is_method_not_found()

    def test_non_lsp_cause_is_not_method_not_found(self) -> None:
        assert not SolidLSPException("request failed", cause=ValueError("x")).is_method_not_found()
        assert not SolidLSPException("request failed").is_method_not_found()


class TestRequestNotSupportedDetection:
    def test_method_not_found_is_request_not_supported(self) -> None:
        exc = SolidLSPException("request failed", cause=LSPError(ErrorCodes.MethodNotFound, "method not found"))
        assert exc.is_request_not_supported()

    def test_request_failed_with_missing_handler_is_request_not_supported(self) -> None:
        # the JetBrains Kotlin language server responds like this for unimplemented methods
        from solidlsp.lsp_protocol_handler.lsp_types import LSPErrorCodes

        exc = SolidLSPException(
            "request failed",
            cause=LSPError(LSPErrorCodes.RequestFailed, "no handler for request: textDocument/prepareCallHierarchy"),
        )
        assert exc.is_request_not_supported()

    def test_request_failed_with_other_message_is_not_request_not_supported(self) -> None:
        from solidlsp.lsp_protocol_handler.lsp_types import LSPErrorCodes

        exc = SolidLSPException("request failed", cause=LSPError(LSPErrorCodes.RequestFailed, "index not ready"))
        assert not exc.is_request_not_supported()

    def test_other_errors_are_not_request_not_supported(self) -> None:
        assert not SolidLSPException("request failed", cause=LSPError(ErrorCodes.InternalError, "boom")).is_request_not_supported()
        assert not SolidLSPException("request failed").is_request_not_supported()
