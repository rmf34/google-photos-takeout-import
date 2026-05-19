"""Tests for fix_extensions.py"""

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

JPEG_MAGIC = b"\xff\xd8\xff"
PNG_MAGIC = b"\x89PNG"

# Patch DRY_RUN and PHOTOS_DIR before importing so module-level constants are set
with patch.object(sys, "argv", ["fix_extensions.py"]):
    import fix_extensions as fx


# ---------------------------------------------------------------------------
# _is_actually_jpeg
# ---------------------------------------------------------------------------


class TestIsActuallyJpeg:
    def test_jpeg_magic_returns_true(self, tmp_path):
        f = tmp_path / "photo.png"
        f.write_bytes(JPEG_MAGIC + b"\x00" * 100)
        assert fx._is_actually_jpeg(f) is True

    def test_png_magic_returns_false(self, tmp_path):
        f = tmp_path / "photo.jpg"
        f.write_bytes(PNG_MAGIC + b"\x00" * 100)
        assert fx._is_actually_jpeg(f) is False

    def test_empty_file_returns_false(self, tmp_path):
        f = tmp_path / "empty.png"
        f.write_bytes(b"")
        assert fx._is_actually_jpeg(f) is False

    def test_missing_file_returns_false(self, tmp_path):
        assert fx._is_actually_jpeg(tmp_path / "nonexistent.png") is False

    def test_only_reads_3_bytes(self, tmp_path):
        # A file with only 2 bytes should not crash — returns False
        f = tmp_path / "short.png"
        f.write_bytes(b"\xff\xd8")
        assert fx._is_actually_jpeg(f) is False


# ---------------------------------------------------------------------------
# _find_all_sidecars
# ---------------------------------------------------------------------------


class TestFindAllSidecars:
    def _make(self, directory: Path, name: str, content: bytes = b"data") -> Path:
        f = directory / name
        f.write_bytes(content)
        return f

    def test_finds_supplemental_metadata_sidecar(self, tmp_path):
        self._make(tmp_path, "photo.png", JPEG_MAGIC)
        self._make(tmp_path, "photo.png.supplemental-metadata.json", b"{}")
        result = fx._find_all_sidecars(tmp_path / "photo.png")
        assert len(result) == 1
        assert result[0].name == "photo.png.supplemental-metadata.json"

    def test_finds_truncated_supple_sidecar(self, tmp_path):
        self._make(tmp_path, "photo.heic", JPEG_MAGIC)
        self._make(tmp_path, "photo.heic.supple.json", b"{}")
        result = fx._find_all_sidecars(tmp_path / "photo.heic")
        assert len(result) == 1
        assert result[0].name == "photo.heic.supple.json"

    def test_no_sidecar_returns_empty(self, tmp_path):
        self._make(tmp_path, "photo.png", JPEG_MAGIC)
        result = fx._find_all_sidecars(tmp_path / "photo.png")
        assert result == []

    def test_duplicate_naming_pattern(self, tmp_path):
        # STEM(N).EXT → STEM.EXT.supplemental-metadata(N).json
        self._make(tmp_path, "photo(1).png", JPEG_MAGIC)
        self._make(tmp_path, "photo.png.supplemental-metadata(1).json", b"{}")
        result = fx._find_all_sidecars(tmp_path / "photo(1).png")
        assert len(result) == 1
        assert result[0].name == "photo.png.supplemental-metadata(1).json"

    def test_fuzzy_fallback_for_unusual_truncation(self, tmp_path):
        # Sidecar uses a truncation length not in SIDECAR_SUFFIXES
        long_name = "A" * 50 + ".webp"
        self._make(tmp_path, long_name, JPEG_MAGIC)
        # Truncated to 40-char prefix + unusual suffix
        sc = tmp_path / (("A" * 40) + ".webp.metadata.json")
        sc.write_bytes(b"{}")
        result = fx._find_all_sidecars(tmp_path / long_name)
        assert len(result) == 1
        assert result[0] == sc

    def test_no_cross_contamination(self, tmp_path):
        # Sidecar for photo_a should not appear in results for photo_b
        self._make(tmp_path, "photo_a.png", JPEG_MAGIC)
        self._make(tmp_path, "photo_b.png", JPEG_MAGIC)
        self._make(tmp_path, "photo_a.png.supplemental-metadata.json", b"{}")
        self._make(tmp_path, "photo_b.png.supplemental-metadata.json", b"{}")
        result = fx._find_all_sidecars(tmp_path / "photo_a.png")
        assert all("photo_a" in sc.name for sc in result)
        assert len(result) == 1


# ---------------------------------------------------------------------------
# _renamed_sidecar
# ---------------------------------------------------------------------------


class TestRenamedSidecar:
    def test_replaces_prefix(self, tmp_path):
        sc = tmp_path / "photo.png.supplemental-metadata.json"
        result = fx._renamed_sidecar(sc, "photo.png", "photo.jpg")
        assert result.name == "photo.jpg.supplemental-metadata.json"

    def test_case_insensitive_match(self, tmp_path):
        sc = tmp_path / "PHOTO.PNG.supplemental-metadata.json"
        result = fx._renamed_sidecar(sc, "photo.png", "photo.jpg")
        assert result.name == "photo.jpg.supplemental-metadata.json"

    def test_no_match_returns_unchanged(self, tmp_path):
        sc = tmp_path / "other.png.supplemental-metadata.json"
        result = fx._renamed_sidecar(sc, "photo.png", "photo.jpg")
        assert result == sc

    def test_duplicate_prefix_replacement(self, tmp_path):
        # Dupe sidecars: old_prefix = stem+ext, sidecar name uses stem.ext.suffix_base(N).json
        sc = tmp_path / "photo.png.supplemental-metadata(1).json"
        result = fx._renamed_sidecar(sc, "photo.png", "photo.jpg")
        assert result.name == "photo.jpg.supplemental-metadata(1).json"
