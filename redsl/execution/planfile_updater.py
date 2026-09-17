"""Update planfile.yaml task status after successful refactor applies.

Supports both schema formats:
- Old: ``schema: '1.0'``, flat ``tasks:`` list
- New: ``apiVersion: redsl.plan/v1``, ``spec.tasks:``
"""

from __future__ import annotations

import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

_PLANFILE_NAME = "planfile.yaml"


def _load_yaml_module() -> Any:
    """Import yaml module with error handling."""
    try:
        import yaml  # type: ignore[import]
        return yaml
    except ImportError:
        logger.warning("planfile_updater: PyYAML not available, skipping planfile update")
        return None


def _load_planfile_data(planfile_path: Path, yaml_mod: Any) -> dict | None:
    """Load and parse planfile.yaml."""
    if not planfile_path.exists():
        logger.debug("planfile_updater: no planfile.yaml in %s, skipping", planfile_path.parent)
        return None
    try:
        return yaml_mod.safe_load(planfile_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        logger.warning("planfile_updater: failed to parse %s: %s", planfile_path, exc)
        return None


def _normalize_applied_files(applied_files: list[str], project_dir: Path) -> set[str]:
    """Normalize applied files to relative paths for matching."""
    norm_applied = set()
    for f in applied_files:
        p = Path(f)
        if p.is_absolute():
            try:
                p = p.relative_to(project_dir)
            except ValueError:
                pass
        norm_applied.add(str(p))
        norm_applied.add(p.name)
    return norm_applied


def _match_and_update_task(
    task: dict, norm_applied: set[str], now_iso: str
) -> bool:
    """Check if task matches applied files and update if so."""
    if task.get("status") == "done":
        return False
    task_file = str(task.get("file", ""))
    task_file_name = Path(task_file).name if task_file else ""
    if task_file not in norm_applied and task_file_name not in norm_applied:
        return False

    task["status"] = "done"
    task["completed_at"] = now_iso
    logger.info(
        "planfile_updater: marked task [%s] done (%s)",
        task.get("id", "?"),
        task_file,
    )
    return True


def _save_planfile_changes(
    planfile_path: Path, data: dict, yaml_mod: Any, updated: int
) -> int:
    """Save changes atomically to planfile."""
    try:
        _atomic_write_yaml(planfile_path, data, yaml_mod)
        logger.info("planfile_updater: updated %d task(s) in %s", updated, planfile_path)
        return updated
    except Exception as exc:
        logger.warning("planfile_updater: failed to write %s: %s", planfile_path, exc)
        return 0


def mark_applied_tasks_done(
    project_dir: Path,
    applied_files: list[str],
) -> int:
    """Mark planfile tasks whose ``file:`` matches applied files as done.

    Returns the number of tasks updated.
    """
    if not applied_files:
        return 0

    yaml_mod = _load_yaml_module()
    if yaml_mod is None:
        return 0

    planfile_path = project_dir / _PLANFILE_NAME
    data = _load_planfile_data(planfile_path, yaml_mod)
    if data is None:
        return 0

    tasks = _get_tasks(data)
    if not tasks:
        return 0

    norm_applied = _normalize_applied_files(applied_files, project_dir)
    now_iso = datetime.now(timezone.utc).isoformat()
    updated = sum(
        _match_and_update_task(task, norm_applied, now_iso) for task in tasks
    )

    if updated:
        _update_stats(data)
        return _save_planfile_changes(planfile_path, data, yaml_mod, updated)

    return updated


# ---------------------------------------------------------------------------
# Schema helpers
# ---------------------------------------------------------------------------

def _get_tasks(data: dict) -> list:
    """Return the mutable task list regardless of schema version."""
    api = data.get("apiVersion", "")
    if api.startswith("redsl.plan"):
        return data.get("spec", {}).get("tasks", [])
    # Old schema: top-level tasks:
    return data.get("tasks", [])


def _update_stats(data: dict) -> None:
    """Recompute stats section for old-schema planfiles."""
    tasks = data.get("tasks")
    if tasks is None:
        return  # new schema — no stats to update
    done = sum(1 for t in tasks if t.get("status") == "done")
    total = len(tasks)
    todo = total - done
    stats = data.setdefault("stats", {})
    stats["total"] = total
    stats["done"] = done
    stats["todo"] = todo


def get_todo_tasks(project_dir: Path) -> list[dict]:
    """Return list of todo tasks from planfile.yaml, sorted by priority (ascending)."""
    yaml_mod = _load_yaml_module()
    if yaml_mod is None:
        return []
    planfile_path = project_dir / _PLANFILE_NAME
    data = _load_planfile_data(planfile_path, yaml_mod)
    if data is None:
        return []
    tasks = _get_tasks(data)
    todo_tasks = [t for t in tasks if t.get("status") == "todo" and t.get("file")]
    # Sort by priority ascending (lower number = higher priority)
    todo_tasks.sort(key=lambda t: (t.get("priority", 99), t.get("id", "")))
    return todo_tasks


from redsl.execution.planfile_runner import (  # noqa: E402
    _planfile_action_to_refactor_action,
    _run_direct_refactor_for_task,
    run_tasks_from_planfile,
)


def _atomic_write_yaml(path: Path, data: dict, yaml_mod) -> None:
    """Write YAML atomically via temp file rename."""
    content = yaml_mod.dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)
    dir_ = path.parent
    fd, tmp_path = tempfile.mkstemp(dir=dir_, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def add_quality_task(
    project_dir: Path,
    title: str,
    description: str = "",
    priority: int = 2,
) -> bool:
    """Append a new todo task to planfile.yaml for quality improvement.

    Used when pyqual gates fail even after ``pyqual tune`` — records the
    quality issue so it can be addressed in a future redsl cycle.

    Returns True if the task was added successfully, False otherwise.
    """
    planfile_path = project_dir / _PLANFILE_NAME
    yaml_mod = _load_yaml_module()
    if yaml_mod is None:
        return False

    data = _load_planfile_data(planfile_path, yaml_mod)
    if data is None:
        # planfile.yaml does not exist — create a minimal one
        data = {"tasks": []}

    tasks = _get_tasks(data)

    # Avoid duplicate tasks with the same title
    for t in tasks:
        if t.get("title") == title and t.get("status") == "todo":
            logger.debug(
                "planfile_updater: duplicate quality task skipped in %s: %s",
                project_dir.name, title,
            )
            return False

    now_iso = datetime.now(timezone.utc).isoformat()

    # Build new task — compatible with both schema versions
    new_task: dict = {
        "id": f"quality-{now_iso[:10]}-{len(tasks) + 1:03d}",
        "title": title,
        "status": "todo",
        "priority": priority,
        "created_at": now_iso,
        "source": "redsl:pyqual_gates",
    }
    if description:
        new_task["description"] = description

    tasks.append(new_task)

    # Keep list reference correct for new-schema planfiles
    api = data.get("apiVersion", "")
    if "redsl.plan" in api:
        data.setdefault("spec", {})["tasks"] = tasks
    else:
        data["tasks"] = tasks
        _update_stats(data)

    try:
        _atomic_write_yaml(planfile_path, data, yaml_mod)
        logger.info(
            "planfile_updater: added quality task '%s' to %s/planfile.yaml",
            title, project_dir.name,
        )
        return True
    except Exception as exc:
        logger.warning(
            "planfile_updater: failed to add quality task in %s: %s",
            project_dir.name, exc,
        )
        return False


def add_decision_tasks(
    project_dir: Path,
    decisions: list[Any],
    source: str = "redsl:dry_run",
    priority: int = 3,
) -> int:
    """Convert refactor decisions into todo tasks in planfile.yaml.

    Used with ``--to-planfile`` flag — instead of (or in addition to) generating
    a markdown report, each decision becomes a planfile task that can be executed
    in a future ``redsl refactor --from-planfile`` run.

    Skips decisions whose (file, action) pair already has a ``todo`` task.
    Returns the number of tasks added.
    """
    if not decisions:
        return 0

    yaml_mod = _load_yaml_module()
    if yaml_mod is None:
        return 0

    planfile_path = project_dir / _PLANFILE_NAME
    data = _load_planfile_data(planfile_path, yaml_mod)
    if data is None:
        # planfile.yaml does not exist — create a minimal one
        data = {"tasks": []}

    tasks = _get_tasks(data)
    now_iso = datetime.now(timezone.utc).isoformat()

    # Build set of existing (file, action) to avoid duplicates
    existing = {
        (str(t.get("file", "")), str(t.get("action", "")))
        for t in tasks
        if t.get("status") == "todo"
    }

    added = 0
    for decision in decisions:
        new_task = _decision_to_task(
            decision, now_iso, len(tasks) + added + 1, priority, source
        )
        key = (new_task["file"], new_task["action"])
        if key in existing:
            logger.debug(
                "planfile_updater: skip duplicate decision task file=%s action=%s",
                key[0], key[1],
            )
            continue

        tasks.append(new_task)
        existing.add(key)
        added += 1
        logger.info(
            "planfile_updater: added decision task [%s] %s → %s",
            new_task["id"], new_task["action"], new_task["file"],
        )

    if added:
        api = data.get("apiVersion", "")
        if "redsl.plan" in api:
            data.setdefault("spec", {})["tasks"] = tasks
        else:
            data["tasks"] = tasks
            _update_stats(data)
        try:
            _atomic_write_yaml(planfile_path, data, yaml_mod)
            logger.info(
                "planfile_updater: added %d decision task(s) to %s/planfile.yaml",
                added, project_dir.name,
            )
        except Exception as exc:
            logger.warning(
                "planfile_updater: failed to write decision tasks in %s: %s",
                project_dir.name, exc,
            )
            return 0

    return added


def _decision_to_task(
    decision: Any,
    now_iso: str,
    seq: int,
    priority: int,
    source: str,
) -> dict:
    """Build a planfile task dict from a refactor decision."""
    target_file = str(getattr(decision, "target_file", ""))
    action = str(getattr(getattr(decision, "action", None), "value", "") or getattr(decision, "action", ""))
    score = float(getattr(decision, "score", 0))
    rationale = str(getattr(decision, "rationale", "") or "")
    rule_name = str(getattr(decision, "rule_name", "") or "")

    new_task: dict = {
        "id": f"refactor-{now_iso[:10]}-{seq:03d}",
        "title": f"{action}: {Path(target_file).name}",
        "status": "todo",
        "action": action,
        "file": target_file,
        "priority": priority,
        "score": round(score, 2),
        "created_at": now_iso,
        "source": source,
    }
    if rationale:
        new_task["description"] = rationale[:200]
    if rule_name:
        new_task["rule"] = rule_name
    return new_task
