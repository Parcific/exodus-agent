from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .archive import Archive
from .job import JobEventKind, JobStore
from .protocols import DiscoverySource, MessageSource
from .runner import DryRunResult, export_dry_run
from .targets.telegram import (
    TelegramImportPlanResult,
    TelegramPackageResult,
    TelegramVerificationResult,
    verify_telegram_staging_package,
    write_telegram_import_plan,
    write_telegram_staging_package,
)
from .targets.telegram_executor import TelegramPlanExecutionResult, execute_import_plan
from .targets.teams_executor import (
    TeamsImportVerificationResult,
    TeamsMessageAdapter,
    TeamsPlanExecutionResult,
    execute_teams_import_plan,
    verify_teams_import,
)
from .targets.teams_mapping import (
    CompletedTeamsConversationMapping,
    TeamsImportPlanResult,
    write_teams_import_plan,
)


@dataclass(frozen=True)
class TelegramDryRunWorkflowResult:
    package: TelegramPackageResult
    verification: TelegramVerificationResult
    import_plan: TelegramImportPlanResult
    execution: TelegramPlanExecutionResult | None

    @property
    def ok(self) -> bool:
        return (
            self.verification.ok
            and self.import_plan.ready
            and self.execution is not None
            and self.execution.ok
        )


@dataclass(frozen=True)
class WebexTelegramDryRunWorkflowResult:
    export: DryRunResult
    telegram: TelegramDryRunWorkflowResult

    @property
    def ok(self) -> bool:
        return self.telegram.ok


@dataclass(frozen=True)
class WebexTeamsDryRunWorkflowResult:
    export: DryRunResult
    teams: TeamsDryRunWorkflowResult

    @property
    def ok(self) -> bool:
        return self.teams.ok


@dataclass(frozen=True)
class TeamsDryRunWorkflowResult:
    import_plan: TeamsImportPlanResult
    execution: TeamsPlanExecutionResult | None
    verification: TeamsImportVerificationResult | None

    @property
    def ok(self) -> bool:
        return (
            self.execution is not None
            and self.execution.ok
            and self.verification is not None
            and self.verification.ok
        )


def run_webex_to_telegram_dry_run_workflow(
    *,
    source: DiscoverySource & MessageSource,
    archive: Archive,
    package_root: Path,
    destination_map: dict[str, str],
    export_job_store: JobStore,
    telegram_job_store: JobStore,
    export_job_id: str,
    telegram_job_id: str,
    name: str,
) -> WebexTelegramDryRunWorkflowResult:
    export = export_dry_run(
        job_id=export_job_id,
        archive=archive,
        job_store=export_job_store,
        source=source,
        source_kind="webex",
        target_kind="telegram",
        name=name,
        reset_archive=True,
    )
    telegram = run_telegram_dry_run_workflow(
        archive=archive,
        package_root=package_root,
        destination_map=destination_map,
        job_store=telegram_job_store,
        job_id=telegram_job_id,
    )
    return WebexTelegramDryRunWorkflowResult(export=export, telegram=telegram)


def run_webex_to_teams_dry_run_workflow(
    *,
    source: DiscoverySource & MessageSource,
    archive: Archive,
    conversation_map: tuple[CompletedTeamsConversationMapping, ...],
    identity_map: dict[str, str],
    import_plan_path: Path,
    message_map_path: Path,
    verification_report_path: Path,
    export_job_store: JobStore,
    teams_job_store: JobStore,
    export_job_id: str,
    teams_job_id: str,
    name: str,
    overwrite_import_plan: bool = False,
    adapter: TeamsMessageAdapter | None = None,
) -> WebexTeamsDryRunWorkflowResult:
    _preflight_webex_to_teams_outputs(
        import_plan_path=import_plan_path,
        message_map_path=message_map_path,
        verification_report_path=verification_report_path,
        overwrite_import_plan=overwrite_import_plan,
    )
    export = export_dry_run(
        job_id=export_job_id,
        archive=archive,
        job_store=export_job_store,
        source=source,
        source_kind="webex",
        target_kind="teams",
        name=name,
        reset_archive=True,
    )
    teams = run_teams_dry_run_workflow(
        archive=archive,
        conversation_map=conversation_map,
        identity_map=identity_map,
        import_plan_path=import_plan_path,
        message_map_path=message_map_path,
        verification_report_path=verification_report_path,
        job_store=teams_job_store,
        job_id=teams_job_id,
        overwrite_import_plan=overwrite_import_plan,
        adapter=adapter,
    )
    return WebexTeamsDryRunWorkflowResult(export=export, teams=teams)


def _preflight_webex_to_teams_outputs(
    *,
    import_plan_path: Path,
    message_map_path: Path,
    verification_report_path: Path,
    overwrite_import_plan: bool,
) -> None:
    if message_map_path.exists():
        raise FileExistsError(
            "Teams message map already exists before fresh Webex export; "
            f"refusing to reuse stale mappings: {message_map_path}"
        )
    if import_plan_path.exists() and not import_plan_path.is_file():
        raise ValueError(f"Teams import plan output path must be a file: {import_plan_path}")
    if import_plan_path.exists() and not overwrite_import_plan:
        raise FileExistsError(import_plan_path)
    if verification_report_path.exists() and not verification_report_path.is_file():
        raise ValueError(
            f"Teams import verification report path must be a file: {verification_report_path}"
        )


def run_telegram_dry_run_workflow(
    *,
    archive: Archive,
    package_root: Path,
    destination_map: dict[str, str],
    job_store: JobStore,
    job_id: str,
) -> TelegramDryRunWorkflowResult:
    if _telegram_import_completed(job_store):
        raise FileExistsError(f"Telegram dry-run workflow already completed for job: {job_id}")
    package = write_telegram_staging_package(archive=archive, output_root=package_root)
    verification = verify_telegram_staging_package(archive=archive, package_root=package_root)
    import_plan = write_telegram_import_plan(
        archive=archive,
        package_root=package_root,
        destination_map=destination_map,
    )
    execution = None
    if verification.ok and import_plan.ready:
        execution = execute_import_plan(
            plan_path=import_plan.plan_path,
            job_store=job_store,
            job_id=job_id,
        )
    return TelegramDryRunWorkflowResult(
        package=package,
        verification=verification,
        import_plan=import_plan,
        execution=execution,
    )


def run_teams_dry_run_workflow(
    *,
    archive: Archive,
    conversation_map: tuple[CompletedTeamsConversationMapping, ...],
    identity_map: dict[str, str],
    import_plan_path: Path,
    message_map_path: Path,
    verification_report_path: Path,
    job_store: JobStore,
    job_id: str,
    overwrite_import_plan: bool = False,
    adapter: TeamsMessageAdapter | None = None,
) -> TeamsDryRunWorkflowResult:
    if _teams_import_completed(job_store):
        raise FileExistsError(f"Teams dry-run workflow already completed for job: {job_id}")
    import_plan = write_teams_import_plan(
        archive=archive,
        conversation_map=conversation_map,
        identity_map=identity_map,
        output_path=import_plan_path,
        overwrite=overwrite_import_plan,
    )
    execution = execute_teams_import_plan(
        plan_path=import_plan.path,
        message_map_path=message_map_path,
        job_store=job_store,
        job_id=job_id,
        adapter=adapter,
    )
    verification = None
    if execution.ok:
        verification = verify_teams_import(
            plan_path=import_plan.path,
            message_map_path=message_map_path,
            report_path=verification_report_path,
        )
    return TeamsDryRunWorkflowResult(
        import_plan=import_plan,
        execution=execution,
        verification=verification,
    )


def _telegram_import_completed(job_store: JobStore) -> bool:
    return any(
        event.get("kind") == JobEventKind.PHASE_COMPLETED.value
        and event.get("phase") == "telegram_import"
        for event in job_store.read_events()
    )


def _teams_import_completed(job_store: JobStore) -> bool:
    return any(
        event.get("kind") == JobEventKind.PHASE_COMPLETED.value
        and event.get("phase") == "teams_import"
        for event in job_store.read_events()
    )
