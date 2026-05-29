#!/usr/bin/env python3
"""
Extract all Google Takeout ZIP files to the staging directory.
- Extracts ALL content including 'Photos from YEAR' albums
- Merges albums split across multiple ZIPs (won't overwrite existing files)
- Deletes each ZIP after successful extraction
- Shows live progress on a single updating line

Note: 'Photos from YEAR' folders are extracted but will not become albums
when uploading — those photos go into the library unsorted, which is correct
since they weren't in any named album originally.
"""

import argparse
import os
import re
import shutil
import sys
import time
import zipfile
from pathlib import Path

YEAR_ALBUM = re.compile(r"^Photos from \d{4}$")


def album_name(zip_member: str) -> str | None:
    """Return the album folder name for a zip path, or None if not in an album."""
    parts = zip_member.split("/")
    try:
        gp_idx = next(i for i, p in enumerate(parts) if p == "Google Photos")
        if gp_idx + 1 < len(parts):
            return parts[gp_idx + 1]
    except StopIteration:
        pass
    return None


def is_year_album(zip_member: str) -> bool:
    name = album_name(zip_member)
    return name is not None and bool(YEAR_ALBUM.match(name))


class Progress:
    def __init__(self, label: str):
        self.label = label
        self.start = time.monotonic()
        self.count = 0
        self.last_print = 0.0

    def update(self, n: int = 1, suffix: str = ""):
        self.count += n
        now = time.monotonic()
        if now - self.last_print < 0.25:
            return
        self.last_print = now
        elapsed = now - self.start
        rate = self.count / elapsed if elapsed > 0 else 0
        line = f"\r  {self.label}  {self.count:,} files  {rate:.1f}/s  {suffix}"
        print(line.ljust(80), end="", flush=True)

    def done(self, suffix: str = ""):
        elapsed = time.monotonic() - self.start
        rate = self.count / elapsed if elapsed > 0 else 0
        print(
            f"\r  {self.label}  {self.count:,} files  {rate:.1f}/s  {elapsed:.0f}s  {suffix}".ljust(
                80
            ),
            flush=True,
        )


def extract_zip(
    zip_path: Path, zip_num: int, zip_total: int, stage_dir: Path
) -> tuple[int, int, int]:
    """Extract one ZIP. Returns (extracted, already_existed, total_files)."""
    extracted = already_existed = 0
    prog = Progress(f"[{zip_num}/{zip_total}] {zip_path.name}")

    with zipfile.ZipFile(zip_path, "r") as zf:
        members = [m for m in zf.infolist() if not m.filename.endswith("/")]
        total = len(members)

        for member in members:
            name = member.filename
            target = stage_dir / name

            if target.exists():
                already_existed += 1
                prog.update()
                continue

            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)

            extracted += 1
            prog.update(suffix=Path(name).parent.name[:40])

    prog.done(f"extracted={extracted} already_existed={already_existed} total={total}")
    return extracted, already_existed, total


def main():
    parser = argparse.ArgumentParser(
        description="Extract Google Takeout ZIPs to staging directory."
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(os.environ.get("TAKEOUT_DATA_DIR") or "."),
        help="Root data directory containing raw_from_drive/ and staged/ (env: TAKEOUT_DATA_DIR)",
    )
    args = parser.parse_args()
    data_dir = args.data_dir
    raw_dir = data_dir / "raw_from_drive"
    stage_dir = data_dir / "staged"

    stage_dir.mkdir(parents=True, exist_ok=True)

    zips = sorted(raw_dir.glob("takeout-*.zip"))
    if not zips:
        print("No ZIP files found in", raw_dir)
        sys.exit(1)

    print(f"Found {len(zips)} ZIP files to extract\n")

    total_extracted = total_skipped = 0
    overall_start = time.monotonic()

    kept = []

    for i, zip_path in enumerate(zips, 1):
        try:
            extracted, already_existed, total = extract_zip(zip_path, i, len(zips), stage_dir)
            total_extracted += extracted
            total_skipped += already_existed

            accounted_for = extracted + already_existed
            if accounted_for == total:
                zip_path.unlink()
                print(f"  └─ deleted {zip_path.name}")
            else:
                missing = total - accounted_for
                print(f"  └─ KEPT {zip_path.name} — {missing} files unaccounted for, not deleting")
                kept.append((zip_path, missing))

        except Exception as e:
            print(f"\n  ERROR processing {zip_path.name}: {e}")
            print("Stopping. Re-run to resume — already-extracted files are skipped automatically.")
            sys.exit(1)

    elapsed = time.monotonic() - overall_start
    print(f"\nDone in {elapsed / 60:.1f} min")
    print(f"  Total extracted    : {total_extracted:,}")
    print(f"  Total already had  : {total_skipped:,}")
    print(f"  Staged at          : {stage_dir / 'Takeout' / 'Google Photos'}")

    if kept:
        print(f"\n  ⚠️  {len(kept)} ZIP(s) kept due to unaccounted files:")
        for p, n in kept:
            print(f"     {p.name}  ({n} missing)")


if __name__ == "__main__":
    main()
