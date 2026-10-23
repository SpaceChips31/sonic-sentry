from __future__ import annotations

import re
import os
from pathlib import Path


MAX_BATCH_RELEASES = 500
_DISC_DIRECTORY = re.compile(
    r"^(?:cd|disc|disk|digital[ ._-]*media|side)[ ._-]*[0-9]+$",
    re.IGNORECASE,
)


class BatchDiscoveryError(ValueError):
    pass


def discover_releases(root: Path, *, excluded_roots: tuple[Path, ...] = ()) -> list[Path]:
    """Find album roots below *root*, collapsing conventional disc folders."""
    root = root.resolve()
    candidates: set[Path] = set()

    def fail(error):
        raise error
    def walk_audio():
        for current, directories, filenames in os.walk(root, followlinks=False, onerror=fail):
            current = Path(current)
            if any(current.is_relative_to(excluded) for excluded in excluded_roots):
                directories[:] = []
                continue
            directories[:] = [name for name in directories if not name.startswith('.')
                               and not (current / name).is_symlink()
                               and not (hasattr(current / name, 'is_junction') and (current / name).is_junction())]
            for name in filenames:
                if Path(name).suffix.lower() == '.flac' and not (current / name).is_symlink():
                    yield current / name
    for audio_file in walk_audio():

        audio_directory = audio_file.parent.resolve()
        if audio_directory != root and _DISC_DIRECTORY.match(audio_directory.name):
            candidate = audio_directory.parent
        else:
            candidate = audio_directory

        try:
            candidate.relative_to(root)
        except ValueError:
            continue

        candidates.add(candidate)
        if len(candidates) > MAX_BATCH_RELEASES:
            raise BatchDiscoveryError(
                f"More than {MAX_BATCH_RELEASES} releases were found. "
                "Select a smaller artist or library directory."
            )

    ordered = sorted(candidates, key=lambda item: str(item).casefold())

    # If a multi-disc parent and one of its disc directories were both found,
    # keep only the parent release.
    result: list[Path] = []
    for candidate in ordered:
        if any(candidate != parent and candidate.is_relative_to(parent) for parent in ordered):
            continue
        result.append(candidate)

    return result
