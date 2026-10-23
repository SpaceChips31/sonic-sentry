"""Shared, deterministic file snapshots for ingestion and verified transfers."""
import hashlib
import json
import os
from pathlib import Path


INCOMPLETE_SUFFIXES = {".part", ".partial", ".tmp", ".crdownload", ".!qb", ".download"}
MARKER = ".sonic-sentry-transfer"


def album_files(root: Path):
    if root.is_symlink() or (hasattr(root, "is_junction") and root.is_junction()):
        raise ValueError("Linked album directories are not supported")
    def onerror(error):
        raise error
    for current, directories, filenames in os.walk(root, followlinks=False, onerror=onerror):
        for name in directories:
            path = Path(current) / name
            if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
                raise ValueError("Linked directories are not supported")
        for name in sorted(filenames):
            path = Path(current) / name
            if path.name == MARKER:
                continue
            if path.is_symlink() or not path.is_file():
                raise ValueError("Only regular album files are supported")
            yield path


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def snapshot(root: Path, *, hashes: bool = False) -> dict:
    result = {}
    for path in album_files(root):
        before = path.stat()
        item = {"size": before.st_size, "mtime": before.st_mtime_ns}
        if hashes:
            item["sha256"] = file_digest(path)
            after = path.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ValueError("Album changed while reading files")
        result[path.relative_to(root).as_posix()] = item
    return dict(sorted(result.items()))


def fingerprint(manifest: dict) -> str:
    return hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()


def audio_content_hash(root: Path) -> str:
    # Exact file duplicates, independent of names/order. This does not equate different masters.
    hashes = sorted(file_digest(path) for path in album_files(root) if path.suffix.lower() == ".flac")
    if not hashes:
        raise ValueError("Album contains no FLAC files")
    return hashlib.sha256("\n".join(hashes).encode()).hexdigest()


def same_content(actual: dict, expected: dict) -> bool:
    return actual.keys() == expected.keys() and all(
        actual[name]["size"] == expected[name]["size"] and actual[name]["sha256"] == expected[name]["sha256"]
        for name in expected
    )
