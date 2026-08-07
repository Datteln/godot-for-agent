"""Composition root for concrete application use cases."""

from __future__ import annotations

from app.application.completed_turns import CompletedTurnLedger
from app.application.context_service import SessionContextService
from app.application.history import HistoryQueryService
from app.application.lifecycle import SessionLifecycleService
from app.application.model_selection import model_for_effort, thinking_budget_for_effort
from app.application.progress import TurnActivityRegistry, TurnProgressRegistry
from app.application.publication import SubmissionPublisher
from app.application.session_uow import SessionUnitOfWork
from app.application.submission.backend_recovery import BackendRecoveryService
from app.application.submission.commit_service import SubmissionCommitService
from app.application.submission.coordinator import SubmissionCoordinator
from app.application.submission.preflight import SubmissionPreflightService
from app.application.submission.tool_artifacts import ToolArtifactService
from app.application.submission.tool_result_processor import ToolResultProcessor
from app.application.submission.tool_result_submission import ToolResultSubmissionUseCase
from app.application.submission.turn_service import TurnExecutionService
from app.application.submission.user_submission import UserSubmissionUseCase
from app.application.use_cases import (
    ApplicationUseCases,
    CompactionUseCase,
    HistoryUseCase,
    InterruptionUseCase,
    MapTaskControlUseCase,
    RecoveryUseCase,
    ResetUseCase,
    ResponseMappingUseCase,
    ResumeUseCase,
    SessionSettingsUseCase,
)
from app.config import AppSettings
from app.events.store import EventStore
from app.llm.cache_decision_engine import CacheDecisionEngine
from app.llm.cache_observability import CacheMetricsCollector
from app.llm.provider import LLMProvider
from app.orchestrator.map_artifacts import CoordinatedCommitFailureInjector
from app.output_styles.catalog import OutputStyleCatalog
from app.recovery.pointer import RecoveryPointerStore
from app.recovery.supervisor import RecoveryFailureInjector, RecoverySupervisor
from app.security.settings import SecuritySettings
from app.sessions.store import SessionStore
from app.skills.catalog import SkillCatalog
from app.tools.registry import REGISTRY
from app.verify.runner import VerifyRunner


def build_application_use_cases(
    *,
    settings: AppSettings,
    session_store: SessionStore,
    llm: LLMProvider,
    base_security: SecuritySettings,
    skill_catalog: SkillCatalog | None,
    output_style_catalog: OutputStyleCatalog | None,
    event_store: EventStore | None,
    recovery_store: RecoveryPointerStore | None,
    cache_engine: CacheDecisionEngine | None = None,
    cache_metrics: CacheMetricsCollector | None = None,
    coordinated_commit_failure_injector: CoordinatedCommitFailureInjector | None = None,
    recovery_failure_injector: RecoveryFailureInjector | None = None,
) -> ApplicationUseCases:
    """Construct independently injected route-facing use cases."""
    resolved_cache_engine = cache_engine or CacheDecisionEngine()
    recovery_supervisor = RecoverySupervisor(
        recovery_failure_injector,
        session_store.save_task_run,
    )
    progress = TurnProgressRegistry()
    activity = TurnActivityRegistry()
    history_cache: dict[tuple[str, str], tuple[tuple[int, int, int], list[object]]] = {}
    def available_tools() -> set[str]:
        return set(REGISTRY)
    publisher = SubmissionPublisher(
        settings=settings,
        store=session_store,
        events=event_store,
        recovery=recovery_store,
        recovery_supervisor=recovery_supervisor,
        available_tools=available_tools,
    )
    context_service = SessionContextService(
        settings=settings,
        store=session_store,
        llm=llm,
        cache_engine=resolved_cache_engine,
        emit=publisher.emit,
        available_tools=available_tools,
    )
    tool_artifacts = ToolArtifactService(
        settings=settings,
        store=session_store,
        available_tools=available_tools,
    )
    tool_result_processor = ToolResultProcessor(
        settings=settings,
        store=session_store,
        publisher=publisher,
        artifacts=tool_artifacts,
    )
    verify_runner = VerifyRunner(
        settings,
        llm,
        publisher.emit,
        lambda effort: model_for_effort(settings, effort),
        lambda effort: thinking_budget_for_effort(settings, effort),
    )
    turn_service = TurnExecutionService(
        settings=settings,
        llm=llm,
        base_security=base_security,
        skill_catalog=skill_catalog,
        output_styles=output_style_catalog,
        cache_engine=resolved_cache_engine,
        cache_metrics=cache_metrics or CacheMetricsCollector(),
        publisher=publisher,
        context_service=context_service,
        tool_results=tool_result_processor,
        verify_runner=verify_runner,
        available_tools=available_tools,
    )
    backend_recovery = BackendRecoveryService(
        store=session_store,
        recovery_supervisor=recovery_supervisor,
        publisher=publisher,
        turn_service=turn_service,
    )
    completed_turns = CompletedTurnLedger(
        settings.project_root,
        settings.completed_response_hot_cache_size,
    )
    commit_service = SubmissionCommitService(
        settings=settings,
        store=session_store,
        completed_turns=completed_turns,
        recovery_supervisor=recovery_supervisor,
        publisher=publisher,
        progress=progress,
        coordinated_failure=coordinated_commit_failure_injector,
    )
    preflight = SubmissionPreflightService(
        settings=settings,
        store=session_store,
        events=event_store,
        completed_turns=completed_turns,
        recovery_supervisor=recovery_supervisor,
        available_tools=available_tools,
    )
    unit_of_work = SessionUnitOfWork(session_store)
    submission = SubmissionCoordinator(
        settings,
        session_store,
        event_store=event_store,
        recovery_store=recovery_store,
        coordinated_commit_failure_injector=coordinated_commit_failure_injector,
        recovery_supervisor=recovery_supervisor,
        publisher=publisher,
        activity=activity,
        progress=progress,
        backend_recovery=backend_recovery,
        completed_turns=completed_turns,
        commit_service=commit_service,
        preflight=preflight,
        unit_of_work=unit_of_work,
    )
    lifecycle = SessionLifecycleService(
        settings=settings,
        store=session_store,
        events=event_store,
        recovery=recovery_store,
        recovery_supervisor=recovery_supervisor,
        publisher=publisher,
        activity=activity,
        progress=progress,
        history_cache=history_cache,
        available_tools=available_tools,
    )
    history = HistoryQueryService(
        settings=settings,
        store=session_store,
        events=event_store,
        cache=history_cache,
        available_tools=available_tools,
    )
    return ApplicationUseCases(
        user_submission=UserSubmissionUseCase(submission),
        tool_result_submission=ToolResultSubmissionUseCase(submission, lifecycle),
        resume=ResumeUseCase(lifecycle),
        interruption=InterruptionUseCase(lifecycle),
        reset=ResetUseCase(lifecycle),
        history=HistoryUseCase(history),
        compaction=CompactionUseCase(context_service),
        settings=SessionSettingsUseCase(lifecycle),
        map_tasks=MapTaskControlUseCase(lifecycle),
        recovery=RecoveryUseCase(lifecycle),
        response_mapping=ResponseMappingUseCase(),
    )
