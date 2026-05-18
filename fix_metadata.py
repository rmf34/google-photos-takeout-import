#!/usr/bin/env python3
"""
Write correct dates, GPS, and captions back into photo/video EXIF
from Google Takeout JSON sidecar files.

Timezone handling:
  - If the photo has GPS coordinates, the local timezone is looked up
    from those coordinates (requires timezonefinder in the venv).
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
import json
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DATA_DIR = Path("~/photos")
PHOTOS_DIR = DATA_DIR / "staged" / "Takeout" / "Google Photos"
SCRIPT_DIR = Path(__file__).parent
VENV_PYTHON = SCRIPT_DIR / "venv" / "bin" / "python"

MEDIA_EXTS = {
    ".jpg", ".jpeg", ".heic", ".heif",
    ".png", ".gif", ".bmp", ".tiff", ".tif", ".webp",
    ".mp4", ".mov", ".avi", ".mkv", ".m4v", ".3gp", ".wmv", ".mpg", ".mpeg",
}

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".3gp", ".avi", ".mkv", ".wmv", ".mpg", ".mpeg"}

SIDECAR_SUFFIXES = [
    ".supplemental-metadata.json",
    ".supple.json",
    ".suppl.json",
    ".supp.json",
    ".sup.json",
    ".json",
]

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


def utc_to_local_str(ts: int, lat: float, lon: float) -> str:
    """
    Convert a UTC Unix timestamp to a local datetime string for EXIF.
    Uses GPS coordinates to look up the timezone when possible.
    Falls back to UTC (dates correct; hour may be off by UTC offset).
    """
    dt_utc = datetime.fromtimestamp(ts, tz=timezone.utc)
    tf = _get_tz_finder()

    if tf and (abs(lat) > 0.001 or abs(lon) > 0.001):
        try:
            tz_name = tf.timezone_at(lat=lat, lng=lon)
            if tz_name:
                from zoneinfo import ZoneInfo
                dt_local = dt_utc.astimezone(ZoneInfo(tz_name))
                return dt_local.strftime("%Y:%m:%d %H:%M:%S")
        except Exception:
            pass

    # UTC fallback — note this in stats
    return dt_utc.strftime("%Y:%m:%d %H:%M:%S")


def find_sidecar(media_path: Path) -> Optional[Path]:
    name = media_path.name
    parent = media_path.parent

    for suffix in SIDECAR_SUFFIXES:
        c = parent / (name + suffix)
        if c.exists():
            return c

    # Fuzzy: any .json starting with first 40 chars of filename
    # Handles unusual Google truncation lengths
    prefix = name[:min(len(name), 40)]
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


def run_exiftool_batch(entries: list[dict]) -> tuple[int, int]:
    if not entries:
        return 0, 0
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as f:
        json.dump(entries, f)
        tmp_path = f.name
    try:
        result = subprocess.run(
            ["exiftool", f"-json={tmp_path}", "-overwrite_original", "-m", "-q"],
            capture_output=True,
            text=True,
        )
        stderr_errors = result.stderr.count("Error")
        stdout_errors = result.stdout.count("Error")
        errors = stderr_errors + stdout_errors
        return max(0, len(entries) - errors), errors
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
        eta_str = f"{eta/60:.0f}m {eta%60:.0f}s" if eta > 60 else f"{eta:.0f}s"
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
            f"\r[{'█'*25}] 100.0%  {self.done:,}/{self.total:,}  "
            f"{rate:.1f}/s  {elapsed/60:.1f} min total".ljust(90),
            flush=True,
        )
        print()


def collect_media_files(photos_dir: Path) -> list[Path]:
    return sorted(
        f for f in photos_dir.rglob("*")
        if f.is_file() and f.suffix.lower() in MEDIA_EXTS
    )


def main():
    if not PHOTOS_DIR.exists():
        print(f"ERROR: {PHOTOS_DIR} not found. Run extract_and_stage.py first.")
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

    print(f"\nScanning {PHOTOS_DIR} ...")
    all_files = collect_media_files(PHOTOS_DIR)
    print(f"Found {len(all_files):,} media files\n")

    stats = {"ok": 0, "no_sidecar": 0, "bad_sidecar": 0, "exiftool_error": 0}
    prog = Progress(len(all_files))

    # Process album by album so exiftool batches stay manageable
    albums: dict[Path, list[Path]] = {}
    for f in all_files:
        albums.setdefault(f.parent, []).append(f)

    for album_dir, files in sorted(albums.items()):
        album_name = album_dir.name
        entries = []

        for f in files:
            sidecar = find_sidecar(f)
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

        ok, err = run_exiftool_batch(entries)
        stats["ok"] += ok
        stats["exiftool_error"] += err

    prog.finish()

    total = len(all_files)
    print(f"{'='*55}")
    print(f"Total media files    : {total:,}")
    print(f"Metadata fixed       : {stats['ok']:,}")
    print(f"No sidecar found     : {stats['no_sidecar']:,}")
    print(f"Bad/empty sidecar    : {stats['bad_sidecar']:,}")
    print(f"Exiftool errors      : {stats['exiftool_error']:,}")

    if stats["no_sidecar"] > 0:
        print(f"\nNote: {stats['no_sidecar']:,} files had no sidecar — they keep whatever")
        print("metadata (or lack of) was in the original file.")
    print("\nReady for upload.")


if __name__ == "__main__":
    main()
