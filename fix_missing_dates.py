#!/usr/bin/env python3
"""
Fix media files that are missing DateTimeOriginal.

Three categories are handled:
  1. '-edited' files: copy date from the original (non-edited) counterpart.
  2. Fake-extension files: JPEG content with .PNG/.HEIC extension got tagged
     with wrong EXIF groups — re-tag using temp-rename to bypass exiftool's
     extension check.
  3. Remaining: derive date from 'Photos from YYYY-MM' directory name.

Run AFTER fix_metadata.py.  Generates a list of fixed files so you can
selectively delete/re-upload them in Google Photos.

Usage:
    uv run python fix_missing_dates.py [--dry-run]
"""

import json
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from fix_metadata import (
    MEDIA_EXTS,
    PHOTOS_DIR,
    VIDEO_EXTS,
    find_sidecar,
    parse_sidecar,
    run_exiftool_batch,
)

SCRIPT_DIR = Path(__file__).parent
JPEG_MAGIC = b"\xff\xd8\xff"
DIR_DATE_RE = re.compile(r"Photos from (\d{4})-(\d{2})")
EDITED_RE = re.compile(r"-edited(?=\.[^.]+$)")


def detect_actual_type(path: Path) -> str:
    """Return 'jpeg' if the file starts with JPEG magic, else 'other'."""
    try:
        with open(path, "rb") as f:
            return "jpeg" if f.read(3) == JPEG_MAGIC else "other"
    except OSError:
        return "other"


def batch_read_dates(paths: list[Path]) -> dict[Path, str]:
    """Read DateTimeOriginal/CreateDate from multiple files in one exiftool call."""
    if not paths:
        return {}

    result: dict[Path, str] = {}
    CHUNK = 500
    for start in range(0, len(paths), CHUNK):
        batch = paths[start : start + CHUNK]
        try:
            r = subprocess.run(
                [
                    "exiftool",
                    "-json",
                    "-DateTimeOriginal",
                    "-CreateDate",
                    "-QuickTime:CreateDate",
                    "-XMP:DateTimeOriginal",
                ]
                + [str(p) for p in batch],
                capture_output=True,
                text=True,
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            print(f"  WARNING: exiftool timed out reading dates for batch at offset {start}")
            continue

        if not r.stdout.strip():
            continue

        try:
            records = json.loads(r.stdout)
        except json.JSONDecodeError:
            print(f"  WARNING: exiftool returned invalid JSON for batch at offset {start}")
            continue

        date_re = re.compile(r"\d{4}:\d{2}:\d{2} \d{2}:\d{2}:\d{2}")
        for rec in records:
            src = Path(rec.get("SourceFile", ""))
            for key in (
                "DateTimeOriginal",
                "CreateDate",
                "QuickTime:CreateDate",
                "XMP:DateTimeOriginal",
            ):
                val = rec.get(key, "")
                if val and date_re.match(str(val)):
                    result[src] = str(val)
                    break

    return result


def date_from_directory(path: Path) -> str | None:
    """Derive a date from 'Photos from YYYY-MM' directory name."""
    m = DIR_DATE_RE.search(path.parent.name)
    if m:
        year, month = m.group(1), m.group(2)
        if month == "00" or not (1 <= int(month) <= 12):
            return f"{year}:06:15 12:00:00"
        return f"{year}:{month}:15 12:00:00"

    m2 = re.search(r"(\d{4})-unknown", path.parent.name)
    if m2:
        return f"{m2.group(1)}:06:15 12:00:00"

    m3 = re.match(r"(\d{4})_", path.name)
    if m3:
        return f"{m3.group(1)}:06:15 12:00:00"

    return None


def build_name_index(photos_dir: Path) -> dict[str, list[Path]]:
    """Build basename -> [paths] index for locating originals of edited files."""
    idx: dict[str, list[Path]] = defaultdict(list)
    for f in photos_dir.rglob("*"):
        if f.is_file() and f.suffix.lower() in MEDIA_EXTS:
            idx[f.name.lower()].append(f)
    return idx


def find_original_for_edited(edited_path: Path, name_index: dict[str, list[Path]]) -> Path | None:
    """Find the non-edited original for a '-edited' file."""
    base_name = EDITED_RE.sub("", edited_path.name)
    if base_name == edited_path.name:
        return None

    same_dir = edited_path.parent / base_name
    if same_dir.exists() and same_dir != edited_path:
        return same_dir

    candidates = name_index.get(base_name.lower(), [])
    for c in candidates:
        if c != edited_path and "-edited" not in c.name:
            return c

    return None


def exiftool_tags_for_file(path: Path, dt: str) -> dict:
    """Build the correct exiftool tag dict based on actual file type."""
    entry: dict = {"SourceFile": str(path)}
    ext = path.suffix.lower()
    actual = detect_actual_type(path)

    if actual == "jpeg" or ext in {".jpg", ".jpeg", ".heic", ".heif", ".tiff", ".tif"}:
        entry["DateTimeOriginal"] = dt
        entry["CreateDate"] = dt
        entry["ModifyDate"] = dt
    elif ext in VIDEO_EXTS:
        entry["QuickTime:CreateDate"] = dt
        entry["QuickTime:ModifyDate"] = dt
    elif ext == ".png":
        entry["XMP-exif:DateTimeOriginal"] = dt
        entry["XMP-xmp:CreateDate"] = dt
        entry["PNG:CreationTime"] = dt
    else:
        entry["DateTimeOriginal"] = dt
        entry["CreateDate"] = dt
        entry["ModifyDate"] = dt

    return entry


def fix_via_temp_rename(path: Path, dt: str) -> bool:
    """Fix a fake-extension JPEG by copying to a temp .jpg, writing EXIF, copying back."""
    tmp = Path(tempfile.gettempdir()) / f"exiffix_{path.stem}.jpg"
    try:
        shutil.copy2(str(path), str(tmp))
        r = subprocess.run(
            [
                "exiftool",
                "-overwrite_original",
                "-m",
                f"-DateTimeOriginal={dt}",
                f"-CreateDate={dt}",
                f"-ModifyDate={dt}",
                str(tmp),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        output = r.stdout + r.stderr
        if "updated" in output:
            shutil.copy2(str(tmp), str(path))
            return True
        return False
    except (OSError, subprocess.TimeoutExpired):
        return False
    finally:
        tmp.unlink(missing_ok=True)


def filter_missing_dates(candidates: list[Path]) -> list[Path]:
    """Batch-check which candidates actually lack date metadata."""
    missing = []
    skipped_errors = 0
    CHUNK = 500
    for start in range(0, len(candidates), CHUNK):
        batch = candidates[start : start + CHUNK]
        try:
            r = subprocess.run(
                [
                    "exiftool",
                    "-q",
                    "-m",
                    "-if",
                    "not $DateTimeOriginal and not $CreateDate "
                    "and not $QuickTime:CreateDate and not $XMP:DateTimeOriginal",
                    "-p",
                    "$directory/$filename",
                ]
                + [str(f) for f in batch],
                capture_output=True,
                text=True,
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            print(
                f"  WARNING: exiftool timed out on batch at offset {start}, "
                f"treating {len(batch)} files as missing dates"
            )
            missing.extend(batch)
            skipped_errors += len(batch)
            continue

        for line in r.stdout.strip().splitlines():
            line = line.strip()
            if line:
                missing.append(Path(line))

        error_lines = [err for err in r.stderr.splitlines() if err.startswith("Error:")]
        skipped_errors += len(error_lines)

    if skipped_errors > 0:
        print(f"  WARNING: {skipped_errors} exiftool errors during date check")

    return missing


def find_candidate_files(photos_dir: Path) -> list[Path]:
    """Find files likely to be missing dates — fast filesystem scan, no exiftool."""
    candidates = []
    for f in photos_dir.rglob("*"):
        if not f.is_file() or f.suffix.lower() not in MEDIA_EXTS:
            continue
        if "-edited" in f.name.lower():
            candidates.append(f)
            continue
        if f.suffix.lower() not in {".jpg", ".jpeg"} and detect_actual_type(f) == "jpeg":
            candidates.append(f)
    return candidates


def resolve_dates(
    missing: list[Path],
    name_index: dict[str, list[Path]],
) -> list[tuple[Path, str, str]]:
    """Resolve dates for files missing them.

    Returns [(path, datetime_str, source_label), ...] for files we can fix.
    Also returns stats via the returned list's source labels.
    """
    # Batch-read dates from all potential original files at once
    edited_files = [f for f in missing if "-edited" in f.name.lower()]
    originals = {}
    if edited_files:
        original_paths = []
        edited_to_original: dict[Path, Path] = {}
        for f in edited_files:
            orig = find_original_for_edited(f, name_index)
            if orig:
                edited_to_original[f] = orig
                original_paths.append(orig)

        print(f"  Reading dates from {len(original_paths):,} original files...")
        originals = batch_read_dates(original_paths)

    resolved: list[tuple[Path, str, str]] = []

    for f in missing:
        dt = None
        source = None

        if "-edited" in f.name.lower():
            orig = edited_to_original.get(f)
            if orig and orig in originals:
                dt = originals[orig]
                source = "edited_from_original"

        if not dt and detect_actual_type(f) == "jpeg" and f.suffix.lower() not in {".jpg", ".jpeg"}:
            sc = find_sidecar(f)
            if sc:
                meta = parse_sidecar(sc)
                if "datetime" in meta:
                    dt = meta["datetime"]
                    source = "fake_ext_sidecar"

        if not dt:
            dt = date_from_directory(f)
            if dt:
                source = "directory_name"

        if dt and source:
            resolved.append((f, dt, source))

    return resolved


def main():
    dry_run = "--dry-run" in sys.argv

    if not PHOTOS_DIR.exists():
        print(f"ERROR: {PHOTOS_DIR} not found.")
        sys.exit(1)

    if dry_run:
        print("DRY RUN — no files will be modified\n")

    # Step 1: find candidate files (fast filesystem scan)
    print("Scanning for -edited files and fake-extension files...")
    candidates = find_candidate_files(PHOTOS_DIR)
    n_edited = sum(1 for f in candidates if "-edited" in f.name.lower())
    n_fake_ext = len(candidates) - n_edited
    print(f"Found {len(candidates):,} candidates ({n_edited:,} edited, {n_fake_ext:,} fake-ext)\n")

    if not candidates:
        print("Nothing to fix!")
        return

    # Step 2: filter to only files actually missing dates
    print("Checking which files are actually missing dates...")
    missing = filter_missing_dates(candidates)
    print(f"{len(missing):,} of {len(candidates):,} are missing dates\n")

    if not missing:
        print("All candidate files already have dates — nothing to fix!")
        return

    # Step 3: build name index for cross-directory original lookups
    print("Building file name index...")
    name_index = build_name_index(PHOTOS_DIR)
    print(f"Indexed {sum(len(v) for v in name_index.values()):,} files\n")

    # Step 4: resolve dates for each file
    resolved = resolve_dates(missing, name_index)

    stats: Counter = Counter()
    for _, _, source in resolved:
        stats[source] += 1
    no_date_count = len(missing) - len(resolved)
    if no_date_count > 0:
        stats["no_date_found"] = no_date_count

    print(f"\nResolved dates for {len(resolved):,} files:")
    for source, count in stats.most_common():
        print(f"  {source:30s} {count:,}")

    if not resolved:
        print("\nNo dates could be resolved.")
        return

    # Step 5: write EXIF — split into normal batch and fake-ext temp-rename
    normal_entries = []
    fake_ext_files: list[tuple[Path, str]] = []

    for path, dt, source in resolved:
        is_fake_ext = detect_actual_type(path) == "jpeg" and path.suffix.lower() not in {
            ".jpg",
            ".jpeg",
        }
        if is_fake_ext:
            fake_ext_files.append((path, dt))
        else:
            normal_entries.append(exiftool_tags_for_file(path, dt))

    error_log_path = SCRIPT_DIR / "fix_missing_dates_errors.log"

    if not dry_run:
        total_ok = 0
        total_err = 0

        if normal_entries:
            print(f"\nWriting EXIF to {len(normal_entries):,} files (batch)...")
            with open(error_log_path, "w", encoding="utf-8") as error_log:
                ok, err = run_exiftool_batch(normal_entries, error_log)
            total_ok += ok
            total_err += err
            print(f"  Batch: {ok:,} updated, {err:,} errors")

        if fake_ext_files:
            print(f"Fixing {len(fake_ext_files):,} fake-extension files (temp-rename)...")
            for path, dt in fake_ext_files:
                if fix_via_temp_rename(path, dt):
                    total_ok += 1
                else:
                    total_err += 1
                    print(f"  FAILED: {path.name}")
            print(
                f"  Temp-rename: {len(fake_ext_files) - total_err:,} updated, "
                f"{total_err - (len(normal_entries) - (total_ok - (len(fake_ext_files) - total_err))):,} errors"
            )

        print(f"\nTotal: {total_ok:,} updated, {total_err:,} errors")
        if error_log_path.exists() and error_log_path.stat().st_size > 0:
            print(f"Error details: {error_log_path}")
    else:
        print(
            f"\nDRY RUN: would write EXIF to {len(normal_entries):,} files (batch)"
            f" + {len(fake_ext_files):,} files (temp-rename)"
        )

    # Step 6: write list of fixed files for selective re-upload
    fixed_list_path = SCRIPT_DIR / "fixed_files.txt"
    fixed_paths = [path for path, _, _ in resolved]
    if not dry_run:
        with open(fixed_list_path, "w") as fh:
            for path in fixed_paths:
                try:
                    rel = path.relative_to(PHOTOS_DIR)
                except ValueError:
                    rel = path
                fh.write(str(rel) + "\n")
        print(f"\nFixed file list: {fixed_list_path}")
        print(f"({len(fixed_paths):,} paths for selective re-upload)")
    else:
        print(f"\nDRY RUN: would save {len(fixed_paths):,} paths to {fixed_list_path}")

    if no_date_count > 0:
        print(
            f"\nWARNING: {no_date_count:,} files could not be dated "
            "— no original, sidecar, or directory hint."
        )


if __name__ == "__main__":
    main()
