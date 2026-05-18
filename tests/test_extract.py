import pytest
from extract_and_stage import is_skipped


class TestIsSkipped:
    def test_year_album_skipped(self):
        assert is_skipped("Takeout/Google Photos/Photos from 2019/IMG_1234.jpg")

    def test_year_album_all_years(self):
        for year in [1931, 1999, 2000, 2024, 2025]:
            assert is_skipped(f"Takeout/Google Photos/Photos from {year}/photo.jpg")

    def test_named_album_not_skipped(self):
        assert not is_skipped("Takeout/Google Photos/Italy Feb 2019/IMG_1234.jpg")

    def test_album_with_year_in_name_not_skipped(self):
        # Albums that contain a year but aren't the auto-generated pattern
        assert not is_skipped("Takeout/Google Photos/Colombia March 2018/photo.jpg")
        assert not is_skipped("Takeout/Google Photos/2017 Travel/photo.jpg")
        assert not is_skipped("Takeout/Google Photos/2017-18 general/photo.jpg")

    def test_five_digit_year_not_skipped(self):
        assert not is_skipped("Takeout/Google Photos/Photos from 20190/photo.jpg")

    def test_three_digit_year_not_skipped(self):
        assert not is_skipped("Takeout/Google Photos/Photos from 201/photo.jpg")

    def test_no_google_photos_segment(self):
        assert not is_skipped("Takeout/print-subscriptions.json")

    def test_metadata_json_in_year_album_skipped(self):
        assert is_skipped("Takeout/Google Photos/Photos from 2019/metadata.json")
