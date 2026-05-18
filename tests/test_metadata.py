import json
import pytest
from pathlib import Path
from fix_metadata import find_sidecar, parse_sidecar, build_exiftool_entry, utc_to_local_str


# ---------------------------------------------------------------------------
# find_sidecar
# ---------------------------------------------------------------------------

class TestFindSidecar:
    def _make_photo(self, tmp_path, name="IMG_1234.jpg"):
        photo = tmp_path / name
        photo.write_bytes(b"fake")
        return photo

    def test_full_supplemental_metadata(self, tmp_path):
        photo = self._make_photo(tmp_path)
        sidecar = tmp_path / "IMG_1234.jpg.supplemental-metadata.json"
        sidecar.write_text("{}")
        assert find_sidecar(photo) == sidecar

    def test_supple_truncation(self, tmp_path):
        photo = self._make_photo(tmp_path)
        sidecar = tmp_path / "IMG_1234.jpg.supple.json"
        sidecar.write_text("{}")
        assert find_sidecar(photo) == sidecar

    def test_suppl_truncation(self, tmp_path):
        photo = self._make_photo(tmp_path)
        sidecar = tmp_path / "IMG_1234.jpg.suppl.json"
        sidecar.write_text("{}")
        assert find_sidecar(photo) == sidecar

    def test_supp_truncation(self, tmp_path):
        photo = self._make_photo(tmp_path)
        sidecar = tmp_path / "IMG_1234.jpg.supp.json"
        sidecar.write_text("{}")
        assert find_sidecar(photo) == sidecar

    def test_old_json_format(self, tmp_path):
        photo = self._make_photo(tmp_path)
        sidecar = tmp_path / "IMG_1234.jpg.json"
        sidecar.write_text("{}")
        assert find_sidecar(photo) == sidecar

    def test_prefers_full_name_over_truncated(self, tmp_path):
        photo = self._make_photo(tmp_path)
        full = tmp_path / "IMG_1234.jpg.supplemental-metadata.json"
        truncated = tmp_path / "IMG_1234.jpg.supp.json"
        full.write_text("{}")
        truncated.write_text("{}")
        assert find_sidecar(photo) == full

    def test_no_sidecar_returns_none(self, tmp_path):
        photo = self._make_photo(tmp_path)
        assert find_sidecar(photo) is None

    def test_does_not_match_other_photos_sidecar(self, tmp_path):
        photo = self._make_photo(tmp_path, "IMG_1234.jpg")
        other_sidecar = tmp_path / "IMG_5678.jpg.supplemental-metadata.json"
        other_sidecar.write_text("{}")
        assert find_sidecar(photo) is None

    def test_long_hash_filename_fuzzy_match(self, tmp_path):
        # Simulates Google's hash-named files with unusual truncation
        name = "74e9759860b7f42de0b918340df0664f-1-.jpg"
        photo = self._make_photo(tmp_path, name)
        sidecar = tmp_path / (name + ".supple.json")
        sidecar.write_text("{}")
        assert find_sidecar(photo) == sidecar


# ---------------------------------------------------------------------------
# parse_sidecar
# ---------------------------------------------------------------------------

def make_sidecar(tmp_path, data: dict, name="photo.jpg.supplemental-metadata.json") -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(data))
    return p


class TestParseSidecarTimestamp:
    def test_extracts_datetime(self, tmp_path):
        s = make_sidecar(tmp_path, {
            "photoTakenTime": {"timestamp": "1550521691"},
            "geoData": {"latitude": 0.0, "longitude": 0.0, "altitude": 0.0},
            "geoDataExif": {"latitude": 0.0, "longitude": 0.0, "altitude": 0.0},
        })
        result = parse_sidecar(s)
        assert "datetime" in result
        # Timestamp 1550521691 = 2019-02-18 20:28:11 UTC
        # No GPS so UTC fallback — check date portion at minimum
        assert result["datetime"].startswith("2019:02:18")

    def test_missing_timestamp_no_datetime(self, tmp_path):
        s = make_sidecar(tmp_path, {
            "geoData": {"latitude": 0.0, "longitude": 0.0, "altitude": 0.0},
            "geoDataExif": {"latitude": 0.0, "longitude": 0.0, "altitude": 0.0},
        })
        result = parse_sidecar(s)
        assert "datetime" not in result

    def test_epoch_timestamp_ignored(self, tmp_path):
        # timestamp "0" means no data — should not write 1970-01-01 to EXIF
        s = make_sidecar(tmp_path, {
            "photoTakenTime": {"timestamp": "0"},
            "geoData": {"latitude": 0.0, "longitude": 0.0, "altitude": 0.0},
            "geoDataExif": {"latitude": 0.0, "longitude": 0.0, "altitude": 0.0},
        })
        assert "datetime" not in parse_sidecar(s)

    def test_negative_timestamp_ignored(self, tmp_path):
        s = make_sidecar(tmp_path, {
            "photoTakenTime": {"timestamp": "-1"},
            "geoData": {"latitude": 0.0, "longitude": 0.0, "altitude": 0.0},
            "geoDataExif": {"latitude": 0.0, "longitude": 0.0, "altitude": 0.0},
        })
        assert "datetime" not in parse_sidecar(s)

    def test_invalid_json_returns_empty(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("not valid json {{")
        assert parse_sidecar(p) == {}


class TestParseSidecarGPS:
    def _sidecar(self, tmp_path, lat, lon, alt=0.0, exif_lat=0.0, exif_lon=0.0):
        return make_sidecar(tmp_path, {
            "photoTakenTime": {"timestamp": "1550521691"},
            "geoData": {"latitude": lat, "longitude": lon, "altitude": alt},
            "geoDataExif": {"latitude": exif_lat, "longitude": exif_lon, "altitude": 0.0},
        })

    def test_northern_eastern_hemisphere(self, tmp_path):
        s = self._sidecar(tmp_path, lat=40.65, lon=73.79)
        gps = parse_sidecar(s)["gps"]
        assert gps["lat_ref"] == "N"
        assert gps["lon_ref"] == "E"
        assert gps["lat"] == pytest.approx(40.65)
        assert gps["lon"] == pytest.approx(73.79)

    def test_southern_western_hemisphere(self, tmp_path):
        # São Paulo: lat negative (S), lon negative (W)
        s = self._sidecar(tmp_path, lat=-23.55, lon=-46.63)
        gps = parse_sidecar(s)["gps"]
        assert gps["lat_ref"] == "S"
        assert gps["lon_ref"] == "W"
        assert gps["lat"] == pytest.approx(23.55)
        assert gps["lon"] == pytest.approx(46.63)

    def test_southern_eastern_hemisphere(self, tmp_path):
        # Sydney: lat negative (S), lon positive (E)
        s = self._sidecar(tmp_path, lat=-33.87, lon=151.21)
        gps = parse_sidecar(s)["gps"]
        assert gps["lat_ref"] == "S"
        assert gps["lon_ref"] == "E"

    def test_zero_coords_no_gps(self, tmp_path):
        s = self._sidecar(tmp_path, lat=0.0, lon=0.0)
        assert "gps" not in parse_sidecar(s)

    def test_near_zero_coords_no_gps(self, tmp_path):
        s = self._sidecar(tmp_path, lat=0.0001, lon=0.0)
        assert "gps" not in parse_sidecar(s)

    def test_above_sea_level(self, tmp_path):
        s = self._sidecar(tmp_path, lat=40.0, lon=-74.0, alt=100.0)
        gps = parse_sidecar(s)["gps"]
        assert gps["alt"] == pytest.approx(100.0)
        assert gps["alt_ref"] == "Above Sea Level"

    def test_below_sea_level(self, tmp_path):
        # Dead Sea elevation ~-430m
        s = self._sidecar(tmp_path, lat=31.5, lon=35.5, alt=-430.0)
        gps = parse_sidecar(s)["gps"]
        assert gps["alt"] == pytest.approx(430.0)
        assert gps["alt_ref"] == "Below Sea Level"

    def test_prefers_geoDataExif_when_nonzero(self, tmp_path):
        # geoDataExif has real coords; geoData has different ones
        s = make_sidecar(tmp_path, {
            "photoTakenTime": {"timestamp": "1550521691"},
            "geoData": {"latitude": 10.0, "longitude": 20.0, "altitude": 0.0},
            "geoDataExif": {"latitude": 51.5, "longitude": -0.12, "altitude": 0.0},
        })
        gps = parse_sidecar(s)["gps"]
        assert gps["lat"] == pytest.approx(51.5)   # geoDataExif, not geoData

    def test_falls_back_to_geoData_when_exif_zero(self, tmp_path):
        s = make_sidecar(tmp_path, {
            "photoTakenTime": {"timestamp": "1550521691"},
            "geoData": {"latitude": 48.86, "longitude": 2.35, "altitude": 0.0},
            "geoDataExif": {"latitude": 0.0, "longitude": 0.0, "altitude": 0.0},
        })
        gps = parse_sidecar(s)["gps"]
        assert gps["lat"] == pytest.approx(48.86)

    def test_ts_utc_stored_for_gps_timestamp(self, tmp_path):
        s = self._sidecar(tmp_path, lat=40.65, lon=-73.79)
        result = parse_sidecar(s)
        assert "ts_utc" in result
        assert result["ts_utc"] == 1550521691


class TestParseSidecarDescription:
    def test_description_included(self, tmp_path):
        s = make_sidecar(tmp_path, {
            "photoTakenTime": {"timestamp": "1550521691"},
            "description": "Trip to Italy",
            "geoData": {"latitude": 0.0, "longitude": 0.0, "altitude": 0.0},
            "geoDataExif": {"latitude": 0.0, "longitude": 0.0, "altitude": 0.0},
        })
        assert parse_sidecar(s)["description"] == "Trip to Italy"

    def test_empty_description_not_included(self, tmp_path):
        s = make_sidecar(tmp_path, {
            "photoTakenTime": {"timestamp": "1550521691"},
            "description": "",
            "geoData": {"latitude": 0.0, "longitude": 0.0, "altitude": 0.0},
            "geoDataExif": {"latitude": 0.0, "longitude": 0.0, "altitude": 0.0},
        })
        assert "description" not in parse_sidecar(s)


# ---------------------------------------------------------------------------
# build_exiftool_entry
# ---------------------------------------------------------------------------

SAMPLE_METADATA = {
    "datetime": "2019:02:18 15:28:11",
    "ts_utc": 1550521691,
    "gps": {
        "lat": 40.65, "lat_ref": "N",
        "lon": 73.79, "lon_ref": "W",
        "alt": 10.0, "alt_ref": "Above Sea Level",
    },
    "description": "A caption",
}


class TestBuildExiftoolEntry:
    def test_jpeg_uses_exif_date_tags(self, tmp_path):
        photo = tmp_path / "photo.jpg"
        photo.write_bytes(b"x")
        entry = build_exiftool_entry(photo, SAMPLE_METADATA)
        assert "DateTimeOriginal" in entry
        assert "CreateDate" in entry
        assert "QuickTime:CreateDate" not in entry
        assert "XMP-exif:DateTimeOriginal" not in entry

    def test_heic_uses_exif_date_tags(self, tmp_path):
        photo = tmp_path / "photo.heic"
        photo.write_bytes(b"x")
        entry = build_exiftool_entry(photo, SAMPLE_METADATA)
        assert "DateTimeOriginal" in entry

    def test_png_uses_xmp_date_tags(self, tmp_path):
        photo = tmp_path / "photo.png"
        photo.write_bytes(b"x")
        entry = build_exiftool_entry(photo, SAMPLE_METADATA)
        assert "XMP-exif:DateTimeOriginal" in entry
        assert "DateTimeOriginal" not in entry

    def test_mp4_uses_quicktime_tags(self, tmp_path):
        photo = tmp_path / "video.mp4"
        photo.write_bytes(b"x")
        entry = build_exiftool_entry(photo, SAMPLE_METADATA)
        assert "QuickTime:CreateDate" in entry
        assert "DateTimeOriginal" not in entry

    def test_mov_uses_quicktime_tags(self, tmp_path):
        photo = tmp_path / "video.mov"
        photo.write_bytes(b"x")
        entry = build_exiftool_entry(photo, SAMPLE_METADATA)
        assert "QuickTime:CreateDate" in entry

    def test_gps_tags_written(self, tmp_path):
        photo = tmp_path / "photo.jpg"
        photo.write_bytes(b"x")
        entry = build_exiftool_entry(photo, SAMPLE_METADATA)
        assert entry["GPSLatitude"] == pytest.approx(40.65)
        assert entry["GPSLatitudeRef"] == "N"
        assert entry["GPSLongitude"] == pytest.approx(73.79)
        assert entry["GPSLongitudeRef"] == "W"

    def test_below_sea_level_alt_ref(self, tmp_path):
        photo = tmp_path / "photo.jpg"
        photo.write_bytes(b"x")
        meta = {**SAMPLE_METADATA, "gps": {**SAMPLE_METADATA["gps"], "alt": 430.0, "alt_ref": "Below Sea Level"}}
        entry = build_exiftool_entry(photo, meta)
        assert entry["GPSAltitudeRef"] == "Below Sea Level"
        assert entry["GPSAltitude"] == pytest.approx(430.0)

    def test_gps_datestamp_and_timestamp_written(self, tmp_path):
        photo = tmp_path / "photo.jpg"
        photo.write_bytes(b"x")
        entry = build_exiftool_entry(photo, SAMPLE_METADATA)
        assert entry["GPSDateStamp"] == "2019:02:18"
        assert entry["GPSTimeStamp"] == "20:28:11"

    def test_description_written(self, tmp_path):
        photo = tmp_path / "photo.jpg"
        photo.write_bytes(b"x")
        entry = build_exiftool_entry(photo, SAMPLE_METADATA)
        assert entry["ImageDescription"] == "A caption"
        assert entry["XMP-dc:Description"] == "A caption"

    def test_no_gps_in_metadata(self, tmp_path):
        photo = tmp_path / "photo.jpg"
        photo.write_bytes(b"x")
        entry = build_exiftool_entry(photo, {"datetime": "2019:02:18 15:28:11"})
        assert "GPSLatitude" not in entry

    def test_empty_metadata_returns_none(self, tmp_path):
        photo = tmp_path / "photo.jpg"
        photo.write_bytes(b"x")
        assert build_exiftool_entry(photo, {}) is None

    def test_source_file_is_absolute_path(self, tmp_path):
        photo = tmp_path / "photo.jpg"
        photo.write_bytes(b"x")
        entry = build_exiftool_entry(photo, SAMPLE_METADATA)
        assert entry["SourceFile"] == str(photo)


# ---------------------------------------------------------------------------
# utc_to_local_str — timezone conversion
# ---------------------------------------------------------------------------

class TestUtcToLocalStr:
    def test_new_york_winter_est(self):
        # 2019-02-18 20:28:11 UTC → 2019-02-18 15:28:11 EST (UTC-5)
        result = utc_to_local_str(1550521691, lat=40.71, lon=-74.01)
        assert result == "2019:02:18 15:28:11"

    def test_new_york_summer_edt(self):
        # 2019-07-04 20:00:00 UTC → 2019-07-04 16:00:00 EDT (UTC-4)
        result = utc_to_local_str(1562270400, lat=40.71, lon=-74.01)
        assert result == "2019:07:04 16:00:00"

    def test_london_winter_utc(self):
        # London in winter is UTC+0
        result = utc_to_local_str(1550521691, lat=51.51, lon=-0.13)
        assert result == "2019:02:18 20:28:11"

    def test_london_summer_bst(self):
        # 2019-07-04 20:00:00 UTC → 2019-07-04 21:00:00 BST (UTC+1)
        result = utc_to_local_str(1562270400, lat=51.51, lon=-0.13)
        assert result == "2019:07:04 21:00:00"

    def test_no_gps_falls_back_to_utc(self):
        result = utc_to_local_str(1550521691, lat=0.0, lon=0.0)
        assert result == "2019:02:18 20:28:11"
