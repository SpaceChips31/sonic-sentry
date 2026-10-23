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
        "movable_roots": [str(p) for p in config.MOVABLE_ROOTS],
        "staging": str(config.STAGING_ROOT) if config.STAGING_ROOT else "",
        "quarantine": str(config.QUARANTINE_ROOT) if config.QUARANTINE_ROOT else "",
        "rejected": str(config.REJECTED_ROOT) if config.REJECTED_ROOT else "",
    }
    with SessionLocal() as session:
        saved = session.get(WorkflowSettings, 1)
        if saved:
            defaults.update(json.loads(saved.payload))
    return defaults


def save_workflow_settings(*, enabled: bool, policy: str, movable_roots: str,
                           staging: str, quarantine: str, rejected: str) -> None:
    if policy not in {"manual", "pass", "all"}:
        raise WorkflowConfigurationError("Invalid routing policy")
    if policy != "manual" and not enabled:
        raise WorkflowConfigurationError("Enable file operations before automatic routing.")

    def directory(value: str) -> str:
        value = value.strip()
        if not value:
            return ""
        path = Path(value)
        if not path.is_absolute() or (enabled and not path.is_dir()):
            raise WorkflowConfigurationError(f"Use an existing absolute directory: {value}")
        return str(path.resolve())

    roots = list(dict.fromkeys(directory(line) for line in movable_roots.splitlines() if line.strip()))
    targets = {"staging": directory(staging), "quarantine": directory(quarantine),
               "rejected": directory(rejected)}
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
                with tempfile.TemporaryFile(dir=path):
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
