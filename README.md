# google-photos-takeout-import

Migrate a Google Takeout photo archive to an existing Google Photos account.

## What it does

1. **`extract_and_stage.py`** — Extracts all Takeout ZIP files into a staging directory, merging albums split across multiple ZIPs. Includes `Photos from YEAR` auto-albums (which hold any photo not in a named album — skipping them would lose the bulk of the library). Deletes each ZIP only after verifying the extracted file count matches the ZIP's member list.

2. **`split_year_albums.py`** — `Photos from YEAR` directories can contain up to 58,000 files. Passing that many paths to exiftool in one call exceeds the kernel's `ARG_MAX` limit (~3.2 MB on this system) and crashes with `Argument list too long`. This script splits each year directory into `Photos from YYYY-MM` monthly sub-albums (≈2,000–5,000 files each) based on each photo's `photoTakenTime` sidecar timestamp. Handles Google's duplicate naming quirk (`STEM(N).EXT` → `STEM.EXT.supplemental-metadata(N).json`). Photos with no usable timestamp go into `Photos from YYYY-unknown`. Supports `--dry-run`.

3. **`precheck.py`** — Sanity checks to run before touching EXIF: verifies exiftool is installed, staged directory exists, sidecar coverage ≥ 90%, ≥ 5 GB free, and runs a sample pipeline test on a real photo with GPS (shows before/after EXIF and verifies the written datetime matches the computed one).

4. **`fix_metadata.py`** — Reads each photo's `.supplemental-metadata.json` sidecar and writes correct dates, GPS, and captions back into the file's EXIF using exiftool. See detailed sections below. Safe to re-run — exiftool is idempotent.

## Data layout

```
~/photos/
  raw_from_drive/     ← Takeout ZIPs go here
  staged/             ← extracted output lands here
```

Code lives in `~/portainer/google-photos-takeout-import/`.

## Setup

```bash
# Install exiftool
sudo apt-get install -y libimage-exiftool-perl

# Create .venv and install dependencies
python3 -m venv .venv
.venv/bin/pip install timezonefinder

# Activate (optional, or just prefix commands with .venv/bin/)
source .venv/bin/activate
```

## Usage

```bash
# Step 1: extract all ZIPs (deletes each ZIP after successful extraction)
python3 extract_and_stage.py

# Step 2: split 'Photos from YEAR' into monthly sub-albums (required before fix_metadata)
python3 split_year_albums.py

# Step 3: sanity checks (exits 1 on failure — fix before proceeding)
.venv/bin/python precheck.py

# Step 4: write EXIF metadata from JSON sidecars into all media files
.venv/bin/python fix_metadata.py
```

Steps 1 and 4 are safe to re-run — already-extracted files are skipped, and exiftool is idempotent. Step 2 is also re-runnable: files already in a monthly folder won't be moved again (collision detection skips them).

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
venv/bin/pip install timezonefinder   # enables DST-correct local time from GPS
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

**Video (MP4, MOV, M4V, 3GP, AVI, MKV, WMV, MPG):**
Video containers use QuickTime-style metadata atoms:

| Tag | Value |
|---|---|
| `QuickTime:CreateDate` | Local datetime |
| `QuickTime:ModifyDate` | Local datetime |
| `TrackCreateDate` | Local datetime |
| `TrackModifyDate` | Local datetime |
| `MediaCreateDate` | Local datetime |
| `MediaModifyDate` | Local datetime |

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

---

## Step 5: Upload

TODO — gphotos-uploader-cli OAuth setup and config.
