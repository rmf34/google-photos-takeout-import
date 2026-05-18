import zipfile
from pathlib import Path

from extract_and_stage import album_name, extract_zip, is_year_album


class TestAlbumName:
    def test_returns_album_name(self):
        assert album_name("Takeout/Google Photos/Italy Feb 2019/IMG_1234.jpg") == "Italy Feb 2019"

    def test_returns_year_album_name(self):
        assert (
            album_name("Takeout/Google Photos/Photos from 2019/IMG_1234.jpg") == "Photos from 2019"
        )

    def test_returns_none_for_non_album_path(self):
        assert album_name("Takeout/print-subscriptions.json") is None


class TestIsYearAlbum:
    def test_year_album_detected(self):
        assert is_year_album("Takeout/Google Photos/Photos from 2019/IMG_1234.jpg")

    def test_all_years_detected(self):
        for year in [1931, 1999, 2000, 2024, 2025]:
            assert is_year_album(f"Takeout/Google Photos/Photos from {year}/photo.jpg")

    def test_named_album_not_year(self):
        assert not is_year_album("Takeout/Google Photos/Italy Feb 2019/IMG_1234.jpg")

    def test_album_with_year_in_name_not_year_album(self):
        assert not is_year_album("Takeout/Google Photos/Colombia March 2018/photo.jpg")
        assert not is_year_album("Takeout/Google Photos/2017 Travel/photo.jpg")
        assert not is_year_album("Takeout/Google Photos/2017-18 general/photo.jpg")

    def test_five_digit_year_not_matched(self):
        assert not is_year_album("Takeout/Google Photos/Photos from 20190/photo.jpg")

    def test_three_digit_year_not_matched(self):
        assert not is_year_album("Takeout/Google Photos/Photos from 201/photo.jpg")

    def test_no_google_photos_segment(self):
        assert not is_year_album("Takeout/print-subscriptions.json")

    def test_metadata_json_in_year_album(self):
        assert is_year_album("Takeout/Google Photos/Photos from 2019/metadata.json")


# ---------------------------------------------------------------------------
# extract_zip — count accuracy drives the delete-or-keep decision
# ---------------------------------------------------------------------------


def make_zip(path: Path, entries: dict[str, bytes]) -> Path:
    """Create a ZIP at `path` with {member_name: content} entries."""
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return path


class TestExtractZipCounts:
    ALBUM_PATH = "Takeout/Google Photos/Italy 2019/"

    def _entries(self, n=3):
        entries = {f"{self.ALBUM_PATH}dir/": b""}  # directory entry
        for i in range(n):
            entries[f"{self.ALBUM_PATH}IMG_{i:04d}.jpg"] = b"fake image"
            entries[f"{self.ALBUM_PATH}IMG_{i:04d}.jpg.supplemental-metadata.json"] = b"{}"
        return entries

    def test_fresh_extraction_counts(self, tmp_path, monkeypatch):
        monkeypatch.setattr("extract_and_stage.STAGE_DIR", tmp_path)
        zip_path = make_zip(tmp_path / "test.zip", self._entries(n=3))

        extracted, already_existed, total = extract_zip(zip_path, 1, 1)

        assert extracted == 6  # 3 images + 3 sidecars
        assert already_existed == 0
        assert total == 6  # directory entries excluded from total
        assert extracted + already_existed == total

    def test_rerun_counts_as_already_existed(self, tmp_path, monkeypatch):
        monkeypatch.setattr("extract_and_stage.STAGE_DIR", tmp_path)
        zip_path = make_zip(tmp_path / "test.zip", self._entries(n=3))

        extract_zip(zip_path, 1, 1)  # first pass
        # Re-create zip (it wasn't deleted in test — no main() call)
        zip_path = make_zip(tmp_path / "test.zip", self._entries(n=3))
        extracted, already_existed, total = extract_zip(zip_path, 1, 1)

        assert extracted == 0
        assert already_existed == 6
        assert extracted + already_existed == total

    def test_partial_extraction_count_still_accurate(self, tmp_path, monkeypatch):
        monkeypatch.setattr("extract_and_stage.STAGE_DIR", tmp_path)
        # Pre-place 2 of the 6 files to simulate a partial prior run
        for i in range(2):
            dest = tmp_path / self.ALBUM_PATH / f"IMG_{i:04d}.jpg"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"fake image")

        zip_path = make_zip(tmp_path / "test.zip", self._entries(n=3))
        extracted, already_existed, total = extract_zip(zip_path, 1, 1)

        assert already_existed == 2
        assert extracted == 4  # remaining 1 image + 3 sidecars
        assert extracted + already_existed == total

    def test_directory_entries_excluded_from_total(self, tmp_path, monkeypatch):
        monkeypatch.setattr("extract_and_stage.STAGE_DIR", tmp_path)
        entries = {
            "Takeout/Google Photos/Album/": b"",  # dir — should not count
            "Takeout/Google Photos/Album/photo.jpg": b"x",
        }
        zip_path = make_zip(tmp_path / "test.zip", entries)
        extracted, already_existed, total = extract_zip(zip_path, 1, 1)

        assert total == 1
        assert extracted == 1
