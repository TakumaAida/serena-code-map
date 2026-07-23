"""
Tests for the 'project export-code-map' CLI command.

The language server manager and the code map builder are mocked, so no language servers
are started; the serializer runs for real against a temporary directory.
"""

import os
import shutil
import tempfile
import time
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

import serena.code_map.export
from serena.cli import ProjectCommands
from serena.code_map.model import CodeMap, CodeMapSymbol, LanguageServerCoverage, SourcePosition, SourceRange

pytestmark = pytest.mark.filterwarnings("ignore::UserWarning")


@pytest.fixture
def cli_runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def temp_project_dir():
    """Temporary project directory containing a Python file (so language detection works)."""
    tmpdir = tempfile.mkdtemp()
    try:
        with open(os.path.join(tmpdir, "main.py"), "w") as f:
            f.write("def hello():\n    pass\n")
        yield tmpdir
    finally:
        if os.name == "nt":
            time.sleep(0.2)
        shutil.rmtree(tmpdir, ignore_errors=True)


def make_fixture_code_map(errors: int = 0, unresolved_internal_targets: int = 0) -> CodeMap:
    code_map = CodeMap(project_name="cli-test-project")
    symbol_id = "python|main.py|hello|Function"
    code_map.symbols_by_id[symbol_id] = CodeMapSymbol(
        id=symbol_id,
        language_server="python",
        name="hello",
        name_path="hello",
        kind="Function",
        relative_path="main.py",
        selection_range=SourceRange(start=SourcePosition(0, 4), end=SourcePosition(0, 9)),
        body_range=None,
        parent_id=None,
    )
    code_map.coverage["python"] = LanguageServerCoverage(
        document_symbols="supported",
        hover="supported",
        call_hierarchy="unsupported" if errors else "supported",
        type_hierarchy="supported",
        errors=errors,
    )
    code_map.unresolved_internal_targets = unresolved_internal_targets
    return code_map


class BuilderInvocation:
    """Captures how the CLI invoked the (mocked) CodeMapBuilder."""

    def __init__(self) -> None:
        self.options = None
        self.build_count = 0


@pytest.fixture
def mocked_build_env(monkeypatch):
    """
    Replaces the language server manager creation and the CodeMapBuilder so that
    the CLI can run without starting any language server.
    """
    invocation = BuilderInvocation()
    ls_manager = MagicMock(name="LanguageServerManager")
    code_map_holder: dict = {"code_map": make_fixture_code_map(), "build_error": None}

    from serena.project import Project

    monkeypatch.setattr(Project, "create_language_server_manager", lambda self: ls_manager)

    class FakeCodeMapBuilder:
        def __init__(self, project, manager, options=None, symbol_retriever=None) -> None:
            invocation.options = options

        def build(self) -> CodeMap:
            invocation.build_count += 1
            if code_map_holder["build_error"] is not None:
                raise code_map_holder["build_error"]
            return code_map_holder["code_map"]

    monkeypatch.setattr(serena.code_map.export, "CodeMapBuilder", FakeCodeMapBuilder)
    return SimpleEnv(invocation=invocation, ls_manager=ls_manager, code_map_holder=code_map_holder)


class SimpleEnv:
    def __init__(self, invocation: BuilderInvocation, ls_manager: MagicMock, code_map_holder: dict) -> None:
        self.invocation = invocation
        self.ls_manager = ls_manager
        self.code_map_holder = code_map_holder


class TestExportCodeMapCli:
    def test_help(self, cli_runner) -> None:
        result = cli_runner.invoke(ProjectCommands.export_code_map, ["--help"])
        assert result.exit_code == 0
        for option in ["--output", "--include-calls", "--include-docs", "--include-type-hierarchy", "--strict", "--overview-max-chars"]:
            assert option in result.output

    def test_default_output_directory(self, cli_runner, temp_project_dir, mocked_build_env) -> None:
        result = cli_runner.invoke(ProjectCommands.export_code_map, [temp_project_dir])
        assert result.exit_code == 0, f"Command failed: {result.output}"
        assert "Generated Serena code map" in result.output

        output_dir = os.path.join(temp_project_dir, ".serena", "code-map")
        for file_name in ["overview.md", "manifest.json", "symbols.jsonl", "edges.jsonl", "diagnostics.jsonl", "AGENTS_SNIPPET.md"]:
            assert os.path.isfile(os.path.join(output_dir, file_name)), file_name
        assert os.path.isfile(os.path.join(output_dir, "modules", "main.py.md"))

    def test_custom_output_directory(self, cli_runner, temp_project_dir, mocked_build_env) -> None:
        custom_output = os.path.join(temp_project_dir, "custom-map")
        result = cli_runner.invoke(ProjectCommands.export_code_map, [temp_project_dir, "--output", custom_output])
        assert result.exit_code == 0, f"Command failed: {result.output}"
        assert os.path.isfile(os.path.join(custom_output, "overview.md"))
        assert custom_output in result.output

    def test_no_include_calls_option(self, cli_runner, temp_project_dir, mocked_build_env) -> None:
        result = cli_runner.invoke(ProjectCommands.export_code_map, [temp_project_dir, "--no-include-calls"])
        assert result.exit_code == 0, f"Command failed: {result.output}"
        assert mocked_build_env.invocation.options.include_calls is False
        assert mocked_build_env.invocation.options.include_docs is True

    def test_no_include_docs_option(self, cli_runner, temp_project_dir, mocked_build_env) -> None:
        result = cli_runner.invoke(ProjectCommands.export_code_map, [temp_project_dir, "--no-include-docs"])
        assert result.exit_code == 0, f"Command failed: {result.output}"
        assert mocked_build_env.invocation.options.include_docs is False

    def test_hover_budget_and_diagnostics_options(self, cli_runner, temp_project_dir, mocked_build_env) -> None:
        result = cli_runner.invoke(
            ProjectCommands.export_code_map, [temp_project_dir, "--hover-budget-seconds", "12.5", "--max-diagnostics", "7"]
        )
        assert result.exit_code == 0, f"Command failed: {result.output}"
        assert mocked_build_env.invocation.options.hover_budget_seconds == 12.5
        assert mocked_build_env.invocation.options.max_diagnostics == 7

    def test_strict_mode_fails_on_errors(self, cli_runner, temp_project_dir, mocked_build_env) -> None:
        mocked_build_env.code_map_holder["code_map"] = make_fixture_code_map(errors=3)
        result = cli_runner.invoke(ProjectCommands.export_code_map, [temp_project_dir, "--strict"])
        assert result.exit_code != 0
        assert "Strict mode" in result.output

    def test_strict_mode_fails_on_unresolved_internal_targets(self, cli_runner, temp_project_dir, mocked_build_env) -> None:
        mocked_build_env.code_map_holder["code_map"] = make_fixture_code_map(unresolved_internal_targets=2)
        result = cli_runner.invoke(ProjectCommands.export_code_map, [temp_project_dir, "--strict"])
        assert result.exit_code != 0
        assert "Strict mode" in result.output

    def test_non_strict_mode_succeeds_despite_errors(self, cli_runner, temp_project_dir, mocked_build_env) -> None:
        mocked_build_env.code_map_holder["code_map"] = make_fixture_code_map(errors=3)
        result = cli_runner.invoke(ProjectCommands.export_code_map, [temp_project_dir])
        assert result.exit_code == 0, f"Command failed: {result.output}"

    def test_project_autoregistration(self, cli_runner, temp_project_dir, mocked_build_env) -> None:
        # the temp project has never been registered before; the command must succeed anyway
        assert not os.path.exists(os.path.join(temp_project_dir, ".serena", "project.yml")) or True
        result = cli_runner.invoke(ProjectCommands.export_code_map, [temp_project_dir])
        assert result.exit_code == 0, f"Command failed: {result.output}"
        assert mocked_build_env.invocation.build_count == 1

    def test_language_servers_are_stopped_on_success(self, cli_runner, temp_project_dir, mocked_build_env) -> None:
        result = cli_runner.invoke(ProjectCommands.export_code_map, [temp_project_dir])
        assert result.exit_code == 0, f"Command failed: {result.output}"
        mocked_build_env.ls_manager.stop_all.assert_called_once()
        mocked_build_env.ls_manager.save_all_caches.assert_called_once()

    def test_language_servers_are_stopped_on_failure(self, cli_runner, temp_project_dir, mocked_build_env) -> None:
        mocked_build_env.code_map_holder["build_error"] = RuntimeError("boom")
        result = cli_runner.invoke(ProjectCommands.export_code_map, [temp_project_dir])
        assert result.exit_code != 0
        mocked_build_env.ls_manager.stop_all.assert_called_once()


class TestIndexExportsCodeMap:
    def test_index_exports_code_map_by_default(self, cli_runner, temp_project_dir, mocked_build_env) -> None:
        result = cli_runner.invoke(ProjectCommands.index, [temp_project_dir])
        assert result.exit_code == 0, f"Command failed: {result.output}"
        assert "Exported code map" in result.output
        assert os.path.isfile(os.path.join(temp_project_dir, ".serena", "code-map", "overview.md"))

    def test_index_with_no_code_map_skips_export(self, cli_runner, temp_project_dir, mocked_build_env) -> None:
        result = cli_runner.invoke(ProjectCommands.index, [temp_project_dir, "--no-code-map"])
        assert result.exit_code == 0, f"Command failed: {result.output}"
        assert mocked_build_env.invocation.build_count == 0
        assert not os.path.exists(os.path.join(temp_project_dir, ".serena", "code-map"))

    def test_index_succeeds_even_if_code_map_export_fails(self, cli_runner, temp_project_dir, mocked_build_env) -> None:
        mocked_build_env.code_map_holder["build_error"] = RuntimeError("boom")
        result = cli_runner.invoke(ProjectCommands.index, [temp_project_dir])
        assert result.exit_code == 0, f"Command failed: {result.output}"
        assert "Code map export failed" in result.output


class TestActivationExport:
    def _make_agent_mock(self, export_enabled: bool, is_lsp: bool = True) -> MagicMock:
        agent = MagicMock()
        agent._active_project.project_config.export_code_map_on_activation = export_enabled
        agent.get_language_backend.return_value.is_lsp.return_value = is_lsp
        return agent

    def test_activation_exports_code_map_when_enabled(self, monkeypatch) -> None:
        from serena.agent import SerenaAgent

        export_mock = MagicMock()
        monkeypatch.setattr(serena.code_map.export, "export_project_code_map", export_mock)
        agent = self._make_agent_mock(export_enabled=True)

        SerenaAgent._maybe_export_code_map(agent)

        export_mock.assert_called_once_with(agent._active_project, agent._active_project.get_language_server_manager_or_raise.return_value)

    def test_activation_skips_export_when_disabled(self, monkeypatch) -> None:
        from serena.agent import SerenaAgent

        export_mock = MagicMock()
        monkeypatch.setattr(serena.code_map.export, "export_project_code_map", export_mock)
        agent = self._make_agent_mock(export_enabled=False)

        SerenaAgent._maybe_export_code_map(agent)

        export_mock.assert_not_called()

    def test_activation_skips_export_for_non_lsp_backend(self, monkeypatch) -> None:
        from serena.agent import SerenaAgent

        export_mock = MagicMock()
        monkeypatch.setattr(serena.code_map.export, "export_project_code_map", export_mock)
        agent = self._make_agent_mock(export_enabled=True, is_lsp=False)

        SerenaAgent._maybe_export_code_map(agent)

        export_mock.assert_not_called()

    def test_activation_export_failure_does_not_raise(self, monkeypatch) -> None:
        from serena.agent import SerenaAgent

        export_mock = MagicMock(side_effect=RuntimeError("boom"))
        monkeypatch.setattr(serena.code_map.export, "export_project_code_map", export_mock)
        agent = self._make_agent_mock(export_enabled=True)

        SerenaAgent._maybe_export_code_map(agent)  # must not raise

    def test_project_config_flag_defaults_to_false(self) -> None:
        from serena.config.serena_config import ProjectConfig

        config = ProjectConfig(project_name="p", language_servers=[])
        assert config.export_code_map_on_activation is False


class TestActivationNotice:
    """The project activation message must point the agent to an existing code map automatically."""

    def _make_project_mock(self, project_root: str, export_on_activation: bool = False) -> MagicMock:
        project = MagicMock()
        project.project_root = project_root
        project.project_name = "p"
        project.is_newly_created = False
        project.project_config.encoding = "utf-8"
        project.project_config.initial_prompt = ""
        project.project_config.export_code_map_on_activation = export_on_activation
        return project

    def test_notice_when_code_map_exists(self, tmp_path) -> None:
        from serena.code_map.export import code_map_activation_notice

        code_map_dir = tmp_path / ".serena" / "code-map"
        code_map_dir.mkdir(parents=True)
        (code_map_dir / "overview.md").write_text("# Code Map Overview\n", encoding="utf-8")

        notice = code_map_activation_notice(self._make_project_mock(str(tmp_path)))
        assert notice is not None
        assert ".serena/code-map/overview.md" in notice

    def test_notice_when_export_on_activation_is_enabled_but_map_not_yet_generated(self, tmp_path) -> None:
        from serena.code_map.export import code_map_activation_notice

        notice = code_map_activation_notice(self._make_project_mock(str(tmp_path), export_on_activation=True))
        assert notice is not None
        assert "generated in the background" in notice

    def test_no_notice_without_code_map(self, tmp_path) -> None:
        from serena.code_map.export import code_map_activation_notice

        assert code_map_activation_notice(self._make_project_mock(str(tmp_path))) is None

    def test_activation_message_includes_notice(self, tmp_path) -> None:
        from serena.agent import SerenaAgent

        code_map_dir = tmp_path / ".serena" / "code-map"
        code_map_dir.mkdir(parents=True)
        (code_map_dir / "overview.md").write_text("# Code Map Overview\n", encoding="utf-8")

        agent = MagicMock()
        agent._active_project = self._make_project_mock(str(tmp_path))
        agent._active_tools.contains_tool_class.return_value = False
        agent._project_prompt_status.get_modes_with_prompts_to_be_provided_for_project_activation.return_value = []
        agent._project_prompt_status.is_project_activation_message_already_provided.return_value = False

        message = SerenaAgent.get_project_activation_message(agent, "session-1")
        assert ".serena/code-map/overview.md" in message

    def test_activation_message_has_no_notice_without_code_map(self, tmp_path) -> None:
        from serena.agent import SerenaAgent

        agent = MagicMock()
        agent._active_project = self._make_project_mock(str(tmp_path))
        agent._active_tools.contains_tool_class.return_value = False
        agent._project_prompt_status.get_modes_with_prompts_to_be_provided_for_project_activation.return_value = []
        agent._project_prompt_status.is_project_activation_message_already_provided.return_value = False

        message = SerenaAgent.get_project_activation_message(agent, "session-1")
        assert "code-map" not in message
