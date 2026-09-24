from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from app.database import SessionLocal
from app.models import ApplicationSetting


@dataclass(frozen=True)
class SettingSpec:
    key: str
    env: str
    kind: str
    default: str


SPECS = {
    spec.key: spec
    for spec in (
        SettingSpec("file_operations", "LOSSLESS_FILE_OPERATIONS", "bool", "false"),
        SettingSpec("auto_route", "LOSSLESS_AUTO_ROUTE", "bool", "false"),
        SettingSpec("movable_roots", "LOSSLESS_MOVABLE_ROOTS", "paths", "/data/downloads/soulseek:/data/music/legacy:/data/music/validator/quarantine:/data/music/validator/rejected"),
        SettingSpec("staging_root", "LOSSLESS_STAGING_ROOT", "path", "/data/music/validator/staging"),
        SettingSpec("quarantine_root", "LOSSLESS_QUARANTINE_ROOT", "path", "/data/music/validator/quarantine"),
        SettingSpec("rejected_root", "LOSSLESS_REJECTED_ROOT", "path", "/data/music/validator/rejected"),
        SettingSpec("auth_enabled", "SONIC_SENTRY_AUTH_ENABLED", "bool", "false"),
        SettingSpec("worker_concurrency", "SONIC_SENTRY_WORKER_CONCURRENCY", "int", "1"),
    )
}


class SettingError(ValueError):
    pass


def is_locked(key: str) -> bool:
    return SPECS[key].env in os.environ


def stored_value(key: str) -> str | None:
    with SessionLocal() as session:
        setting = session.get(ApplicationSetting, key)
        return setting.value if setting else None


def raw_value(key: str) -> str:
    spec = SPECS[key]
    if is_locked(key):
        return os.environ.get(spec.env, "")
    return stored_value(key) if stored_value(key) is not None else spec.default


def bool_value(key: str) -> bool:
    return raw_value(key).strip().lower() in {"1", "true", "yes", "on"}


def path_value(key: str) -> Path | None:
    value = raw_value(key).strip()
    return Path(value).resolve() if value else None


def int_value(key: str) -> int:
    return int(raw_value(key))


def paths_value(key: str) -> tuple[Path, ...]:
    return tuple(
        Path(item).resolve()
        for item in raw_value(key).split(os.pathsep)
        if item.strip()
    )


def normalize_value(spec: SettingSpec, value: str) -> str:
    clean = value.strip()
    if spec.kind == "bool":
        if clean.lower() not in {"1", "0", "true", "false", "yes", "no", "on", "off"}:
            raise SettingError("Invalid boolean value")
        return "true" if clean.lower() in {"1", "true", "yes", "on"} else "false"
    if spec.kind == "path":
        return str(Path(clean).resolve()) if clean else ""
    if spec.kind == "int":
        try:
            number = int(clean)
        except ValueError as exc:
            raise SettingError("Invalid integer value") from exc
        if number < 1 or number > 8:
            raise SettingError("Value must be between 1 and 8")
        return str(number)
    if spec.kind == "paths":
        return os.pathsep.join(
            str(Path(item).resolve())
            for item in clean.split(os.pathsep)
            if item.strip()
        )
    return clean


def set_value(key: str, value: str) -> None:
    if key not in SPECS:
        raise SettingError("Unknown setting")
    if is_locked(key):
        raise SettingError(f"{SPECS[key].env} is fixed by the environment")
    normalized = normalize_value(SPECS[key], value)
    with SessionLocal() as session:
        setting = session.get(ApplicationSetting, key)
        if setting is None:
            setting = ApplicationSetting(key=key, value=normalized)
            session.add(setting)
        else:
            setting.value = normalized
        session.commit()


def describe(key: str) -> dict:
    spec = SPECS[key]
    value = raw_value(key)
    return {
        "key": key,
        "env": spec.env,
        "kind": spec.kind,
        "value": value,
        "locked": is_locked(key),
        "source": "environment" if is_locked(key) else ("database" if stored_value(key) is not None else "default"),
    }
