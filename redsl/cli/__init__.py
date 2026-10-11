"""Command-line interface for reDSL."""

from __future__ import annotations

import os
import sys

os.environ.setdefault("LITELLM_LOG", "ERROR")

from dotenv import load_dotenv
load_dotenv()

import click

from redsl.cli.logging import setup_logging as _setup_logging
from redsl.cli.refactor import register_refactor, _save_refactor_reports


def _save_refactor_markdown_report(*args, **kwargs):
    """Backward-compatible wrapper that returns only the markdown path."""
    md_path, _ = _save_refactor_reports(*args, **kwargs)
    return md_path
from redsl.cli.batch import register_batch
from redsl.cli.pyqual import register_pyqual
from redsl.cli.config import register_config
from redsl.cli.debug import register_debug
from redsl.cli.examples import register_examples
from redsl.cli.scan import scan
from redsl.cli.utils import perf_command, cost_command
from redsl.cli.model_policy import register_model_policy
from redsl.cli.models import register_models
from redsl.cli.planfile import register as _register_planfile

# Import for test compatibility
from redsl.orchestrator import RefactorOrchestrator
from redsl.commands import batch_pyqual as batch_pyqual_commands
from redsl.formatters import format_plan_yaml, _serialize_analysis, format_refactor_plan, _serialize_decision, _get_timestamp, format_cycle_report_yaml

# Backward-compatible exports for tests
__all__ = [
    "cli",
    "_setup_logging",
    "_save_refactor_markdown_report",  # alias for _save_refactor_reports
    "RefactorOrchestrator",
    "batch_pyqual_commands",
    "format_plan_yaml",
    "_serialize_analysis",
    "format_refactor_plan",
    "_serialize_decision",
    "_get_timestamp",
    "format_cycle_report_yaml",
]


@click.group()
@click.version_option(version="1.2.0")
@click.option("--verbose", "-v", is_flag=True, help="Enable verbose logging")
@click.pass_context
def cli(ctx: click.Context, verbose: bool) -> None:
    try:
        from .autoupdate import check_for_updates
        check_for_updates("redsl")
    except Exception:
        pass
    """reDSL - Automated code refactoring tool."""
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose


def _build_awareness_manager():
    """Build awareness manager for CLI commands (late-bound for testability)."""
    from redsl.awareness import AwarenessManager
    return AwarenessManager()


def _register_all(cli_group: click.Group) -> None:
    cli_group.add_command(scan)
    register_refactor(cli_group)
    register_batch(cli_group)
    register_pyqual(cli_group)
    register_config(cli_group)
    register_debug(cli_group)
    register_examples(cli_group)
    register_model_policy(cli_group)
    register_models(cli_group)
    _register_planfile(cli_group)
    cli_group.add_command(perf_command)
    cli_group.add_command(cost_command)
    from redsl.commands.cli_doctor import register as _register_doctor
    from redsl.commands.cli_autonomy import register as _register_autonomy
    from redsl.commands.cli_awareness import register as _register_awareness
    _register_doctor(cli_group)
    _register_autonomy(cli_group, sys.modules[__name__])
    _register_awareness(cli_group, sys.modules[__name__])
    from redsl.cli.workflow import register as _register_workflow
    _register_workflow(cli_group)
    from redsl.cli.events import register as _register_events
    _register_events(cli_group)
    from redsl.cli.deploy import register as _register_deploy
    _register_deploy(cli_group)


_register_all(cli)

if __name__ == "__main__":
    cli()
