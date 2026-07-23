"""
High-level entry point for exporting a project's code map, shared by the CLI commands
and the automatic export on project activation.
"""

import logging
import os
from typing import TYPE_CHECKING

from serena.code_map.builder import CodeMapBuilder, CodeMapBuildOptions
from serena.code_map.model import CodeMap
from serena.code_map.overview import DEFAULT_OVERVIEW_MAX_CHARS
from serena.code_map.serializer import CodeMapWriteResult, write_code_map

if TYPE_CHECKING:
    from serena.ls_manager import LanguageServerManager
    from serena.project import Project

log = logging.getLogger(__name__)

DEFAULT_CODE_MAP_DIR_PARTS = (".serena", "code-map")

CODE_MAP_ACTIVATION_NOTICE = (
    "A static code map of this project is available under `.serena/code-map/`. "
    "Read `.serena/code-map/overview.md` first to get an overview of the project structure, and check the relevant "
    "file under `.serena/code-map/modules/` before performing a broad search of the repository. "
    "Treat the code map as a static snapshot: use Serena's tools for exact live symbol, reference, and editing operations."
)


def default_code_map_dir(project_root: str) -> str:
    return os.path.join(project_root, *DEFAULT_CODE_MAP_DIR_PARTS)


def code_map_activation_notice(project: "Project") -> str | None:
    """
    Returns the notice to include in the project activation message, telling the agent how to use
    the static code map — or None if the project has no code map (and none is being generated).
    """
    overview_path = os.path.join(default_code_map_dir(str(project.project_root)), "overview.md")
    if os.path.isfile(overview_path):
        return CODE_MAP_ACTIVATION_NOTICE
    if project.project_config.export_code_map_on_activation:
        return CODE_MAP_ACTIVATION_NOTICE + " (The code map is currently being generated in the background and will be available shortly.)"
    return None


def export_project_code_map(
    project: "Project",
    ls_manager: "LanguageServerManager",
    options: CodeMapBuildOptions | None = None,
    output_dir: str | None = None,
    overview_max_chars: int = DEFAULT_OVERVIEW_MAX_CHARS,
) -> tuple[CodeMap, CodeMapWriteResult]:
    """
    Builds the code map for the given project and writes it to the output directory
    (default: `<project>/.serena/code-map`). The language server manager must already be running.

    :return: the built code map and the write statistics
    """
    options = options or CodeMapBuildOptions(show_progress=False)
    builder = CodeMapBuilder(project, ls_manager, options=options)
    code_map = builder.build()
    resolved_output_dir = output_dir if output_dir is not None else default_code_map_dir(str(project.project_root))
    write_result = write_code_map(code_map, resolved_output_dir, overview_max_chars=overview_max_chars)
    log.info(
        "Exported code map to %s (%d symbols, %d edges, %d files written, %d unchanged)",
        resolved_output_dir,
        len(code_map.symbols_by_id),
        len(code_map.edges),
        len(write_result.files_written),
        len(write_result.files_unchanged),
    )
    return code_map, write_result
