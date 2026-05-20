"""Tests for fix_missing_dates.py"""

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

JPEG_MAGIC = b"\xff\xd8\xff"
PNG_MAGIC = b"\x89PNG"

with patch.object(sys, "argv", ["fix_missing_dates.py"]):
    import fix_missing_dates as fm


# ---------------------------------------------------------------------------
# detect_actual_type
# ---------------------------------------------------------------------------


class TestDetectActualType:
    def test_jpeg_detected(self, tmp_path):
        f = tmp_path / "photo.png"
        f.write_bytes(JPEG_MAGIC + b"\x00" * 100)
        assert fm.detect_actual_type(f) == "jpeg"

    def test_non_jpeg_returns_other(self, tmp_path):
        f = tmp_path / "photo.png"
        f.write_bytes(PNG_MAGIC + b"\x00" * 100)
        assert fm.detect_actual_type(f) == "other"

    def test_missing_file_returns_other(self, tmp_path):
        assert fm.detect_actual_type(tmp_path / "missing.jpg") == "other"

    def test_empty_file_returns_other(self, tmp_path):
        f = tmp_path / "empty.jpg"
        f.write_bytes(b"")
        assert fm.detect_actual_type(f) == "other"


# ---------------------------------------------------------------------------
# date_from_directory
# ---------------------------------------------------------------------------


class TestDateFromDirectory:
    def test_year_month_directory(self, tmp_path):
        album = tmp_path / "Photos from 2017-03"
        album.mkdir()
        f = album / "IMG_001.jpg"
        assert fm.date_from_directory(f) == "2017:03:15 12:00:00"

    def test_unknown_month(self, tmp_path):
        album = tmp_path / "Photos from 2019-unknown"
        album.mkdir()
        f = album / "photo.jpg"
        assert fm.date_from_directory(f) == "2019:06:15 12:00:00"

    def test_invalid_month_zero(self, tmp_path):
        album = tmp_path / "Photos from 2020-00"
        album.mkdir()
        f = album / "photo.jpg"
        assert fm.date_from_directory(f) == "2020:06:15 12:00:00"

    def test_invalid_month_13(self, tmp_path):
        album = tmp_path / "Photos from 2020-13"
        album.mkdir()
        f = album / "photo.jpg"
        assert fm.date_from_directory(f) == "2020:06:15 12:00:00"

    def test_filename_year_fallback(self, tmp_path):
        album = tmp_path / "Assorted Family"
        album.mkdir()
        f = album / "1989_matt_tess_disney_0023.jpg"
        assert fm.date_from_directory(f) == "1989:06:15 12:00:00"

    def test_no_date_hint(self, tmp_path):
        album = tmp_path / "Random Album"
        album.mkdir()
        f = album / "photo.jpg"
        assert fm.date_from_directory(f) is None

    def test_december(self, tmp_path):
        album = tmp_path / "Photos from 2015-12"
        album.mkdir()
        f = album / "photo.jpg"
        assert fm.date_from_directory(f) == "2015:12:15 12:00:00"


# ---------------------------------------------------------------------------
# find_original_for_edited
# ---------------------------------------------------------------------------


class TestFindOriginalForEdited:
    def test_same_directory(self, tmp_path):
        orig = tmp_path / "photo.jpg"
        edited = tmp_path / "photo-edited.jpg"
        orig.write_bytes(b"original")
        edited.write_bytes(b"edited")

        index = {
            "photo.jpg": [orig],
            "photo-edited.jpg": [edited],
        }
        assert fm.find_original_for_edited(edited, index) == orig

    def test_cross_directory(self, tmp_path):
        album1 = tmp_path / "Album1"
        album2 = tmp_path / "Album2"
        album1.mkdir()
        album2.mkdir()

        orig = album1 / "photo.jpg"
        edited = album2 / "photo-edited.jpg"
        orig.write_bytes(b"original")
        edited.write_bytes(b"edited")

        index = {
            "photo.jpg": [orig],
            "photo-edited.jpg": [edited],
        }
        assert fm.find_original_for_edited(edited, index) == orig

    def test_no_original_found(self, tmp_path):
        edited = tmp_path / "photo-edited.jpg"
        edited.write_bytes(b"edited")

        index = {"photo-edited.jpg": [edited]}
        assert fm.find_original_for_edited(edited, index) is None

    def test_edited_only_before_extension(self, tmp_path):
        """Ensure -edited is only stripped before the file extension, not mid-name."""
        orig = tmp_path / "re.jpg"
        edited = tmp_path / "re-edited-edited.jpg"
        orig.write_bytes(b"original")
        edited.write_bytes(b"edited")

        index = {
            "re.jpg": [orig],
            "re-edited.jpg": [],
            "re-edited-edited.jpg": [edited],
        }
        # Should strip the last -edited only (before .jpg), yielding "re-edited.jpg"
        result = fm.find_original_for_edited(edited, index)
        # The original "re.jpg" should NOT be matched — "re-edited.jpg" is the base
        assert result is None or result.name == "re-edited.jpg"

    def test_no_edited_in_name(self, tmp_path):
        f = tmp_path / "normal_photo.jpg"
        f.write_bytes(b"data")
        assert fm.find_original_for_edited(f, {"normal_photo.jpg": [f]}) is None


# ---------------------------------------------------------------------------
# exiftool_tags_for_file
# ---------------------------------------------------------------------------


class TestExiftoolTagsForFile:
    def test_jpeg_file(self, tmp_path):
        f = tmp_path / "photo.jpg"
        f.write_bytes(JPEG_MAGIC + b"\x00" * 100)
        tags = fm.exiftool_tags_for_file(f, "2020:01:15 12:00:00")
        assert tags["DateTimeOriginal"] == "2020:01:15 12:00:00"
        assert "XMP-exif:DateTimeOriginal" not in tags

    def test_png_file(self, tmp_path):
        f = tmp_path / "screenshot.png"
        f.write_bytes(PNG_MAGIC + b"\x00" * 100)
        tags = fm.exiftool_tags_for_file(f, "2020:01:15 12:00:00")
        assert "XMP-exif:DateTimeOriginal" in tags
        assert "PNG:CreationTime" in tags
        assert "DateTimeOriginal" not in tags

    def test_fake_ext_jpeg_as_png(self, tmp_path):
        """A JPEG with .png extension should get JPEG tags based on actual type."""
        f = tmp_path / "fake.png"
        f.write_bytes(JPEG_MAGIC + b"\x00" * 100)
        tags = fm.exiftool_tags_for_file(f, "2020:01:15 12:00:00")
        assert tags["DateTimeOriginal"] == "2020:01:15 12:00:00"
        assert "XMP-exif:DateTimeOriginal" not in tags

    def test_mov_file(self, tmp_path):
        f = tmp_path / "video.mov"
        f.write_bytes(b"\x00" * 100)
        tags = fm.exiftool_tags_for_file(f, "2020:01:15 12:00:00")
        assert "QuickTime:CreateDate" in tags
        assert "DateTimeOriginal" not in tags


# ---------------------------------------------------------------------------
# EDITED_RE
# ---------------------------------------------------------------------------


class TestEditedRegex:
    def test_simple_edited(self):
        assert fm.EDITED_RE.sub("", "photo-edited.jpg") == "photo.jpg"

    def test_preserves_mid_name(self):
        assert fm.EDITED_RE.sub("", "re-edited-v2.jpg") == "re-edited-v2.jpg"

    def test_only_strips_before_extension(self):
        assert fm.EDITED_RE.sub("", "photo-edited-edited.jpg") == "photo-edited.jpg"

    def test_no_match(self):
        assert fm.EDITED_RE.sub("", "normal_photo.jpg") == "normal_photo.jpg"
