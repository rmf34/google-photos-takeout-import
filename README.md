# google-photos-takeout-import

Migrate a Google Takeout photo archive to an existing Google Photos account.

## What it does

1. **`extract_and_stage.py`** — Extracts all Takeout ZIP files into a staging directory, merging albums split across multiple ZIPs. Includes `Photos from YEAR` auto-albums (which hold any photo not in a named album). Deletes each ZIP after a successful count-verified extraction.

2. **`split_year_albums.py`** — Splits large `Photos from YEAR` directories into `Photos from YYYY-MM` monthly sub-albums based on each photo's `photoTakenTime` sidecar timestamp. Handles Google's duplicate naming quirk (`STEM(N).EXT` → `STEM.EXT.supplemental-metadata(N).json`). Photos with no usable timestamp go into `Photos from YYYY-unknown`. Supports `--dry-run`.

3. **`precheck.py`** — Sanity checks to run before touching EXIF: verifies exiftool is installed, staged directory exists, sidecar coverage ≥ 90%, ≥ 5 GB free, and runs a sample pipeline test on a real photo with GPS.

4. **`fix_metadata.py`** — Reads each photo's `.supplemental-metadata.json` sidecar and writes correct dates, GPS, and captions back into the file's EXIF using exiftool. Timezone-aware: converts UTC timestamps to local time via GPS coordinate lookup (DST-correct). Safe to re-run — exiftool is idempotent.

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

# Create venv and install timezone library
python3 -m venv venv
venv/bin/pip install timezonefinder
```

## Usage

```bash
# Step 1: extract all ZIPs (deletes each ZIP after successful extraction)
python3 extract_and_stage.py

# Step 2: split 'Photos from YEAR' into monthly sub-albums
python3 split_year_albums.py

# Step 3: sanity checks (exits 1 on failure — fix before proceeding)
venv/bin/python precheck.py

# Step 4: write EXIF metadata from JSON sidecars into all media files
venv/bin/python fix_metadata.py
```

Steps 1 and 4 are safe to re-run — already-extracted files are skipped, and exiftool is idempotent. Step 2 is also re-runnable: files already in a monthly folder won't be moved again (collision detection skips them).

## Metadata written per file

| Field | Source |
|---|---|
| DateTimeOriginal / CreateDate | `photoTakenTime.timestamp` (UTC → local via GPS timezone lookup) |
| GPSLatitude/Longitude | `geoDataExif` preferred, falls back to `geoData` |
| GPSAltitude + AltitudeRef | Correct above/below sea level |
| GPSDateStamp / GPSTimeStamp | UTC timestamp (per EXIF spec) |
| ImageDescription / XMP-dc:Description | `description` field (user captions) |

Camera tags (make, model, aperture, ISO, etc.) are in the original files and are never touched.

## Sidecar filename variants handled

Google Takeout truncates sidecar names when the total path would be too long, and uses a different duplicate-naming scheme for media files:

| Media file | Sidecar |
|---|---|
| `photo.jpg` | `photo.jpg.supplemental-metadata.json` |
| `photo.jpg` | `photo.jpg.supple.json` (truncated) |
| `photo.jpg` | `photo.jpg.suppl.json` |
| `photo.jpg` | `photo.jpg.supp.json` |
| `photo.jpg` | `photo.jpg.sup.json` |
| `photo.jpg` | `photo.jpg.json` (oldest format) |
| `photo(1).jpg` | `photo.jpg.supplemental-metadata(1).json` |
| `photo(1).jpg` | `photo.jpg.suppl(1).json` (truncated + duplicate) |

## Step 5: Upload

TODO — gphotos-uploader-cli OAuth setup and config.
