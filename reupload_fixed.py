#!/usr/bin/env python3
"""
Safely delete wrong-date files from Google Photos albums and re-upload
the fixed versions.

Safety measures:
  - Only touches files that appear in fixed_files.txt (exact name match)
  - Only touches files in albums that match exactly by name
  - Verifies the Google Photos date is within the upload window before
    deleting — won't touch pre-existing files
  - Generates a full audit log before any deletion
  - Phase 1 (audit) runs first; Phase 2 (delete) requires --execute flag

Usage:
    uv run python reupload_fixed.py                          # audit only
    uv run python reupload_fixed.py --execute                # delete + re-upload
    uv run python reupload_fixed.py --since YYYY-MM-DD       # custom window start
"""

import argparse
import os
import re
import subprocess
import tempfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
FIXED_LIST = SCRIPT_DIR / "fixed_files.txt"
AUDIT_LOG = SCRIPT_DIR / "reupload_audit.log"
DELETE_LOG = SCRIPT_DIR / "reupload_deletions.log"
SECONDS_PER_FILE = 15
MIN_BATCH_TIMEOUT = 120
RCLONE_DELETE_TPS = 2
RCLONE_LIST_TIMEOUT = 120


def load_fixed_files() -> dict[str, set[str]]:
    """Load fixed_files.txt as {album_name: {filename, ...}}."""
    album_files: dict[str, set[str]] = defaultdict(set)
    with open(FIXED_LIST) as f:
        for line in f:
            line = line.strip()
            if "/" in line:
                album, filename = line.split("/", 1)
                album_files[album].add(filename)
    return dict(album_files)


def list_album_files(album: str) -> list[tuple[str, datetime]]:
    """List files in a Google Photos album with their dates.
    Returns [(filename, date), ...]."""
    try:
        r = subprocess.run(
            ["rclone", "lsl", f"google-photos:album/{album}"],
            capture_output=True,
            text=True,
            timeout=RCLONE_LIST_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        print(f"  TIMEOUT listing album: {album}")
        return []

    files = []
    for line in r.stdout.strip().splitlines():
        # rclone lsl format: "    SIZE YYYY-MM-DD HH:MM:SS.NNNNNNNNN FILENAME"
        m = re.match(r"\s*-?\d+\s+(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})\.\d+\s+(.+)", line)
        if m:
            date_str = f"{m.group(1)} {m.group(2)}"
            try:
                dt = datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            files.append((m.group(3), dt))
    return files


def get_gp_album_list() -> set[str]:
    """Get set of all album names in Google Photos."""
    try:
        r = subprocess.run(
            ["rclone", "lsd", "google-photos:album"],
            capture_output=True,
            text=True,
            timeout=RCLONE_LIST_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        print("ERROR: Timed out listing Google Photos albums — API may be quota-throttled.")
        raise SystemExit(1)

    if "invalid_grant" in r.stderr or "token expired" in r.stderr.lower():
        print("ERROR: rclone Google Photos token has expired.")
        print("Re-authenticate with:  rclone config reconnect google-photos:")
        raise SystemExit(1)

    if r.returncode != 0 or "error" in r.stderr.lower():
        print(f"ERROR: rclone lsd failed (rc={r.returncode}): {r.stderr.strip()}")
        raise SystemExit(1)

    albums = set()
    for line in r.stdout.strip().splitlines():
        # Format: "          -1 2026-05-20 16:40:31       140 Album Name Here"
        # Album name starts after the 4th column
        parts = line.split(None, 4)
        if len(parts) >= 5:
            albums.add(parts[4])

    if not albums:
        print("ERROR: Google Photos returned 0 albums — API is likely quota-throttled.")
        print("Wait for quota reset (midnight Pacific) and retry.")
        raise SystemExit(1)

    return albums


def build_delete_list(
    fixed: dict[str, set[str]],
    gp_albums: set[str],
    upload_window_start: datetime,
) -> tuple[list[tuple[str, str, datetime]], list[tuple[str, str, datetime, str]]]:
    """Cross-reference fixed files with Google Photos and apply date-window safety.

    Returns (to_delete, safe_skips).
    """
    target_albums = sorted(set(fixed.keys()) & gp_albums)
    to_delete: list[tuple[str, str, datetime]] = []
    safe_skips: list[tuple[str, str, datetime, str]] = []

    for album in target_albums:
        fixed_names = fixed[album]
        print(f"  Scanning: {album} ({len(fixed_names)} fixed files)...", end=" ", flush=True)

        gp_files = list_album_files(album)
        gp_by_name = {name: dt for name, dt in gp_files}

        matched = 0
        for filename in sorted(fixed_names):
            if filename not in gp_by_name:
                continue

            gp_date = gp_by_name[filename]
            matched += 1

            if gp_date >= upload_window_start:
                to_delete.append((album, filename, gp_date))
            else:
                safe_skips.append(
                    (
                        album,
                        filename,
                        gp_date,
                        f"GP date {gp_date:%Y-%m-%d} is before upload window",
                    )
                )

        not_uploaded = len(fixed_names) - matched
        print(f"{matched} in GP, {not_uploaded} not yet uploaded")

    return to_delete, safe_skips


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Delete wrong-date files from Google Photos and re-upload."
    )
    parser.add_argument(
        "--execute", action="store_true", help="Actually delete files (default is audit-only)"
    )
    parser.add_argument(
        "--since",
        type=str,
        required=True,
        help="Only delete files with a Google Photos date on or after this date (YYYY-MM-DD). Set to your rclone upload start date to avoid touching pre-existing photos.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    upload_window_start = datetime.strptime(args.since, "%Y-%m-%d")

    if not FIXED_LIST.exists():
        print(f"ERROR: {FIXED_LIST} not found. Run fix_missing_dates.py first.")
        raise SystemExit(1)

    print("=" * 65)
    print("REUPLOAD FIXED FILES — SAFE DELETE + RE-UPLOAD")
    print("=" * 65)
    if args.execute:
        print("MODE: EXECUTE (will actually delete and re-upload)")
    else:
        print("MODE: AUDIT ONLY (use --execute to apply changes)")
    print(f"Upload window: files dated on or after {upload_window_start:%Y-%m-%d}")
    print()

    # Load the list of files we fixed
    fixed = load_fixed_files()
    total_fixed = sum(len(v) for v in fixed.values())
    print(f"Fixed files list: {total_fixed:,} files across {len(fixed)} albums")

    # Get Google Photos album list
    print("Fetching Google Photos album list...")
    gp_albums = get_gp_album_list()
    print(f"Found {len(gp_albums)} albums in Google Photos\n")

    # Cross-reference fixed files with Google Photos albums
    target_count = len(set(fixed.keys()) & gp_albums)
    print(f"Albums to check: {target_count}")
    print()

    to_delete, safe_skips = build_delete_list(fixed, gp_albums, upload_window_start)

    print()
    print("=" * 65)
    print(f"FILES TO DELETE AND RE-UPLOAD: {len(to_delete):,}")
    print(f"SAFELY SKIPPED (pre-existing): {len(safe_skips):,}")
    print("=" * 65)
    print()

    # Write audit log
    with open(AUDIT_LOG, "w") as log:
        log.write(f"Reupload Audit Log — {datetime.now():%Y-%m-%d %H:%M:%S}\n")
        log.write(f"{'=' * 65}\n\n")

        log.write(f"FILES TO DELETE ({len(to_delete)}):\n")
        log.write(f"{'-' * 65}\n")
        for album, filename, gp_date in sorted(to_delete):
            log.write(f"  DELETE  {gp_date:%Y-%m-%d %H:%M}  {album}/{filename}\n")

        log.write(f"\nSAFELY SKIPPED ({len(safe_skips)}):\n")
        log.write(f"{'-' * 65}\n")
        for album, filename, gp_date, reason in sorted(safe_skips):
            log.write(f"  SKIP    {gp_date:%Y-%m-%d %H:%M}  {album}/{filename}  ({reason})\n")

    print(f"Full audit log: {AUDIT_LOG}")

    # Show sample of what would be deleted
    if to_delete:
        print("\nSample of files to delete (first 10):")
        for album, filename, gp_date in to_delete[:10]:
            print(f"  {gp_date:%Y-%m-%d %H:%M}  {album}/{filename}")
        if len(to_delete) > 10:
            print(f"  ... and {len(to_delete) - 10} more (see audit log)")

    if safe_skips:
        print(f"\nSafely skipped {len(safe_skips)} files with pre-existing dates:")
        for album, filename, gp_date, reason in safe_skips[:5]:
            print(f"  {gp_date:%Y-%m-%d %H:%M}  {album}/{filename}")
        if len(safe_skips) > 5:
            print(f"  ... and {len(safe_skips) - 5} more")

    if not to_delete:
        print("\nNothing to delete — all fixed files either haven't been uploaded yet")
        print("or have pre-existing dates. Just re-run the rclone copy to upload.")
        return

    # Phase 2: Execute deletions
    if not args.execute:
        print(f"\n{'=' * 65}")
        print("AUDIT COMPLETE — review the log above.")
        print("To proceed with deletion + re-upload, run:")
        print("  uv run python reupload_fixed.py --execute")
        print(f"{'=' * 65}")
        return

    print(f"\n{'=' * 65}")
    print(f"EXECUTING: Deleting {len(to_delete)} files from Google Photos albums...")
    print(f"{'=' * 65}\n")

    album_files: dict[str, list[str]] = defaultdict(list)
    for album, filename, gp_date in to_delete:
        album_files[album].append(filename)

    total_deleted = 0
    total_errors = 0

    with open(DELETE_LOG, "a", encoding="utf-8") as deletion_log:
        deletion_log.write(f"\n{'=' * 65}\n")
        deletion_log.write(f"Deletion run — {datetime.now():%Y-%m-%d %H:%M:%S}\n")
        deletion_log.write(f"{'=' * 65}\n")

        for album, filenames in sorted(album_files.items()):
            print(f"  Deleting {len(filenames)} files from: {album}...", end=" ", flush=True)

            timeout = max(MIN_BATCH_TIMEOUT, len(filenames) * SECONDS_PER_FILE)
            fd, files_from_path = tempfile.mkstemp(
                suffix=".txt", prefix=f"rclone_delete_{os.getpid()}_"
            )
            try:
                with os.fdopen(fd, "w") as fh:
                    fh.write("\n".join(filenames) + "\n")

                r = subprocess.run(
                    [
                        "rclone",
                        "delete",
                        f"google-photos:album/{album}",
                        "--files-from-raw",
                        files_from_path,
                        f"--tpslimit={RCLONE_DELETE_TPS}",
                        "--verbose",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )

                output = r.stdout + "\n" + r.stderr
                deleted_in_batch = sum(1 for line in output.splitlines() if ": Deleted" in line)
                errors_in_batch = sum(
                    1 for line in output.splitlines() if line.startswith("ERROR :")
                )

                if deleted_in_batch > 0 or errors_in_batch > 0:
                    total_deleted += deleted_in_batch
                    total_errors += errors_in_batch
                elif r.returncode == 0:
                    total_deleted += len(filenames)
                else:
                    total_errors += len(filenames)

                if errors_in_batch > 0 or r.returncode != 0:
                    print(f"{deleted_in_batch} deleted, {errors_in_batch} errors")
                    deletion_log.write(f"\nERRORS in {album}:\n{output}\n")
                else:
                    print(f"OK ({deleted_in_batch or len(filenames)})")

                deletion_log.write(
                    f"  {album}: {deleted_in_batch or len(filenames)} deleted,"
                    f" {errors_in_batch} errors\n"
                )

            except subprocess.TimeoutExpired:
                print(f"TIMEOUT ({timeout}s)")
                total_errors += len(filenames)
                deletion_log.write(f"  {album}: TIMEOUT after {timeout}s\n")
            finally:
                Path(files_from_path).unlink(missing_ok=True)

    print(f"\nDeletion complete: {total_deleted} deleted, {total_errors} errors")
    if DELETE_LOG.exists() and DELETE_LOG.stat().st_size > 0:
        print(f"Deletion log: {DELETE_LOG}")

    print(f"\n{'=' * 65}")
    print("Phase 2 complete. Now re-run the rclone upload to re-upload fixed files:")
    print()
    # This path is hint-only — deletion happens via the rclone Google Photos API.
    upload_src = (
        Path(os.environ.get("TAKEOUT_DATA_DIR") or ".") / "staged" / "Takeout" / "Google Photos"
    )
    print(f'rclone copy "{upload_src}" \\')
    print('  "google-photos:album" --transfers 4 --tpslimit 3 --exclude "*.json" \\')
    print("  --log-file rclone_upload.log --log-level NOTICE --progress")
    print(f"{'=' * 65}")


if __name__ == "__main__":
    main()
