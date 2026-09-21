from __future__ import annotations

import re
from pathlib import Path


MAX_BATCH_RELEASES = 500
_DISC_DIRECTORY = re.compile(
    r"^(?:cd|disc|disk|digital[ ._-]*media|side)[ ._-]*[0-9]+",
    re.IGNORECASE,
)


class BatchDiscoveryError(ValueError):
    pass


def discover_releases(root: Path) -> list[Path]:
    """Find album roots below *root*, collapsing conventional disc folders."""
    root = root.resolve()
    candidates: set[Path] = set()

    for audio_file in root.rglob("*"):
        if not audio_file.is_file() or audio_file.suffix.lower() != ".flac":
            continue

        audio_directory = audio_file.parent.resolve()
        if _DISC_DIRECTORY.match(audio_directory.name):
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
