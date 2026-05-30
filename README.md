# google-photos-takeout-import

Migrate a Google Takeout photo archive to an existing Google Photos account.

## What it does

1. **`extract_and_stage.py`** — Extracts all Takeout ZIP files into a staging directory, merging albums split across multiple ZIPs. Includes `Photos from YEAR` auto-albums (which hold any photo not in a named album — skipping them would lose the bulk of the library). Deletes each ZIP only after verifying the extracted file count matches the ZIP's member list.

2. **`split_year_albums.py`** — `Photos from YEAR` directories can contain up to 58,000 files. Passing that many paths to exiftool in one call exceeds the kernel's `ARG_MAX` limit (~3.2 MB on this system) and crashes with `Argument list too long`. This script splits each year directory into `Photos from YYYY-MM` monthly sub-albums (≈2,000–5,000 files each) based on each photo's `photoTakenTime` sidecar timestamp. Handles Google's duplicate naming quirk (`STEM(N).EXT` → `STEM.EXT.supplemental-metadata(N).json`). Photos with no usable timestamp go into `Photos from YYYY-unknown`. Supports `--dry-run`.

3. **`fix_extensions.py`** — Google Takeout exports some JPEGs with the wrong file extension (`.png`, `.heic`, `.webp`, `.gif`, `.avif`, etc.) because Google internally transcodes everything to JPEG but preserves the original filename. exiftool detects the mismatch and refuses to write metadata to those files. This script detects affected files by JPEG magic bytes (`\xff\xd8\xff`) and renames both the media file and its sidecar(s) to `.jpg`. Supports `--dry-run`.

4. **`precheck.py`** — Sanity checks to run before touching EXIF: verifies exiftool is installed, staged directory exists, sidecar coverage ≥ 90%, ≥ 5 GB free, and runs a sample pipeline test on a real photo with GPS (shows before/after EXIF and verifies the written datetime matches the computed one).

5. **`fix_metadata.py`** — Reads each photo's `.supplemental-metadata.json` sidecar and writes correct dates, GPS, and captions back into the file's EXIF using exiftool. See detailed sections below. Safe to re-run — exiftool is idempotent.

6. **`fix_missing_dates.py`** — Fixes media files still missing `DateTimeOriginal` after `fix_metadata.py`. Handles three categories: (a) `-edited` files (Google exports edits without sidecar JSON) — copies date from the non-edited original; (b) fake-extension JPEGs (`.png`/`.heic` that are actually JPEGs) — re-tags via temp-rename to bypass exiftool's extension check; (c) remaining files — derives date from `Photos from YYYY-MM` directory name. Generates `fixed_files.txt` listing all fixed files for selective re-upload. Supports `--dry-run`.

7. **`reupload_fixed.py`** — Safely deletes wrong-date files from Google Photos albums and prepares for re-upload. Only touches files listed in `fixed_files.txt`, only in albums that match exactly by name, and only if the Google Photos date falls within the upload window (`--since` is required — set it to your rclone upload start date to avoid touching pre-existing photos). Runs audit-first (no `--execute` = dry run). Writes `reupload_audit.log` before any deletion and `reupload_deletions.log` during execution. Batches deletions per album via `rclone delete --files-from-raw`.

## Data layout

```
~/photos/
  raw_from_drive/     ← Takeout ZIPs go here
  staged/             ← extracted output lands here
```

Pass `--data-dir ~/photos` to each script, or set `export TAKEOUT_DATA_DIR=~/photos` once in your shell.

## Setup

```bash
# 1. Install uv (fast Python package manager, replaces pip + venv)
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. Install exiftool (system package)
sudo apt-get install -y libimage-exiftool-perl

# 3. Install all Python dependencies (creates .venv automatically)
uv sync --extra dev

# 4. Install git hooks (runs ruff on commit, pytest on push)
uv run pre-commit install --hook-type pre-commit --hook-type pre-push
```

To confirm everything is ready:

```bash
exiftool -ver                             # should print a version number (e.g. 12.76)
uv run python -c "import timezonefinder; print('ok')"
uv run pytest -q                          # all tests should pass
```

### Running tests

```bash
uv run pytest -q                                        # all tests (unit + integration)
uv run pytest tests/test_metadata.py -q                 # unit tests only (no exiftool needed)
uv run pytest tests/test_exiftool_integration.py -v     # integration tests (requires exiftool)
```

Integration tests in `tests/test_exiftool_integration.py` call real exiftool and are skipped automatically if it is not installed. They verify that `run_exiftool_batch` correctly writes EXIF tags to real JPEG and PNG files.

### Git hooks

After `pre-commit install`, hooks run automatically:

| Event | Checks |
|---|---|
| `git commit` | `ruff check --fix` (lint + auto-fix), `ruff format` |
| `git push` | `pytest -q` (full test suite) |

If ruff auto-fixes files on commit, it will abort the commit and show the changes — re-stage them (`git add`) and commit again.

## Usage

### Quick start

```bash
export TAKEOUT_DATA_DIR=~/photos   # directory with raw_from_drive/ and staged/
uv run python run.py
```

`run.py` prompts you to choose a mode:

```
What would you like to do?
  1. Fix metadata in a Google Takeout download
  2. Fix metadata + upload to Google Photos

Choice [1/2]:
```

Or skip the prompt with `--mode`:

```bash
uv run python run.py --mode local                # fix metadata only
uv run python run.py --mode upload               # fix metadata + upload to Google Photos
uv run python run.py --mode upload-only          # skip metadata steps, upload only (use after quota reset)
uv run python run.py --data-dir ~/photos --mode local   # override data dir inline
```

`upload-only` is the right command for day 2+: metadata is already fixed, the upload just needs to resume. It prints current progress (files uploaded vs total) before starting rclone.

`upload` and `upload-only` run these steps in order, stopping on any failure:

| Step | Script | What it does |
|---|---|---|
| 1 | `extract_and_stage.py` | Extract Takeout ZIPs; delete each after success |
| 2 | `split_year_albums.py` | Split "Photos from YEAR" folders into monthly sub-albums |
| 3 | `fix_extensions.py` | Rename fake-extension JPEGs (Google exports some as .png, .heic, etc.) |
| 4 | `precheck.py` | Sanity checks — exits non-zero on failure |
| 5 | `fix_metadata.py` | Write EXIF dates, GPS, and captions from JSON sidecars |
| 6 | `fix_missing_dates.py` | Fix files still missing `DateTimeOriginal` after step 5 |
| 7 _(upload only)_ | rclone | Upload staged files to Google Photos |

All steps are safe to re-run — already-extracted files are skipped, collisions are skipped, and exiftool is idempotent.

### Re-upload wrong-date files (if needed)

If some files landed in Google Photos with incorrect dates after upload, fix and re-upload them:

```bash
uv run python reupload_fixed.py --since YYYY-MM-DD               # audit only
uv run python reupload_fixed.py --since YYYY-MM-DD --execute     # delete + re-upload
```

Replace `YYYY-MM-DD` with the date you started your rclone upload (e.g. `2024-03-15`). Only photos with a Google Photos date on or after that date will be considered for deletion.

### Running steps individually

Each script can also be run standalone if you need to re-run a single step:

```bash
# Steps 1-3: stdlib only
python3 extract_and_stage.py --data-dir ~/photos
python3 split_year_albums.py
python3 fix_extensions.py

# Steps 4-6: need the venv (uv run ensures it)
uv run python precheck.py
uv run python fix_metadata.py --data-dir ~/photos
uv run python fix_missing_dates.py

# Upload manually (shows rclone's session-only progress, not the overall count run.py shows)
rclone copy "$TAKEOUT_DATA_DIR/staged/Takeout/Google Photos" \
  "google-photos:album" --transfers 4 --tpslimit 3 --exclude "*.json" \
  --log-file rclone_upload.log --log-level NOTICE --stats 30s
```


---

## Timezone handling

Google stores `photoTakenTime` as a **UTC Unix timestamp** in the sidecar JSON. The correct local time depends on where the photo was taken, which varies by photo — a camera set to local time in Tokyo gives a different EXIF time than one set to local time in New York for the same UTC instant. Simple UTC-offset math is wrong for anyone who has travelled or crossed a DST boundary.

**How the conversion works:**

1. GPS coordinates are read from the sidecar (see GPS section below).
2. If GPS is available, `timezonefinder` looks up the IANA timezone name for those coordinates (e.g. `America/New_York`).
3. Python's `zoneinfo` module converts the UTC timestamp to the correct local wall-clock time, including historical DST rules.
4. The result is written as `DateTimeOriginal` — the time the shutter fired, in the local time zone where the photo was taken.

**Fallback:** If the photo has no GPS, or `timezonefinder` is not installed, the UTC timestamp is written directly. The **date** will always be correct. The **clock time** may be off by your UTC offset (e.g. a photo taken at 3pm EST is written as 8pm UTC). Install `timezonefinder` to avoid this.

```bash
uv sync --extra dev   # timezonefinder is included; enables DST-correct local time from GPS
```

**GPS date/time stamps** (`GPSDateStamp`, `GPSTimeStamp`) are always written in UTC regardless of timezone — this is required by the EXIF specification. `DateTimeOriginal` is local time; GPS timestamps are UTC. Both are written.

---

## GPS handling

The Takeout sidecar contains two GPS fields:

| Field | Meaning |
|---|---|
| `geoDataExif` | Original GPS from the camera at capture time |
| `geoData` | Google's copy, which may be rounded or edited |

**`geoDataExif` is always preferred.** If it is zero or missing, `geoData` is used as a fallback. If both are zero (lat ≈ 0, lon ≈ 0 within 0.001°), no GPS is written — zero coordinates mean "no data", not the Gulf of Guinea.

**Altitude** is written with the correct reference:
- Positive altitude → `GPSAltitudeRef = Above Sea Level`
- Negative altitude → `GPSAltitudeRef = Below Sea Level`, and the stored value is the absolute magnitude (EXIF stores altitude as unsigned, the ref indicates sign)

All four written GPS fields:

| EXIF tag | Value |
|---|---|
| `GPSLatitude` | Absolute degrees |
| `GPSLatitudeRef` | `N` or `S` |
| `GPSLongitude` | Absolute degrees |
| `GPSLongitudeRef` | `E` or `W` |
| `GPSAltitude` | Absolute metres |
| `GPSAltitudeRef` | `Above Sea Level` or `Below Sea Level` |
| `GPSDateStamp` | UTC date (`YYYY:MM:DD`) |
| `GPSTimeStamp` | UTC time (`HH:MM:SS`) |

---

## Metadata written per file type

Different file formats require different EXIF tag namespaces. The script detects the file type by extension and writes to the correct tags.

**JPEG, HEIC, TIFF (and most images):**

| Tag | Value |
|---|---|
| `DateTimeOriginal` | Local datetime (UTC → local via GPS timezone) |
| `CreateDate` | Same as DateTimeOriginal |
| `ModifyDate` | Same as DateTimeOriginal |

**PNG:**
PNG does not support standard EXIF date tags. XMP is used instead:

| Tag | Value |
|---|---|
| `XMP-exif:DateTimeOriginal` | Local datetime |
| `XMP-xmp:CreateDate` | Local datetime |
| `PNG:CreationTime` | Local datetime |

**Video (MP4, MOV, M4V, 3GP, MPG):**
Video containers use QuickTime-style metadata atoms:

| Tag | Value |
|---|---|
| `QuickTime:CreateDate` | Local datetime |
| `QuickTime:ModifyDate` | Local datetime |
| `TrackCreateDate` | Local datetime |
| `TrackModifyDate` | Local datetime |
| `MediaCreateDate` | Local datetime |
| `MediaModifyDate` | Local datetime |

**Unsupported video formats (MKV, WebM, AVI, WMV):** exiftool cannot write metadata to these containers. Files of these types are counted as `Exiftool errors` in the summary — this is expected and harmless. The files themselves are untouched.

**All file types (when present in sidecar):**

| Tag | Value |
|---|---|
| `ImageDescription` | User caption from `description` field |
| `XMP-dc:Description` | Same caption (for XMP-aware readers) |
| GPS tags | See GPS section above |

**What is never touched:** Camera make, model, lens, aperture, shutter speed, ISO, focal length, and all other camera EXIF tags already in the file are left completely untouched. Only the fields above are written.

**No backup files are created.** exiftool normally writes a `filename_original` backup alongside each file it modifies. This script passes `-overwrite_original` to suppress that, because a large photo library would temporarily require double the disk space. The original Takeout ZIPs (on the flash drive) are the backup.

---

## Edge cases handled

**Epoch timestamps** — Some sidecars have `photoTakenTime.timestamp = "0"` (no date recorded). These are silently skipped; `1970-01-01` is never written into EXIF.

**Duplicate filenames** — When Google Takeout has two files with the same name, it appends `(N)` to the media filename but puts the `(N)` at the end of the sidecar filename before `.json`:

| Media file | Sidecar |
|---|---|
| `photo(1).jpg` | `photo.jpg.supplemental-metadata(1).json` |
| `photo(2).jpg` | `photo.jpg.supplemental-metadata(2).json` |

Both `fix_metadata.py` and `split_year_albums.py` detect this pattern and match correctly.

**Truncated sidecar names** — When the full sidecar name would exceed filesystem limits, Google progressively shortens the suffix:

| Sidecar suffix | Notes |
|---|---|
| `.supplemental-metadata.json` | Full name |
| `.supple.json` | Truncated |
| `.suppl.json` | |
| `.supp.json` | |
| `.sup.json` | |
| `.json` | Oldest format |

The above variants are tried in order for every file. A fuzzy prefix match (first 40 characters of the filename) catches any remaining unusual truncation lengths.

**Truncated + duplicate** — Both truncation and `(N)` duplication can occur on the same file, e.g. `photo(1).jpg` → `photo.jpg.suppl(1).json`. This is also handled.

**Fake extensions** — Google internally transcodes many uploads to JPEG but preserves the original filename. A photo originally named `IMG_001.png` may be a valid JPEG inside, causing exiftool to reject it (`Not a valid PNG (looks more like a JPEG)`). `fix_extensions.py` detects these by checking the JPEG magic bytes (`\xff\xd8\xff`) and renames both the file and its sidecar to `.jpg` before `fix_metadata.py` runs.

---

## Upload

Upload uses `rclone` with the Google Photos backend. The `google-photos:album` remote maps each subdirectory in the staged Takeout to a Google Photos album. Run via `run.py` rather than rclone directly so failures are diagnosed automatically:

```bash
uv run python run.py --mode upload-only   # resume upload (metadata already fixed)
uv run python run.py --mode upload        # fix metadata + upload (first run)
```

On failure, `run.py` tails `rclone_upload.log` and surfaces the root cause:

- **Token expired** — `rclone config reconnect google-photos:` then re-run
- **Quota exceeded** — wait until midnight Pacific and re-run

rclone skips already-uploaded files, so re-running is always safe. While uploading, `run.py` shows a live progress line every few seconds:

```
  12,450/50,000 (25%)  37,550 left  ~28.4 GB  46 KiB/s  ≥4 days
```

The file count combines the pre-session snapshot with transfers logged in `rclone_upload.log`. The days estimate is `remaining ÷ 10,000` (the daily API (Application Programming Interface) quota).

### Concurrency and rate-limiting

The upload runs **4 parallel transfers** (`--transfers 4`) capped at **3 API requests per second** (`--tpslimit 3`). These are the defaults baked into `run.py`.

The Google Photos API enforces a daily quota (~10,000 requests/day on the free tier). For a large library this can take several weeks of daily re-runs. Tuning options:

| Flag | Default | Effect |
|---|---|---|
| `--transfers N` | 4 | Parallel file uploads. Higher values don't help much — the bottleneck is the API quota, not throughput. |
| `--tpslimit N` | 3 | Max API requests per second. Reducing this (e.g. `--tpslimit 1`) can prevent mid-session 429 errors but doesn't increase the daily quota. |

To override, run rclone directly (note: this shows rclone's session-only file count, not the overall count that `run.py` computes):

```bash
rclone copy "$TAKEOUT_DATA_DIR/staged/Takeout/Google Photos" \
  "google-photos:album" --transfers 4 --tpslimit 3 --exclude "*.json" \
  --log-file rclone_upload.log --log-level NOTICE --stats 30s
```
