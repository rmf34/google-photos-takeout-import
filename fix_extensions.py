#!/usr/bin/env python3
"""
Rename fake-extension files to .jpg.

Google Takeout exports some JPEGs with wrong extensions (.png, .heic, .webp,
.gif, .avif, etc.). This script detects them by magic bytes and renames both
the media file and its sidecar(s) so fix_metadata.py can process them.

Run AFTER split_year_albums.py, BEFORE fix_metadata.py (or re-run fix_metadata
after this to pick up the newly renamed files).

Usage:
    python3 fix_extensions.py [--dry-run]
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from fix_metadata import _DUPE_RE, MEDIA_EXTS, PHOTOS_DIR, SIDECAR_SUFFIXES

JPEG_MAGIC = b"\xff\xd8\xff"
JPEG_EXTS = {".jpg", ".jpeg"}
DRY_RUN = "--dry-run" in sys.argv


def _is_actually_jpeg(path: Path) -> bool:
    try:
        return path.read_bytes()[:3] == JPEG_MAGIC
    except Exception:
        return False


def _find_all_sidecars(media_path: Path) -> list[Path]:
    """Return all existing sidecar files for this media file."""
    name = media_path.name
    parent = media_path.parent
    sidecars: list[Path] = []
    seen: set[Path] = set()

    for suffix in SIDECAR_SUFFIXES:
        c = parent / (name + suffix)
        if c.exists() and c not in seen:
            sidecars.append(c)
            seen.add(c)

    m = _DUPE_RE.match(name)
    if m:
        stem, dupe_n, ext = m.group(1), m.group(2), m.group(3)
        base = stem + ext
        for suffix in SIDECAR_SUFFIXES:
            suffix_base = suffix[: -len(".json")]
            c = parent / (base + suffix_base + dupe_n + ".json")
            if c.exists() and c not in seen:
                sidecars.append(c)
                seen.add(c)

    return sidecars


def _renamed_sidecar(sidecar: Path, old_prefix: str, new_prefix: str) -> Path:
    """Return sidecar path with old_prefix replaced by new_prefix (case-insensitive)."""
    name = sidecar.name
    if name.lower().startswith(old_prefix.lower()):
        return sidecar.parent / (new_prefix + name[len(old_prefix) :])
    return sidecar


def main():
    if not PHOTOS_DIR.exists():
        print(f"ERROR: {PHOTOS_DIR} not found.")
        sys.exit(1)

    if DRY_RUN:
        print("DRY RUN — no files will be renamed\n")

    print("Scanning for fake-extension files (JPEG content, non-JPEG extension)...")
    scan_start = time.monotonic()

    candidates = [
        f
        for f in PHOTOS_DIR.rglob("*")
        if f.is_file() and f.suffix.lower() in MEDIA_EXTS and f.suffix.lower() not in JPEG_EXTS
    ]

    fake = [f for f in candidates if _is_actually_jpeg(f)]
    scan_elapsed = time.monotonic() - scan_start
    print(f"Found {len(fake):,} files to rename  ({scan_elapsed:.0f}s scan)\n")

    renamed = skipped_collision = skipped_error = 0
    start = time.monotonic()
    last_print = 0.0

    for i, f in enumerate(fake):
        new_media = f.with_suffix(".jpg")

        if new_media.exists():
            skipped_collision += 1
        else:
            sidecars = _find_all_sidecars(f)

            # Build sidecar prefix: for duplicates STEM(N).EXT the sidecar uses
            # STEM.EXT as the prefix (without the (N)), so strip it.
            m = _DUPE_RE.match(f.name)
            if m:
                stem, _, ext = m.group(1), m.group(2), m.group(3)
                old_prefix, new_prefix = stem + ext, stem + ".jpg"
            else:
                old_prefix, new_prefix = f.name, f.stem + ".jpg"

            try:
                if not DRY_RUN:
                    f.rename(new_media)
                for sc in sidecars:
                    new_sc = _renamed_sidecar(sc, old_prefix, new_prefix)
                    if new_sc != sc and not new_sc.exists():
                        if not DRY_RUN:
                            sc.rename(new_sc)
                renamed += 1
            except Exception as e:
                print(f"\n  ERROR renaming {f.name}: {e}")
                skipped_error += 1

        now = time.monotonic()
        if now - last_print >= 0.25 or i == len(fake) - 1:
            last_print = now
            elapsed = now - start
            rate = (i + 1) / elapsed if elapsed > 0 else 0
            pct = (i + 1) / len(fake) * 100
            bar_w = 25
            bar = "█" * int(bar_w * pct / 100) + "░" * (bar_w - int(bar_w * pct / 100))
            print(
                f"\r[{bar}] {pct:5.1f}%  {i + 1:,}/{len(fake):,}  {rate:.0f}/s",
                end="",
                flush=True,
            )

    print()
    elapsed = time.monotonic() - start
    print(f"\n{'=' * 55}")
    print(f"Renamed              : {renamed:,}")
    print(f"Collisions (skipped) : {skipped_collision:,}")
    print(f"Errors               : {skipped_error:,}")
    print(f"Elapsed              : {elapsed:.1f}s")

    if DRY_RUN:
        print("\nDry run complete — re-run without --dry-run to apply.")
    else:
        print("\nDone. Re-run: uv run python fix_metadata.py")


if __name__ == "__main__":
    main()
