"""Tests for split_year_albums.py"""
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# Patch DRY_RUN before importing the module so the module-level constant is False
with patch.object(sys, "argv", ["split_year_albums.py"]):
    import split_year_albums as sya


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_photo(directory: Path, name: str, ts: int | None = 1609459200) -> Path:
    """Create a fake photo file and its sidecar. ts=None → no sidecar."""
    f = directory / name
    f.write_bytes(b"fake image")
    if ts is not None:
        sc = directory / (name + ".supplemental-metadata.json")
        sc.write_text(
            json.dumps({"photoTakenTime": {"timestamp": str(ts)}}),
            encoding="utf-8",
        )
    return f


# ---------------------------------------------------------------------------
# _parse_month
# ---------------------------------------------------------------------------

class TestParseMonth:
    def test_normal_timestamp(self, tmp_path):
        sc = tmp_path / "photo.jpg.supplemental-metadata.json"
        sc.write_text(json.dumps({"photoTakenTime": {"timestamp": "1609459200"}}))
        assert sya._parse_month(sc) == "2021-01"

    def test_epoch_zero_returns_none(self, tmp_path):
        sc = tmp_path / "photo.jpg.supplemental-metadata.json"
        sc.write_text(json.dumps({"photoTakenTime": {"timestamp": "0"}}))
        assert sya._parse_month(sc) is None

    def test_negative_ts_returns_none(self, tmp_path):
        sc = tmp_path / "photo.jpg.supplemental-metadata.json"
        sc.write_text(json.dumps({"photoTakenTime": {"timestamp": "-1"}}))
        assert sya._parse_month(sc) is None

    def test_missing_timestamp_key_returns_none(self, tmp_path):
        sc = tmp_path / "photo.jpg.supplemental-metadata.json"
        sc.write_text(json.dumps({"photoTakenTime": {}}))
        assert sya._parse_month(sc) is None

    def test_malformed_json_returns_none(self, tmp_path):
        sc = tmp_path / "photo.jpg.supplemental-metadata.json"
        sc.write_bytes(b"not json")
        assert sya._parse_month(sc) is None

    def test_different_months(self, tmp_path):
        cases = [
            ("1614556800", "2021-03"),   # March 2021
            ("1625097600", "2021-07"),   # July 2021
            ("1640995200", "2022-01"),   # January 2022
        ]
        for ts, expected in cases:
            sc = tmp_path / f"{ts}.json"
            sc.write_text(json.dumps({"photoTakenTime": {"timestamp": ts}}))
            assert sya._parse_month(sc) == expected


# ---------------------------------------------------------------------------
# get_month
# ---------------------------------------------------------------------------

class TestGetMonth:
    def test_finds_supplemental_metadata_sidecar(self, tmp_path):
        make_photo(tmp_path, "IMG_001.jpg", ts=1609459200)
        json_set = {f for f in tmp_path.iterdir() if f.suffix == ".json"}
        assert sya.get_month(tmp_path / "IMG_001.jpg", json_set) == "2021-01"

    def test_returns_none_when_no_sidecar(self, tmp_path):
        make_photo(tmp_path, "IMG_001.jpg", ts=None)
        assert sya.get_month(tmp_path / "IMG_001.jpg", set()) is None

    def test_fuzzy_match_truncated_name(self, tmp_path):
        # Simulate Google's truncation: sidecar has fewer chars than photo name
        photo = tmp_path / ("A" * 50 + ".jpg")
        photo.write_bytes(b"fake")
        # Sidecar named with 40-char prefix
        sc = tmp_path / (("A" * 40) + ".jpg.supplemental-metadata.json")
        sc.write_text(json.dumps({"photoTakenTime": {"timestamp": "1609459200"}}))
        json_set = {sc}
        assert sya.get_month(photo, json_set) == "2021-01"


# ---------------------------------------------------------------------------
# find_sidecars
# ---------------------------------------------------------------------------

class TestFindSidecars:
    def test_finds_supplemental_metadata(self, tmp_path):
        make_photo(tmp_path, "IMG_001.jpg", ts=1609459200)
        json_set = {f for f in tmp_path.iterdir() if f.suffix == ".json"}
        result = sya.find_sidecars(tmp_path / "IMG_001.jpg", json_set)
        assert len(result) == 1
        assert result[0].name == "IMG_001.jpg.supplemental-metadata.json"

    def test_no_sidecar_returns_empty(self, tmp_path):
        make_photo(tmp_path, "IMG_001.jpg", ts=None)
        result = sya.find_sidecars(tmp_path / "IMG_001.jpg", set())
        assert result == []

    def test_does_not_match_other_photos_sidecars(self, tmp_path):
        make_photo(tmp_path, "IMG_001.jpg", ts=1609459200)
        make_photo(tmp_path, "IMG_002.jpg", ts=1609459200)
        json_set = {f for f in tmp_path.iterdir() if f.suffix == ".json"}
        result = sya.find_sidecars(tmp_path / "IMG_001.jpg", json_set)
        assert all("IMG_001" in sc.name for sc in result)


# ---------------------------------------------------------------------------
# safe_move
# ---------------------------------------------------------------------------

class TestSafeMove:
    def test_moves_file(self, tmp_path):
        src = tmp_path / "src" / "photo.jpg"
        src.parent.mkdir()
        src.write_bytes(b"data")
        dst_dir = tmp_path / "dst"

        result = sya.safe_move(src, dst_dir)

        assert result is True
        assert (dst_dir / "photo.jpg").exists()
        assert not src.exists()

    def test_returns_false_on_collision(self, tmp_path):
        src = tmp_path / "src" / "photo.jpg"
        src.parent.mkdir()
        src.write_bytes(b"data")
        dst_dir = tmp_path / "dst"
        dst_dir.mkdir()
        (dst_dir / "photo.jpg").write_bytes(b"existing")

        result = sya.safe_move(src, dst_dir)

        assert result is False
        assert src.exists()  # not moved

    def test_returns_true_when_src_is_dst(self, tmp_path):
        f = tmp_path / "photo.jpg"
        f.write_bytes(b"data")
        result = sya.safe_move(f, tmp_path)
        assert result is True


# ---------------------------------------------------------------------------
# split_album (integration)
# ---------------------------------------------------------------------------

class TestSplitAlbum:
    def _make_album(self, base: Path, year: str, photos: list[tuple[str, int | None]]) -> Path:
        """Create a 'Photos from YEAR' album with given (filename, timestamp) pairs."""
        album = base / f"Photos from {year}"
        album.mkdir(parents=True)
        for name, ts in photos:
            make_photo(album, name, ts)
        return album

    def test_splits_into_month_dirs(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sya, "PHOTOS_DIR", tmp_path)
        album = self._make_album(tmp_path, "2021", [
            ("jan.jpg", 1609459200),   # 2021-01
            ("mar.jpg", 1614556800),   # 2021-03
        ])

        stats = sya.split_album(album, "2021")

        assert stats["moved"] == 2
        assert stats["no_month"] == 0
        assert (tmp_path / "Photos from 2021-01" / "jan.jpg").exists()
        assert (tmp_path / "Photos from 2021-03" / "mar.jpg").exists()

    def test_sidecars_move_with_photo(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sya, "PHOTOS_DIR", tmp_path)
        album = self._make_album(tmp_path, "2021", [("photo.jpg", 1609459200)])

        sya.split_album(album, "2021")

        dst = tmp_path / "Photos from 2021-01"
        assert (dst / "photo.jpg").exists()
        assert (dst / "photo.jpg.supplemental-metadata.json").exists()

    def test_no_sidecar_goes_to_unknown(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sya, "PHOTOS_DIR", tmp_path)
        album = self._make_album(tmp_path, "2021", [("nosidecar.jpg", None)])

        stats = sya.split_album(album, "2021")

        assert stats["no_month"] == 1
        assert (tmp_path / "Photos from 2021-unknown" / "nosidecar.jpg").exists()

    def test_epoch_timestamp_goes_to_unknown(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sya, "PHOTOS_DIR", tmp_path)
        album = self._make_album(tmp_path, "2021", [("epoch.jpg", 0)])

        stats = sya.split_album(album, "2021")

        assert stats["no_month"] == 1
        assert (tmp_path / "Photos from 2021-unknown" / "epoch.jpg").exists()

    def test_collision_counted_not_overwritten(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sya, "PHOTOS_DIR", tmp_path)
        # Pre-place a file in the destination
        dst = tmp_path / "Photos from 2021-01"
        dst.mkdir()
        (dst / "photo.jpg").write_bytes(b"existing")

        album = self._make_album(tmp_path, "2021", [("photo.jpg", 1609459200)])

        stats = sya.split_album(album, "2021")

        assert stats["collision"] == 1
        # Original not overwritten
        assert (dst / "photo.jpg").read_bytes() == b"existing"

    def test_mixed_months(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sya, "PHOTOS_DIR", tmp_path)
        photos = [
            ("jan_a.jpg", 1609459200),   # 2021-01
            ("jan_b.jpg", 1611878400),   # 2021-01
            ("feb.jpg",   1613520000),   # 2021-02
            ("none.jpg",  None),
        ]
        album = self._make_album(tmp_path, "2021", photos)

        stats = sya.split_album(album, "2021")

        assert stats["moved"] == 4
        assert stats["no_month"] == 1
        assert len(list((tmp_path / "Photos from 2021-01").iterdir())) == 4  # 2 photos + 2 sidecars
        assert (tmp_path / "Photos from 2021-02" / "feb.jpg").exists()
        assert (tmp_path / "Photos from 2021-unknown" / "none.jpg").exists()

    def test_empty_album_returns_zero(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sya, "PHOTOS_DIR", tmp_path)
        album = tmp_path / "Photos from 2021"
        album.mkdir()

        stats = sya.split_album(album, "2021")

        assert stats["total"] == 0
        assert stats["moved"] == 0
