"""Download monthly ERA5 single-level NetCDF files and upload them to Google Drive.

Run locally or as a Railway worker. Completed monthly files are kept in Drive;
temporary local downloads are deleted after a verified upload.
"""
from __future__ import annotations

import argparse
import calendar
import hashlib
import logging
import os
import random
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any

import cdsapi
import xarray as xr
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

# ---------------------------------------------------------------------------
# EDIT THESE SETTINGS IN CODE
# ---------------------------------------------------------------------------
CDS_API_URL = "https://cds.climate.copernicus.eu/api"  # Public endpoint, not secret
START_YEAR = 2006
END_YEAR = 2025

# Approximate Akure Airport coordinates; verify against the Nigeria AIP if needed.
LAT = 7.25
LON = 5.30
AREA = [7.50, 5.00, 7.00, 5.50]  # [North, West, South, East]

DATASET = "reanalysis-era5-single-levels"
VARIABLES = [
    "10m_u_component_of_wind",
    "10m_v_component_of_wind",
    "100m_u_component_of_wind",
    "100m_v_component_of_wind",
    "10m_wind_gust_since_previous_post_processing",
    "instantaneous_10m_wind_gust",
    "2m_temperature",
    "surface_pressure",
]
FILE_PREFIX = "era5_akure"
DRIVE_FOLDER_MIME = "application/vnd.google-apps.folder"
DRIVE_FILE_MIME = "application/x-netcdf"
GOOGLE_SCOPES = ["https://www.googleapis.com/auth/drive"]
MAX_RETRIES = 4
BASE_RETRY_WAIT_SECONDS = 30
MAX_RETRY_WAIT_SECONDS = 900
CHUNK_SIZE = 8 * 1024 * 1024

log = logging.getLogger("era5-akure")


def load_local_dotenv() -> None:
    env_path = Path(__file__).with_name(".env")
    if not env_path.is_file():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not name or name in os.environ:
            continue

        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ[name] = value


def configure_logging() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def make_cds_client() -> cdsapi.Client:
    # Explicit URL/key means no ~/.cdsapirc file is required.
    return cdsapi.Client(url=CDS_API_URL, key=required_env("CDSAPI_KEY"))


def make_drive_service():
    credentials = Credentials(
        token=None,
        refresh_token=required_env("GOOGLE_REFRESH_TOKEN"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=required_env("GOOGLE_CLIENT_ID"),
        client_secret=required_env("GOOGLE_CLIENT_SECRET"),
        scopes=GOOGLE_SCOPES,
    )
    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def escape_query(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def list_children(service, parent_id: str, name: str) -> list[dict[str, Any]]:
    query = (
        f"name = '{escape_query(name)}' and "
        f"'{escape_query(parent_id)}' in parents and trashed = false"
    )
    results: list[dict[str, Any]] = []
    page_token = None
    while True:
        response = service.files().list(
            q=query,
            pageSize=100,
            pageToken=page_token,
            fields="nextPageToken,files(id,name,mimeType,size,md5Checksum,modifiedTime)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        results.extend(response.get("files", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            return results


def get_or_create_year_folder(service, root_id: str, year: int) -> str:
    name = str(year)
    for item in list_children(service, root_id, name):
        if item.get("mimeType") == DRIVE_FOLDER_MIME:
            return item["id"]
    folder = service.files().create(
        body={"name": name, "mimeType": DRIVE_FOLDER_MIME, "parents": [root_id]},
        fields="id,name",
        supportsAllDrives=True,
    ).execute()
    log.info("Created Drive folder %s", name)
    return folder["id"]


def find_file(service, parent_id: str, filename: str) -> dict[str, Any] | None:
    matches = [
        item for item in list_children(service, parent_id, filename)
        if item.get("mimeType") != DRIVE_FOLDER_MIME
    ]
    if not matches:
        return None
    matches.sort(key=lambda item: item.get("modifiedTime", ""), reverse=True)
    return matches[0]


def extract_downloaded_file(path: Path) -> Path:
    if not path.exists():
        raise FileNotFoundError(f"Downloaded file not found: {path}")

    if zipfile.is_zipfile(path):
        extraction_dir = path.parent / f"{path.stem}_extracted"
        extraction_dir.mkdir(exist_ok=True)
        with zipfile.ZipFile(path, "r") as archive:
            members = [member for member in archive.namelist() if member.lower().endswith(".nc")]
            if not members:
                raise RuntimeError(f"Downloaded zip archive {path.name} does not contain any .nc files")
            archive.extract(members[0], extraction_dir)

        extracted_path = extraction_dir / Path(members[0]).name
        if not extracted_path.exists():
            extracted_path = extraction_dir / members[0]
        if extracted_path.exists():
            return extracted_path
        raise RuntimeError(f"Extracted NetCDF file not found for {path.name}")

    return path


def md5_file(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def upload_and_verify(service, path: Path, parent_id: str, filename: str) -> None:
    existing = find_file(service, parent_id, filename)
    media = MediaFileUpload(
        str(path), mimetype=DRIVE_FILE_MIME, resumable=True, chunksize=CHUNK_SIZE
    )
    fields = "id,name,size,md5Checksum,modifiedTime"
    if existing:
        result = service.files().update(
            fileId=existing["id"], media_body=media, fields=fields,
            supportsAllDrives=True,
        ).execute()
    else:
        result = service.files().create(
            body={"name": filename, "parents": [parent_id]},
            media_body=media, fields=fields, supportsAllDrives=True,
        ).execute()

    local_size = path.stat().st_size
    try:
        remote_size = int(result.get("size", -1))
    except (TypeError, ValueError):
        remote_size = -1
    if remote_size != local_size:
        raise RuntimeError(
            f"Drive size verification failed for {filename}: "
            f"local={local_size}, Drive={remote_size}"
        )
    remote_md5 = result.get("md5Checksum")
    if remote_md5 and remote_md5.lower() != md5_file(path).lower():
        raise RuntimeError(f"Drive MD5 verification failed for {filename}")
    log.info("[uploaded] %s (%s bytes)", filename, local_size)


def month_filename(year: int, month: int) -> str:
    return f"{FILE_PREFIX}_{year}_{month:02d}.nc"


def build_request(year: int, month: int) -> dict[str, Any]:
    days = calendar.monthrange(year, month)[1]
    return {
        "product_type": ["reanalysis"],
        "variable": VARIABLES,
        "year": [str(year)],
        "month": [f"{month:02d}"],
        "day": [f"{day:02d}" for day in range(1, days + 1)],
        "time": [f"{hour:02d}:00" for hour in range(24)],
        "area": AREA,
        "data_format": "netcdf",
        "download_format": "unarchived",
    }


def validate_netcdf(path: Path, year: int, month: int) -> tuple[int, list[str]]:
    if not path.is_file() or path.stat().st_size < 1024:
        raise RuntimeError("Downloaded file is missing or implausibly small")

    aliases = {
        "10m_u_component_of_wind": {"u10", "10m_u_component_of_wind"},
        "10m_v_component_of_wind": {"v10", "10m_v_component_of_wind"},
        "100m_u_component_of_wind": {"u100", "100m_u_component_of_wind"},
        "100m_v_component_of_wind": {"v100", "100m_v_component_of_wind"},
        "10m_wind_gust_since_previous_post_processing": {
            "fg10", "10m_wind_gust_since_previous_post_processing"
        },
        "instantaneous_10m_wind_gust": {"i10fg", "instantaneous_10m_wind_gust"},
        "2m_temperature": {"t2m", "2m_temperature"},
        "surface_pressure": {"sp", "surface_pressure"},
    }
    with xr.open_dataset(path, engine="netcdf4") as ds:
        names = set(ds.data_vars)
        if not names:
            raise RuntimeError("NetCDF contains no data variables")
        time_name = next(
            (name for name in ("valid_time", "time") if name in ds.variables), None
        )
        if time_name is None:
            raise RuntimeError("No 'valid_time' or 'time' coordinate found")
        timestamps = ds[time_name].values
        expected_hours = 24 * calendar.monthrange(year, month)[1]
        if len(timestamps) != expected_hours:
            raise RuntimeError(
                f"Expected {expected_hours} hourly timestamps, found {len(timestamps)}"
            )
        missing = [name for name, options in aliases.items() if not (names & options)]
        if missing:
            raise RuntimeError(
                f"Missing expected variable(s): {', '.join(missing)}. "
                f"Variables present: {sorted(names)}"
            )
        # Ensure the timestamps belong to the requested calendar month.
        first = str(timestamps[0])
        if not first.startswith(f"{year}-{month:02d}"):
            raise RuntimeError(f"Unexpected first timestamp: {first}")
        return len(timestamps), sorted(names)


def process_month(client, drive, root_id: str, year: int, month: int) -> str:
    filename = month_filename(year, month)
    year_folder = get_or_create_year_folder(drive, root_id, year)
    existing = find_file(drive, year_folder, filename)
    # Drive cannot validate an existing file without downloading it. We skip a
    # non-trivial existing file for resumability; delete it manually to force refresh.
    if existing:
        try:
            if int(existing.get("size", 0)) >= 1024:
                log.info("[skip] %s already exists in Drive (%s bytes)", filename, existing.get("size"))
                return "skipped"
        except (TypeError, ValueError):
            pass

    request = build_request(year, month)
    last_error: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        log.info("[get] %s (attempt %d/%d)", filename, attempt, MAX_RETRIES)
        try:
            with tempfile.TemporaryDirectory(prefix="era5_akure_") as temp_dir:
                path = Path(temp_dir) / filename
                client.retrieve(DATASET, request, str(path))
                extracted_path = extract_downloaded_file(path)
                hours, found = validate_netcdf(extracted_path, year, month)
                log.info("[valid] %s: %d timestamps; variables: %s", filename, hours, ", ".join(found))
                upload_and_verify(drive, extracted_path, year_folder, filename)
            log.info("[done] %s", filename)
            return "downloaded"
        except Exception as exc:
            last_error = exc
            log.exception("[error] %s attempt %d failed", filename, attempt)
            if attempt < MAX_RETRIES:
                wait = min(MAX_RETRY_WAIT_SECONDS, BASE_RETRY_WAIT_SECONDS * (2 ** (attempt - 1)))
                wait += random.uniform(0, min(10, wait * 0.2))
                log.info("Retrying in %.0f seconds", wait)
                time.sleep(wait)
    raise RuntimeError(f"Failed {filename} after {MAX_RETRIES} attempts") from last_error


def main() -> int:
    load_local_dotenv()
    configure_logging()
    parser = argparse.ArgumentParser(description="Download monthly ERA5 files to Google Drive")
    parser.add_argument("--year", type=int, help="Test or rerun one specific year")
    parser.add_argument("--start-month", type=int, default=1)
    parser.add_argument("--end-month", type=int, default=12)
    args = parser.parse_args()

    start_year = args.year if args.year is not None else START_YEAR
    end_year = args.year if args.year is not None else END_YEAR
    if start_year > end_year:
        parser.error("start year must be <= end year")
    if not (1 <= args.start_month <= 12 and 1 <= args.end_month <= 12):
        parser.error("month values must be from 1 to 12")
    if args.start_month > args.end_month:
        parser.error("--start-month must be <= --end-month")

    root_id = required_env("GOOGLE_DRIVE_FOLDER_ID")
    required_env("CDSAPI_KEY")
    required_env("GOOGLE_CLIENT_ID")
    required_env("GOOGLE_CLIENT_SECRET")
    required_env("GOOGLE_REFRESH_TOKEN")

    log.info("ERA5 Akure monthly downloader starting: years %d-%d, months %02d-%02d",
             start_year, end_year, args.start_month, args.end_month)
    client = make_cds_client()
    drive = make_drive_service()
    total = (end_year - start_year + 1) * (args.end_month - args.start_month + 1)
    done = skipped = 0
    failures: list[str] = []
    processed = 0

    for year in range(start_year, end_year + 1):
        for month in range(args.start_month, args.end_month + 1):
            processed += 1
            log.info("Progress %d/%d | %04d-%02d", processed, total, year, month)
            try:
                status = process_month(client, drive, root_id, year, month)
                if status == "skipped":
                    skipped += 1
                else:
                    done += 1
            except Exception as exc:
                failures.append(f"{year}-{month:02d}: {exc}")
                log.error("Month failed after retries; continuing to next month")

    log.info("Run summary: downloaded=%d, skipped=%d, failed=%d, total=%d",
             done, skipped, len(failures), total)
    if failures:
        for failure in failures:
            log.error("FAILED: %s", failure)
        log.error("Rerun the app to retry failed months; existing Drive files will be skipped.")
        return 1
    log.info("All selected months are present in Google Drive")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log.warning("Interrupted. Rerun to resume from Drive files already uploaded.")
        sys.exit(130)
    except Exception:
        log.exception("Fatal startup error")
        sys.exit(2)
