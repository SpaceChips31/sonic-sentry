from pathlib import Path

import pytest

from app.services.batch import BatchDiscoveryError, discover_releases


def touch_flac(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fLaC")


def test_disc_directories_are_grouped_as_one_release(tmp_path):
    artist = tmp_path / "Artist"
    touch_flac(artist / "Album (2001)" / "CD 01" / "01.flac")
    touch_flac(artist / "Album (2001)" / "CD 02" / "02.flac")

    assert discover_releases(artist) == [artist / "Album (2001)"]


def test_mixed_library_finds_independent_album_roots(tmp_path):
    library = tmp_path / "library"
    touch_flac(library / "Artist A" / "Album One" / "01.flac")
    touch_flac(library / "Artist B" / "EP" / "track.flac")

    assert discover_releases(library) == [
        library / "Artist A" / "Album One",
        library / "Artist B" / "EP",
    ]


def test_digital_media_directories_are_grouped(tmp_path):
    album = tmp_path / "Rio (1982)"
    touch_flac(album / "Digital Media 01" / "01.flac")
    touch_flac(album / "Digital Media 02" / "02.flac")

    assert discover_releases(tmp_path) == [album]
