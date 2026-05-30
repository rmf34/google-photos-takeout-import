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
import os
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent

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
        print("\nDIAGNOSIS: rclone OAuth token has expired.")
        print("Fix:  rclone config reconnect google-photos:")
    elif "Quota exceeded" in combined or "RESOURCE_EXHAUSTED" in combined:
        print("\nDIAGNOSIS: Google Photos daily API quota exceeded.")
        print("Fix:  wait until midnight Pacific and re-run.")


def _count_local_files(photos_dir: Path) -> int:
    total = 0
    for _, _, files in os.walk(photos_dir):
        total += sum(1 for f in files if not f.lower().endswith(".json"))
    return total


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


def _print_upload_progress(photos_dir: Path) -> None:
    local_total = _count_local_files(photos_dir)
    print(f"\n  Local files to upload : {local_total:,}")

    result = subprocess.run(
        ["rclone", "lsd", "google-photos:album"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0 or not result.stdout.strip():
        print("  Google Photos count  : (unavailable)")
        return

    gp_total = _parse_gp_file_count(result.stdout)
    pct = gp_total / local_total * 100 if local_total else 0
    remaining = max(0, local_total - gp_total)
    print(f"  Uploaded so far      : {gp_total:,} / {local_total:,} ({pct:.0f}%)")
    print(f"  Remaining            : {remaining:,} files")


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
        _print_upload_progress(photos_dir)
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
            "--progress",
        ]
        if not _run_step("Upload to Google Photos (rclone)", rclone_cmd, env, diagnose=True):
            sys.exit(1)

    _header("All steps complete")
    if mode == "local":
        print(f"  Fixed files are in: {data_dir / 'staged' / 'Takeout' / 'Google Photos'}")
    else:
        print("  Upload complete. Check rclone_upload.log for details.")
        print("  Re-run with --mode upload-only to resume after a quota reset.")
    print()


if __name__ == "__main__":
    main()
