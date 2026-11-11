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


def manifest_audio_hash(manifest: dict) -> str:
    hashes = sorted(item["sha256"] for name, item in manifest.items() if Path(name).suffix.lower() == ".flac")
    if not hashes:
        raise ValueError("Album contains no FLAC files")
    return hashlib.sha256("\n".join(hashes).encode()).hexdigest()


def same_content(actual: dict, expected: dict) -> bool:
    return actual.keys() == expected.keys() and all(
        actual[name]["size"] == expected[name]["size"] and actual[name]["sha256"] == expected[name]["sha256"]
        for name in expected
    )


def contains_flac(root: Path) -> bool:
    """Short-circuit recursive check, without following links or hidden work folders."""
    if not root.is_dir() or root.is_symlink() or (hasattr(root, "is_junction") and root.is_junction()):
        return False
    for current, directories, filenames in os.walk(root, followlinks=False):
        directory = Path(current)
        directories[:] = [name for name in directories if not name.startswith(".")
            and not (directory / name).is_symlink()
            and not (hasattr(directory / name, "is_junction") and (directory / name).is_junction())]
        if any(Path(name).suffix.lower() == ".flac" and (directory / name).is_file()
               and not (directory / name).is_symlink() for name in filenames):
            return True
    return False


def flac_frame_hash(path: Path) -> str | None:
    """Hash STREAMINFO audio properties and encoded frames, excluding mutable tag blocks.

    Native FLAC structure: RFC 9639 section 6. Re-encoding is intentionally not equated.
    """
    try:
        if path.is_symlink() or path.suffix.lower() != ".flac":
            return None
        before = path.stat()
        with path.open("rb") as source:
            if source.read(4) != b"fLaC":
                return None
            properties = None
            for index in range(10000):
                header = source.read(4)
                if len(header) != 4:
                    return None
                kind, length = header[0] & 127, int.from_bytes(header[1:], "big")
                if index == 0:
                    if kind != 0 or length != 34:
                        return None
                    info = source.read(34)
                    if len(info) != 34:
                        return None
                    properties = info[10:18]  # Sample rate/channels/bit depth/sample count.
                else:
                    if kind == 0 or kind == 127 or source.tell() + length > before.st_size:
                        return None
                    source.seek(length, 1)
                if header[0] & 128:
                    break
            else:
                return None
            value = hashlib.sha256(properties)
            count = 0
            for part in iter(lambda: source.read(1024 * 1024), b""):
                value.update(part)
                count += len(part)
        after = path.stat()
        if count == 0 or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            return None
        return "flac-frames-v1:" + value.hexdigest()
    except OSError:
        return None
