from __future__ import annotations

import json
import mimetypes
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SUPPORTED_METHODS = frozenset(
    {
        "messages.checkHistoryImport",
        "messages.checkHistoryImportPeer",
        "messages.initHistoryImport",
        "messages.uploadImportedMedia",
        "messages.startHistoryImport",
    }
)


class MtprotoRunnerError(RuntimeError):
    pass


@dataclass(frozen=True)
class RunnerConfig:
    api_id: str
    api_hash: str
    session: str
    live_enabled: bool


def main(argv: list[str] | None = None) -> None:
    del argv
    try:
        operation = _read_operation(sys.stdin.read())
        result = execute_operation(operation, _config_from_env())
    except Exception as exc:  # noqa: BLE001 - subprocess boundary reports concise failure.
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc
    print(json.dumps(result, sort_keys=True))


def execute_operation(
    operation: dict[str, Any],
    config: RunnerConfig,
    *,
    importer: TelethonHistoryImporter | None = None,
) -> dict[str, Any]:
    method = operation.get("method")
    if not isinstance(method, str) or not method:
        raise MtprotoRunnerError("Operation missing method")
    if method not in SUPPORTED_METHODS:
        raise MtprotoRunnerError(f"Unsupported MTProto operation method: {method}")
    _validate_required_fields(operation, method)
    if not config.live_enabled:
        raise MtprotoRunnerError(
            "Live MTProto execution is disabled; set EXODUS_TELEGRAM_LIVE=1 "
            "after installing a concrete MTProto client implementation"
        )
    _validate_live_file_inputs(operation, method)
    importer = importer or TelethonHistoryImporter.from_installed()
    return importer.execute(operation, config)


def _config_from_env() -> RunnerConfig:
    return RunnerConfig(
        api_id=_required_env("TELEGRAM_API_ID"),
        api_hash=_required_env("TELEGRAM_API_HASH"),
        session=_required_env("TELEGRAM_SESSION"),
        live_enabled=os.environ.get("EXODUS_TELEGRAM_LIVE") == "1",
    )


def _read_operation(raw: str) -> dict[str, Any]:
    if not raw.strip():
        raise MtprotoRunnerError("Operation payload is required on stdin")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise MtprotoRunnerError("Operation payload must be a JSON object")
    return payload


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise MtprotoRunnerError(f"Missing required environment variable: {name}")
    return value


def _validate_required_fields(operation: dict[str, Any], method: str) -> None:
    required = {
        "messages.checkHistoryImport": ("conversation_id", "import_head_path"),
        "messages.checkHistoryImportPeer": ("conversation_id", "peer"),
        "messages.initHistoryImport": ("conversation_id", "peer", "file_path", "media_count"),
        "messages.uploadImportedMedia": (
            "conversation_id",
            "peer",
            "import_id",
            "file_name",
            "file_path",
            "source_attachment_id",
        ),
        "messages.startHistoryImport": ("conversation_id", "peer", "import_id"),
    }[method]
    missing = [field for field in required if operation.get(field) in (None, "")]
    if missing:
        raise MtprotoRunnerError(f"Operation {method} missing required fields: {', '.join(missing)}")
    for field in required:
        if field in {"media_count", "import_id"}:
            continue
        _validate_str_field(operation, field)
    if method == "messages.initHistoryImport":
        _validate_int_field(operation, "media_count", minimum=0)
    if method in {"messages.uploadImportedMedia", "messages.startHistoryImport"}:
        _validate_int_field(operation, "import_id", minimum=1)
    if method == "messages.checkHistoryImport" and "import_head_lines" in operation:
        _validate_int_field(operation, "import_head_lines", minimum=1)


def _validate_str_field(operation: dict[str, Any], field: str) -> None:
    value = operation.get(field)
    if not isinstance(value, str) or not value.strip():
        raise MtprotoRunnerError(f"Operation field {field} must be a non-empty string")


def _validate_int_field(operation: dict[str, Any], field: str, *, minimum: int) -> None:
    value = operation.get(field)
    if not isinstance(value, int) or isinstance(value, bool):
        raise MtprotoRunnerError(f"Operation field {field} must be an integer")
    if value < minimum:
        raise MtprotoRunnerError(f"Operation field {field} must be at least {minimum}")


def _validate_live_file_inputs(operation: dict[str, Any], method: str) -> None:
    if method == "messages.checkHistoryImport":
        _validate_readable_file(Path(str(operation["import_head_path"])), "import_head_path")
    if method in {"messages.initHistoryImport", "messages.uploadImportedMedia"}:
        _validate_readable_file(Path(str(operation["file_path"])), "file_path")


def _validate_readable_file(path: Path, field: str) -> None:
    if not path.exists():
        raise MtprotoRunnerError(f"Operation field {field} file does not exist: {path}")
    if not path.is_file():
        raise MtprotoRunnerError(f"Operation field {field} must reference a file: {path}")


@dataclass(frozen=True)
class TelethonHistoryImporter:
    client_factory: Any
    functions: Any
    types: Any

    @classmethod
    def from_installed(cls) -> TelethonHistoryImporter:
        try:
            from telethon import TelegramClient, functions, types
        except ImportError as exc:
            raise MtprotoRunnerError(
                "Telethon is required for live MTProto execution; install exodus-agent[telegram]"
            ) from exc
        return cls(
            client_factory=TelegramClient,
            functions=functions,
            types=types,
        )

    def execute(self, operation: dict[str, Any], config: RunnerConfig) -> dict[str, Any]:
        api_id = _parse_api_id(config.api_id)
        method = str(operation["method"])
        with self.client_factory(config.session, api_id, config.api_hash) as client:
            if method == "messages.checkHistoryImport":
                import_head = _read_import_head(
                    Path(str(operation["import_head_path"])),
                    int(operation.get("import_head_lines", 100)),
                )
                result = client(
                    self.functions.messages.CheckHistoryImportRequest(
                        import_head=import_head,
                    )
                )
                return _result(method, parsed=_stringify(result))

            if method == "messages.checkHistoryImportPeer":
                result = client(
                    self.functions.messages.CheckHistoryImportPeerRequest(
                        peer=operation["peer"],
                    )
                )
                return _result(method, peer_ok=_stringify(result))

            if method == "messages.initHistoryImport":
                uploaded = client.upload_file(str(operation["file_path"]))
                result = client(
                    self.functions.messages.InitHistoryImportRequest(
                        peer=operation["peer"],
                        file=uploaded,
                        media_count=int(operation["media_count"]),
                    )
                )
                import_id = _extract_import_id(result)
                return _result(method, import_id=import_id)

            if method == "messages.uploadImportedMedia":
                file_path = str(operation["file_path"])
                uploaded = client.upload_file(file_path)
                media = self.types.InputMediaUploadedDocument(
                    file=uploaded,
                    mime_type=_mime_type(operation, file_path),
                    attributes=[
                        self.types.DocumentAttributeFilename(
                            file_name=str(operation["file_name"]),
                        )
                    ],
                )
                result = client(
                    self.functions.messages.UploadImportedMediaRequest(
                        peer=operation["peer"],
                        import_id=int(operation["import_id"]),
                        file_name=str(operation["file_name"]),
                        media=media,
                    )
                )
                return _result(method, uploaded_media=_stringify(result))

            if method == "messages.startHistoryImport":
                result = client(
                    self.functions.messages.StartHistoryImportRequest(
                        peer=operation["peer"],
                        import_id=int(operation["import_id"]),
                    )
                )
                return _result(method, started=bool(result))

        raise MtprotoRunnerError(f"Unsupported MTProto operation method: {method}")


def _parse_api_id(value: str) -> int:
    try:
        return int(value)
    except ValueError as exc:
        raise MtprotoRunnerError("TELEGRAM_API_ID must be an integer") from exc


def _read_import_head(path: Path, lines: int) -> str:
    if lines <= 0:
        raise MtprotoRunnerError("import_head_lines must be greater than zero")
    if not path.exists():
        raise MtprotoRunnerError(f"Import transcript does not exist: {path}")
    return "\n".join(path.read_text(encoding="utf-8").splitlines()[:lines])


def _extract_import_id(result: Any) -> int:
    for field in ("id", "import_id"):
        value = getattr(result, field, None)
        if isinstance(value, int):
            return value
    if isinstance(result, dict):
        value = result.get("id", result.get("import_id"))
        if isinstance(value, int):
            return value
    raise MtprotoRunnerError("MTProto initHistoryImport response did not include import_id")


def _mime_type(operation: dict[str, Any], file_path: str) -> str:
    value = operation.get("mime_type")
    if isinstance(value, str) and value:
        return value
    guessed, _ = mimetypes.guess_type(file_path)
    return guessed or "application/octet-stream"


def _result(method: str, **metadata: object) -> dict[str, object]:
    return {
        "live": True,
        "method": method,
        **metadata,
    }


def _stringify(value: Any) -> str:
    stringify = getattr(value, "stringify", None)
    if callable(stringify):
        return str(stringify())
    return str(value)


if __name__ == "__main__":
    main()
