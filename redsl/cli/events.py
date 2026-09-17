"""CLI for browsing .redsl/history.jsonl decision events."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

import click


def _load_events(project_dir: Path) -> list[dict[str, Any]]:
    history_file = project_dir / ".redsl" / "history.jsonl"
    if not history_file.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in history_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


@click.group("events")
def events_group() -> None:
    """Browse and analyze .redsl/history.jsonl decision events."""


_EVENT_COLORS = {
    "proposal_applied": "green",
    "cycle_completed": "green",
    "validator_gates_passed": "green",
    "proposal_rejected": "red",
    "cycle_rollback": "red",
    "validator_gates_failed": "red",
    "validator_tune_failed": "red",
    "cycle_started": "cyan",
    "decision_started": "blue",
    "proposal_generated": "yellow",
    "proposal_reflected": "yellow",
}


def _filter_events(
    events: list[dict[str, Any]],
    event_type: str | None,
    target_file: str | None,
    cycle_number: int | None,
) -> list[dict[str, Any]]:
    if event_type:
        events = [e for e in events if e.get("event_type") == event_type]
    if target_file:
        events = [e for e in events if target_file in (e.get("target_file") or "")]
    if cycle_number is not None:
        events = [e for e in events if e.get("cycle_number") == cycle_number]
    return events


def _print_event(ev: dict[str, Any]) -> None:
    ts = (ev.get("created_at") or "?")[:19]
    etype = ev.get("event_type", "?")
    header = f"[{ts}] cycle={ev.get('cycle_number')}  {etype}"
    click.echo(click.style(header, fg=_EVENT_COLORS.get(etype, "white"), bold=True))
    if tfile := ev.get("target_file"):
        click.echo(f"  file:   {tfile}  action={ev.get('action') or ''}  status={ev.get('status') or ''}")
    if thought := ev.get("thought"):
        click.echo(f"  →  {thought[:120]}")
    if reason := ev.get("reason"):
        click.echo(f"  reason: {reason[:120]}")


@events_group.command("show")
@click.argument("project", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--type", "event_type", default=None, help="Filter by event type (e.g. cycle_started, proposal_applied)")
@click.option("--last", "last_n", default=20, show_default=True, help="Show last N events")
@click.option("--file", "target_file", default=None, help="Filter by target file path (substring match)")
@click.option("--cycle", "cycle_number", default=None, type=int, help="Filter by cycle number")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON lines")
def events_show(
    project: Path,
    event_type: str | None,
    last_n: int,
    target_file: str | None,
    cycle_number: int | None,
    as_json: bool,
) -> None:
    """Show decision events for a project from .redsl/history.jsonl."""
    events = _load_events(project)
    if not events:
        click.echo(f"No events found in {project / '.redsl' / 'history.jsonl'}")
        return

    events = _filter_events(events, event_type, target_file, cycle_number)[-last_n:]

    if not events:
        click.echo("No matching events.")
        return

    if as_json:
        for ev in events:
            click.echo(json.dumps(ev, ensure_ascii=False))
        return

    for ev in events:
        _print_event(ev)


def _print_apply_rate(events: list[dict[str, Any]], applied: int) -> None:
    proposals = sum(1 for e in events if e.get("event_type") == "proposal_generated")
    if not proposals:
        return
    rate = applied / proposals * 100
    color = "green" if rate >= 70 else "yellow" if rate >= 40 else "red"
    click.echo()
    click.echo(click.style(f"Apply rate: {rate:.0f}%  ({applied}/{proposals})", fg=color))


@events_group.command("summary")
@click.argument("project", type=click.Path(exists=True, file_okay=False, path_type=Path))
def events_summary(project: Path) -> None:
    """Print a statistical summary of all recorded events."""
    events = _load_events(project)
    if not events:
        click.echo(f"No events found in {project / '.redsl' / 'history.jsonl'}")
        return

    types = Counter(e.get("event_type") for e in events)
    _print_summary_header(project, events, types)
    _print_apply_rate(events, sum(1 for e in events if e.get("status") == "applied"))


def _print_summary_header(
    project: Path,
    events: list[dict[str, Any]],
    types: Counter,
) -> None:
    cycles = {e.get("cycle_number") for e in events if e.get("cycle_number") is not None}
    applied = sum(1 for e in events if e.get("status") == "applied")
    rejected = sum(1 for e in events if e.get("status") == "rejected")
    cycle_errors = [e for e in events if e.get("event_type") == "cycle_completed" and e.get("status") == "error"]
    rollbacks = sum(1 for e in events if e.get("event_type") == "cycle_rollback")

    click.echo(f"Project:   {project}")
    click.echo(f"Events:    {len(events)}  across {len(cycles)} cycle(s)")
    click.echo(f"Applied:   {applied}  |  Rejected: {rejected}  |  Rollbacks: {rollbacks}")
    if cycle_errors:
        click.echo(click.style(f"Errors:    {len(cycle_errors)} cycle(s) ended with error", fg="red"))
    click.echo()
    click.echo("Event type breakdown:")
    for etype, count in types.most_common():
        bar = "█" * min(count, 30)
        click.echo(f"  {count:4d}  {bar}  {etype}")


def _print_reflections_fallback(events: list[dict[str, Any]]) -> None:
    click.echo("No cycle_started / cycle_completed events found.")
    click.echo("(Run a refactor cycle to generate them. Older runs only have cycle_reflection events.)")
    reflections = [e for e in events if e.get("event_type") == "cycle_reflection"]
    if reflections:
        click.echo(f"\nFound {len(reflections)} cycle_reflection event(s) from pre-v1.2 runs:")
        for r in reflections:
            details = r.get("details") or {}
            click.echo(
                f"  cycle={r.get('cycle_number')}  applied={details.get('proposals_applied')}/"
                f"{details.get('proposals_generated')}  "
                f"rejected={details.get('proposals_rejected')}  "
                f"errors={len(details.get('errors') or [])}"
            )


def _print_cycle_row(
    cn: int,
    started: dict[int, dict[str, Any]],
    completed: dict[int, dict[str, Any]],
    rollbacks: set,
) -> None:
    s = started.get(cn, {})
    c = completed.get(cn, {})
    model = (s.get("details") or {}).get("llm_model", "?")[:28]
    status = c.get("status", "?")
    details = c.get("details") or {}
    applied = details.get("proposals_applied", "?")
    generated = details.get("proposals_generated", "?")
    rejected = details.get("proposals_rejected", "?")
    rb = "YES" if cn in rollbacks else "-"
    color = "green" if status == "ok" else "red" if status == "error" else "white"
    row = f"  {cn:>4}  {model:<30}  {click.style(status, fg=color):<8}  {str(applied)+'/'+str(generated):>12}  {str(rejected):>6}  {rb:>8}"
    click.echo(row)


@events_group.command("cycles")
@click.argument("project", type=click.Path(exists=True, file_okay=False, path_type=Path))
def events_cycles(project: Path) -> None:
    """Show per-cycle summary from cycle_started / cycle_completed events."""
    events = _load_events(project)

    started = {e.get("cycle_number"): e for e in events if e.get("event_type") == "cycle_started"}
    completed = {e.get("cycle_number"): e for e in events if e.get("event_type") == "cycle_completed"}
    rollbacks = {e.get("cycle_number") for e in events if e.get("event_type") == "cycle_rollback"}

    all_cycles = sorted(set(started) | set(completed))
    if not all_cycles:
        _print_reflections_fallback(events)
        return

    click.echo(f"{'Cycle':>6}  {'Model':<30}  {'Status':<8}  {'Applied/Gen':>12}  {'reject':>6}  {'rollback':>8}")
    click.echo("-" * 80)
    for cn in all_cycles:
        _print_cycle_row(cn, started, completed, rollbacks)


def register(cli_group: click.Group) -> None:
    cli_group.add_command(events_group)
