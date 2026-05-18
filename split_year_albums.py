#!/usr/bin/env python3
"""
Split 'Photos from YEAR' albums into 'Photos from YYYY-MM' monthly sub-albums.

Reads each photo's JSON sidecar to find the month from photoTakenTime,
then moves the photo and its sidecar(s) into Photos from YYYY-MM/.
Photos with no usable timestamp go into Photos from YYYY-unknown/.

Run AFTER extract_and_stage.py and BEFORE precheck.py / fix_metadata.py.

Usage:
    python split_year_albums.py [--dry-run]
"""

import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from fix_metadata import _DUPE_RE, MEDIA_EXTS, PHOTOS_DIR, SIDECAR_SUFFIXES

YEAR_RE = re.compile(r"^Photos from (\d{4})$")
DRY_RUN = "--dry-run" in sys.argv


def year_albums() -> list[tuple[Path, str]]:
    if not PHOTOS_DIR.exists():
        return []
    result = []
    for d in sorted(PHOTOS_DIR.iterdir()):
        if d.is_dir():
            m = YEAR_RE.match(d.name)
            if m:
                result.append((d, m.group(1)))
    return result


def _parse_month(sidecar: Path) -> str | None:
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        ts_str = data.get("photoTakenTime", {}).get("timestamp")
        if not ts_str:
            return None
        ts = int(ts_str)
        if ts <= 0:
            return None
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        return dt.strftime("%Y-%m")
    except Exception:
        return None


def _dupe_candidates(name: str, parent: Path) -> list[Path]:
    """For STEM(N).EXT, return candidate sidecar paths STEM.EXT.suffix_base(N).json."""
    m = _DUPE_RE.match(name)
    if not m:
        return []
    stem, dupe_n, ext = m.group(1), m.group(2), m.group(3)
    base = stem + ext
    return [
        parent / (base + suffix[: -len(".json")] + dupe_n + ".json") for suffix in SIDECAR_SUFFIXES
    ]


def get_month(media_path: Path, json_set: set[Path]) -> str | None:
    """Return 'YYYY-MM' from the photo's sidecar. json_set is a pre-scanned set."""
    name = media_path.name
    parent = media_path.parent

    for suffix in SIDECAR_SUFFIXES:
        c = parent / (name + suffix)
        if c in json_set:
            return _parse_month(c)

    # Handle Google Takeout duplicate naming: STEM(N).EXT → STEM.EXT.suffix_base(N).json
    for c in _dupe_candidates(name, parent):
        if c in json_set:
            return _parse_month(c)

    # Fuzzy fallback for Google's unusual truncation lengths
    prefix = name[: min(len(name), 40)]
    for c in json_set:
        if c.name.startswith(prefix):
            return _parse_month(c)

    return None


def find_sidecars(media_path: Path, json_set: set[Path]) -> list[Path]:
    """Return all sidecar files for this media file. Uses pre-scanned json_set."""
    sidecars = []
    seen: set[Path] = set()
    name = media_path.name
    parent = media_path.parent

    for suffix in SIDECAR_SUFFIXES:
        c = parent / (name + suffix)
        if c in json_set and c not in seen:
            sidecars.append(c)
            seen.add(c)

    # Handle Google Takeout duplicate naming: STEM(N).EXT → STEM.EXT.suffix_base(N).json
    for c in _dupe_candidates(name, parent):
        if c in json_set and c not in seen:
            sidecars.append(c)
            seen.add(c)

    # O(n) fuzzy scan only when direct + dupe matching found nothing.
    # Skipping this for matched files eliminates O(n²) on large albums.
    if not sidecars:
        prefix = name[: min(len(name), 40)]
        for c in json_set:
            if c.name.startswith(prefix) and c not in seen:
                sidecars.append(c)
                seen.add(c)

    return sidecars


def safe_move(src: Path, dst_dir: Path) -> bool:
    """Move src into dst_dir. Returns False on collision. dst_dir must already exist."""
    dst = dst_dir / src.name
    if dst == src:
        return True
    if dst.exists():
        return False
    if not DRY_RUN:
        src.rename(dst)
    return True


def split_album(album_dir: Path, year: str) -> dict:
    stats = {"total": 0, "moved": 0, "no_month": 0, "collision": 0}

    # Precompute directory listing once — avoids repeated iterdir() calls
    all_entries = list(album_dir.iterdir())
    json_set = {f for f in all_entries if f.is_file() and f.suffix == ".json"}
    media_files = sorted(f for f in all_entries if f.is_file() and f.suffix.lower() in MEDIA_EXTS)

    stats["total"] = len(media_files)
    if not media_files:
        return stats

    # Pre-parse every sidecar's month upfront — eliminates per-file JSON reads
    # in the main loop (one sequential pass over all JSONs instead of random access).
    sidecar_months: dict[Path, str | None] = {sc: _parse_month(sc) for sc in json_set}

    # Determine all destination dirs and pre-create them — avoids 60k mkdir
    # syscalls inside the loop (at most 13 dirs: 12 months + unknown).
    months_needed = set(sidecar_months.values())
    for m in months_needed:
        subdir = f"Photos from {m}" if m else f"Photos from {year}-unknown"
        if not DRY_RUN:
            (PHOTOS_DIR / subdir).mkdir(parents=True, exist_ok=True)
    if not DRY_RUN:
        (PHOTOS_DIR / f"Photos from {year}-unknown").mkdir(parents=True, exist_ok=True)

    start = time.monotonic()
    last_print = 0.0

    for i, media in enumerate(media_files):
        sidecars = find_sidecars(media, json_set)
        month = sidecar_months[sidecars[0]] if sidecars else None
        subdir = f"Photos from {month}" if month else f"Photos from {year}-unknown"
        dst_dir = PHOTOS_DIR / subdir

        if not safe_move(media, dst_dir):
            stats["collision"] += 1
            continue

        # Remove moved files from json_set so they aren't double-matched
        for sc in sidecars:
            safe_move(sc, dst_dir)
            json_set.discard(sc)

        stats["moved"] += 1
        if month is None:
            stats["no_month"] += 1

        now = time.monotonic()
        if now - last_print >= 0.25 or i == stats["total"] - 1:
            last_print = now
            elapsed = now - start
            rate = (i + 1) / elapsed if elapsed > 0 else 0
            pct = (i + 1) / stats["total"] * 100
            bar_w = 20
            filled = int(bar_w * pct / 100)
            bar = "█" * filled + "░" * (bar_w - filled)
            print(
                f"\r  [{bar}] {pct:5.1f}%  {i + 1:,}/{stats['total']:,}  {rate:.0f}/s",
                end="",
                flush=True,
            )

    print()
    return stats


def main():
    if not PHOTOS_DIR.exists():
        print(f"ERROR: {PHOTOS_DIR} not found. Run extract_and_stage.py first.")
        sys.exit(1)

    albums = year_albums()
    if not albums:
        print("No 'Photos from YEAR' albums found — nothing to split.")
        sys.exit(0)

    if DRY_RUN:
        print("DRY RUN — no files will be moved\n")

    print(f"Found {len(albums)} year album(s) to split:\n")
    total_media = 0
    for album_dir, year in albums:
        n = sum(1 for f in album_dir.iterdir() if f.is_file() and f.suffix.lower() in MEDIA_EXTS)
        total_media += n
        print(f"  {album_dir.name}  ({n:,} media files)")
    print(f"\n  Total: {total_media:,} media files\n")

    overall_stats = {"total": 0, "moved": 0, "no_month": 0, "collision": 0}
    overall_start = time.monotonic()

    for album_dir, year in albums:
        print(f"Splitting {album_dir.name} ...")
        stats = split_album(album_dir, year)
        for k in overall_stats:
            overall_stats[k] += stats[k]

        line = f"  moved={stats['moved']:,}"
        if stats["no_month"]:
            line += f"  no_month={stats['no_month']:,}"
        if stats["collision"]:
            line += f"  collisions={stats['collision']:,}"
        print(line)

        # Clean up original directory if all media was moved out
        if not DRY_RUN and album_dir.exists():
            remaining = list(album_dir.iterdir())
            remaining_media = [
                f for f in remaining if f.is_file() and f.suffix.lower() in MEDIA_EXTS
            ]
            if remaining_media:
                print(f"  WARN: {len(remaining_media):,} media file(s) still in {album_dir.name}")
            elif not remaining:
                album_dir.rmdir()
            else:
                non_media = [f.name for f in remaining[:5]]
                print(
                    f"  {len(remaining)} non-media file(s) left in {album_dir.name} (e.g. {', '.join(non_media)})"
                )
        print()

    elapsed = time.monotonic() - overall_start
    print("=" * 55)
    print(f"Total media files    : {overall_stats['total']:,}")
    print(f"Moved                : {overall_stats['moved']:,}")
    print(f"No month (→ unknown) : {overall_stats['no_month']:,}")
    print(f"Collisions (skipped) : {overall_stats['collision']:,}")
    print(f"Elapsed              : {elapsed / 60:.1f} min")
    if DRY_RUN:
        print("\nDRY RUN complete — re-run without --dry-run to apply changes.")
    else:
        print("\nDone. Run precheck.py to verify, then fix_metadata.py.")


if __name__ == "__main__":
    main()
