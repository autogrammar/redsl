"""Deploy configuration detector for redsl.

Scans a project directory and determines which push / publish mechanisms
are available and how to invoke them.

Priority order:
  1. ``pyqual.yaml`` pipeline stages named ``push`` / ``publish``
     (honours ``when`` and ``optional`` flags)
  2. ``Taskfile.yml`` — tasks named ``push`` / ``publish``
  3. ``Makefile`` — targets named ``push`` / ``publish``
  4. ``.github/workflows/`` — notes that CI will handle deployment
     automatically (no local action needed)

Results are returned as :class:`DetectedDeployConfig` and stored in
:attr:`WorkflowConfig.deploy` after workflow loading.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

DeployMethod = Literal["pyqual_stage", "task", "make", "none"]


@dataclass
class DeployAction:
    """A single detected push or publish action."""

    method: DeployMethod = "none"
    command: list[str] = field(default_factory=list)
    when: str = "on_success"   # on_success | always | never
    optional: bool = True
    # Human-readable label for logs / workflow show
    label: str = ""


@dataclass
class DetectedDeployConfig:
    """Result of auto-detection for a single project."""

    project_dir: str = ""

    # git push
    push: DeployAction = field(default_factory=lambda: DeployAction(method="none"))
    # registry / PyPI publish
    publish: DeployAction = field(default_factory=lambda: DeployAction(method="none"))

    # True if GitHub Actions already handles deployment (CI workflow detected)
    ci_handles_deploy: bool = False
    ci_workflow_files: list[str] = field(default_factory=list)

    def has_any(self) -> bool:
        return self.push.method != "none" or self.publish.method != "none"

    def summary(self) -> str:
        parts = []
        if self.push.method != "none":
            parts.append(f"push={self.push.method}({self.push.label})")
        if self.publish.method != "none":
            parts.append(f"publish={self.publish.method}({self.publish.label})")
        if self.ci_handles_deploy:
            parts.append(f"ci={','.join(self.ci_workflow_files)}")
        return " | ".join(parts) if parts else "none"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _exe_in_project(name: str, project_dir: Path) -> str | None:
    """Return path to *name* executable: project venv first, then PATH."""
    for venv in [".venv", "venv"]:
        p = project_dir / venv / "bin" / name
        if p.exists():
            return str(p)
    return shutil.which(name)


# ---------------------------------------------------------------------------
# Detectors
# ---------------------------------------------------------------------------

def _detect_from_pyqual(project_dir: Path) -> tuple[DeployAction, DeployAction]:
    """Parse pyqual.yaml and return (push_action, publish_action)."""
    pyqual_file = project_dir / "pyqual.yaml"
    if not pyqual_file.exists():
        return DeployAction(), DeployAction()

    try:
        import yaml  # type: ignore[import-untyped]
        data = yaml.safe_load(pyqual_file.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        logger.debug("deploy_detector: cannot parse pyqual.yaml in %s: %s", project_dir.name, exc)
        return DeployAction(), DeployAction()

    stages = data.get("pipeline", {}).get("stages", [])
    pyqual_exe = _exe_in_project("pyqual", project_dir) or "pyqual"

    for stage in stages:
        if not isinstance(stage, dict):
            continue
        name = stage.get("name", "").lower()
        when_raw = stage.get("when", "always")
        optional = bool(stage.get("optional", True))

        # Map pyqual "when" to our convention
        when = "on_success" if when_raw in ("metrics_pass", "on_success") else "always"

        if "push" in name and push_action.method == "none":
            push_action = DeployAction(
                method="pyqual_stage",
                command=[pyqual_exe, "run", "--stage", stage["name"]],
                when=when,
                optional=optional,
                label=f"pyqual run --stage {stage['name']}",
            )

        if "publish" in name and publish_action.method == "none":
            publish_action = DeployAction(
                method="pyqual_stage",
                command=[pyqual_exe, "run", "--stage", stage["name"]],
                when=when,
                optional=optional,
                label=f"pyqual run --stage {stage['name']}",
            )

    return push_action, publish_action


def _detect_from_taskfile(project_dir: Path) -> tuple[DeployAction, DeployAction]:
    """Check Taskfile.yml for push/publish tasks."""
    tf = project_dir / "Taskfile.yml"
    if not tf.exists():
        return DeployAction(), DeployAction()

    try:
        import yaml  # type: ignore[import-untyped]
        data = yaml.safe_load(tf.read_text(encoding="utf-8")) or {}
    except Exception:
        return DeployAction(), DeployAction()

    tasks = data.get("tasks", {})
    task_exe = shutil.which("task") or "task"

    if "push" in tasks:
        push_action = DeployAction(
            method="task",
            command=[task_exe, "push"],
            when="on_success",
            optional=True,
            label="task push",
        )
    if "publish" in tasks:
        publish_action = DeployAction(
            method="task",
            command=[task_exe, "publish"],
            when="on_success",
            optional=True,
            label="task publish",
        )

    return push_action, publish_action


def _detect_from_makefile(project_dir: Path) -> tuple[DeployAction, DeployAction]:
    """Check Makefile for push/publish targets."""
    mf = project_dir / "Makefile"
    if not mf.exists():
        return DeployAction(), DeployAction()

    try:
        content = mf.read_text(encoding="utf-8")
    except Exception:
        return DeployAction(), DeployAction()

    make_exe = shutil.which("make") or "make"
    import re
    targets = re.findall(r"^([a-zA-Z_-]+)\s*:", content, re.MULTILINE)

    if "push" in targets:
        push_action = DeployAction(
            method="make",
            command=[make_exe, "push"],
            when="on_success",
            optional=True,
            label="make push",
        )
    if "publish" in targets:
        publish_action = DeployAction(
            method="make",
            command=[make_exe, "publish"],
            when="on_success",
            optional=True,
            label="make publish",
        )

    return push_action, publish_action


def _detect_from_scripts(project_dir: Path) -> tuple[DeployAction, DeployAction]:
    """Detect push/publish scripts in scripts/, bin/, or root-level sh files."""
    # Candidate scripts per action
    push_candidates: list[Path] = []
    publish_candidates: list[Path] = []

    for search_dir in [project_dir / "scripts", project_dir / "bin", project_dir]:
        if not search_dir.is_dir():
            continue
        # Only scan one level (don't recurse into examples/)
        max_depth = 0 if search_dir == project_dir else None
        try:
            entries = list(search_dir.iterdir())
        except PermissionError:
            continue
        for f in entries:
            if not f.is_file():
                continue
            stem = f.stem.lower()
            suffix = f.suffix.lower()
            if suffix not in (".sh", "") and not f.stat().st_mode & 0o111:
                continue  # skip non-executable / non-sh files in project root
            if "push" in stem:
                push_candidates.append(f)
            elif "publish" in stem or "release" in stem:
                publish_candidates.append(f)

    return (
        _script_action(push_candidates, project_dir),
        _script_action(publish_candidates, project_dir),
    )


def _script_action(candidates: list[Path], project_dir: Path) -> DeployAction:
    """Build a DeployAction for the first matching script candidate."""
    if not candidates:
        return DeployAction()
    script = candidates[0]
    cmd = ["bash", str(script)] if script.suffix == ".sh" else [str(script)]
    return DeployAction(
        method="script",
        command=cmd,
        when="on_success",
        optional=True,
        label=script.relative_to(project_dir).as_posix(),
    )


def _detect_ci_workflows(project_dir: Path) -> tuple[bool, list[str]]:
    """Check for GitHub Actions / CI workflow files."""
    gh_dir = project_dir / ".github" / "workflows"
    if not gh_dir.is_dir():
        return False, []

    files = [f.name for f in gh_dir.iterdir() if f.suffix in (".yml", ".yaml")]
    # CI "handles deploy" if any workflow file name hints at deploy/publish/release
    deploy_hints = {"publish", "deploy", "release", "cd", "registry"}
    ci_handles = any(
        any(hint in f.lower() for hint in deploy_hints)
        for f in files
    )
    return ci_handles, sorted(files)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def detect_deploy_config(project_dir: Path) -> DetectedDeployConfig:
    """Auto-detect push/publish mechanisms for *project_dir*.

    Returns :class:`DetectedDeployConfig` with the highest-priority
    source for each action (push / publish).

    Detection order per action:
    1. pyqual.yaml stages
    2. Taskfile.yml tasks
    3. Makefile targets
    """
    project_dir = Path(project_dir)
    result = DetectedDeployConfig(project_dir=str(project_dir))

    # --- CI detection (informational only) ---------------------------------
    result.ci_handles_deploy, result.ci_workflow_files = _detect_ci_workflows(project_dir)

    # --- Push ---------------------------------------------------------------
    pyqual_push, pyqual_publish = _detect_from_pyqual(project_dir)
    task_push, task_publish = _detect_from_taskfile(project_dir)
    make_push, make_publish = _detect_from_makefile(project_dir)
    script_push, script_publish = _detect_from_scripts(project_dir)

    result.push = (
        pyqual_push  if pyqual_push.method != "none"  else
        task_push    if task_push.method != "none"    else
        make_push    if make_push.method != "none"    else
        script_push
    )

    result.publish = (
        pyqual_publish  if pyqual_publish.method != "none"  else
        task_publish    if task_publish.method != "none"    else
        make_publish    if make_publish.method != "none"    else
        script_publish
    )

    logger.debug(
        "deploy_detector: %s → %s",
        project_dir.name,
        result.summary(),
    )
    return result


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------

def run_deploy_action(
    action: DeployAction,
    project_dir: Path,
    dry_run: bool = False,
) -> bool:
    """Execute a single deploy action. Returns True on success."""
    if action.method == "none" or not action.command:
        return True

    cmd_str = " ".join(action.command)
    if dry_run:
        logger.info("deploy [dry-run]: would run: %s", cmd_str)
        return True

    logger.info("deploy: running %s in %s", cmd_str, project_dir.name)
    try:
        proc = subprocess.run(
            action.command,
            cwd=str(project_dir),
            capture_output=False,   # let output stream to log file
            text=True,
            timeout=120,
        )
        if proc.returncode == 0:
            logger.info("deploy: ✓ %s succeeded", action.label)
            return True
        msg = f"deploy: ✗ {action.label} failed (rc={proc.returncode})"
        if action.optional:
            logger.warning("%s — skipping (optional)", msg)
            return True   # optional failure is non-blocking
        logger.error("%s", msg)
        return False
    except subprocess.TimeoutExpired:
        logger.warning("deploy: %s timed out (optional=%s)", action.label, action.optional)
        return action.optional
    except Exception as exc:
        logger.warning("deploy: %s error: %s (optional=%s)", action.label, exc, action.optional)
        return action.optional
