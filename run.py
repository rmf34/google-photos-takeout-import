#!/usr/bin/env python3
"""
Orchestrate the full Google Takeout metadata pipeline.

Usage:
    uv run python run.py                              # interactive mode prompt
    uv run python run.py --mode local                 # fix metadata only
    uv run python run.py --mode upload                # fix metadata + upload to Google Photos
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


def _run_step(label: str, cmd: list[str], env: dict[str, str]) -> bool:
    _header(f"Step: {label}")
    result = subprocess.run(cmd, env=env)
    if result.returncode != 0:
        print(f"\nERROR: '{label}' failed (exit {result.returncode}). Stopping.")
        return False
    return True


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
        choices=["local", "upload"],
        help="Pipeline mode: 'local' stops after metadata fix; 'upload' also pushes to Google Photos",
    )
    args = parser.parse_args()

    mode = args.mode or _prompt_mode()
    data_dir = args.data_dir.expanduser().resolve()

    env = {**os.environ, "TAKEOUT_DATA_DIR": str(data_dir)}

    print(f"\nData directory : {data_dir}")
    print(
        f"Mode           : {'local only' if mode == 'local' else 'local + upload to Google Photos'}"
    )

    for label, runner, script in LOCAL_STEPS:
        cmd = list(runner) + [str(SCRIPT_DIR / script)]
        if not _run_step(label, cmd, env):
            sys.exit(1)

    if mode == "upload":
        photos_dir = data_dir / "staged" / "Takeout" / "Google Photos"
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
        if not _run_step("Upload to Google Photos (rclone)", rclone_cmd, env):
            sys.exit(1)

    _header("All steps complete")
    if mode == "local":
        print(f"  Fixed files are in: {data_dir / 'staged' / 'Takeout' / 'Google Photos'}")
    else:
        print("  Upload complete. Check rclone_upload.log for details.")
    print()


if __name__ == "__main__":
    main()
