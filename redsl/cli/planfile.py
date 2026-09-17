"""CLI commands for SUMR → planfile integration."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import click
import yaml


def _dump_json(data: Any) -> None:
    click.echo(json.dumps(data, indent=2, ensure_ascii=False, default=str))


def _output_json(result: Any) -> None:
    """Output planfile result as JSON."""
    _dump_json({
        "project": str(result.project_path),
        "planfile": str(result.planfile_path),
        "written": result.written,
        "dry_run": result.dry_run,
        "sources": result.sources,
        "task_count": len(result.tasks),
        "tasks": [t.to_dict() for t in result.tasks],
    })


def _output_yaml(result: Any) -> None:
    """Output planfile result as YAML."""
    click.echo(yaml.safe_dump(
        {
            "project": str(result.project_path),
            "tasks": [t.to_dict() for t in result.tasks],
        },
        sort_keys=False,
        allow_unicode=True,
    ))


def _format_task_line(t: Any, priority_icon: dict[int, str]) -> list[str]:
    """Format a single task line for text output."""
    icon = priority_icon.get(t.priority, "⚪")
    status_marker = "✓" if t.status == "done" else " "
    lines = [f"  [{status_marker}] {icon} [{t.id}] {t.title}"]
    if t.file:
        lines.append(f"        file: {t.file}")
    if t.description:
        desc = t.description[:80] + ("…" if len(t.description) > 80 else "")
        lines.append(f"        {desc}")
    return lines


def _print_summary(result: Any) -> None:
    """Print planfile summary header."""
    todo = [t for t in result.tasks if t.status == "todo"]
    done = [t for t in result.tasks if t.status == "done"]

    click.echo(f"Project:  {result.project_path.name}")
    click.echo(f"Sources:  {', '.join(result.sources) or '(none)'}")
    click.echo(f"Tasks:    {len(result.tasks)} total, {len(todo)} todo, {len(done)} done")
    if result.dry_run:
        click.echo("Mode:     dry-run (planfile.yaml NOT written)")
    else:
        click.echo(f"Output:   {result.planfile_path}")
    click.echo("")


def _print_task_list(tasks: list) -> None:
    """Print sorted task list with priority icons."""
    priority_icon = {1: "🔴", 2: "🟠", 3: "🟡", 4: "🟢"}
    for t in sorted(tasks, key=lambda x: (x.priority, x.id)):
        for line in _format_task_line(t, priority_icon):
            click.echo(line)


def _print_result_message(result: Any) -> None:
    """Print final result message."""
    if result.written:
        click.echo(f"\n✓ planfile.yaml written ({len(result.tasks)} tasks)")
    elif result.dry_run:
        click.echo(f"\n(dry-run) Would write {len(result.tasks)} tasks to {result.planfile_path}")


def _output_text(result: Any) -> None:
    """Output planfile result as formatted text."""
    _print_summary(result)
    _print_task_list(result.tasks)
    _print_result_message(result)


@click.group("planfile")
def planfile_group() -> None:
    """SUMR.md → planfile.yaml task generation."""


@planfile_group.command("sync")
@click.argument(
    "project_path",
    type=click.Path(path_type=Path, exists=True, file_okay=False),
    default=".",
)
@click.option(
    "--sumr",
    "sumr_path",
    type=click.Path(path_type=Path, exists=True),
    default=None,
    help="Path to SUMR.md (default: PROJECT_PATH/SUMR.md)",
)
@click.option(
    "--dry-run/--no-dry-run",
    default=False,
    help="Print tasks without writing planfile.yaml",
)
@click.option(
    "--no-merge/--merge",
    "no_merge",
    default=False,
    help="Overwrite planfile.yaml completely (default: merge, preserve in_progress/done)",
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["text", "json", "yaml"]),
    default="text",
    show_default=True,
)
def planfile_sync(
    project_path: Path,
    sumr_path: Path | None,
    dry_run: bool,
    no_merge: bool,
    output_format: str,
) -> None:
    """Generate or update planfile.yaml from SUMR.md.

    Reads the SUMR.md (and any refactor_plan.yaml / *.toon.yaml) in
    PROJECT_PATH and writes structured tasks to planfile.yaml.

    \b
    Examples:
      redsl planfile sync .
      redsl planfile sync /path/to/code2docs --dry-run
      redsl planfile sync . --format json
    """
    from redsl.commands.sumr_planfile import generate_planfile

    try:
        result = generate_planfile(
            project_path,
            dry_run=dry_run,
            merge=not no_merge,
            sumr_path=sumr_path,
        )
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc

    if not result.tasks:
        click.echo("No refactoring tasks found. Check SUMR.md or *.toon.yaml files.", err=True)
        return

    if output_format == "json":
        _output_json(result)
    elif output_format == "yaml":
        _output_yaml(result)
    else:
        _output_text(result)


@planfile_group.command("show")
@click.argument(
    "project_path",
    type=click.Path(path_type=Path, exists=True, file_okay=False),
    default=".",
)
@click.option(
    "--status",
    type=click.Choice(["todo", "in_progress", "done", "all"]),
    default="todo",
    show_default=True,
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["text", "json"]),
    default="text",
    show_default=True,
)
def planfile_show(project_path: Path, status: str, output_format: str) -> None:
    """Show tasks from an existing planfile.yaml."""
    planfile = Path(project_path) / "planfile.yaml"
    if not planfile.exists():
        raise click.ClickException(
            f"planfile.yaml not found at {planfile}. Run: redsl planfile sync {project_path}"
        )

    tasks = _read_tasks_from_planfile(planfile)
    if status != "all":
        tasks = [t for t in tasks if isinstance(t, dict) and t.get("status") == status]

    if output_format == "json":
        _dump_json({"planfile": str(planfile), "tasks": tasks})
        return

    _print_tasks_text(tasks, status)


def _read_tasks_from_planfile(planfile: Path) -> list:
    """Load task list from planfile.yaml, handling both formats."""
    content = planfile.read_text(encoding="utf-8")
    if "\ntasks:" in content:
        tasks_raw = yaml.safe_load("tasks:" + content.split("\ntasks:", 1)[1])
        return (tasks_raw or {}).get("tasks", [])
    data = yaml.safe_load(content)
    return (data or {}).get("tasks", [])


_PRIORITY_ICON = {1: "🔴", 2: "🟠", 3: "🟡", 4: "🟢"}


def _print_tasks_text(tasks: list, status: str) -> None:
    """Print task list as formatted text to stdout."""
    if not tasks:
        click.echo(f"No tasks with status={status!r}")
        return
    for t in sorted(tasks, key=lambda x: (x.get("priority", 3), x.get("id", ""))):
        if not isinstance(t, dict):
            continue
        icon = _PRIORITY_ICON.get(t.get("priority", 3), "⚪")
        click.echo(f"{icon} [{t.get('id','?')}] {t.get('title','?')}")
        click.echo(f"     status={t.get('status')}  effort={t.get('effort')}  action={t.get('action')}")
        if t.get("file"):
            click.echo(f"     file: {t['file']}")


# ---------------------------------------------------------------------------
# redsl auth github login
# ---------------------------------------------------------------------------

@click.group("auth")
def auth_group() -> None:
    """Manage authentication credentials for planfile sources."""


@auth_group.group("github")
def auth_github() -> None:
    """GitHub authentication helpers."""


@auth_github.command("login")
@click.option(
    "--token",
    default=None,
    help="Personal access token (PAT). If omitted, prints export instructions.",
)
@click.option(
    "--env-var",
    default="GITHUB_TOKEN",
    show_default=True,
    help="Environment variable name to reference in planfile.",
)
def auth_github_login(token: str | None, env_var: str) -> None:
    """Store a GitHub token for planfile sync.

    \b
    Usage (recommended — no token in shell history):
      export GITHUB_TOKEN=$(cat ~/secrets/github.token)
      redsl auth github login --env-var GITHUB_TOKEN

    The planfile will reference env:GITHUB_TOKEN, never the token itself.
    """
    import os

    if token:
        click.echo(
            "WARNING: passing tokens via --token leaves them in shell history. "
            "Prefer: export GITHUB_TOKEN=<token>",
            err=True,
        )
        # Offer to save to a secrets file
        secrets_dir = Path.home() / ".config" / "redsl" / "secrets"
        secrets_dir.mkdir(parents=True, exist_ok=True)
        token_file = secrets_dir / "github.token"
        token_file.write_text(token, encoding="utf-8")
        token_file.chmod(0o600)
        click.echo(f"Token saved to {token_file} (chmod 600)")
        click.echo(f"Use auth_ref: file:{token_file}  in planfile.yaml")
    else:
        existing = os.environ.get(env_var)
        if existing:
            click.echo(f"✓ {env_var} is set in environment ({len(existing)} chars)")
            click.echo(f"Use auth_ref: env:{env_var}  in planfile.yaml")
        else:
            click.echo(f"✗ {env_var} is NOT set. To configure:", err=True)
            click.echo(f"  export {env_var}=<your-github-pat>")
            click.echo(f"  # then run: redsl auth github login")
            raise SystemExit(1)


# ---------------------------------------------------------------------------
# redsl planfile source (subgroup)
# ---------------------------------------------------------------------------

def _load_planfile_data(planfile: Path) -> dict:
    """Load planfile.yaml or raise ClickException if missing."""
    if not planfile.exists():
        raise click.ClickException(
            f"planfile.yaml not found at {planfile}. Run: redsl planfile sync ."
        )
    return yaml.safe_load(planfile.read_text(encoding="utf-8")) or {}


def _new_planfile_skeleton(project_path: Path) -> dict:
    return {
        "apiVersion": "redsl.plan/v1",
        "kind": "Planfile",
        "metadata": {"name": Path(project_path).resolve().name, "version": 1},
        "sources": [],
        "spec": {"tasks": []},
    }


@planfile_group.group("source")
def source_group() -> None:
    """Manage task sources (GitHub, SUMR) in planfile.yaml."""


@source_group.command("list")
@click.option(
    "--project",
    "project_path",
    type=click.Path(path_type=Path, exists=True, file_okay=False),
    default=".",
)
def source_list(project_path: Path) -> None:
    """List configured sources in planfile.yaml."""
    planfile = Path(project_path) / "planfile.yaml"
    data = _load_planfile_data(planfile)
    sources = data.get("sources") or []
    if not sources:
        click.echo("No sources configured.")
        return
    for s in sources:
        click.echo(
            f"  id={s.get('id')}  type={s.get('type')}  "
            f"repo={s.get('repo')}  auth_ref={s.get('auth_ref')}"
        )


@source_group.command("remove")
@click.option("--source-id", required=True, help="Source id to remove")
@click.option(
    "--project",
    "project_path",
    type=click.Path(path_type=Path, exists=True, file_okay=False),
    default=".",
)
def source_remove(source_id: str, project_path: Path) -> None:
    """Remove a source from planfile.yaml."""
    planfile = Path(project_path) / "planfile.yaml"
    data = _load_planfile_data(planfile)
    before = len(data.get("sources") or [])
    data["sources"] = [s for s in (data.get("sources") or []) if s.get("id") != source_id]
    if len(data["sources"]) == before:
        raise click.ClickException(f"Source {source_id!r} not found.")
    planfile.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    click.echo(f"Removed source {source_id!r}")


@source_group.command("add")
@click.option("--repo", required=True, help="GitHub repo (owner/repo)")
@click.option("--auth-ref", default="env:GITHUB_TOKEN", show_default=True)
@click.option("--labels", default=None, help="Comma-separated label filter")
@click.option(
    "--state",
    default="open",
    show_default=True,
    type=click.Choice(["open", "closed", "all"]),
)
@click.option("--source-id", default=None, help="Custom source id (default: gh-<repo-slug>)")
@click.option(
    "--project",
    "project_path",
    type=click.Path(path_type=Path, exists=True, file_okay=False),
    default=".",
)
def source_add(
    repo: str,
    auth_ref: str,
    labels: str | None,
    state: str,
    source_id: str | None,
    project_path: Path,
) -> None:
    """Add a GitHub source to planfile.yaml.

    \b
    Examples:
      redsl planfile source add --repo org/myproject
      redsl planfile source add --repo org/myproject --labels bug,p1
    """
    planfile = Path(project_path) / "planfile.yaml"
    sid = source_id or ("gh-" + repo.split("/")[-1].lower().replace("-", "_"))
    data = yaml.safe_load(planfile.read_text(encoding="utf-8")) if planfile.exists() else _new_planfile_skeleton(project_path)

    sources = data.get("sources") or []
    if any(s.get("id") == sid for s in sources):
        raise click.ClickException(
            f"Source {sid!r} already exists. Remove first: redsl planfile source remove --source-id {sid}"
        )

    sources.append({
        "id": sid,
        "type": "github",
        "repo": repo,
        "auth_ref": auth_ref,
        "sync_mode": "read-only",
        "filters": {
            "state": state,
            "labels": [lb.strip() for lb in labels.split(",")] if labels else [],
        },
        "poll_interval_sec": 300,
    })
    data["sources"] = sources
    planfile.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    click.echo(f"Added source {sid!r} (repo={repo}, auth_ref={auth_ref})")
    click.echo(f"Run: redsl planfile gh-sync {project_path}  # to fetch issues")


def _output_json_sync(result: Any) -> None:
    """Output sync result as JSON."""
    _dump_json({
        "planfile": str(result.planfile_path),
        "written": result.written,
        "dry_run": result.dry_run,
        "sources_synced": result.sources_synced,
        "errors": result.errors,
        "merge": {
            sid: {
                "added": len(mr.added),
                "updated": len(mr.updated),
                "unchanged": len(mr.unchanged),
                "archived": len(mr.archived),
            }
            for sid, mr in result.merge_results.items()
        },
    })


def _print_sync_errors(errors: list[str]) -> None:
    """Print sync errors if any."""
    for err in errors:
        click.echo(f"⚠  {err}", err=True)


def _print_sync_results(result: Any) -> None:
    """Print sync results for each source."""
    if not result.sources_synced:
        click.echo("No github sources found. Add one: redsl planfile source add --repo org/repo")
        return

    for sid, mr in result.merge_results.items():
        click.echo(f"[{sid}]  +{len(mr.added)} added  ~{len(mr.updated)} updated  "
                   f"={len(mr.unchanged)} unchanged  /{len(mr.archived)} archived")

    if result.dry_run:
        click.echo("(dry-run) planfile.yaml NOT written")
    elif result.written:
        click.echo(f"✓ {result.planfile_path} updated")


# ---------------------------------------------------------------------------
# redsl planfile gh-sync
# ---------------------------------------------------------------------------

@planfile_group.command("gh-sync")
@click.argument(
    "project_path",
    type=click.Path(path_type=Path, exists=True, file_okay=False),
    default=".",
)
@click.option("--dry-run/--no-dry-run", default=False)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["text", "json"]),
    default="text",
    show_default=True,
)
def planfile_gh_sync(
    project_path: Path,
    dry_run: bool,
    output_format: str,
) -> None:
    """Fetch GitHub issues into planfile.yaml (three-way merge).

    Reads all sources of type=github from planfile.yaml, fetches open issues,
    merges with local tasks, and writes the result atomically.

    \b
    Merge rules:
      New issues        → added as GH-NNN tasks
      Changed issues    → external fields updated, local_notes/priority_override preserved
      Removed issues    → moved to sync_state.archived (not deleted)

    \b
    Example:
      redsl planfile source add --repo org/myproject
      redsl planfile gh-sync .
      redsl planfile show . --status todo
    """
    from redsl.commands.plan_sync import apply_planfile_sources

    planfile = Path(project_path) / "planfile.yaml"
    try:
        result = apply_planfile_sources(planfile, dry_run=dry_run)
    except FileNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc
    except Exception as exc:
        raise click.ClickException(f"Sync error: {exc}") from exc

    if output_format == "json":
        _output_json_sync(result)
        return

    _print_sync_errors(result.errors)
    _print_sync_results(result)


# ---------------------------------------------------------------------------
# redsl planfile validate — check ticket freshness
# ---------------------------------------------------------------------------

def _get_current_cc(file_path: Path) -> int | None:
    """Return max cyclomatic complexity found in a Python file, or None."""
    if not file_path.exists() or not file_path.suffix == ".py":
        return None
    try:
        from radon.complexity import cc_visit
        results = cc_visit(file_path.read_text(encoding="utf-8", errors="replace"))
        if not results:
            return None
        return max(r.complexity for r in results)
    except Exception:
        return None


def _extract_cc_threshold(title: str, labels: list) -> int | None:
    """Parse expected CC from ticket title (e.g. 'CC=14') or labels (e.g. 'cc-14')."""
    import re
    m = re.search(r"CC=(\d+)", title)
    if m:
        return int(m.group(1))
    for lbl in labels:
        m = re.match(r"cc-(\d+)$", str(lbl))
        if m:
            return int(m.group(1))
    return None


@planfile_group.command("validate")
@click.argument(
    "project_path",
    type=click.Path(path_type=Path, exists=True, file_okay=False),
    default=".",
)
@click.option(
    "--fix",
    is_flag=True,
    default=False,
    help="Auto-mark stale tickets as 'stale' in planfile.yaml",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Output result as JSON",
)
def _extract_tasks(data: dict) -> list:
    """Return task list from planfile data, supporting both schema versions."""
    return data.get("tasks") or (data.get("spec") or {}).get("tasks") or []


def _load_applied_pairs(project_path: Path) -> set[tuple[str, str]]:
    """Load already-applied (file, action) pairs from .redsl/history.jsonl."""
    applied_pairs: set[tuple[str, str]] = set()
    history_file = Path(project_path) / ".redsl" / "history.jsonl"
    if not history_file.exists():
        return applied_pairs
    for line in history_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("event_type") == "proposal_applied":
            tf = ev.get("target_file") or ""
            action = ev.get("action") or ""
            if tf and action:
                applied_pairs.add((tf, action))
    return applied_pairs


def _evaluate_task(
    task: dict,
    project_path: Path,
    applied_pairs: set[tuple[str, str]],
) -> dict:
    """Evaluate one open planfile task and return a result entry dict."""
    tid = task.get("id", "?")
    title = task.get("title", "")
    file_rel = task.get("file", "")
    action = task.get("action", "")
    labels = task.get("labels") or []

    verdict = "ok"
    reason = ""

    if file_rel:
        abs_file = Path(project_path) / file_rel
        if not abs_file.exists():
            verdict = "stale_file_missing"
            reason = f"File not found: {file_rel}"
        elif (file_rel, action) in applied_pairs:
            verdict = "stale_applied"
            reason = f"action={action} already applied to {file_rel} (found in history.jsonl)"
        elif action in ("reduce_complexity", "extract_functions", "split_module"):
            threshold = _extract_cc_threshold(title, labels)
            if threshold is not None:
                current_cc = _get_current_cc(abs_file)
                if current_cc is not None and current_cc < threshold:
                    verdict = "stale_fixed"
                    reason = f"CC now {current_cc} (was ~{threshold} when ticket created)"
                elif current_cc is not None:
                    reason = f"CC still {current_cc} (threshold {threshold})"
    else:
        reason = "no file reference"

    return {
        "id": tid,
        "title": title,
        "file": file_rel,
        "action": action,
        "verdict": verdict,
        "reason": reason,
    }


def _print_validate_report(project_path: Path, results: list[dict]) -> None:
    """Print the human-readable validate report."""
    ok_count = sum(1 for r in results if r["verdict"] == "ok")
    stale_count = len(results) - ok_count

    click.echo(f"Project:  {project_path}")
    click.echo(f"Checked:  {len(results)} open tasks  |  OK: {ok_count}  |  Stale: {stale_count}")
    click.echo()

    verdict_color = {
        "ok": "green",
        "stale_file_missing": "red",
        "stale_applied": "cyan",
        "stale_fixed": "yellow",
    }
    for r in results:
        color = verdict_color.get(r["verdict"], "white")
        verdict_label = r["verdict"].upper()
        click.echo(
            click.style(f"[{verdict_label:20}]", fg=color, bold=True) +
            f" [{r['id']:6}] {r['title'][:60]}"
        )
        if r["reason"]:
            click.echo(f"               → {r['reason']}")


def _mark_stale_tasks(data: dict, stale_ids: list[str]) -> int:
    """Mark stale task ids as 'stale' in the loaded planfile data in-place."""
    task_list = _extract_tasks(data)
    patched = 0
    for task in task_list:
        if not isinstance(task, dict):
            continue
        if task.get("id") in stale_ids:
            task["status"] = "stale"
            patched += 1
    return patched


def planfile_validate(project_path: Path, fix: bool, as_json: bool) -> None:
    """Check whether planfile.yaml tickets are still current.

    For each open task, validates:
    - File still exists (otherwise: STALE_FILE_MISSING)
    - For reduce_complexity/extract_functions: current CC re-checked via radon
      If CC is now below threshold → STALE_FIXED
    - Whether the action was already applied via history.jsonl → STALE_APPLIED

    \b
    Examples:
      redsl planfile validate .
      redsl planfile validate . --fix
      redsl planfile validate /path/to/goal --json
    """
    planfile = Path(project_path) / "planfile.yaml"
    if not planfile.exists():
        raise click.ClickException(
            f"planfile.yaml not found at {planfile}. Run: redsl planfile sync ."
        )

    data = yaml.safe_load(planfile.read_text(encoding="utf-8")) or {}

    # Support both planfile schema versions
    tasks = _extract_tasks(data)
    if not tasks:
        click.echo("No tasks in planfile.yaml.")
        return

    applied_pairs = _load_applied_pairs(project_path)

    results = []
    stale_ids: list[str] = []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        if task.get("status", "todo") in ("done", "closed"):
            continue
        entry = _evaluate_task(task, project_path, applied_pairs)
        results.append(entry)
        if entry["verdict"].startswith("stale"):
            stale_ids.append(entry["id"])

    if as_json:
        click.echo(json.dumps(
            {"project": str(project_path), "tasks_checked": len(results), "results": results},
            indent=2,
            ensure_ascii=False,
        ))
        return

    _print_validate_report(project_path, results)

    stale_count = len(stale_ids)
    if stale_count and fix:
        click.echo()
        patched = _mark_stale_tasks(data, stale_ids)
        planfile.write_text(
            yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False),
            encoding="utf-8",
        )
        click.echo(click.style(f"✓ Marked {patched} ticket(s) as 'stale' in planfile.yaml", fg="yellow"))
    elif stale_count and not fix:
        click.echo()
        click.echo(f"Tip: run with --fix to auto-mark {stale_count} stale ticket(s).")


def register(cli_group: "click.Group") -> None:
    cli_group.add_command(planfile_group)
    cli_group.add_command(auth_group)
