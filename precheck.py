#!/usr/bin/env python3
"""
Sanity checks to run after extraction and before fix_metadata.py.

Exits 0 if all hard checks pass (warnings are non-fatal).
Exits 1 if any hard check fails.

Usage:
    .venv/bin/python precheck.py
"""

import shutil
import subprocess
import sys
from pathlib import Path

# Must match fix_metadata.py
sys.path.insert(0, str(Path(__file__).parent))
from fix_metadata import (
    MEDIA_EXTS,
    PHOTOS_DIR,
    _get_tz_finder,
    build_exiftool_entry,
    find_sidecar,
    parse_sidecar,
    run_exiftool_batch,
)

PASS = "\033[32m✓\033[0m"
WARN = "\033[33m⚠\033[0m"
FAIL = "\033[31m✗\033[0m"

failures = []
warnings = []


def check(label: str, ok: bool, detail: str = "", fatal: bool = True):
    if ok:
        print(f"  {PASS}  {label}" + (f"  {detail}" if detail else ""))
    elif fatal:
        print(f"  {FAIL}  {label}" + (f"  {detail}" if detail else ""))
        failures.append(label)
    else:
        print(f"  {WARN}  {label}" + (f"  {detail}" if detail else ""))
        warnings.append(label)


# ---------------------------------------------------------------------------
print("\n── Environment ─────────────────────────────────────────")

# exiftool
result = subprocess.run(["exiftool", "-ver"], capture_output=True, text=True)
check(
    "exiftool installed",
    result.returncode == 0,
    f"v{result.stdout.strip()}" if result.returncode == 0 else result.stderr.strip(),
)

# timezonefinder
tf = _get_tz_finder()
check(
    "timezonefinder available (DST-aware local time)",
    bool(tf),
    fatal=False,
    detail="dates will be written as UTC without this" if not tf else "",
)

# ---------------------------------------------------------------------------
print("\n── Staged directory ────────────────────────────────────")

check("staged directory exists", PHOTOS_DIR.exists(), str(PHOTOS_DIR))

if PHOTOS_DIR.exists():
    albums = [d for d in PHOTOS_DIR.iterdir() if d.is_dir()]
    check("albums found", len(albums) > 0, f"{len(albums):,} albums")

    all_files = list(PHOTOS_DIR.rglob("*"))
    media = [f for f in all_files if f.is_file() and f.suffix.lower() in MEDIA_EXTS]
    sidecars = [f for f in all_files if f.is_file() and f.suffix.lower() == ".json"]
    check("media files found", len(media) > 0, f"{len(media):,} files")
    coverage = len(sidecars) / len(media) * 100 if media else 0
    check(
        "sidecar coverage ≥ 90%",
        coverage >= 90,
        f"{coverage:.1f}%  ({len(sidecars):,} sidecars / {len(media):,} media)",
        fatal=False,
    )

# ---------------------------------------------------------------------------
print("\n── Disk space ──────────────────────────────────────────")

usage = shutil.disk_usage(PHOTOS_DIR if PHOTOS_DIR.exists() else Path.home())
free_gb = usage.free / 1024**3
check("≥ 5 GB free (exiftool temp files)", free_gb >= 5, f"{free_gb:.1f} GB free")

# ---------------------------------------------------------------------------
print("\n── Sample pipeline test ────────────────────────────────")

if PHOTOS_DIR.exists() and failures == []:
    # Find the first album that has both a media file with GPS and a sidecar
    sample_file = None
    sample_meta = None
    for album in sorted(PHOTOS_DIR.iterdir()):
        if not album.is_dir():
            continue
        for f in sorted(album.iterdir()):
            if not f.is_file() or f.suffix.lower() not in MEDIA_EXTS:
                continue
            sidecar = find_sidecar(f)
            if not sidecar:
                continue
            meta = parse_sidecar(sidecar)
            if meta.get("gps") and meta.get("datetime"):
                sample_file = f
                sample_meta = meta
                break
        if sample_file:
            break

    if sample_file:
        print(f"  Sample: {sample_file.parent.name}/{sample_file.name}")

        # Show EXIF before
        before = subprocess.run(
            ["exiftool", "-DateTimeOriginal", "-GPSLatitude", "-GPSLongitude", sample_file],
            capture_output=True,
            text=True,
        )
        print("  Before:")
        for line in before.stdout.strip().splitlines():
            print(f"    {line.strip()}")

        # Run fix on just this one file
        entry = build_exiftool_entry(sample_file, sample_meta)
        ok, err = run_exiftool_batch([entry] if entry else [])

        check("exiftool write succeeded", ok == 1 and err == 0, f"ok={ok} errors={err}")

        # Show EXIF after
        after = subprocess.run(
            [
                "exiftool",
                "-DateTimeOriginal",
                "-GPSLatitude",
                "-GPSLongitude",
                "-GPSDateStamp",
                "-GPSTimeStamp",
                sample_file,
            ],
            capture_output=True,
            text=True,
        )
        print("  After:")
        for line in after.stdout.strip().splitlines():
            print(f"    {line.strip()}")

        # Verify the computed datetime matches what was written
        written_dt = None
        for line in after.stdout.splitlines():
            if "Date/Time Original" in line:
                written_dt = line.split(":", 1)[1].strip()
        check(
            "written datetime matches computed datetime",
            written_dt == sample_meta["datetime"],
            f"expected={sample_meta['datetime']}  got={written_dt}",
        )
    else:
        check(
            "found sample file with GPS + sidecar",
            False,
            fatal=False,
            detail="no GPS photo found to test with",
        )

# ---------------------------------------------------------------------------
print("\n── Summary ─────────────────────────────────────────────")

if failures:
    print(f"\n  {FAIL}  {len(failures)} check(s) failed — fix before running fix_metadata.py:")
    for f in failures:
        print(f"       • {f}")
    sys.exit(1)
elif warnings:
    print(f"\n  {WARN}  {len(warnings)} warning(s) — safe to proceed:")
    for w in warnings:
        print(f"       • {w}")
    print("\n  Ready for: venv/bin/python fix_metadata.py")
else:
    print(f"\n  {PASS}  All checks passed.")
    print("\n  Ready for: venv/bin/python fix_metadata.py")
