import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

UPLOAD_ROOT = Path(
    os.getenv(
        "LOSSLESS_UPLOAD_ROOT",
        str(PROJECT_ROOT / "uploads"),
    )
).resolve()

REPORT_ROOT = Path(
    os.getenv(
        "LOSSLESS_REPORT_ROOT",
        str(PROJECT_ROOT / "reports"),
    )
).resolve()

source_roots_value = os.getenv(
    "LOSSLESS_SOURCE_ROOTS",
    str(PROJECT_ROOT / "sample-input"),
)

SOURCE_ROOTS = tuple(
    Path(value).resolve()
    for value in source_roots_value.split(os.pathsep)
    if value.strip()
)

UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
REPORT_ROOT.mkdir(parents=True, exist_ok=True)

for root in SOURCE_ROOTS:
    root.mkdir(parents=True, exist_ok=True)


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def optional_path(name: str) -> Path | None:
    value = os.getenv(name, "").strip()
    return Path(value).resolve() if value else None


FILE_OPERATIONS_ENABLED = env_bool("LOSSLESS_FILE_OPERATIONS", False)
AUTO_ROUTE_RELEASES = env_bool("LOSSLESS_AUTO_ROUTE", False)

movable_roots_value = os.getenv("LOSSLESS_MOVABLE_ROOTS", "")
MOVABLE_ROOTS = tuple(
    Path(value).resolve()
    for value in movable_roots_value.split(os.pathsep)
    if value.strip()
)

STAGING_ROOT = optional_path("LOSSLESS_STAGING_ROOT")
QUARANTINE_ROOT = optional_path("LOSSLESS_QUARANTINE_ROOT")
REJECTED_ROOT = optional_path("LOSSLESS_REJECTED_ROOT")
