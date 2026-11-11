import json
from pathlib import Path

APP_NAME = "SonicSentry"
try:
    _build = json.loads(Path(__file__).with_name("build.json").read_text(encoding="utf-8"))
except (OSError, ValueError):
    _build = {}
APP_VERSION = _build.get("version", "0.4.0")
APP_REVISION = _build.get("revision", "source")
