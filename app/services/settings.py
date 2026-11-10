"""Persistent settings shared by the web application and the worker."""
import json
from pathlib import Path

from app import config
from app.database import SessionLocal
from app.models import WorkflowSettings


class WorkflowConfigurationError(ValueError):
    pass


def workflow_settings() -> dict:
    defaults = {
        "enabled": config.FILE_OPERATIONS_ENABLED,
        "auto_route": config.AUTO_ROUTE_RELEASES,
        "route_nonpass": True,
        "scan_interval": 60,
        "settle_seconds": 300,
        "analysis_timeout": 7200,
        "stall_timeout": 1800,
        "retry_limit": 1,
        "report_days": 30,
        "upload_days": 7,
        "movable_roots": [str(p) for p in config.MOVABLE_ROOTS],
        "staging": str(config.STAGING_ROOT) if config.STAGING_ROOT else "",
        "quarantine": str(config.QUARANTINE_ROOT) if config.QUARANTINE_ROOT else "",
        "rejected": str(config.REJECTED_ROOT) if config.REJECTED_ROOT else "",
        "trash": str(config.TRASH_ROOT) if config.TRASH_ROOT else "",
    }
    with SessionLocal() as session:
        saved = session.get(WorkflowSettings, 1)
        if saved:
            defaults.update(json.loads(saved.payload))
    if not defaults["trash"] and defaults["quarantine"]:
        defaults["trash"] = str(Path(defaults["quarantine"]).parent / "trash")
    defaults["configuration_origin"] = "database" if saved else "environment"
    defaults["locked"] = config.FILE_OPERATIONS_LOCKED
    if config.FILE_OPERATIONS_LOCKED:
        defaults["enabled"] = defaults["auto_route"] = False
    return defaults


def save_workflow_settings(*, enabled: bool, policy: str, movable_roots: str,
                           staging: str, quarantine: str, rejected: str, trash: str | None = None) -> None:
    if enabled and config.FILE_OPERATIONS_LOCKED:
        raise WorkflowConfigurationError("File operations are locked by the deployment.")
    if policy not in {"manual", "pass", "all"}:
        raise WorkflowConfigurationError("Invalid routing policy")
    if policy != "manual" and not enabled:
        raise WorkflowConfigurationError("Enable file operations before automatic routing.")

    def directory(value: str, *, allow_new: bool = False) -> str:
        value = value.strip()
        if not value:
            return ""
        path = Path(value)
        if not path.is_absolute() or (enabled and not path.is_dir() and not (allow_new and path.parent.is_dir())):
            raise WorkflowConfigurationError(f"Use an existing absolute directory: {value}")
        return str(path.resolve())

    roots = list(dict.fromkeys(directory(line) for line in movable_roots.splitlines() if line.strip()))
    targets = {"staging": directory(staging), "quarantine": directory(quarantine),
               "rejected": directory(rejected)}
    trash_value = trash if trash is not None else workflow_settings()["trash"] or (str(Path(quarantine).parent / "trash") if quarantine else "")
    targets["trash"] = directory(trash_value, allow_new=True)
    configured = [Path(p) for p in targets.values() if p]
    for i, path in enumerate(configured):
        if any(path.is_relative_to(other) or other.is_relative_to(path)
               for other in configured[i + 1:]):
            raise WorkflowConfigurationError("Destination folders must be separate and must not contain each other.")
        if any(Path(root) != path and Path(root).is_relative_to(path) for root in roots):
            raise WorkflowConfigurationError("A destination cannot contain an authorized source folder.")
    if enabled and not roots:
        raise WorkflowConfigurationError("Add at least one authorized source folder.")
    required = ("staging", "quarantine", "rejected") if policy == "all" else ("staging",) if policy == "pass" else ()
    if any(not targets[key] for key in required):
        raise WorkflowConfigurationError("Configure every destination required by the routing policy.")
    if enabled:
        import tempfile
        for path in roots + list(filter(None, targets.values())):
            try:
                with tempfile.TemporaryFile(dir=path if Path(path).is_dir() else Path(path).parent):
                    pass
            except OSError as exc:
                raise WorkflowConfigurationError(f"Directory is not writable: {path}") from exc
    payload = dict(enabled=enabled, auto_route=policy != "manual", route_nonpass=policy == "all",
                   movable_roots=roots, **targets)
    with SessionLocal() as session:
        from app.services.file_workflow import lock_file_operations
        lock_file_operations(session)
        saved = session.get(WorkflowSettings, 1)
        if saved is None:
            saved = WorkflowSettings(id=1, payload="{}")
            session.add(saved)
        saved.payload = json.dumps({**json.loads(saved.payload), **payload})
        session.commit()


def save_operational_settings(analysis_timeout: int, stall_timeout: int, retry_limit: int,
                              report_days: int, upload_days: int) -> None:
    if not 30 <= analysis_timeout <= 604800 or not 30 <= stall_timeout <= analysis_timeout:
        raise WorkflowConfigurationError("Timeouts must be at least 30 seconds; stall timeout cannot exceed total timeout.")
    if not 0 <= retry_limit <= 3 or not 1 <= report_days <= 3650 or not 1 <= upload_days <= 3650:
        raise WorkflowConfigurationError("Use 0–3 retries and 1–3650 retention days.")
    with SessionLocal() as session:
        from app.services.file_workflow import lock_file_operations
        lock_file_operations(session)
        saved = session.get(WorkflowSettings, 1)
        if saved is None:
            saved = WorkflowSettings(id=1, payload="{}")
            session.add(saved)
        saved.payload = json.dumps({**json.loads(saved.payload), "analysis_timeout": analysis_timeout,
            "stall_timeout": stall_timeout, "retry_limit": retry_limit, "report_days": report_days, "upload_days": upload_days})
        session.commit()


def save_ingestion_settings(scan_interval: int, settle_seconds: int) -> None:
    if not 10 <= scan_interval <= 86400 or not 30 <= settle_seconds <= 86400:
        raise WorkflowConfigurationError("Scan interval must be 10–86400 seconds; stability wait must be 30–86400 seconds.")
    with SessionLocal() as session:
        from app.services.file_workflow import lock_file_operations
        lock_file_operations(session)
        saved = session.get(WorkflowSettings, 1)
        if saved is None:
            saved = WorkflowSettings(id=1, payload="{}")
            session.add(saved)
        saved.payload = json.dumps({**json.loads(saved.payload), "scan_interval": scan_interval, "settle_seconds": settle_seconds})
        session.commit()
