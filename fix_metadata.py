#!/usr/bin/env python3
"""
Write correct dates, GPS, and captions back into photo/video EXIF
from Google Takeout JSON sidecar files.

Timezone handling:
  - If the photo has GPS coordinates, the local timezone is looked up
    from those coordinates (requires timezonefinder — install via uv sync).
  - Without GPS, the UTC timestamp from the JSON is written as-is.
    Dates will be correct; the clock time may be off by your UTC offset.

GPS:
  - Prefers geoDataExif (original camera GPS) over geoData (Google's copy).

Captions:
  - Writes the 'description' field from the JSON to ImageDescription and
    XMP-dc:Description.

Camera tags (make, model, aperture, ISO, etc.) are already in the image
files and are never touched by this script.

Safe to re-run — exiftool is idempotent on already-correct files.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Optional

SCRIPT_DIR = Path(__file__).parent
PHOTOS_DIR = (
    Path(os.environ.get("TAKEOUT_DATA_DIR") or ".") / "staged" / "Takeout" / "Google Photos"
)

MEDIA_EXTS = {
    ".jpg",
    ".jpeg",
    ".heic",
    ".heif",
    ".heics",
    ".png",
    ".gif",
    ".bmp",
    ".tiff",
    ".tif",
    ".webp",
    ".jp2",
    ".avif",
    ".jxl",
    ".mp4",
    ".mov",
    ".avi",
    ".mkv",
    ".m4v",
    ".3gp",
    ".3g2",
    ".wmv",
    ".mpg",
    ".mpeg",
    ".webm",
}

VIDEO_EXTS = {
    ".mp4",
    ".mov",
    ".m4v",
    ".3gp",
    ".3g2",
    ".avi",
    ".mkv",
    ".wmv",
    ".mpg",
    ".mpeg",
    ".webm",
}

SIDECAR_SUFFIXES = [
    ".supplemental-metadata.json",
    ".supple.json",
    ".suppl.json",
    ".supp.json",
    ".sup.json",
    ".json",
]

# Google Takeout duplicate naming: STEM(N).EXT → STEM.EXT.suffix_base(N).json
_DUPE_RE = re.compile(r"^(.*?)(\(\d+\))(\.[^.]+)$")

# Timezone finder — loaded once if available
_tf = None


def _get_tz_finder():
    global _tf
    if _tf is not None:
        return _tf
    try:
        from timezonefinder import TimezoneFinder

        _tf = TimezoneFinder()
    except ImportError:
        _tf = False
    return _tf


# Timezone name cache keyed by (lat rounded to 0.1°, lon rounded to 0.1°).
# Photos in the same album are almost always in the same city (~10 km precision
# is more than enough for timezone resolution).
_tz_cache: dict[tuple[float, float], str | None] = {}


def utc_to_local_str(ts: int, lat: float, lon: float) -> str:
    """
    Convert a UTC Unix timestamp to a local datetime string for EXIF.
    Uses GPS coordinates to look up the timezone when possible.
    Falls back to UTC (dates correct; hour may be off by UTC offset).
    """
    dt_utc = datetime.fromtimestamp(ts, tz=timezone.utc)
    tf = _get_tz_finder()

    if tf and (abs(lat) > 0.001 or abs(lon) > 0.001):
        key = (round(lat, 1), round(lon, 1))
        if key not in _tz_cache:
            try:
                _tz_cache[key] = tf.timezone_at(lat=lat, lng=lon)
            except Exception:
                _tz_cache[key] = None
        tz_name = _tz_cache[key]
        if tz_name:
            try:
                from zoneinfo import ZoneInfo

                dt_local = dt_utc.astimezone(ZoneInfo(tz_name))
                return dt_local.strftime("%Y:%m:%d %H:%M:%S")
            except Exception:
                pass

    return dt_utc.strftime("%Y:%m:%d %H:%M:%S")


def find_sidecar(media_path: Path, json_set: Optional[set[Path]] = None) -> Optional[Path]:
    name = media_path.name
    parent = media_path.parent

    if json_set is not None:
        # Fast path: O(1) set membership instead of stat() per suffix.
        # Caller pre-scanned the album directory once; we reuse it here.
        for suffix in SIDECAR_SUFFIXES:
            c = parent / (name + suffix)
            if c in json_set:
                return c

        m = _DUPE_RE.match(name)
        if m:
            stem, dupe_n, ext = m.group(1), m.group(2), m.group(3)
            base = stem + ext
            for suffix in SIDECAR_SUFFIXES:
                suffix_base = suffix[: -len(".json")]
                c = parent / (base + suffix_base + dupe_n + ".json")
                if c in json_set:
                    return c

        prefix = name[: min(len(name), 40)]
        for c in json_set:
            if c.name.startswith(prefix):
                return c

        return None

    # Slow path: individual stat() calls — used by precheck.py on single files.
    for suffix in SIDECAR_SUFFIXES:
        c = parent / (name + suffix)
        if c.exists():
            return c

    m = _DUPE_RE.match(name)
    if m:
        stem, dupe_n, ext = m.group(1), m.group(2), m.group(3)
        base = stem + ext
        for suffix in SIDECAR_SUFFIXES:
            suffix_base = suffix[: -len(".json")]
            c = parent / (base + suffix_base + dupe_n + ".json")
            if c.exists():
                return c

    prefix = name[: min(len(name), 40)]
    for c in parent.iterdir():
        if c.name.startswith(prefix) and c.suffix == ".json" and c != media_path:
            return c

    return None


def parse_sidecar(json_path: Path) -> dict:
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except Exception:
        return {}

    result = {}

    # GPS — prefer geoDataExif (original camera GPS), fall back to geoData
    for geo_key in ("geoDataExif", "geoData"):
        geo = data.get(geo_key, {})
        lat = float(geo.get("latitude", 0))
        lon = float(geo.get("longitude", 0))
        alt = float(geo.get("altitude", 0))
        if abs(lat) > 0.001 or abs(lon) > 0.001:
            result["gps"] = {
                "lat": abs(lat),
                "lat_ref": "N" if lat >= 0 else "S",
                "lon": abs(lon),
                "lon_ref": "E" if lon >= 0 else "W",
                "alt": abs(alt),
                "alt_ref": "Above Sea Level" if alt >= 0 else "Below Sea Level",
            }
            break

    # Timestamp — convert to local time using GPS if available
    taken = data.get("photoTakenTime", {})
    ts_str = taken.get("timestamp")
    if ts_str:
        try:
            ts = int(ts_str)
            if ts <= 0:
                raise ValueError("epoch or negative timestamp — treat as missing")
            gps = result.get("gps", {})
            lat = gps.get("lat", 0) * (1 if gps.get("lat_ref") == "N" else -1)
            lon = gps.get("lon", 0) * (1 if gps.get("lon_ref") == "E" else -1)
            result["datetime"] = utc_to_local_str(ts, lat, lon)
            result["ts_utc"] = ts
        except (ValueError, OSError):
            pass

    # Description/caption
    desc = data.get("description", "").strip()
    if desc:
        result["description"] = desc

    return result


def build_exiftool_entry(media_path: Path, metadata: dict) -> Optional[dict]:
    if not metadata:
        return None

    entry: dict = {"SourceFile": str(media_path)}
    ext = media_path.suffix.lower()

    if "datetime" in metadata:
        dt = metadata["datetime"]
        if ext in VIDEO_EXTS:
            entry["QuickTime:CreateDate"] = dt
            entry["QuickTime:ModifyDate"] = dt
            entry["TrackCreateDate"] = dt
            entry["TrackModifyDate"] = dt
            entry["MediaCreateDate"] = dt
            entry["MediaModifyDate"] = dt
        elif ext == ".png":
            entry["XMP-exif:DateTimeOriginal"] = dt
            entry["XMP-xmp:CreateDate"] = dt
            entry["PNG:CreationTime"] = dt
        else:
            # JPEG, HEIC, TIFF, etc.
            entry["DateTimeOriginal"] = dt
            entry["CreateDate"] = dt
            entry["ModifyDate"] = dt

    if "gps" in metadata:
        gps = metadata["gps"]
        entry["GPSLatitude"] = gps["lat"]
        entry["GPSLatitudeRef"] = gps["lat_ref"]
        entry["GPSLongitude"] = gps["lon"]
        entry["GPSLongitudeRef"] = gps["lon_ref"]
        entry["GPSAltitude"] = gps["alt"]
        entry["GPSAltitudeRef"] = gps["alt_ref"]
        # GPS date/time are always UTC by EXIF spec — separate from DateTimeOriginal
        if "ts_utc" in metadata:
            dt_utc = datetime.fromtimestamp(metadata["ts_utc"], tz=timezone.utc)
            entry["GPSDateStamp"] = dt_utc.strftime("%Y:%m:%d")
            entry["GPSTimeStamp"] = dt_utc.strftime("%H:%M:%S")

    if "description" in metadata:
        entry["ImageDescription"] = metadata["description"]
        entry["XMP-dc:Description"] = metadata["description"]

    return entry if len(entry) > 1 else None


def run_exiftool_batch(entries: list[dict], error_log: "IO[str] | None" = None) -> tuple[int, int]:
    if not entries:
        return 0, 0

    # Chunk to stay under ARG_MAX — each path is at most ~200 chars;
    # stay safely below the 3.2 MB limit with 10k files per call.
    CHUNK = 10_000
    if len(entries) > CHUNK:
        total_ok = total_err = 0
        for start in range(0, len(entries), CHUNK):
            ok, err = run_exiftool_batch(entries[start : start + CHUNK], error_log)
            total_ok += ok
            total_err += err
        return total_ok, total_err

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(entries, f)
        tmp_path = f.name
    try:
        files = [e["SourceFile"] for e in entries]
        result = subprocess.run(
            ["exiftool", f"-json={tmp_path}", "-overwrite_original", "-m"] + files,
            capture_output=True,
            text=True,
        )
        # Parse "N image files updated" from both stdout and stderr — exiftool
        # outputs this to stdout with -overwrite_original, stderr otherwise.
        # Note: exiftool exits 1 when ANY file fails (e.g. unsupported MKV/WebM),
        # even if others succeeded. Always parse output — never bail on returncode.
        updated = 0
        combined = result.stdout + "\n" + result.stderr
        for line in combined.splitlines():
            if "image files updated" in line or "image files unchanged" in line:
                try:
                    updated += int(line.strip().split()[0])
                except ValueError:
                    pass
            elif line.startswith("Error:") and error_log is not None:
                error_log.write(line + "\n")
        # Fall back to entry count minus errors if parsing fails
        if updated == 0 and result.returncode == 0:
            updated = len(entries)
        errors = len(entries) - updated
        return updated, max(0, errors)
    finally:
        Path(tmp_path).unlink(missing_ok=True)


class Progress:
    def __init__(self, total_files: int):
        self.total = total_files
        self.done = 0
        self.start = time.monotonic()
        self.last_print = 0.0

    def update(self, n: int = 1, album: str = ""):
        self.done += n
        now = time.monotonic()
        if now - self.last_print < 0.25:
            return
        self.last_print = now
        self._render(album)

    def _render(self, album: str = ""):
        elapsed = time.monotonic() - self.start
        rate = self.done / elapsed if elapsed > 0 else 0
        pct = self.done / self.total * 100 if self.total else 0
        eta = (self.total - self.done) / rate if rate > 0 else 0
        eta_str = f"{eta / 60:.0f}m {eta % 60:.0f}s" if eta > 60 else f"{eta:.0f}s"
        bar_w = 25
        filled = int(bar_w * pct / 100)
        bar = "█" * filled + "░" * (bar_w - filled)
        album_trunc = (album[:28] + "…") if len(album) > 29 else album.ljust(29)
        line = (
            f"\r[{bar}] {pct:5.1f}%  "
            f"{self.done:,}/{self.total:,}  "
            f"{rate:5.1f}/s  "
            f"ETA {eta_str}  "
            f"{album_trunc}"
        )
        print(line, end="", flush=True)

    def finish(self):
        elapsed = time.monotonic() - self.start
        rate = self.done / elapsed if elapsed > 0 else 0
        print(
            f"\r[{'█' * 25}] 100.0%  {self.done:,}/{self.total:,}  "
            f"{rate:.1f}/s  {elapsed / 60:.1f} min total".ljust(90),
            flush=True,
        )
        print()


def collect_media_files(photos_dir: Path) -> list[Path]:
    return sorted(
        f for f in photos_dir.rglob("*") if f.is_file() and f.suffix.lower() in MEDIA_EXTS
    )


def main():
    parser = argparse.ArgumentParser(
        description="Write EXIF metadata from Google Takeout JSON sidecars."
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(os.environ.get("TAKEOUT_DATA_DIR") or "."),
        help="Root data directory containing staged/Takeout/Google Photos (env: TAKEOUT_DATA_DIR)",
    )
    args = parser.parse_args()
    photos_dir = args.data_dir / "staged" / "Takeout" / "Google Photos"

    if not photos_dir.exists():
        print(f"ERROR: {photos_dir} not found. Run extract_and_stage.py first.")
        sys.exit(1)

    try:
        subprocess.run(["exiftool", "-ver"], capture_output=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("ERROR: exiftool not found. Install with: sudo apt install libimage-exiftool-perl")
        sys.exit(1)

    tf = _get_tz_finder()
    if tf:
        print("✓ timezonefinder loaded — dates will use local timezone from GPS when available")
    else:
        print("⚠  timezonefinder not found — dates will be written in UTC")
        print("   (dates will be correct; clock time may be off by your UTC offset)")

    print(f"\nScanning {photos_dir} ...")
    all_files = collect_media_files(photos_dir)
    print(f"Found {len(all_files):,} media files\n")

    stats = {"ok": 0, "no_sidecar": 0, "bad_sidecar": 0, "exiftool_error": 0}
    prog = Progress(len(all_files))

    error_log_path = SCRIPT_DIR / "fix_metadata_errors.log"

    with open(error_log_path, "w", encoding="utf-8") as error_log:
        # Process album by album so exiftool batches stay manageable
        albums: dict[Path, list[Path]] = {}
        for f in all_files:
            albums.setdefault(f.parent, []).append(f)

        for album_dir, files in sorted(albums.items()):
            album_name = album_dir.name
            json_set = {f for f in album_dir.iterdir() if f.suffix == ".json"}
            entries = []

            for f in files:
                sidecar = find_sidecar(f, json_set)
                if not sidecar:
                    stats["no_sidecar"] += 1
                    prog.update(album=album_name)
                    continue

                metadata = parse_sidecar(sidecar)
                if not metadata:
                    stats["bad_sidecar"] += 1
                    prog.update(album=album_name)
                    continue

                entry = build_exiftool_entry(f, metadata)
                if entry:
                    entries.append(entry)
                else:
                    stats["no_sidecar"] += 1
                prog.update(album=album_name)

            ok, err = run_exiftool_batch(entries, error_log)
            stats["ok"] += ok
            stats["exiftool_error"] += err

    prog.finish()

    total = len(all_files)
    print(f"{'=' * 55}")
    print(f"Total media files    : {total:,}")
    print(f"Metadata fixed       : {stats['ok']:,}")
    print(f"No sidecar found     : {stats['no_sidecar']:,}")
    print(f"Bad/empty sidecar    : {stats['bad_sidecar']:,}")
    print(f"Exiftool errors      : {stats['exiftool_error']:,}")

    if stats["exiftool_error"] > 0:
        print(f"\nError details: {error_log_path}")
        with open(error_log_path, encoding="utf-8") as lf:
            lines = lf.readlines()
        # Bucket by error type (first ~60 chars before the path)
        buckets: Counter = Counter()
        for line in lines:
            msg = line.split(" - ")[0].strip()
            buckets[msg] += 1
        print("  Error breakdown:")
        for msg, count in buckets.most_common(10):
            print(f"    {count:6,}  {msg}")

    if stats["no_sidecar"] > 0:
        print(f"\nNote: {stats['no_sidecar']:,} files had no sidecar — they keep whatever")
        print("metadata (or lack of) was in the original file.")
    print("\nReady for upload.")


if __name__ == "__main__":
    main()
