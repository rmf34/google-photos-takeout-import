"""Tests for reupload_fixed.py"""

import subprocess
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import reupload_fixed as rf

# ---------------------------------------------------------------------------
# load_fixed_files
# ---------------------------------------------------------------------------


class TestLoadFixedFiles:
    def test_basic_loading(self, tmp_path):
        fixed = tmp_path / "fixed_files.txt"
        fixed.write_text("Album One/photo1.jpg\nAlbum One/photo2.jpg\nAlbum Two/video.mp4\n")
        with patch.object(rf, "FIXED_LIST", fixed):
            result = rf.load_fixed_files()

        assert result == {
            "Album One": {"photo1.jpg", "photo2.jpg"},
            "Album Two": {"video.mp4"},
        }

    def test_skips_lines_without_slash(self, tmp_path):
        fixed = tmp_path / "fixed_files.txt"
        fixed.write_text("orphan_file.jpg\nAlbum/real.jpg\n\n")
        with patch.object(rf, "FIXED_LIST", fixed):
            result = rf.load_fixed_files()

        assert result == {"Album": {"real.jpg"}}

    def test_nested_path_splits_on_first_slash(self, tmp_path):
        fixed = tmp_path / "fixed_files.txt"
        fixed.write_text("Album Name/subdir/photo.jpg\n")
        with patch.object(rf, "FIXED_LIST", fixed):
            result = rf.load_fixed_files()

        assert result == {"Album Name": {"subdir/photo.jpg"}}

    def test_empty_file(self, tmp_path):
        fixed = tmp_path / "fixed_files.txt"
        fixed.write_text("")
        with patch.object(rf, "FIXED_LIST", fixed):
            result = rf.load_fixed_files()

        assert result == {}

    def test_whitespace_stripped(self, tmp_path):
        fixed = tmp_path / "fixed_files.txt"
        fixed.write_text("  Album/photo.jpg  \n")
        with patch.object(rf, "FIXED_LIST", fixed):
            result = rf.load_fixed_files()

        assert result == {"Album": {"photo.jpg"}}

    def test_deduplicates_filenames(self, tmp_path):
        fixed = tmp_path / "fixed_files.txt"
        fixed.write_text("Album/photo.jpg\nAlbum/photo.jpg\n")
        with patch.object(rf, "FIXED_LIST", fixed):
            result = rf.load_fixed_files()

        assert result == {"Album": {"photo.jpg"}}


# ---------------------------------------------------------------------------
# list_album_files — parses rclone lsl output
# ---------------------------------------------------------------------------


class TestListAlbumFiles:
    def test_parses_rclone_lsl_output(self):
        stdout = (
            "   123456 2026-05-19 05:51:30.000000000 photo1.jpg\n"
            "   789012 2026-05-20 10:30:00.000000000 photo2.png\n"
        )
        mock_result = MagicMock(stdout=stdout, stderr="", returncode=0)
        with patch("reupload_fixed.subprocess.run", return_value=mock_result):
            files = rf.list_album_files("Test Album")

        assert len(files) == 2
        assert files[0] == ("photo1.jpg", datetime(2026, 5, 19, 5, 51, 30))
        assert files[1] == ("photo2.png", datetime(2026, 5, 20, 10, 30, 0))

    def test_handles_negative_size(self):
        stdout = "       -1 2026-05-19 12:00:00.000000000 weird_file.jpg\n"
        mock_result = MagicMock(stdout=stdout, stderr="", returncode=0)
        with patch("reupload_fixed.subprocess.run", return_value=mock_result):
            files = rf.list_album_files("Album")

        assert len(files) == 1
        assert files[0][0] == "weird_file.jpg"

    def test_skips_malformed_lines(self):
        stdout = "this is not rclone output\n   123 2026-05-19 10:00:00.000 valid.jpg\n"
        mock_result = MagicMock(stdout=stdout, stderr="", returncode=0)
        with patch("reupload_fixed.subprocess.run", return_value=mock_result):
            files = rf.list_album_files("Album")

        assert len(files) == 1
        assert files[0][0] == "valid.jpg"

    def test_empty_output(self):
        mock_result = MagicMock(stdout="", stderr="", returncode=0)
        with patch("reupload_fixed.subprocess.run", return_value=mock_result):
            files = rf.list_album_files("Empty Album")

        assert files == []

    def test_timeout_returns_empty(self):
        with patch(
            "reupload_fixed.subprocess.run",
            side_effect=subprocess.TimeoutExpired("rclone", 60),
        ):
            files = rf.list_album_files("Slow Album")

        assert files == []

    def test_filename_with_spaces(self):
        stdout = "   100 2026-05-19 08:00:00.000000000 my vacation photo.jpg\n"
        mock_result = MagicMock(stdout=stdout, stderr="", returncode=0)
        with patch("reupload_fixed.subprocess.run", return_value=mock_result):
            files = rf.list_album_files("Album")

        assert files[0][0] == "my vacation photo.jpg"


# ---------------------------------------------------------------------------
# get_gp_album_list — parses rclone lsd output
# ---------------------------------------------------------------------------


class TestGetGpAlbumList:
    def test_parses_album_names(self):
        stdout = (
            "          -1 2026-05-20 16:40:31       140 Assorted Family Pictures\n"
            "          -1 2026-05-20 16:40:31        50 Photos from 2017-03\n"
            "          -1 2026-05-20 16:40:31        12 Le Maouts\n"
        )
        mock_result = MagicMock(stdout=stdout, stderr="", returncode=0)
        with patch("reupload_fixed.subprocess.run", return_value=mock_result):
            albums = rf.get_gp_album_list()

        assert albums == {"Assorted Family Pictures", "Photos from 2017-03", "Le Maouts"}

    def test_empty_list_exits_with_error(self):
        mock_result = MagicMock(stdout="", stderr="", returncode=0)
        with (
            patch("reupload_fixed.subprocess.run", return_value=mock_result),
            pytest.raises(SystemExit),
        ):
            rf.get_gp_album_list()

    def test_rclone_error_exits(self):
        mock_result = MagicMock(stdout="", stderr="ERROR : quota exceeded", returncode=1)
        with (
            patch("reupload_fixed.subprocess.run", return_value=mock_result),
            pytest.raises(SystemExit),
        ):
            rf.get_gp_album_list()

    def test_stderr_error_with_zero_returncode_exits(self):
        mock_result = MagicMock(stdout="", stderr="ERROR : partial failure", returncode=0)
        with (
            patch("reupload_fixed.subprocess.run", return_value=mock_result),
            pytest.raises(SystemExit),
        ):
            rf.get_gp_album_list()

    def test_skips_short_lines(self):
        stdout = "bad\n          -1 2026-05-20 16:40:31       140 Valid Album\n"
        mock_result = MagicMock(stdout=stdout, stderr="", returncode=0)
        with patch("reupload_fixed.subprocess.run", return_value=mock_result):
            albums = rf.get_gp_album_list()

        assert albums == {"Valid Album"}

    def test_album_name_with_special_chars(self):
        stdout = "          -1 2026-05-20 16:40:31       10 Album Name With (Special Characters\n"
        mock_result = MagicMock(stdout=stdout, stderr="", returncode=0)
        with patch("reupload_fixed.subprocess.run", return_value=mock_result):
            albums = rf.get_gp_album_list()

        assert "Album Name With (Special Characters" in albums

    def test_timeout_exits_with_error(self):
        with (
            patch(
                "reupload_fixed.subprocess.run",
                side_effect=subprocess.TimeoutExpired("rclone", 120),
            ),
            pytest.raises(SystemExit),
        ):
            rf.get_gp_album_list()


# ---------------------------------------------------------------------------
# build_delete_list — core safety logic
# ---------------------------------------------------------------------------


def _mock_list_album_files(lsl_stdout):
    """Build a mock for list_album_files that returns parsed lsl output."""
    mock_result = MagicMock(stdout=lsl_stdout, stderr="", returncode=0)
    return patch("reupload_fixed.subprocess.run", return_value=mock_result)


class TestBuildDeleteList:
    """Tests the extracted build_delete_list() function directly."""

    def test_recent_file_scheduled_for_deletion(self):
        fixed = {"TestAlbum": {"photo.jpg"}}
        gp_albums = {"TestAlbum"}
        window = datetime(2026, 5, 18)

        with _mock_list_album_files("   100 2026-05-19 10:00:00.000000000 photo.jpg\n"):
            to_delete, safe_skips = rf.build_delete_list(fixed, gp_albums, window)

        assert len(to_delete) == 1
        assert to_delete[0][1] == "photo.jpg"
        assert len(safe_skips) == 0

    def test_old_file_safely_skipped(self):
        fixed = {"TestAlbum": {"old_photo.jpg"}}
        gp_albums = {"TestAlbum"}
        window = datetime(2026, 5, 18)

        with _mock_list_album_files("   100 2020-03-15 10:00:00.000000000 old_photo.jpg\n"):
            to_delete, safe_skips = rf.build_delete_list(fixed, gp_albums, window)

        assert len(to_delete) == 0
        assert len(safe_skips) == 1
        assert safe_skips[0][1] == "old_photo.jpg"

    def test_mixed_old_and_new(self):
        fixed = {"TestAlbum": {"new.jpg", "old.jpg"}}
        gp_albums = {"TestAlbum"}
        window = datetime(2026, 5, 18)

        with _mock_list_album_files(
            "   100 2026-05-19 10:00:00.000000000 new.jpg\n"
            "   200 2015-06-01 12:00:00.000000000 old.jpg\n"
        ):
            to_delete, safe_skips = rf.build_delete_list(fixed, gp_albums, window)

        assert len(to_delete) == 1
        assert to_delete[0][1] == "new.jpg"
        assert len(safe_skips) == 1
        assert safe_skips[0][1] == "old.jpg"

    def test_file_not_in_gp_is_ignored(self):
        fixed = {"TestAlbum": {"not_uploaded.jpg"}}
        gp_albums = {"TestAlbum"}
        window = datetime(2026, 5, 18)

        with _mock_list_album_files(""):
            to_delete, safe_skips = rf.build_delete_list(fixed, gp_albums, window)

        assert len(to_delete) == 0
        assert len(safe_skips) == 0

    def test_album_not_in_gp_is_ignored(self):
        fixed = {"NonexistentAlbum": {"photo.jpg"}}
        gp_albums = {"OtherAlbum"}
        window = datetime(2026, 5, 18)

        to_delete, safe_skips = rf.build_delete_list(fixed, gp_albums, window)

        assert len(to_delete) == 0
        assert len(safe_skips) == 0

    def test_exact_boundary_date_included(self):
        fixed = {"TestAlbum": {"boundary.jpg"}}
        gp_albums = {"TestAlbum"}
        window = datetime(2026, 5, 18)

        with _mock_list_album_files("   100 2026-05-18 00:00:00.000000000 boundary.jpg\n"):
            to_delete, safe_skips = rf.build_delete_list(fixed, gp_albums, window)

        assert len(to_delete) == 1
        assert len(safe_skips) == 0

    def test_one_second_before_boundary_skipped(self):
        fixed = {"TestAlbum": {"before.jpg"}}
        gp_albums = {"TestAlbum"}
        window = datetime(2026, 5, 18)

        with _mock_list_album_files("   100 2026-05-17 23:59:59.000000000 before.jpg\n"):
            to_delete, safe_skips = rf.build_delete_list(fixed, gp_albums, window)

        assert len(to_delete) == 0
        assert len(safe_skips) == 1

    def test_custom_since_date(self):
        fixed = {"TestAlbum": {"photo.jpg"}}
        gp_albums = {"TestAlbum"}
        window = datetime(2026, 5, 20)

        with _mock_list_album_files("   100 2026-05-19 10:00:00.000000000 photo.jpg\n"):
            to_delete, safe_skips = rf.build_delete_list(fixed, gp_albums, window)

        assert len(to_delete) == 0
        assert len(safe_skips) == 1

    def test_only_intersecting_albums_processed(self):
        fixed = {"Album A": {"a.jpg"}, "Album B": {"b.jpg"}, "Album C": {"c.jpg"}}
        gp_albums = {"Album B", "Album C", "Album D"}
        window = datetime(2026, 5, 18)

        with _mock_list_album_files(
            "   100 2026-05-19 10:00:00.000000000 b.jpg\n"
            "   100 2026-05-19 10:00:00.000000000 c.jpg\n"
        ):
            to_delete, safe_skips = rf.build_delete_list(fixed, gp_albums, window)

        deleted_albums = {album for album, _, _ in to_delete}
        assert "Album A" not in deleted_albums
        assert "Album D" not in deleted_albums

    def test_no_album_overlap(self):
        fixed = {"Album X": {"x.jpg"}}
        gp_albums = {"Album Y"}
        window = datetime(2026, 5, 18)

        to_delete, safe_skips = rf.build_delete_list(fixed, gp_albums, window)

        assert to_delete == []
        assert safe_skips == []

    def test_exact_filename_match_required(self):
        fixed = {"TestAlbum": {"photo.jpg"}}
        gp_albums = {"TestAlbum"}
        window = datetime(2026, 5, 18)

        with _mock_list_album_files(
            "   100 2026-05-19 10:00:00.000000000 photo_long_name.jpg\n"
            "   100 2026-05-19 10:00:00.000000000 Photo.JPG\n"
        ):
            to_delete, safe_skips = rf.build_delete_list(fixed, gp_albums, window)

        assert len(to_delete) == 0
        assert len(safe_skips) == 0

    def test_multiple_albums_each_checked(self):
        fixed = {
            "Album1": {"a.jpg"},
            "Album2": {"b.jpg"},
        }
        gp_albums = {"Album1", "Album2"}
        window = datetime(2026, 5, 18)

        lsl_data = {
            "Album1": "   100 2026-05-19 10:00:00.000000000 a.jpg\n",
            "Album2": "   100 2015-01-01 12:00:00.000000000 b.jpg\n",
        }

        def mock_run(cmd, **kwargs):
            for album_name, stdout in lsl_data.items():
                if album_name in cmd[2]:
                    return MagicMock(stdout=stdout, stderr="", returncode=0)
            return MagicMock(stdout="", stderr="", returncode=0)

        with patch("reupload_fixed.subprocess.run", side_effect=mock_run):
            to_delete, safe_skips = rf.build_delete_list(fixed, gp_albums, window)

        assert len(to_delete) == 1
        assert to_delete[0][0] == "Album1"
        assert len(safe_skips) == 1
        assert safe_skips[0][0] == "Album2"
