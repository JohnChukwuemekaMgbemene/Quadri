# ERA5 Akure Monthly Downloader

Download-only Python worker: requests monthly ERA5 single-level data as NetCDF, validates each file, uploads it to Google Drive, and removes the temporary local file. It does not extract grid points or create a CSV.

## Fixed settings

Edit `START_YEAR` and `END_YEAR` in `fetch_era5_akure.py` (defaults: 2006–2025). The CDS API endpoint is hardcoded as `https://cds.climate.copernicus.eu/api`.

The downloader requests these eight variables: 10 m U/V wind, 100 m U/V wind, gust since previous post-processing, instantaneous 10 m gust, 2 m temperature, and surface pressure. It downloads a small box around Akure and leaves nearest-grid extraction to your later local script.

## Required environment variables

- `CDSAPI_KEY` — your Copernicus CDS personal access token
- `GOOGLE_CLIENT_ID`
- `GOOGLE_CLIENT_SECRET`
- `GOOGLE_REFRESH_TOKEN`
- `GOOGLE_DRIVE_FOLDER_ID` — ID of the destination Drive folder
- Optional: `LOG_LEVEL` (default `INFO`)

`CDSAPI_URL` is intentionally not an environment variable. The CDS URL is public; only the token is secret.

## Local installation and smoke test

Use Python 3.11+:

```bash
pip install -r requirements.txt
```

Set the environment variables in your shell or keep them in a local `.env` file next to `fetch_era5_akure.py`; the script loads that file automatically when present. Do not commit a real `.env` file.

Test one month (December 2025):

```bash
python fetch_era5_akure.py --year 2025 --start-month 12 --end-month 12
```

Run all months for the configured period:

```bash
python fetch_era5_akure.py
```

To run a specific range of months for each configured year, use `--start-month` and `--end-month`. To test one month from a different year, use `--year YYYY --start-month M --end-month M`.

## Railway

Deploy this repository as a worker/background service, not a web service. Set the five required variables in Railway's Variables panel. Run command:

```bash
python fetch_era5_akure.py
```

The application stores only a single monthly file in temporary local storage at a time and deletes it after upload. Google Drive is the persistent destination. Railway may stop or restart the process; rerun it to continue. Existing non-empty files with expected names in the Drive year folders are skipped.

## Drive layout

The configured root folder receives year folders such as `2006/`, each containing `era5_akure_2006_01.nc`, etc. The Google account represented by the OAuth refresh token must have write access to the destination folder. The refresh token must have been granted a Drive scope that allows listing, creating, and updating files. If not, re-authorize OAuth with appropriate Drive access and generate a new refresh token.

## Validation and resumption notes

Each new download is opened as NetCDF and checked for all eight expected fields and the expected number of hourly timestamps before upload. The uploaded file's size and, if supplied by Drive, MD5 checksum are checked. A month is considered complete after successful upload verification.

For resumption, an existing non-trivial file with the expected name is skipped. The script does not download existing Drive files to revalidate them; delete a suspect file from Drive to force a fresh download. Failed months are logged and do not stop later months from being attempted; rerun to retry failures.

## Important CDS note

Confirm that the dataset licence has been accepted in CDS and that the CDS API credentials are current. If CDS changes its request schema or variable availability, inspect the API error and update the request accordingly.
