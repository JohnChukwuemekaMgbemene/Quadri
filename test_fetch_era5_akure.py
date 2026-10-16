import sys
import tempfile
import types
import unittest
import zipfile
from pathlib import Path

sys.modules.setdefault("cdsapi", types.ModuleType("cdsapi"))
sys.modules["cdsapi"].Client = object

xarray_stub = types.ModuleType("xarray")
xarray_stub.open_dataset = object()
sys.modules.setdefault("xarray", xarray_stub)

google_stub = types.ModuleType("google")
oauth2_stub = types.ModuleType("google.oauth2")
credentials_stub = types.ModuleType("google.oauth2.credentials")
credentials_stub.Credentials = object

sys.modules.setdefault("google", google_stub)
sys.modules.setdefault("google.oauth2", oauth2_stub)
sys.modules.setdefault("google.oauth2.credentials", credentials_stub)

googleapiclient_stub = types.ModuleType("googleapiclient")
discovery_stub = types.ModuleType("googleapiclient.discovery")
discovery_stub.build = object
errors_stub = types.ModuleType("googleapiclient.errors")
errors_stub.HttpError = type("HttpError", (Exception,), {})
http_stub = types.ModuleType("googleapiclient.http")
http_stub.MediaFileUpload = object

sys.modules.setdefault("googleapiclient", googleapiclient_stub)
sys.modules.setdefault("googleapiclient.discovery", discovery_stub)
sys.modules.setdefault("googleapiclient.errors", errors_stub)
sys.modules.setdefault("googleapiclient.http", http_stub)

from fetch_era5_akure import extract_downloaded_file, validate_netcdf


class ExtractDownloadedFileTests(unittest.TestCase):
    def test_returns_original_zip_path_without_opening_it(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            archive_path = root / "era5_akure_2006_01.zip"
            nested_dir = root / "bundle"
            nested_dir.mkdir()
            netcdf_path = nested_dir / "era5_akure_2006_01.nc"
            netcdf_path.write_bytes(b"fake-netcdf-bytes")

            with zipfile.ZipFile(archive_path, "w") as zf:
                zf.write(netcdf_path, arcname="bundle/era5_akure_2006_01.nc")

            self.assertEqual(extract_downloaded_file(archive_path), archive_path)
            self.assertTrue(archive_path.is_file())
            self.assertFalse(any(p.name.endswith("_extracted") for p in root.iterdir()))

    def test_returns_original_path_for_plain_netcdf(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "plain.nc"
            path.write_bytes(b"plain")
            self.assertEqual(extract_downloaded_file(path), path)


if __name__ == "__main__":
    unittest.main()
