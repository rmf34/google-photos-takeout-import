#!/usr/bin/env python3
"""
Orchestrate the full Google Takeout metadata pipeline.

Usage:
    uv run python run.py                              # interactive mode prompt
    uv run python run.py --mode local                 # fix metadata only
    uv run python run.py --mode upload                # fix metadata + upload to Google Photos
    uv run python run.py --mode upload-only           # skip local steps, upload only
    uv run python run.py --data-dir ~/photos          # override data directory
"""

import argparse
import enum
import math
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent

DAILY_QUOTA = 10_000  # Google Photos API (Application Programming Interface) files/day limit
RCLONE_PROGRESS_INTERVAL = 5  # seconds between progress line updates and rclone stats logging
RCLONE_LIST_TIMEOUT = 120  # seconds to wait for rclone lsd before giving up
QUOTA_RETRY_INTERVAL = 3600  # seconds to wait between quota-exhausted retries
PROGRESS_LINE_WIDTH = 100  # terminal width for \r-overwritten progress line
TAIL_BYTES = 16_384  # max bytes read from log tail — covers ~150 lines regardless of file age

DATELESS_REPORT = SCRIPT_DIR / "dateless_skipped.txt"
_DATELESS_EXCLUDE = SCRIPT_DIR / "dateless_exclude.txt"
_QUOTA_ERROR_MARKERS = ("Quota exceeded", "RESOURCE_EXHAUSTED")

# Scripts that only need stdlib are run with sys.executable directly.
# Scripts that need the venv (exiftool wrappers, timezonefinder) use uv run.
_UV = ("uv", "run", "python")
_PY = (sys.executable,)

LOCAL_STEPS = [
    ("Extract ZIPs", _PY, "extract_and_stage.py"),
    ("Split year albums", _PY, "split_year_albums.py"),
    ("Fix extensions", _PY, "fix_extensions.py"),
    ("Precheck", _UV, "precheck.py"),
    ("Fix metadata (EXIF)", _UV, "fix_metadata.py"),
    ("Fix missing dates", _UV, "fix_missing_dates.py"),
]


@dataclass(frozen=True)
class UploadProgress:
    gp_initial: int | None  # files in Google Photos before this session; None when fetch failed
    local_total: int  # total local non-JSON files to upload
    avg_bytes: float  # average bytes per file (for gigabyte estimate)


class _UploadResult(enum.Enum):
    OK = enum.auto()
    QUOTA_EXHAUSTED = enum.auto()
    FAILED = enum.auto()


def _is_quota_error(log_path: Path) -> bool:
    try:
        tail = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-20:]
    except OSError:
        print(
            f"\n  WARNING: could not read {log_path} to classify failure — treating as hard error."
        )
        return False
    combined = "\n".join(tail)
    return any(marker in combined for marker in _QUOTA_ERROR_MARKERS)


def _find_bad_date_files(photos_dir: Path) -> list[Path]:
    """Return photo paths with no EXIF date or an EXIF date after today.

    Both conditions produce wrong dates in Google Photos: missing dates get
    stamped with the upload date; future dates are nonsense from failed metadata
    fixups (e.g. sidecar not matched after extension conversion).
    """
    if not photos_dir.is_dir():
        print(f"\n  WARNING: photos directory not found: {photos_dir} — skipping date scan.")
        return []
    today = time.strftime("%Y:%m:%d 23:59:59")
    result = subprocess.run(
        [
            "exiftool",
            "-r",
            "-p",
            "$Directory/$FileName",
            "-if",
            (
                "(not $DateTimeOriginal and not $CreateDate) or "
                f"($DateTimeOriginal and $DateTimeOriginal gt '{today}') or "
                f"($CreateDate and $CreateDate gt '{today}')"
            ),
            str(photos_dir),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(
            f"\n  ERROR: exiftool date scan failed (exit {result.returncode}). "
            "Cannot safely upload without validating dates — stopping."
        )
        sys.exit(1)
    return [
        Path(line.strip())
        for line in result.stdout.splitlines()
        if line.strip() and not line.strip().lower().endswith(".json")
    ]


def _header(text: str) -> None:
    bar = "=" * 65
    print(f"\n{bar}")
    print(f"  {text}")
    print(bar)


def _run_step(label: str, cmd: list[str], env: dict[str, str], diagnose: bool = False) -> bool:
    _header(f"Step: {label}")
    result = subprocess.run(cmd, env=env)
    if result.returncode != 0:
        print(f"\nERROR: '{label}' failed (exit {result.returncode}). Stopping.")
        if diagnose:
            _diagnose_rclone_failure()
        return False
    return True


def _diagnose_rclone_failure() -> None:
    log = SCRIPT_DIR / "rclone_upload.log"
    if not log.exists():
        return
    try:
        tail = log.read_text(encoding="utf-8", errors="replace").splitlines()[-20:]
    except OSError:
        return
    combined = "\n".join(tail)
    if "invalid_grant" in combined or "token expired" in combined.lower():
        print("\nDIAGNOSIS: rclone OAuth (Open Authorization) token has expired.")
        print("Fix:  rclone config reconnect google-photos:")
    elif any(marker in combined for marker in _QUOTA_ERROR_MARKERS):
        print(
            "\nDIAGNOSIS: Google Photos daily API (Application Programming Interface) quota exceeded."
        )
        print("Fix:  wait until midnight Pacific and re-run.")


def _count_local_files(photos_dir: Path) -> tuple[int, int]:
    """Return (file_count, total_bytes) for all non-JSON files."""
    count = 0
    total_bytes = 0
    for dirpath, _, files in os.walk(photos_dir):
        for f in files:
            if not f.lower().endswith(".json"):
                count += 1
                try:
                    total_bytes += Path(dirpath, f).stat().st_size
                except OSError:
                    pass
    return count, total_bytes


def _parse_gp_file_count(lsd_stdout: str) -> int:
    total = 0
    for line in lsd_stdout.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 5:
            continue
        try:
            count = int(parts[3])
            if count > 0:
                total += count
        except ValueError:
            continue
    return total


def _parse_rclone_stats(log_path: Path, log_offset: int) -> tuple[int, float]:
    """Return (files_transferred_this_session, speed_kib_s) from latest rclone log stats."""
    try:
        with log_path.open("rb") as fh:
            fh.seek(0, 2)
            end = fh.tell()
            fh.seek(max(log_offset, end - TAIL_BYTES))
            lines = fh.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return 0, 0.0

    files_transferred = 0
    speed_kib_s = 0.0
    for line in reversed(lines):
        if not files_transferred:
            # "Transferred:            34 / 1314, 6%" — integer file-count stats line
            m = re.search(r"Transferred:\s+(\d[\d,]*)\s*/\s*[\d,]+,\s*\d+%\s*$", line)
            if m:
                files_transferred = int(m.group(1).replace(",", ""))
        if not speed_kib_s:
            m = re.search(r"([\d.]+)\s*(KiB|MiB|GiB)/s", line)
            if m:
                val = float(m.group(1))
                mult = {"KiB": 1.0, "MiB": 1024.0, "GiB": 1024.0**2}[m.group(2)]
                speed_kib_s = val * mult
        if files_transferred and speed_kib_s:
            break
    return files_transferred, speed_kib_s


def _compute_remaining_stats(remaining: int, avg_bytes: float) -> tuple[float, int]:
    """Pure function: return (remaining_gb, days_remaining) from file count and average size."""
    remaining_gb = remaining * avg_bytes / (1024**3)
    days_remaining = math.ceil(remaining / DAILY_QUOTA) if remaining > 0 else 0
    return remaining_gb, days_remaining


def _compute_progress_line(
    gp_initial: int | None,
    files_this_session: int,
    local_total: int,
    avg_bytes: float,
    speed_kib_s: float,
) -> str:
    """Pure function: compute all progress metrics and return a formatted line."""
    if speed_kib_s >= 1024:
        speed_str = f"{speed_kib_s / 1024:.1f} MiB/s"
    elif speed_kib_s > 0:
        speed_str = f"{speed_kib_s:.0f} KiB/s"
    else:
        speed_str = "starting..."

    if gp_initial is None:
        # Baseline unknown — show session-only count without misleading totals
        return f"  +{files_this_session:,} this session / {local_total:,} total  {speed_str}"

    uploaded = gp_initial + files_this_session
    remaining = max(0, local_total - uploaded)
    pct = uploaded / local_total * 100 if local_total else 0
    remaining_gb, days_remaining = _compute_remaining_stats(remaining, avg_bytes)

    return (
        f"  {uploaded:,}/{local_total:,} ({pct:.0f}%)  "
        f"{remaining:,} left  ~{remaining_gb:.1f} GB  "
        f"{speed_str}  ≥{days_remaining} days"
    )


def _format_progress_line(progress: UploadProgress, log_path: Path, log_offset: int) -> str:
    """Orchestrator: read log stats, then compute formatted progress line."""
    files_this_session, speed_kib_s = _parse_rclone_stats(log_path, log_offset)
    return _compute_progress_line(
        progress.gp_initial,
        files_this_session,
        progress.local_total,
        progress.avg_bytes,
        speed_kib_s,
    )


def _fetch_upload_stats(photos_dir: Path) -> tuple[int, int, int | None]:
    """Return (local_total, local_bytes, gp_initial) without printing.

    gp_initial is None when the Google Photos album count could not be fetched.
    """
    local_total, local_bytes = _count_local_files(photos_dir)

    result = subprocess.run(
        ["rclone", "lsd", "google-photos:album"],
        capture_output=True,
        text=True,
        timeout=RCLONE_LIST_TIMEOUT,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return local_total, local_bytes, None

    gp_initial = _parse_gp_file_count(result.stdout)
    return local_total, local_bytes, gp_initial


def _print_upload_progress(
    local_total: int, local_bytes: int, avg_bytes: float, gp_initial: int | None
) -> None:
    """Print the pre-session snapshot. avg_bytes must be pre-computed by the caller."""
    total_gb = local_bytes / (1024**3)
    print(f"\n  Local files          : {local_total:,}  ({total_gb:.1f} GB total)")
    if gp_initial is None:
        print("  Google Photos count  : (unavailable)")
    else:
        remaining = max(0, local_total - gp_initial)
        pct = gp_initial / local_total * 100 if local_total else 0
        remaining_gb, days_remaining = _compute_remaining_stats(remaining, avg_bytes)
        print(f"  Uploaded             : {gp_initial:,} / {local_total:,} ({pct:.0f}%)")
        print(f"  Remaining            : {remaining:,} files  ~{remaining_gb:.1f} GB")
        print(f"  Days remaining (min) : {days_remaining}  (quota: ~{DAILY_QUOTA:,} files/day)")
    print()


def _run_rclone(
    label: str, cmd: list[str], env: dict[str, str], progress: UploadProgress
) -> _UploadResult:
    _header(f"Step: {label}")
    log_path = SCRIPT_DIR / "rclone_upload.log"
    try:
        log_offset = log_path.stat().st_size
    except OSError:
        log_offset = 0
    proc = subprocess.Popen(cmd, env=env)

    last_update = time.monotonic() - RCLONE_PROGRESS_INTERVAL  # show on first tick

    while proc.poll() is None:
        if time.monotonic() - last_update >= RCLONE_PROGRESS_INTERVAL:
            last_update = time.monotonic()
            line = _format_progress_line(progress, log_path, log_offset)
            print(f"\r{line:<{PROGRESS_LINE_WIDTH}}", end="", flush=True)
        time.sleep(1)

    line = _format_progress_line(progress, log_path, log_offset)
    print(f"\r{line:<{PROGRESS_LINE_WIDTH}}")

    if proc.returncode == 0:
        return _UploadResult.OK
    if _is_quota_error(log_path):
        return _UploadResult.QUOTA_EXHAUSTED
    print(f"\nERROR: '{label}' failed (exit {proc.returncode}). Stopping.")
    _diagnose_rclone_failure()
    return _UploadResult.FAILED


def _prompt_mode() -> str:
    print("\nWhat would you like to do?")
    print("  1. Fix metadata in a Google Takeout download")
    print("  2. Fix metadata + upload to Google Photos\n")
    while True:
        choice = input("Choice [1/2]: ").strip()
        if choice == "1":
            return "local"
        if choice == "2":
            return "upload"
        print("  Please enter 1 or 2.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the full Google Takeout metadata pipeline.")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(os.environ.get("TAKEOUT_DATA_DIR") or "."),
        help="Root data directory containing raw_from_drive/ and staged/ (env: TAKEOUT_DATA_DIR)",
    )
    parser.add_argument(
        "--mode",
        choices=["local", "upload", "upload-only"],
        help="Pipeline mode: 'local' = metadata only; 'upload' = metadata + upload; 'upload-only' = skip to upload",
    )
    args = parser.parse_args()

    mode = args.mode or _prompt_mode()
    data_dir = args.data_dir.expanduser().resolve()

    env = {**os.environ, "TAKEOUT_DATA_DIR": str(data_dir)}

    mode_label = {
        "local": "local only",
        "upload": "local + upload",
        "upload-only": "upload only (skip local steps)",
    }
    print(f"\nData directory : {data_dir}")
    print(f"Mode           : {mode_label[mode]}")

    if mode != "upload-only":
        for label, runner, script in LOCAL_STEPS:
            cmd = list(runner) + [str(SCRIPT_DIR / script)]
            if not _run_step(label, cmd, env):
                sys.exit(1)

    if mode in ("upload", "upload-only"):
        photos_dir = data_dir / "staged" / "Takeout" / "Google Photos"
        print("\n  Counting local files...")
        local_total, local_bytes, gp_initial = _fetch_upload_stats(photos_dir)
        avg_bytes = local_bytes / local_total if local_total else 0
        _print_upload_progress(local_total, local_bytes, avg_bytes, gp_initial)
        progress = UploadProgress(
            gp_initial=gp_initial, local_total=local_total, avg_bytes=avg_bytes
        )
        if mode == "upload" or not DATELESS_REPORT.exists():
            print("  Scanning for missing or future EXIF dates...")
            bad_files = _find_bad_date_files(photos_dir)
            if bad_files:
                DATELESS_REPORT.write_text(
                    "\n".join(str(p) for p in bad_files) + "\n", encoding="utf-8"
                )
                _DATELESS_EXCLUDE.write_text(
                    "\n".join(str(p.relative_to(photos_dir)) for p in bad_files) + "\n",
                    encoding="utf-8",
                )
                print(
                    f"  {len(bad_files):,} files skipped (bad/missing date) — "
                    f"paths written to {DATELESS_REPORT.name}"
                )
            else:
                print("  All files have valid EXIF dates.")
                DATELESS_REPORT.unlink(missing_ok=True)
                _DATELESS_EXCLUDE.unlink(missing_ok=True)
        else:
            bad_files = [
                Path(line.strip())
                for line in DATELESS_REPORT.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            if bad_files and not _DATELESS_EXCLUDE.exists():
                _DATELESS_EXCLUDE.write_text(
                    "\n".join(str(p.relative_to(photos_dir)) for p in bad_files) + "\n",
                    encoding="utf-8",
                )
            print(
                f"  Reusing date scan: {len(bad_files):,} files to skip "
                f"(run --mode upload to re-scan)"
            )
        rclone_cmd = [
            "rclone",
            "copy",
            str(photos_dir),
            "google-photos:album",
            "--transfers",
            "4",
            "--tpslimit",
            "3",
            "--exclude",
            "*.json",
            "--log-file",
            "rclone_upload.log",
            "--log-level",
            "NOTICE",
            "--stats",
            f"{RCLONE_PROGRESS_INTERVAL}s",
            "--stats-log-level",
            "NOTICE",
        ]
        if bad_files:
            rclone_cmd += ["--exclude-from", str(_DATELESS_EXCLUDE)]
        quota_attempt = 0
        while True:
            result = _run_rclone("Upload to Google Photos (rclone)", rclone_cmd, env, progress)
            if result == _UploadResult.OK:
                break
            if result == _UploadResult.FAILED:
                sys.exit(1)
            # Quota exhausted — wait QUOTA_RETRY_INTERVAL then retry
            quota_attempt += 1
            wait_m = QUOTA_RETRY_INTERVAL // 60
            print(f"\n  Quota exhausted (attempt {quota_attempt}). Retrying in {wait_m}m...")
            deadline = time.monotonic() + QUOTA_RETRY_INTERVAL
            while True:
                remaining = max(0, int(deadline - time.monotonic()))
                if remaining == 0:
                    break
                rm = remaining // 60
                print(f"\r  Retrying in {rm}m...{' ' * 10}", end="", flush=True)
                time.sleep(min(60, remaining))
            print(f"\r  Re-fetching stats and retrying...{' ' * 20}")
            local_total, local_bytes, gp_initial = _fetch_upload_stats(photos_dir)
            avg_bytes = local_bytes / local_total if local_total else 0
            _print_upload_progress(local_total, local_bytes, avg_bytes, gp_initial)
            progress = UploadProgress(
                gp_initial=gp_initial, local_total=local_total, avg_bytes=avg_bytes
            )

    _header("All steps complete")
    if mode == "local":
        print(f"  Fixed files are in: {data_dir / 'staged' / 'Takeout' / 'Google Photos'}")
    else:
        print("  Upload complete. Check rclone_upload.log for details.")
        print("  Re-run with --mode upload-only to resume after a quota reset.")
    print()


if __name__ == "__main__":
    main()
