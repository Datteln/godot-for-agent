"""Test composition helpers for the concrete application use cases."""

from __future__ import annotations

from dataclasses import dataclass

from app.api.schemas import ChatRequest, ChatResponse
from app.application.composition import build_application_use_cases
from app.application.lifecycle import SessionLifecycleService
from app.application.submission.coordinator import SubmissionCoordinator
from app.application.publication import SubmissionPublisher
from app.application.use_cases import ApplicationUseCases
from app.config import AppSettings
from app.events.store import EventStore
from app.llm.provider import LLMProvider
from app.orchestrator.map_artifacts import CoordinatedCommitFailureInjector
from app.recovery.pointer import RecoveryPointerStore
from app.recovery.supervisor import RecoveryFailureInjector
from app.security.settings import SecuritySettings, security_settings_from_app
from app.sessions.store import SessionStore


@dataclass(frozen=True, slots=True)
class ApplicationTestRig:
    """Expose the real use-case graph and named test observability ports."""

    use_cases: ApplicationUseCases
    store: SessionStore
    events: EventStore | None

    @property
    def coordinator(self) -> SubmissionCoordinator:
        return self.use_cases.user_submission.coordinator

    @property
    def available_tools(self) -> set[str]:
        return self.coordinator.available_tools

    @property
    def publisher(self) -> SubmissionPublisher:
        return self.coordinator._publisher

    @property
    def lifecycle(self) -> SessionLifecycleService:
        return self.use_cases.reset._service

    async def execute(self, request: ChatRequest) -> ChatResponse:
        if request.tool_results:
            return await self.use_cases.tool_result_submission.execute(request)
        return await self.use_cases.user_submission.execute(request)


def build_test_application(
    *,
    settings: AppSettings,
    session_store: SessionStore,
    llm: LLMProvider,
    event_store: EventStore | None = None,
    recovery_store: RecoveryPointerStore | None = None,
    base_security: SecuritySettings | None = None,
    coordinated_commit_failure_injector: CoordinatedCommitFailureInjector | None = None,
    recovery_failure_injector: RecoveryFailureInjector | None = None,
) -> ApplicationTestRig:
    """Build the production graph with explicit test doubles at external ports."""
    use_cases = build_application_use_cases(
        settings=settings,
        session_store=session_store,
        llm=llm,
        base_security=base_security or security_settings_from_app(settings),
        skill_catalog=None,
        output_style_catalog=None,
        event_store=event_store,
        recovery_store=recovery_store,
        coordinated_commit_failure_injector=coordinated_commit_failure_injector,
        recovery_failure_injector=recovery_failure_injector,
    )
    return ApplicationTestRig(use_cases, session_store, event_store)
