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
