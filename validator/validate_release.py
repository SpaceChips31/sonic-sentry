#!/usr/bin/env python3

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

AUDIO_EXTENSIONS = {".flac"}


def run_flac_test(path: Path):
    proc = subprocess.run(
        ["flac", "-t", "--silent", str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "error": proc.stderr.strip() if proc.returncode else None,
    }


def run_forensics(path: Path):
    proc = subprocess.run(
        [
            "python",
            "/opt/audio-forensic/audio_forensic.py",
            "--json",
            str(path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    if proc.returncode != 0:
        return {
            "ok": False,
            "error": proc.stderr.strip() or f"exit code {proc.returncode}",
        }

    try:
        payload = json.loads(proc.stdout)

        if not payload:
            raise ValueError("empty result")

        return {
            "ok": True,
            "result": payload[0],
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": f"invalid JSON: {exc}",
        }


def classify(forensic):
    if not forensic["ok"]:
        return "QUARANTINE"

    spectral = (
        forensic["result"]
        .get("authenticity", {})
        .get("spectral", {})
    )

    if spectral.get("verdict_label", "") == "GENUINE":
        return "PASS"

    return "QUARANTINE"


def print_summary(report, report_path=None):
    print()
    print(f"Release: {report['release']}")
    print()

    release = Path(report["release"])

    for item in report["files"]:
        try:
            display_path = Path(item["path"]).relative_to(release)
        except ValueError:
            display_path = Path(item["path"]).name

        label = f"[{item['status']}]"
        print(f"{label:<14} {display_path}")

    summary = report["summary"]

    print()
    print(f"Result: {report['status']}")
    print(
        f"{summary['total']} tracks | "
        f"{summary['pass']} pass | "
        f"{summary['quarantine']} quarantine | "
        f"{summary['rejected']} rejected"
    )

    if report_path is not None:
        print(f"Full report: {report_path}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("release", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--json-stdout", action="store_true")
    args = parser.parse_args()

    release = args.release.resolve()

    if not release.is_dir():
        print(f"Release directory not found: {release}", file=sys.stderr)
        return 2

    files = sorted(
        path for path in release.rglob("*")
        if path.is_file()
        and path.suffix.lower() in AUDIO_EXTENSIONS
    )

    if not files:
        print("No supported audio files found.", file=sys.stderr)
        return 2

    report = {
        "schema_version": 1,
        "release": str(release),
        "analysed_at": datetime.now(timezone.utc).isoformat(),
        "files": [],
    }

    release_status = "PASS"

    for path in files:
        print(f"Checking: {path.name}", file=sys.stderr)

        integrity = run_flac_test(path)

        if not integrity["ok"]:
            status = "REJECTED"
            forensic = None
            release_status = "REJECTED"
        else:
            forensic = run_forensics(path)
            status = classify(forensic)

            if (
                status == "QUARANTINE"
                and release_status != "REJECTED"
            ):
                release_status = "QUARANTINE"

        item = {
            "path": str(path),
            "integrity": integrity,
            "status": status,
        }

        if forensic is not None:
            item["forensics"] = forensic

        report["files"].append(item)

    report["status"] = release_status
    report["summary"] = {
        "total": len(report["files"]),
        "pass": sum(x["status"] == "PASS" for x in report["files"]),
        "quarantine": sum(
            x["status"] == "QUARANTINE" for x in report["files"]
        ),
        "rejected": sum(
            x["status"] == "REJECTED" for x in report["files"]
        ),
    }

    output = json.dumps(report, indent=2, ensure_ascii=False)
    report_path = None

    if args.report:
        report_path = args.report.resolve()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(output + "\n", encoding="utf-8")

    if args.json_stdout:
        print(output)
    else:
        print_summary(report, report_path)

    return {
        "PASS": 0,
        "QUARANTINE": 10,
        "REJECTED": 20,
    }[release_status]


if __name__ == "__main__":
    sys.exit(main())
