"""Worker heartbeat and role-aware Docker healthcheck (no HTTP server in the worker)."""
import json
import os
import sys
import threading
import time
import urllib.request
from pathlib import Path

from app.database import DATA_DIR


class Heartbeat:
    def __init__(self):
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.values = {"pid": os.getpid(), "phase": "STARTING", "job_id": None}
        self.thread = threading.Thread(target=self._run, daemon=True)

    def update(self, **values):
        with self.lock:
            self.values.update(values)

    def _run(self):
        while not self.stop.is_set():
            try:
                with self.lock:
                    payload = {**self.values, "heartbeat_at": time.time()}
                temporary = DATA_DIR / f".worker-health-{os.getpid()}.tmp"
                temporary.write_text(json.dumps(payload), encoding="utf-8")
                temporary.replace(DATA_DIR / "worker-health.json")
            except OSError as exc:
                print(f"Cannot write worker heartbeat: {exc}", flush=True)
            self.stop.wait(5)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stop.set()
        self.thread.join(timeout=6)


def worker_status(*, verify_process: bool = False) -> dict:
    try:
        values = json.loads((DATA_DIR / "worker-health.json").read_text(encoding="utf-8"))
        if not isinstance(values, dict) or time.time() - float(values["heartbeat_at"]) > 30:
            raise ValueError("Worker heartbeat is missing or stale")
        pid = int(values["pid"])
        if pid <= 0:
            raise ValueError("Invalid worker PID")
        if verify_process:
            os.kill(pid, 0)
        if values.get("phase") == "VALIDATING":
            if time.time() > values.get("deadline", float("inf")) or time.time() - values.get("last_progress", time.time()) > values.get("stall_timeout", 1800):
                return {**values, "status": "blocked"}
        return {**values, "status": "ok"}
    except (OSError, ValueError, KeyError, TypeError):
        return {"status": "unavailable"}


def is_worker_container() -> bool:
    role = os.getenv("SONIC_PROCESS_ROLE", "auto")
    if role != "auto":
        return role == "worker"
    # Also supports existing deployments that only override the image command.
    for path in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            arguments = path.read_bytes().split(b"\0")
            if b"app.worker" in arguments:
                return True
        except OSError:
            continue
    return False


def main():
    if is_worker_container():
        result = worker_status(verify_process=True)
        print(json.dumps(result))
        return 0 if result["status"] == "ok" else 1
    try:
        with urllib.request.urlopen("http://127.0.0.1:8090/health", timeout=3) as response:
            return 0 if response.status == 200 else 1
    except OSError:
        return 1


if __name__ == "__main__":
    sys.exit(main())
