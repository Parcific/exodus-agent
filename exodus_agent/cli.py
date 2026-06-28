from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path
from typing import Callable, TypeVar

from .archive import Archive
from .config import MigrationConfig, load_config
from .secrets import SecretResolutionError, resolve_secret
from .job import JobStore, validate_job_id
from .planner import build_plan
from .runner import export_dry_run
from .sources.webex import source_from_config as webex_source_from_config
from .targets.telegram import (
    verify_telegram_staging_package,
    write_telegram_destination_map_template,
    write_telegram_import_plan,
    write_telegram_staging_package,
)
from .targets.telegram_executor import SubprocessTelegramAdapter, execute_import_plan
from .targets.teams_executor import (
    DryRunTeamsAdapter,
    TeamsMessageAdapter,
    execute_teams_import_plan,
    verify_teams_import,
)
from .targets.teams_mapping import (
    build_teams_identity_prefill_from_entra,
    load_teams_conversation_map,
    load_entra_users,
    load_teams_identity_map,
    write_teams_identity_map_template,
    write_teams_import_plan,
    write_teams_mapping_template,
)
from .workflow import (
    run_teams_dry_run_workflow,
    run_telegram_dry_run_workflow,
    run_webex_to_teams_dry_run_workflow,
    run_webex_to_telegram_dry_run_workflow,
)

T = TypeVar("T")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="exodus",
        description="Local-first migration tooling for collaboration data.",
    )
    parser.add_argument("--version", action="store_true", help="Print version and exit.")

    subparsers = parser.add_subparsers(dest="command")

    doctor = subparsers.add_parser("doctor", help="Validate configuration and environment.")
    doctor.add_argument("--config", required=True, type=Path)

    plan = subparsers.add_parser("plan", help="Print a migration plan from config.")
    plan.add_argument("--config", required=True, type=Path)

    init = subparsers.add_parser("init-job", help="Initialize archive and job metadata.")
    init.add_argument("--config", required=True, type=Path)
    init.add_argument("--job-id", default="default")

    dry_run = subparsers.add_parser("export-dry-run", help="Extract source data into an archive.")
    dry_run.add_argument("--config", required=True, type=Path)
    dry_run.add_argument("--job-id", default="dry-run")

    telegram_package = subparsers.add_parser(
        "telegram-package",
        help="Generate Telegram staging package from a canonical archive.",
    )
    telegram_package.add_argument("--config", required=True, type=Path)
    telegram_package.add_argument("--output", type=Path)

    telegram_verify = subparsers.add_parser(
        "telegram-verify",
        help="Verify Telegram staging package against a canonical archive.",
    )
    telegram_verify.add_argument("--config", required=True, type=Path)
    telegram_verify.add_argument("--package", type=Path)

    telegram_destination_map = subparsers.add_parser(
        "telegram-destination-map-template",
        help="Write a Telegram destination-map JSON template from archived conversations.",
    )
    telegram_destination_map.add_argument("--config", required=True, type=Path)
    telegram_destination_map.add_argument("--output", type=Path)
    telegram_destination_map.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace an existing destination-map template.",
    )

    telegram_import_plan = subparsers.add_parser(
        "telegram-import-plan",
        help="Create MTProto import operation plan from a verified Telegram package.",
    )
    telegram_import_plan.add_argument("--config", required=True, type=Path)
    telegram_import_plan.add_argument("--package", type=Path)
    telegram_import_plan.add_argument(
        "--destination-map",
        type=Path,
        help="JSON object mapping source conversation IDs to Telegram peer IDs/usernames.",
    )

    telegram_execute = subparsers.add_parser(
        "telegram-execute-plan",
        help="Execute a Telegram import plan with the dry-run adapter.",
    )
    telegram_execute.add_argument("--config", required=True, type=Path)
    telegram_execute.add_argument("--plan", type=Path)
    telegram_execute.add_argument("--job-id", default="telegram-import")
    telegram_execute.add_argument(
        "--adapter-command",
        help="Optional external command that executes one MTProto operation from stdin JSON.",
    )

    telegram_workflow = subparsers.add_parser(
        "telegram-dry-run-workflow",
        help="Package, verify, plan, and dry-run execute a Telegram migration from an archive.",
    )
    telegram_workflow.add_argument("--config", required=True, type=Path)
    telegram_workflow.add_argument("--package", type=Path)
    telegram_workflow.add_argument("--destination-map", required=True, type=Path)
    telegram_workflow.add_argument("--job-id", default="telegram-dry-run")

    webex_telegram_workflow = subparsers.add_parser(
        "webex-telegram-dry-run",
        help="Extract Webex data, package for Telegram, verify, plan, and dry-run execute.",
    )
    webex_telegram_workflow.add_argument("--config", required=True, type=Path)
    webex_telegram_workflow.add_argument("--destination-map", required=True, type=Path)
    webex_telegram_workflow.add_argument("--package", type=Path)
    webex_telegram_workflow.add_argument("--job-id", default="webex-telegram-dry-run")

    teams_identity_map = subparsers.add_parser(
        "teams-identity-map-template",
        help="Write a Teams identity-map JSON template from archived Webex participants.",
    )
    teams_identity_map.add_argument("--config", required=True, type=Path)
    teams_identity_map.add_argument("--output", type=Path)
    teams_identity_map.add_argument("--prefill", type=Path, help="Existing completed Teams identity map.")
    teams_identity_map.add_argument(
        "--entra-users",
        type=Path,
        help="Graph JSON or CSV export of Entra users for exact email/UPN/proxy-address prefill.",
    )
    teams_identity_map.add_argument("--overwrite", action="store_true")

    teams_conversation_map = subparsers.add_parser(
        "teams-conversation-map-template",
        help="Write Teams conversation mapping suggestions from an archive and identity map.",
    )
    teams_conversation_map.add_argument("--config", required=True, type=Path)
    teams_conversation_map.add_argument("--identity-map", required=True, type=Path)
    teams_conversation_map.add_argument("--output", type=Path)
    teams_conversation_map.add_argument("--overwrite", action="store_true")
    teams_conversation_map.add_argument("--group-chat-member-limit", type=int, default=8)

    teams_import_plan = subparsers.add_parser(
        "teams-import-plan",
        help="Write a Teams import plan from an archive, identity map, and completed conversation map.",
    )
    teams_import_plan.add_argument("--config", required=True, type=Path)
    teams_import_plan.add_argument("--identity-map", required=True, type=Path)
    teams_import_plan.add_argument("--conversation-map", required=True, type=Path)
    teams_import_plan.add_argument("--output", type=Path)
    teams_import_plan.add_argument("--overwrite", action="store_true")

    teams_execute = subparsers.add_parser(
        "teams-execute-plan",
        help="Execute a Teams import plan with the dry-run adapter and write a message map.",
    )
    teams_execute.add_argument("--config", required=True, type=Path)
    teams_execute.add_argument("--plan", type=Path)
    teams_execute.add_argument("--message-map", type=Path)
    teams_execute.add_argument("--job-id", default="teams-import")

    teams_verify = subparsers.add_parser(
        "teams-verify-import",
        help="Verify a Teams import plan against the persisted message map.",
    )
    teams_verify.add_argument("--config", required=True, type=Path)
    teams_verify.add_argument("--plan", type=Path)
    teams_verify.add_argument("--message-map", type=Path)
    teams_verify.add_argument("--report", type=Path)

    teams_workflow = subparsers.add_parser(
        "teams-dry-run-workflow",
        help="Plan, dry-run execute, and verify a Teams migration from an archive.",
    )
    teams_workflow.add_argument("--config", required=True, type=Path)
    teams_workflow.add_argument("--identity-map", required=True, type=Path)
    teams_workflow.add_argument("--conversation-map", required=True, type=Path)
    teams_workflow.add_argument("--plan", type=Path)
    teams_workflow.add_argument("--message-map", type=Path)
    teams_workflow.add_argument("--report", type=Path)
    teams_workflow.add_argument("--job-id", default="teams-dry-run")
    teams_workflow.add_argument("--overwrite-plan", action="store_true")

    webex_teams_workflow = subparsers.add_parser(
        "webex-teams-dry-run",
        help="Extract Webex data, plan, dry-run execute, and verify a Teams migration.",
    )
    webex_teams_workflow.add_argument("--config", required=True, type=Path)
    webex_teams_workflow.add_argument("--identity-map", required=True, type=Path)
    webex_teams_workflow.add_argument("--conversation-map", required=True, type=Path)
    webex_teams_workflow.add_argument("--plan", type=Path)
    webex_teams_workflow.add_argument("--message-map", type=Path)
    webex_teams_workflow.add_argument("--report", type=Path)
    webex_teams_workflow.add_argument("--job-id", default="webex-teams-dry-run")
    webex_teams_workflow.add_argument("--overwrite-plan", action="store_true")

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.version:
        from . import __version__

        print(__version__)
        return

    if args.command == "doctor":
        config = load_config(args.config)
        plan = build_plan(config)
        print("Config: OK")
        print(f"Migration: {config.source.kind} -> {config.target.kind}")
        print(f"Mode: {config.mode}")
        print(f"Runtime: {config.runtime}")
        print(f"Workspace: {config.workspace}")
        try:
            resolve_secret(config.source.settings.get("auth"), field_name="source.auth")
            print("Secrets: OK")
        except SecretResolutionError as exc:
            print(f"Secrets: FAILED — {exc}")
        if plan.warnings:
            print("Warnings:")
            for warning in plan.warnings:
                print(f"  - {warning}")
        return

    if args.command == "plan":
        config = load_config(args.config)
        plan = build_plan(config)
        print(f"Migration plan: {config.name}")
        print(f"  Source: {config.source.kind}")
        print(f"  Target: {config.target.kind}")
        print(f"  Mode: {config.mode}")
        print(f"  Runtime: {config.runtime}")
        print(f"  Workspace: {config.workspace}")
        print("  Phases:")
        for phase in plan.phases:
            print(f"    - {phase}")
        if plan.warnings:
            print("  Warnings:")
            for warning in plan.warnings:
                print(f"    - {warning}")
        return

    if args.command == "init-job":
        config = load_config(args.config)
        build_plan(config)
        archive = Archive(config.workspace / "archive")
        archive.initialize(
            source_kind=config.source.kind,
            target_kind=config.target.kind,
            name=config.name,
        )
        store = _job_store(config.workspace, args.job_id)
        _run_cli_action(lambda: store.create(job_id=args.job_id))
        print(f"Initialized job: {args.job_id}")
        print(f"Workspace: {config.workspace}")
        return

    if args.command == "export-dry-run":
        config = load_config(args.config)
        build_plan(config)
        if config.source.kind != "webex":
            raise SystemExit(f"export-dry-run currently supports webex sources, got {config.source.kind!r}")
        source = webex_source_from_config(config.source)
        result = _run_cli_action(
            lambda: export_dry_run(
                job_id=args.job_id,
                archive=Archive(config.workspace / "archive"),
                job_store=_job_store(config.workspace, args.job_id),
                source=source,
                source_kind=config.source.kind,
                target_kind=config.target.kind,
                name=config.name,
                reset_archive=True,
            )
        )
        print(f"Exported conversations: {result.conversations}")
        print(f"Exported participants: {result.participants}")
        print(f"Exported memberships: {result.memberships}")
        print(f"Exported messages: {result.messages}")
        print(f"Exported attachments: {result.attachments}")
        print(f"Workspace: {config.workspace}")
        return

    if args.command == "telegram-package":
        config = load_config(args.config)
        build_plan(config)
        if config.target.kind != "telegram":
            raise SystemExit(
                f"telegram-package requires telegram target, got {config.target.kind!r}"
            )
        output = args.output or (config.workspace / "telegram-package")
        result = _run_cli_action(
            lambda: write_telegram_staging_package(
                archive=Archive(config.workspace / "archive"),
                output_root=output,
            )
        )
        print(f"Package: {result.package_root}")
        print(f"Conversations: {result.conversations}")
        print(f"Messages: {result.messages}")
        return

    if args.command == "telegram-verify":
        config = load_config(args.config)
        build_plan(config)
        if config.target.kind != "telegram":
            raise SystemExit(
                f"telegram-verify requires telegram target, got {config.target.kind!r}"
            )
        package_root = args.package or (config.workspace / "telegram-package")
        result = _run_cli_action(
            lambda: verify_telegram_staging_package(
                archive=Archive(config.workspace / "archive"),
                package_root=package_root,
            )
        )
        print(f"Verification: {'OK' if result.ok else 'FAILED'}")
        print(f"Report: {result.report_path}")
        print(f"Conversations: {result.conversations_found}/{result.conversations_expected}")
        print(f"Messages: {result.messages_found}/{result.messages_expected}")
        if result.issues:
            print("Issues:")
            for issue in result.issues:
                print(f"  - {issue}")
            raise SystemExit(1)
        return

    if args.command == "telegram-destination-map-template":
        config = load_config(args.config)
        build_plan(config)
        if config.target.kind != "telegram":
            raise SystemExit(
                "telegram-destination-map-template requires telegram target, "
                f"got {config.target.kind!r}"
            )
        output = args.output or (
            config.workspace / "archive" / "mappings" / "telegram-destination-map.json"
        )
        result = _run_cli_action(
            lambda: write_telegram_destination_map_template(
                archive=Archive(config.workspace / "archive"),
                output_path=output,
                overwrite=args.overwrite,
            )
        )
        print(f"Destination map template: {result.path}")
        print(f"Conversations: {result.conversations}")
        return

    if args.command == "telegram-import-plan":
        config = load_config(args.config)
        build_plan(config)
        if config.target.kind != "telegram":
            raise SystemExit(
                f"telegram-import-plan requires telegram target, got {config.target.kind!r}"
            )
        package_root = args.package or (config.workspace / "telegram-package")
        destination_map = _load_destination_map(args.destination_map)
        result = _run_cli_action(
            lambda: write_telegram_import_plan(
                archive=Archive(config.workspace / "archive"),
                package_root=package_root,
                destination_map=destination_map,
            )
        )
        print(f"Import plan: {result.plan_path}")
        print(f"Ready: {'yes' if result.ready else 'no'}")
        print(f"Conversations: {result.conversations}")
        print(f"Messages: {result.messages}")
        print(f"Media: {result.media}")
        if result.issues:
            print("Issues:")
            for issue in result.issues:
                print(f"  - {issue}")
            raise SystemExit(1)
        return

    if args.command == "telegram-execute-plan":
        config = load_config(args.config)
        build_plan(config)
        if config.target.kind != "telegram":
            raise SystemExit(
                f"telegram-execute-plan requires telegram target, got {config.target.kind!r}"
            )
        plan_path = args.plan or (config.workspace / "telegram-package" / "import-plan.json")
        adapter = (
            SubprocessTelegramAdapter(command=shlex.split(args.adapter_command))
            if args.adapter_command
            else None
        )
        result = _run_cli_action(
            lambda: execute_import_plan(
                plan_path=plan_path,
                job_store=_job_store(config.workspace, args.job_id),
                job_id=args.job_id,
                adapter=adapter,
            )
        )
        print(f"Execution: {'OK' if result.ok else 'FAILED'}")
        print(f"Operations: {result.operations_completed}/{result.operations_total}")
        if result.issues:
            print("Issues:")
            for issue in result.issues:
                print(f"  - {issue}")
            raise SystemExit(1)
        return

    if args.command == "telegram-dry-run-workflow":
        config = load_config(args.config)
        build_plan(config)
        if config.target.kind != "telegram":
            raise SystemExit(
                f"telegram-dry-run-workflow requires telegram target, got {config.target.kind!r}"
            )
        package_root = args.package or (config.workspace / "telegram-package")
        result = _run_cli_action(
            lambda: run_telegram_dry_run_workflow(
                archive=Archive(config.workspace / "archive"),
                package_root=package_root,
                destination_map=_load_destination_map(args.destination_map),
                job_store=_job_store(config.workspace, args.job_id),
                job_id=args.job_id,
            )
        )
        print(f"Workflow: {'OK' if result.ok else 'FAILED'}")
        print(f"Package: {result.package.package_root}")
        print(f"Verification report: {result.verification.report_path}")
        print(f"Import plan: {result.import_plan.plan_path}")
        if result.execution is not None:
            print(
                "Operations: "
                f"{result.execution.operations_completed}/{result.execution.operations_total}"
            )
        issues = (
            list(result.verification.issues)
            + list(result.import_plan.issues)
            + (list(result.execution.issues) if result.execution is not None else [])
        )
        if issues:
            print("Issues:")
            for issue in issues:
                print(f"  - {issue}")
            raise SystemExit(1)
        return

    if args.command == "webex-telegram-dry-run":
        config = load_config(args.config)
        build_plan(config)
        if config.source.kind != "webex" or config.target.kind != "telegram":
            raise SystemExit(
                "webex-telegram-dry-run requires source.kind=webex and target.kind=telegram"
            )
        package_root = args.package or (config.workspace / "telegram-package")
        result = _run_cli_action(
            lambda: run_webex_to_telegram_dry_run_workflow(
                source=webex_source_from_config(config.source),
                archive=Archive(config.workspace / "archive"),
                package_root=package_root,
                destination_map=_load_destination_map(args.destination_map),
                export_job_store=_job_store(config.workspace, f"{args.job_id}-export"),
                telegram_job_store=_job_store(config.workspace, f"{args.job_id}-telegram"),
                export_job_id=f"{args.job_id}-export",
                telegram_job_id=f"{args.job_id}-telegram",
                name=config.name,
            )
        )
        print(f"Workflow: {'OK' if result.ok else 'FAILED'}")
        print(f"Exported conversations: {result.export.conversations}")
        print(f"Exported participants: {result.export.participants}")
        print(f"Exported memberships: {result.export.memberships}")
        print(f"Exported messages: {result.export.messages}")
        print(f"Exported attachments: {result.export.attachments}")
        print(f"Package: {result.telegram.package.package_root}")
        print(f"Verification report: {result.telegram.verification.report_path}")
        print(f"Import plan: {result.telegram.import_plan.plan_path}")
        if result.telegram.execution is not None:
            print(
                "Operations: "
                f"{result.telegram.execution.operations_completed}/"
                f"{result.telegram.execution.operations_total}"
            )
        issues = (
            list(result.telegram.verification.issues)
            + list(result.telegram.import_plan.issues)
            + (
                list(result.telegram.execution.issues)
                if result.telegram.execution is not None
                else []
            )
        )
        if issues:
            print("Issues:")
            for issue in issues:
                print(f"  - {issue}")
            raise SystemExit(1)
        return

    if args.command == "teams-identity-map-template":
        config = load_config(args.config)
        build_plan(config)
        if config.target.kind != "teams":
            raise SystemExit(
                f"teams-identity-map-template requires teams target, got {config.target.kind!r}"
            )
        archive = Archive(config.workspace / "archive")
        existing_identity_map = _load_teams_identity_map_for_cli(args.prefill) if args.prefill else {}
        identity_map_reasons: dict[str, str] = {}
        if args.entra_users:
            entra_identity_map, identity_map_reasons = _load_entra_identity_prefill_for_cli(
                archive=archive,
                path=args.entra_users,
            )
            entra_identity_map.update(existing_identity_map)
            for source_user_id in existing_identity_map:
                identity_map_reasons[source_user_id] = "Pre-filled from existing identity map."
            existing_identity_map = entra_identity_map
        output = args.output or (config.workspace / "archive" / "mappings" / "teams-identity-map.json")
        result = _run_cli_action(
            lambda: write_teams_identity_map_template(
                archive=archive,
                output_path=output,
                existing_identity_map=existing_identity_map or None,
                identity_map_reasons=identity_map_reasons,
                overwrite=args.overwrite,
            )
        )
        print(f"Teams identity map template: {result.path}")
        print(f"Identities: {result.identities}")
        return

    if args.command == "teams-conversation-map-template":
        config = load_config(args.config)
        build_plan(config)
        if config.target.kind != "teams":
            raise SystemExit(
                f"teams-conversation-map-template requires teams target, got {config.target.kind!r}"
            )
        identity_map = _load_teams_identity_map_for_cli(args.identity_map)
        output = args.output or (config.workspace / "archive" / "mappings" / "teams-conversation-map.json")
        result = _run_cli_action(
            lambda: write_teams_mapping_template(
                archive=Archive(config.workspace / "archive"),
                identity_map=identity_map,
                output_path=output,
                overwrite=args.overwrite,
                group_chat_member_limit=args.group_chat_member_limit,
            )
        )
        print(f"Teams conversation map template: {result.path}")
        print(f"Conversations: {result.conversations}")
        print(f"Review required: {result.review_required}")
        return

    if args.command == "teams-import-plan":
        config = load_config(args.config)
        build_plan(config)
        if config.target.kind != "teams":
            raise SystemExit(
                f"teams-import-plan requires teams target, got {config.target.kind!r}"
            )
        identity_map = _load_teams_identity_map_for_cli(args.identity_map)
        conversation_map = _load_teams_conversation_map_for_cli(args.conversation_map)
        output = args.output or (config.workspace / "archive" / "plans" / "teams-import-plan.json")
        result = _run_cli_action(
            lambda: write_teams_import_plan(
                archive=Archive(config.workspace / "archive"),
                conversation_map=conversation_map,
                identity_map=identity_map,
                output_path=output,
                overwrite=args.overwrite,
            )
        )
        print(f"Teams import plan: {result.path}")
        print(f"Conversations: {result.conversations}")
        print(f"Messages: {result.messages}")
        print(f"Attachments: {result.attachments}")
        print(f"Unsupported attachments: {result.unsupported_attachments}")
        print(f"Timestamp adjustments: {result.timestamp_adjustments}")
        return

    if args.command == "teams-execute-plan":
        config = load_config(args.config)
        build_plan(config)
        if config.target.kind != "teams":
            raise SystemExit(
                f"teams-execute-plan requires teams target, got {config.target.kind!r}"
            )
        plan_path = args.plan or (config.workspace / "archive" / "plans" / "teams-import-plan.json")
        message_map_path = args.message_map or (
            config.workspace / "archive" / "mappings" / "teams-message-map.json"
        )
        adapter = _run_cli_action(lambda: _teams_adapter_from_config(config))
        adapter_name = type(adapter).__name__
        print(f"Adapter: {adapter_name}")
        result = _run_cli_action(
            lambda _a=adapter: execute_teams_import_plan(
                plan_path=plan_path,
                message_map_path=message_map_path,
                job_store=_job_store(config.workspace, args.job_id),
                job_id=args.job_id,
                adapter=_a,
            )
        )
        print(f"Execution: {'OK' if result.ok else 'FAILED'}")
        print(f"Message map: {result.message_map_path}")
        print(f"Messages: {result.messages_imported}/{result.messages_total}")
        print(f"Skipped: {result.messages_skipped}")
        if result.issues:
            print("Issues:")
            for issue in result.issues:
                print(f"  - {issue}")
            raise SystemExit(1)
        return

    if args.command == "teams-verify-import":
        config = load_config(args.config)
        build_plan(config)
        if config.target.kind != "teams":
            raise SystemExit(
                f"teams-verify-import requires teams target, got {config.target.kind!r}"
            )
        plan_path = args.plan or (config.workspace / "archive" / "plans" / "teams-import-plan.json")
        message_map_path = args.message_map or (
            config.workspace / "archive" / "mappings" / "teams-message-map.json"
        )
        report_path = args.report or (
            config.workspace / "archive" / "reports" / "teams-import-verification.json"
        )
        result = _run_cli_action(
            lambda: verify_teams_import(
                plan_path=plan_path,
                message_map_path=message_map_path,
                report_path=report_path,
            )
        )
        print(f"Verification: {'OK' if result.ok else 'FAILED'}")
        print(f"Report: {result.report_path}")
        print(f"Messages: {result.messages_mapped}/{result.messages_expected}")
        print(f"Extra mappings: {result.extra_mappings}")
        print(f"Unsupported attachments: {result.unsupported_attachments}")
        if result.issues:
            print("Issues:")
            for issue in result.issues:
                print(f"  - {issue}")
            raise SystemExit(1)
        return

    if args.command == "teams-dry-run-workflow":
        config = load_config(args.config)
        build_plan(config)
        if config.target.kind != "teams":
            raise SystemExit(
                f"teams-dry-run-workflow requires teams target, got {config.target.kind!r}"
            )
        identity_map = _load_teams_identity_map_for_cli(args.identity_map)
        conversation_map = _load_teams_conversation_map_for_cli(args.conversation_map)
        plan_path = args.plan or (config.workspace / "archive" / "plans" / "teams-import-plan.json")
        message_map_path = args.message_map or (
            config.workspace / "archive" / "mappings" / "teams-message-map.json"
        )
        report_path = args.report or (
            config.workspace / "archive" / "reports" / "teams-import-verification.json"
        )
        adapter = _run_cli_action(lambda: _teams_adapter_from_config(config))
        print(f"Adapter: {type(adapter).__name__}")
        result = _run_cli_action(
            lambda _a=adapter: run_teams_dry_run_workflow(
                archive=Archive(config.workspace / "archive"),
                conversation_map=conversation_map,
                identity_map=identity_map,
                import_plan_path=plan_path,
                message_map_path=message_map_path,
                verification_report_path=report_path,
                job_store=_job_store(config.workspace, args.job_id),
                job_id=args.job_id,
                overwrite_import_plan=args.overwrite_plan,
                adapter=_a,
            )
        )
        print(f"Workflow: {'OK' if result.ok else 'FAILED'}")
        print(f"Import plan: {result.import_plan.path}")
        print(f"Unsupported attachments: {result.import_plan.unsupported_attachments}")
        if result.execution is not None:
            print(f"Message map: {result.execution.message_map_path}")
            print(f"Messages: {result.execution.messages_imported}/{result.execution.messages_total}")
            print(f"Skipped: {result.execution.messages_skipped}")
        if result.verification is not None:
            print(f"Verification report: {result.verification.report_path}")
            print(f"Verified: {result.verification.messages_mapped}/{result.verification.messages_expected}")
            print(f"Unsupported attachments: {result.verification.unsupported_attachments}")
        issues = (
            (list(result.execution.issues) if result.execution is not None else [])
            + (list(result.verification.issues) if result.verification is not None else [])
        )
        if issues:
            print("Issues:")
            for issue in issues:
                print(f"  - {issue}")
            raise SystemExit(1)
        return

    if args.command == "webex-teams-dry-run":
        config = load_config(args.config)
        build_plan(config)
        if config.source.kind != "webex" or config.target.kind != "teams":
            raise SystemExit(
                "webex-teams-dry-run requires source.kind=webex and target.kind=teams"
            )
        identity_map = _load_teams_identity_map_for_cli(args.identity_map)
        conversation_map = _load_teams_conversation_map_for_cli(args.conversation_map)
        plan_path = args.plan or (config.workspace / "archive" / "plans" / "teams-import-plan.json")
        message_map_path = args.message_map or (
            config.workspace / "archive" / "mappings" / "teams-message-map.json"
        )
        report_path = args.report or (
            config.workspace / "archive" / "reports" / "teams-import-verification.json"
        )
        adapter = _run_cli_action(lambda: _teams_adapter_from_config(config))
        print(f"Adapter: {type(adapter).__name__}")
        result = _run_cli_action(
            lambda _a=adapter: run_webex_to_teams_dry_run_workflow(
                source=webex_source_from_config(config.source),
                archive=Archive(config.workspace / "archive"),
                conversation_map=conversation_map,
                identity_map=identity_map,
                import_plan_path=plan_path,
                message_map_path=message_map_path,
                verification_report_path=report_path,
                export_job_store=_job_store(config.workspace, f"{args.job_id}-export"),
                teams_job_store=_job_store(config.workspace, f"{args.job_id}-teams"),
                export_job_id=f"{args.job_id}-export",
                teams_job_id=f"{args.job_id}-teams",
                name=config.name,
                overwrite_import_plan=args.overwrite_plan,
                adapter=_a,
            )
        )
        print(f"Workflow: {'OK' if result.ok else 'FAILED'}")
        print(f"Exported conversations: {result.export.conversations}")
        print(f"Exported participants: {result.export.participants}")
        print(f"Exported memberships: {result.export.memberships}")
        print(f"Exported messages: {result.export.messages}")
        print(f"Exported attachments: {result.export.attachments}")
        print(f"Import plan: {result.teams.import_plan.path}")
        print(f"Unsupported attachments: {result.teams.import_plan.unsupported_attachments}")
        if result.teams.execution is not None:
            print(f"Message map: {result.teams.execution.message_map_path}")
            print(
                "Messages: "
                f"{result.teams.execution.messages_imported}/"
                f"{result.teams.execution.messages_total}"
            )
            print(f"Skipped: {result.teams.execution.messages_skipped}")
        if result.teams.verification is not None:
            print(f"Verification report: {result.teams.verification.report_path}")
            print(
                "Verified: "
                f"{result.teams.verification.messages_mapped}/"
                f"{result.teams.verification.messages_expected}"
            )
            print(f"Unsupported attachments: {result.teams.verification.unsupported_attachments}")
        issues = (
            (list(result.teams.execution.issues) if result.teams.execution is not None else [])
            + (list(result.teams.verification.issues) if result.teams.verification is not None else [])
        )
        if issues:
            print("Issues:")
            for issue in issues:
                print(f"  - {issue}")
            raise SystemExit(1)
        return

    parser.print_help(sys.stderr)
    raise SystemExit(2)


def _load_destination_map(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    if not path.exists():
        raise SystemExit(f"Destination map does not exist: {path}")
    if not path.is_file():
        raise SystemExit(f"Destination map must be a file: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except UnicodeDecodeError as exc:
        raise SystemExit(f"Destination map is not valid UTF-8: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Destination map is not valid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise SystemExit("Destination map must be a JSON object")
    destination_map: dict[str, str] = {}
    for key, value in payload.items():
        if not isinstance(key, str) or not key.strip():
            raise SystemExit("Destination map keys must be non-empty strings")
        source_conversation_id = key.strip()
        if source_conversation_id in destination_map:
            raise SystemExit(f"Destination map duplicates conversation id: {source_conversation_id}")
        if isinstance(value, str):
            peer = value.strip()
        elif isinstance(value, dict):
            raw_peer = value.get("peer")
            peer = raw_peer.strip() if isinstance(raw_peer, str) else ""
        else:
            raise SystemExit("Destination map values must be strings or objects with a peer field")
        if not peer:
            raise SystemExit("Destination map entries must include non-empty Telegram peers")
        destination_map[source_conversation_id] = peer
    return destination_map


def _teams_adapter_from_config(config: MigrationConfig) -> TeamsMessageAdapter:
    """Build GraphTeamsAdapter if Graph credentials are present in config; else dry-run."""
    settings = config.target.settings
    raw_tenant = settings.get("tenant_id")
    raw_client = settings.get("client_id")
    raw_secret = settings.get("client_secret")
    has_tenant = isinstance(raw_tenant, str) and raw_tenant.strip()
    has_client = isinstance(raw_client, str) and raw_client.strip()
    has_secret = isinstance(raw_secret, str) and raw_secret.strip()
    if has_tenant or has_client or has_secret:
        missing = [name for name, present in [
            ("tenant_id", has_tenant),
            ("client_id", has_client),
            ("client_secret", has_secret),
        ] if not present]
        if missing:
            raise ValueError(
                f"Partial Graph credentials in [target]: missing {', '.join(missing)}."
                f" Provide all three (tenant_id, client_id, client_secret) or none."
            )
        from .targets.graph_teams_adapter import GraphTeamsAdapter
        tenant_id = resolve_secret(raw_tenant, field_name="target.tenant_id").reveal()  # type: ignore[arg-type]
        client_id = resolve_secret(raw_client, field_name="target.client_id").reveal()  # type: ignore[arg-type]
        client_secret = resolve_secret(raw_secret, field_name="target.client_secret").reveal()  # type: ignore[arg-type]
        return GraphTeamsAdapter(
            tenant_id=tenant_id,
            client_id=client_id,
            client_secret=client_secret,
        )
    return DryRunTeamsAdapter()


def _run_cli_action(action: Callable[[], T]) -> T:
    try:
        return action()
    except (FileExistsError, ValueError, SecretResolutionError) as exc:
        raise SystemExit(str(exc)) from exc


def _job_store(workspace: Path, job_id: str) -> JobStore:
    try:
        safe_job_id = validate_job_id(job_id)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    return JobStore(workspace / "jobs" / safe_job_id)


def _load_teams_identity_map_for_cli(path: Path) -> dict[str, str]:
    if not path.exists():
        raise SystemExit(f"Teams identity map does not exist: {path}")
    if not path.is_file():
        raise SystemExit(f"Teams identity map must be a file: {path}")
    try:
        return load_teams_identity_map(path)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc


def _load_teams_conversation_map_for_cli(path: Path):
    if not path.exists():
        raise SystemExit(f"Teams conversation map does not exist: {path}")
    if not path.is_file():
        raise SystemExit(f"Teams conversation map must be a file: {path}")
    try:
        return load_teams_conversation_map(path)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc


def _load_entra_identity_prefill_for_cli(
    *,
    archive: Archive,
    path: Path,
) -> tuple[dict[str, str], dict[str, str]]:
    if not path.exists():
        raise SystemExit(f"Entra users export does not exist: {path}")
    if not path.is_file():
        raise SystemExit(f"Entra users export must be a file: {path}")
    try:
        return build_teams_identity_prefill_from_entra(
            archive=archive,
            entra_users=load_entra_users(path),
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
