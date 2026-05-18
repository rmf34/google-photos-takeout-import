#!/usr/bin/env python3
"""
Extract all Google Takeout ZIP files to the staging directory.
- Skips 'Photos from YEAR' albums (auto-generated year buckets)
- Merges albums split across multiple ZIPs (won't overwrite existing files)
- Deletes each ZIP after successful extraction
- Shows live progress on a single updating line
"""
import re
import shutil
import sys
import time
import zipfile
from pathlib import Path

DATA_DIR = Path("~/photos")
RAW_DIR = DATA_DIR / "raw_from_drive"
STAGE_DIR = DATA_DIR / "staged"

SKIP_ALBUM = re.compile(r"^Photos from \d{4}$")


def is_skipped(zip_member: str) -> bool:
    parts = zip_member.split("/")
    try:
        gp_idx = next(i for i, p in enumerate(parts) if p == "Google Photos")
        if gp_idx + 1 < len(parts):
            return bool(SKIP_ALBUM.match(parts[gp_idx + 1]))
    except StopIteration:
        pass
    return False


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
            f"\r  {self.label}  {self.count:,} files  {rate:.1f}/s  {elapsed:.0f}s  {suffix}".ljust(80),
            flush=True,
        )


def extract_zip(zip_path: Path, zip_num: int, zip_total: int) -> tuple[int, int]:
    extracted = skipped = 0
    prog = Progress(f"[{zip_num}/{zip_total}] {zip_path.name}")

    with zipfile.ZipFile(zip_path, "r") as zf:
        members = [m for m in zf.infolist() if not m.filename.endswith("/")]

        for member in members:
            name = member.filename

            if is_skipped(name):
                skipped += 1
                prog.update(suffix="(skipping year albums)")
                continue

            target = STAGE_DIR / name
            if target.exists():
                skipped += 1
                prog.update()
                continue

            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)

            extracted += 1
            prog.update(suffix=Path(name).parent.name[:40])

    prog.done(f"extracted={extracted} skipped={skipped}")
    return extracted, skipped


def main():
    STAGE_DIR.mkdir(parents=True, exist_ok=True)

    zips = sorted(RAW_DIR.glob("takeout-*.zip"))
    if not zips:
        print("No ZIP files found in", RAW_DIR)
        sys.exit(1)

    print(f"Found {len(zips)} ZIP files to extract\n")

    total_extracted = total_skipped = 0
    overall_start = time.monotonic()

    for i, zip_path in enumerate(zips, 1):
        try:
            extracted, skipped = extract_zip(zip_path, i, len(zips))
            total_extracted += extracted
            total_skipped += skipped

            zip_path.unlink()
            print(f"  └─ deleted {zip_path.name}")

        except Exception as e:
            print(f"\n  ERROR processing {zip_path.name}: {e}")
            print("Stopping. Re-run to resume — already-extracted files are skipped automatically.")
            sys.exit(1)

    elapsed = time.monotonic() - overall_start
    print(f"\nDone in {elapsed/60:.1f} min")
    print(f"  Total extracted : {total_extracted:,}")
    print(f"  Total skipped   : {total_skipped:,}")
    print(f"  Staged at       : {STAGE_DIR / 'Takeout' / 'Google Photos'}")


if __name__ == "__main__":
    main()
