# photos-takeout-import

Migrate a Google Takeout photo archive to a new Google Photos account.

## What it does

1. **`extract_and_stage.py`** — Extracts all Takeout ZIP files into a staging directory, merging albums that are split across multiple ZIPs. Skips auto-generated `Photos from YEAR` albums. Deletes each ZIP after extraction to stay within disk space limits.

2. **`fix_metadata.py`** — Reads each photo's `.supplemental-metadata.json` sidecar (handles all truncated variants) and writes correct dates, GPS, and captions back into the file's EXIF using exiftool. Timezone-aware: looks up local time from GPS coordinates (DST-correct) when available.

## Data layout

```
~/photos/
  raw_from_drive/     ← Takeout ZIPs go here
  staged/             ← extracted output lands here
```

Code lives in `~/portainer/photos-takeout-import/`.

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
# Step 1: extract all ZIPs (deletes each ZIP after extraction)
python3 extract_and_stage.py

# Step 2: fix EXIF metadata from JSON sidecars
venv/bin/python fix_metadata.py
```

Both scripts are safe to re-run — already-extracted files are skipped, and exiftool is idempotent.

## Metadata written per file

| Field | Source |
|---|---|
| DateTimeOriginal / CreateDate | `photoTakenTime.timestamp` (UTC → local via GPS timezone lookup) |
| GPSLatitude/Longitude | `geoDataExif` preferred, falls back to `geoData` |
| GPSAltitude + AltitudeRef | Correct above/below sea level |
| GPSDateStamp / GPSTimeStamp | UTC timestamp (per EXIF spec) |
| ImageDescription / XMP-dc:Description | `description` field (user captions) |

Camera tags (make, model, aperture, ISO, etc.) are in the original files and are never touched.

## Step 3: Upload

TODO — gphotos-uploader-cli OAuth setup and config.
