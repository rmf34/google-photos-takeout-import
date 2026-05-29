"""
Integration tests for run_exiftool_batch and build_exiftool_entry.

These tests call real exiftool and are skipped automatically if it is not
installed. Add it with:  sudo apt-get install -y libimage-exiftool-perl
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from fix_metadata import build_exiftool_entry, run_exiftool_batch

pytestmark = pytest.mark.skipif(shutil.which("exiftool") is None, reason="exiftool not installed")

# Minimal valid 1×1 images — enough for exiftool to read and write tags.
# Generated with Python's struct/zlib; verified against exiftool 12.76.
_MINIMAL_JPEG = (
    b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    b"\xff\xdb\x00C\x00" + b"\x08" * 64 + b"\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00"
    b"\xff\xc4\x00\x14\x00\x00\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
    b"\x00\x00\x00\x00\x00\xff\xda\x00\x08\x01\x01\x00\x00?\x00\x00\xff\xd9"
)

_MINIMAL_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xff\xff?"
    b"\x00\x05\xfe\x02\xfe\r\xefF\xb8\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _read_tags(path: Path, numeric: bool = False) -> dict:
    cmd = ["exiftool", "-j"] + (["-n"] if numeric else []) + [str(path)]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return json.loads(result.stdout)[0]


class TestExiftoolBatchJpeg:
    def test_writes_datetime(self, tmp_path):
        photo = tmp_path / "photo.jpg"
        photo.write_bytes(_MINIMAL_JPEG)

        entry = build_exiftool_entry(photo, {"datetime": "2023:06:15 14:30:00"})
        ok, err = run_exiftool_batch([entry])

        assert ok == 1 and err == 0
        tags = _read_tags(photo)
        assert tags["DateTimeOriginal"] == "2023:06:15 14:30:00"
        assert tags["CreateDate"] == "2023:06:15 14:30:00"

    def test_writes_gps(self, tmp_path):
        photo = tmp_path / "photo.jpg"
        photo.write_bytes(_MINIMAL_JPEG)

        metadata = {
            "gps": {
                "lat": 40.7128,
                "lat_ref": "N",
                "lon": 74.006,
                "lon_ref": "W",
                "alt": 10.0,
                "alt_ref": "Above Sea Level",
            },
            "ts_utc": 1686839400,
        }
        entry = build_exiftool_entry(photo, metadata)
        ok, err = run_exiftool_batch([entry])

        assert ok == 1 and err == 0
        tags = _read_tags(photo, numeric=True)
        assert abs(tags["GPSLatitude"] - 40.7128) < 0.0001
        assert tags["GPSLatitudeRef"] == "N"
        assert abs(abs(tags["GPSLongitude"]) - 74.006) < 0.0001
        assert tags["GPSLongitudeRef"] == "W"
        assert tags["GPSAltitude"] == pytest.approx(10.0)

    def test_writes_description(self, tmp_path):
        photo = tmp_path / "photo.jpg"
        photo.write_bytes(_MINIMAL_JPEG)

        entry = build_exiftool_entry(photo, {"description": "Sunset at the beach"})
        ok, err = run_exiftool_batch([entry])

        assert ok == 1 and err == 0
        tags = _read_tags(photo)
        assert tags["ImageDescription"] == "Sunset at the beach"

    def test_batch_multiple_files(self, tmp_path):
        photos = [tmp_path / f"photo_{i:02d}.jpg" for i in range(3)]
        for p in photos:
            p.write_bytes(_MINIMAL_JPEG)

        entries = [
            build_exiftool_entry(p, {"datetime": f"2023:0{i + 1}:01 12:00:00"})
            for i, p in enumerate(photos)
        ]
        ok, err = run_exiftool_batch(entries)

        assert ok == 3 and err == 0
        for i, p in enumerate(photos):
            assert _read_tags(p)["DateTimeOriginal"] == f"2023:0{i + 1}:01 12:00:00"

    def test_empty_entries(self):
        ok, err = run_exiftool_batch([])
        assert ok == 0 and err == 0


class TestExiftoolBatchPng:
    def test_writes_xmp_datetime(self, tmp_path):
        photo = tmp_path / "photo.png"
        photo.write_bytes(_MINIMAL_PNG)

        entry = build_exiftool_entry(photo, {"datetime": "2023:06:15 14:30:00"})
        ok, err = run_exiftool_batch([entry])

        assert ok == 1 and err == 0
        tags = _read_tags(photo)
        # PNG datetime is written via XMP-exif — exiftool composites it as DateTimeOriginal
        assert tags["DateTimeOriginal"] == "2023:06:15 14:30:00"
        assert tags["CreateDate"] == "2023:06:15 14:30:00"
