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


def discover_releases(root: Path, *, excluded_roots: tuple[Path, ...] = (),
                      include_root: bool = True, limit: int | None = MAX_BATCH_RELEASES) -> list[Path]:
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

        if (candidate == root and not include_root) or any(excluded.is_relative_to(candidate) for excluded in excluded_roots):
            continue
        candidates.add(candidate)
        if limit is not None and len(candidates) > limit:
            raise BatchDiscoveryError(
                f"More than {limit} releases were found. "
                "Select a smaller artist or library directory."
            )

    ordered = sorted(candidates, key=lambda item: str(item).casefold())

    # If a multi-disc parent and one of its disc directories were both found,
    # keep only the parent release.
    result: list[Path] = []
    for candidate in ordered:
        if any(parent in candidates for parent in candidate.parents):
            continue
        result.append(candidate)

    return result
