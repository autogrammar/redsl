"""Main refactoring cycle orchestration."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from redsl.analyzers import AnalysisResult
from redsl.analyzers import code2llm_bridge
from redsl.execution.backup_manager import cleanup_backups, rollback_from_backups

if TYPE_CHECKING:
    from redsl.orchestrator import CycleReport, RefactorOrchestrator
    from redsl.history import HistoryWriter

logger = logging.getLogger(__name__)


def _new_cycle_report(orchestrator: "RefactorOrchestrator") -> "CycleReport":
    from redsl.orchestrator import CycleReport

    return CycleReport(
        cycle_number=orchestrator._cycle_count,
        analysis_summary="",
        decisions_count=0,
        proposals_generated=0,
        proposals_applied=0,
        proposals_rejected=0,
    )


def _analyze_project(
    orchestrator: "RefactorOrchestrator",
    project_dir: Path,
    use_code2llm: bool,
) -> AnalysisResult:
    if use_code2llm:
        return code2llm_bridge.maybe_analyze(project_dir, orchestrator.analyzer) \
            or orchestrator.analyzer.analyze_project(project_dir)
    return orchestrator.analyzer.analyze_project(project_dir)


def _summarize_analysis(analysis: AnalysisResult) -> str:
    return (
        f"{analysis.total_files} files, {analysis.total_lines} lines, "
        f"avg CC={analysis.avg_cc:.1f}, {analysis.critical_count} critical"
    )


def _run_perceive_phase(
    orchestrator: "RefactorOrchestrator",
    project_dir: Path,
    use_code2llm: bool,
    report: "CycleReport",
) -> "AnalysisResult":
    """Run PERCEIVE phase: analyze project and enrich data."""
    logger.info("=== CYCLE %d: PERCEIVE ===", orchestrator._cycle_count)
    analysis = _analyze_project(orchestrator, project_dir, use_code2llm)

    from redsl.analyzers import redup_bridge
    if redup_bridge.is_available():
        analysis = redup_bridge.enrich_analysis(analysis, project_dir)
        dup_count = len(analysis.duplicates)
        if dup_count:
            logger.info("redup: enriched analysis with %d duplicate groups", dup_count)

    report.analysis_summary = _summarize_analysis(analysis)
    return analysis


def _run_decide_phase(
    orchestrator: "RefactorOrchestrator",
    analysis: "AnalysisResult",
    max_actions: int,
    target_file: str | None,
    validate_regix: bool,
    run_tests: bool,
    project_dir: Path,
) -> tuple[list, "Any", "Any"]:
    """Run DECIDE phase: select decisions and prepare validation snapshots."""
    from redsl.execution.decision import _select_decisions
    from redsl.execution.resolution import _consult_memory_for_decisions
    from redsl.execution.test_validation import run_tests_baseline
    from redsl.execution.validation import _snapshot_regix_before

    logger.info("=== CYCLE %d: DECIDE ===", orchestrator._cycle_count)
    decisions = _select_decisions(orchestrator, analysis, max_actions, target_file=target_file)

    if not decisions:
        logger.info("No refactoring decisions — code looks good!")
        return [], None, None

    regix_before = _snapshot_regix_before(project_dir, validate_regix)
    _consult_memory_for_decisions(orchestrator, decisions)
    tests_baseline = run_tests_baseline(project_dir) if run_tests else None

    return decisions, regix_before, tests_baseline


def _run_execute_phase(
    orchestrator: "RefactorOrchestrator",
    decisions: list,
    project_dir: Path,
    use_sandbox: bool,
    report: "CycleReport",
) -> None:
    """Run PLAN + EXECUTE phase: apply decisions."""
    from redsl.execution.decision import _execute_decisions
    logger.info("=== CYCLE %d: PLAN + EXECUTE ===", orchestrator._cycle_count)
    _execute_decisions(orchestrator, decisions, project_dir, use_sandbox, report)


def _run_validate_phase(
    orchestrator: "RefactorOrchestrator",
    project_dir: Path,
    regix_before: "Any",
    rollback_on_failure: bool,
    validate_regix: bool,
    report: "CycleReport",
) -> None:
    """Run VALIDATE phase: check regix compliance."""
    from redsl.execution.validation import _validate_with_regix
    if validate_regix and report.proposals_applied > 0:
        logger.info("=== CYCLE %d: VALIDATE (regix) ===", orchestrator._cycle_count)
        _validate_with_regix(project_dir, regix_before, rollback_on_failure, report)


def _get_applied_files(report: "CycleReport") -> list[str]:
    """Extract list of applied file paths from report results."""
    return [
        ch.file_path
        for r in report.results
        if r.applied and r.proposal
        for ch in r.proposal.changes
    ]


def _run_update_planfile_phase(
    orchestrator: "RefactorOrchestrator",
    project_dir: Path,
    report: "CycleReport",
) -> None:
    """Run UPDATE PLANFILE phase: mark completed tasks."""
    if report.proposals_applied > 0:
        logger.info("=== CYCLE %d: UPDATE PLANFILE ===", orchestrator._cycle_count)
        from redsl.execution.planfile_updater import mark_applied_tasks_done
        applied_files = _get_applied_files(report)
        updated = mark_applied_tasks_done(project_dir, applied_files)
        if updated:
            logger.info("planfile: marked %d task(s) done", updated)


from redsl.execution.pyqual_validators import (  # noqa: E402
    _find_project_pyqual,
    _pyqual_tune_conservative,
    _run_project_validators_phase,
)


def _run_test_validation_phase(
    orchestrator: "RefactorOrchestrator",
    project_dir: Path,
    tests_baseline: "Any",
    run_tests: bool,
    report: "CycleReport",
) -> None:
    """Run VALIDATE (tests) phase."""
    from redsl.execution.test_validation import validate_with_tests
    if run_tests and report.proposals_applied > 0:
        logger.info("=== CYCLE %d: VALIDATE (tests) ===", orchestrator._cycle_count)
        applied_files = _get_applied_files(report)
        validate_with_tests(project_dir, tests_baseline, applied_files, report)


def _run_reflect_phase(
    orchestrator: "RefactorOrchestrator",
    report: "CycleReport",
) -> None:
    """Run REFLECT phase: learn from cycle results."""
    from redsl.execution.reflector import _reflect_on_cycle
    logger.info("=== CYCLE %d: REFLECT ===", orchestrator._cycle_count)
    _reflect_on_cycle(orchestrator, report)


def _run_push_action(
    deploy_cfg: "Any",
    detected: "Any",
    project_dir: Path,
    report: "CycleReport",
    history: "HistoryWriter | None",
    dry_run: bool,
) -> None:
    """Run the git push deploy action if configured and detected."""
    from redsl.execution.deploy_detector import run_deploy_action

    should_push = (
        deploy_cfg.push is True or
        (deploy_cfg.push == "auto" and detected.push.method != "none")
    )
    if should_push and detected.push.method != "none":
        push_ok = run_deploy_action(detected.push, project_dir, dry_run=dry_run)
        if history:
            history.record_event(
                "deploy_push",
                cycle_number=report.cycle_number,
                status="ok" if push_ok else "error",
                thought=f"git push {'succeeded' if push_ok else 'failed'}: {detected.push.label}",
                details={"method": detected.push.method, "label": detected.push.label, "dry_run": dry_run},
            )
    elif should_push:
        logger.debug("deploy: push requested but no mechanism detected in %s", project_dir.name)


def _run_publish_action(
    deploy_cfg: "Any",
    detected: "Any",
    project_dir: Path,
    report: "CycleReport",
    history: "HistoryWriter | None",
    dry_run: bool,
) -> None:
    """Run the registry/PyPI publish deploy action if configured and detected."""
    from redsl.execution.deploy_detector import run_deploy_action

    should_publish = (
        deploy_cfg.publish is True or
        (deploy_cfg.publish == "auto" and detected.publish.method != "none")
    )
    if should_publish and detected.publish.method != "none":
        publish_ok = run_deploy_action(detected.publish, project_dir, dry_run=dry_run)
        if history:
            history.record_event(
                "deploy_publish",
                cycle_number=report.cycle_number,
                status="ok" if publish_ok else "error",
                thought=f"publish {'succeeded' if publish_ok else 'failed'}: {detected.publish.label}",
                details={"method": detected.publish.method, "label": detected.publish.label, "dry_run": dry_run},
            )
    elif should_publish:
        logger.debug("deploy: publish requested but no mechanism detected in %s", project_dir.name)


def _run_deploy_phase(
    project_dir: Path,
    report: "CycleReport",
    workflow: "WorkflowConfig",
    dry_run: bool = False,
    history: "HistoryWriter | None" = None,
) -> None:
    """DEPLOY phase: push to git and/or publish to registry if configured.

    Respects ``workflow.deploy`` settings:
    - ``enabled: auto/true/false`` — master switch
    - ``push: auto/true/false`` — git push
    - ``publish: auto/true/false`` — registry/PyPI publish
    - ``on_success_only: true`` — skip if cycle applied 0 actions
    """
    from redsl.execution.deploy_detector import detect_deploy_config

    deploy_cfg = workflow.deploy
    logger.info("=== CYCLE %d: DEPLOY ===", report.cycle_number)

    # Master switch
    if deploy_cfg.enabled is False:
        logger.debug("deploy: disabled in workflow config")
        return

    # on_success_only: skip if nothing was applied
    if deploy_cfg.on_success_only and report.proposals_applied == 0:
        logger.debug("deploy: skipping — no actions applied (on_success_only=True)")
        return

    # Auto-detect available deploy mechanisms
    detected = detect_deploy_config(project_dir)

    if detected.ci_handles_deploy:
        logger.info(
            "deploy: CI workflows detected (%s) — git push will trigger CI",
            ", ".join(detected.ci_workflow_files),
        )

    _run_push_action(deploy_cfg, detected, project_dir, report, history, dry_run)
    _run_publish_action(deploy_cfg, detected, project_dir, report, history, dry_run)


def _configure_chat_log(
    orchestrator: "RefactorOrchestrator",
    project_dir: Path,
    wf: "WorkflowConfig",
) -> None:
    """Configure LLM chat log path if storage is enabled."""
    if wf.storage.chat_log_enabled and hasattr(orchestrator, "llm"):
        chat_log = project_dir / wf.storage.base_dir / wf.storage.chat_log_filename
        orchestrator.llm.set_chat_log(chat_log)
        logger.debug("chat_log: %s", chat_log)


def _resolve_cycle_flags(
    wf: "WorkflowConfig",
    max_actions: int,
    use_code2llm: bool,
    use_sandbox: bool,
    rollback_on_failure: bool,
    run_tests: bool,
) -> tuple[int, bool, bool, bool, bool]:
    """Resolve CLI flags against workflow defaults."""
    tests_step = wf.validate.get_step("tests")
    return (
        max_actions if max_actions != 5 else wf.decide.max_actions,
        use_code2llm or wf.perceive.use_code2llm,
        use_sandbox or wf.execute.use_sandbox,
        rollback_on_failure or wf.execute.rollback_on_failure,
        run_tests or (tests_step is not None and bool(tests_step.enabled)),
    )


def _record_cycle_started(
    orchestrator: "RefactorOrchestrator",
    project_dir: Path,
    wf: "WorkflowConfig",
    max_actions: int,
    use_code2llm: bool,
    rollback_on_failure: bool,
    use_sandbox: bool,
) -> None:
    """Record cycle start — snapshot of key config for post-mortem."""
    llm_cfg = getattr(getattr(orchestrator, "config", None), "llm", None)
    llm_model_name = llm_cfg.model if llm_cfg else "unknown"
    if wf.decide.llm_model != "auto":
        llm_model_name = wf.decide.llm_model
    orchestrator.history.record_event(
        "cycle_started",
        cycle_number=orchestrator._cycle_count,
        thought=f"project={project_dir.name} max_actions={max_actions} model={llm_model_name}",
        details={
            "project_dir": str(project_dir),
            "max_actions": max_actions,
            "llm_model": llm_model_name,
            "llm_temperature": wf.decide.llm_temperature,
            "workflow_source": wf.source,
            "workflow_name": wf.name,
            "use_code2llm": use_code2llm,
            "rollback_on_failure": rollback_on_failure,
            "use_sandbox": use_sandbox,
        },
    )


def _record_cycle_completed(
    orchestrator: "RefactorOrchestrator",
    project_dir: Path,
    report: "CycleReport",
) -> None:
    """Record cycle outcome regardless of success/failure."""
    orchestrator.history.record_event(
        "cycle_completed",
        cycle_number=orchestrator._cycle_count,
        status="ok" if not report.errors else "error",
        thought=(
            f"applied={report.proposals_applied}/{report.proposals_generated} "
            f"decisions={report.decisions_count} errors={len(report.errors)}"
        ),
        details={
            "project_dir": str(project_dir),
            "proposals_generated": report.proposals_generated,
            "proposals_applied": report.proposals_applied,
            "proposals_rejected": report.proposals_rejected,
            "decisions_count": report.decisions_count,
            "errors": report.errors[:5],
            "analysis_summary": report.analysis_summary,
        },
    )


def _handle_cycle_error(
    orchestrator: "RefactorOrchestrator",
    project_dir: Path,
    report: "CycleReport",
    error: Exception,
) -> None:
    """Log cycle failure and restore backed-up files."""
    logger.error("Cycle %d failed: %s", orchestrator._cycle_count, error)
    report.errors.append(str(error))
    rolled_back = rollback_from_backups(project_dir)
    if rolled_back:
        logger.info("Rolled back %d file(s) from backups", rolled_back)
        orchestrator.history.record_event(
            "cycle_rollback",
            cycle_number=orchestrator._cycle_count,
            thought=f"Rolled back {rolled_back} file(s) after error: {error}",
            details={"files_rolled_back": rolled_back, "error": str(error)},
        )


def run_cycle(
    orchestrator: "RefactorOrchestrator",
    project_dir: Path,
    max_actions: int = 5,
    use_code2llm: bool = False,
    validate_regix: bool = False,
    rollback_on_failure: bool = False,
    use_sandbox: bool = False,
    target_file: str | None = None,
    run_tests: bool = False,
    workflow: "WorkflowConfig | None" = None,
) -> "CycleReport":
    """Run a complete refactoring cycle driven by WorkflowConfig.

    If *workflow* is None, ``load_workflow(project_dir)`` is called automatically
    which searches for ``redsl.yaml`` in the project, then falls back to
    the bundled default.
    """
    from redsl.execution.workflow import load_workflow

    wf = workflow or load_workflow(project_dir)
    logger.debug("workflow: using '%s' (source: %s)", wf.name, wf.source)

    _configure_chat_log(orchestrator, project_dir, wf)
    _max_actions, _use_code2llm, _use_sandbox, _rollback, _run_tests = _resolve_cycle_flags(
        wf, max_actions, use_code2llm, use_sandbox, rollback_on_failure, run_tests
    )

    orchestrator._cycle_count += 1
    report = _new_cycle_report(orchestrator)
    _record_cycle_started(
        orchestrator, project_dir, wf, _max_actions, _use_code2llm, _rollback, _use_sandbox
    )

    try:
        analysis = _run_perceive_phase(orchestrator, project_dir, _use_code2llm, report)

        decisions, regix_before, tests_baseline = _run_decide_phase(
            orchestrator, analysis, _max_actions, target_file, validate_regix, _run_tests, project_dir
        )
        report.decisions_count = len(decisions)

        if not decisions:
            return report

        _run_execute_phase(orchestrator, decisions, project_dir, _use_sandbox, report)
        _run_validate_phase(orchestrator, project_dir, regix_before, _rollback, validate_regix, report)
        if wf.planfile.update_on_apply:
            _run_update_planfile_phase(orchestrator, project_dir, report)
        _run_project_validators_phase(project_dir, report, wf)
        _run_test_validation_phase(orchestrator, project_dir, tests_baseline, _run_tests, report)
        if wf.reflect.enabled:
            _run_reflect_phase(orchestrator, report)
        _run_deploy_phase(project_dir, report, wf, history=orchestrator.history)
        # Successful cycle — remove backups
        cleanup_backups(project_dir)

    except Exception as e:
        _handle_cycle_error(orchestrator, project_dir, report, e)

    _record_cycle_completed(orchestrator, project_dir, report)
    return report


def run_from_toon_content(
    orchestrator: "RefactorOrchestrator",
    project_toon: str = "",
    duplication_toon: str = "",
    validation_toon: str = "",
    source_files: dict[str, str] | None = None,
    max_actions: int = 5,
) -> "CycleReport":
    """Run a cycle from pre-parsed toon content."""
    from redsl.execution.reflector import _reflect_on_cycle
    from redsl.orchestrator import CycleReport

    orchestrator._cycle_count += 1
    report = CycleReport(
        cycle_number=orchestrator._cycle_count,
        analysis_summary="",
        decisions_count=0,
        proposals_generated=0,
        proposals_applied=0,
        proposals_rejected=0,
    )

    analysis = orchestrator.analyzer.analyze_from_toon_content(
        project_toon=project_toon,
        duplication_toon=duplication_toon,
        validation_toon=validation_toon,
    )
    report.analysis_summary = (
        f"{analysis.total_files} files, {analysis.total_lines} lines"
    )

    contexts = analysis.to_dsl_contexts()
    from redsl.execution.decision import _select_decisions
    decisions = orchestrator.dsl_engine.top_decisions(contexts, limit=max_actions)
    report.decisions_count = len(decisions)

    for decision in decisions:
        if not decision.should_execute:
            continue

        source = (source_files or {}).get(decision.target_file, "# source not provided")

        try:
            proposal = orchestrator.refactor_engine.generate_proposal(decision, source)
            proposal = orchestrator.refactor_engine.reflect_on_proposal(proposal, source)
            result = orchestrator.refactor_engine.validate_proposal(proposal)
            report.results.append(result)
            report.proposals_generated += 1

            if result.validated:
                orchestrator.refactor_engine._save_proposal(proposal)

        except Exception as e:
            logger.error("Failed: %s", e)
            report.errors.append(str(e))

    _reflect_on_cycle(orchestrator, report)

    return report


__all__ = ["_new_cycle_report", "_analyze_project", "_summarize_analysis", "run_cycle", "run_from_toon_content"]
