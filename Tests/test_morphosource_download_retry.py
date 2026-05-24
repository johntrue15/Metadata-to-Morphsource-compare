"""Unit tests for the transient-network retry logic in
``.github/scripts/nninteractive_compare.py::_download``.

The Lacerta viridis run (26372422562) failed mid-download with
``IncompleteRead(82 MB read, 225 MB more expected)``. We retry up to 3
times on transient network errors, with exponential backoff, and clean
up partial .zip files between attempts.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / ".github" / "scripts"))


def _run_download(media_id, dest, *, max_retries=3, side_effects=None,
                  sleep_fn=lambda s: None):
    """Drive ``nninteractive_compare._download`` with a scripted
    ``download_media`` side-effect sequence.
    """
    import nninteractive_compare as mod

    calls = []
    seq = iter(side_effects or [])

    def fake_download_media(mid, d):
        calls.append((mid, d))
        try:
            return next(seq)
        except StopIteration:
            raise AssertionError(
                "fake_download_media called more times than scripted")

    # The module imports ``download_media`` lazily inside _download via
    # `from morphosource_api_download import download_media`. We provide
    # a stub module so that import resolves to our fake.
    stub_mod = type(sys)("morphosource_api_download")
    stub_mod.download_media = fake_download_media

    with patch.dict(sys.modules, {"morphosource_api_download": stub_mod}), \
         patch.object(mod, "time", new=type("T", (), {"sleep": sleep_fn})):
        result = mod._download(media_id, dest, max_retries=max_retries)
    return result, calls


class DownloadRetryTests(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    # --- Success on first try ---------------------------------------
    def test_succeeds_immediately(self):
        res, calls = _run_download(
            "000048796", self.tmp,
            side_effects=[{"success": True, "media_id": "000048796",
                           "downloaded_file": "x.zip", "file_size": 100}],
        )
        self.assertTrue(res["success"])
        self.assertEqual(len(calls), 1)

    # --- Retry on transient IncompleteRead ---------------------------
    def test_retries_on_incomplete_read(self):
        transient_err = {
            "success": False,
            "media_id": "000048796",
            "error": ("Download error: ('Connection broken: "
                      "IncompleteRead(82263837 bytes read, 225621186 "
                      "more expected)', IncompleteRead(...))"),
        }
        ok = {"success": True, "media_id": "000048796",
              "downloaded_file": "x.zip", "file_size": 100}

        # Simulate two failures then success
        slept = []
        res, calls = _run_download(
            "000048796", self.tmp,
            side_effects=[transient_err, transient_err, ok],
            sleep_fn=lambda s: slept.append(s),
        )
        self.assertTrue(res["success"], res)
        self.assertEqual(len(calls), 3,
                         f"Expected 3 attempts, got {len(calls)}")
        # Exponential backoff: 5s, 10s
        self.assertEqual(slept, [5, 10])

    # --- Give up after max_retries -----------------------------------
    def test_gives_up_after_max_retries(self):
        transient_err = {
            "success": False, "media_id": "000048796",
            "error": "Connection broken: IncompleteRead(...)",
        }
        res, calls = _run_download(
            "000048796", self.tmp, max_retries=3,
            side_effects=[transient_err, transient_err, transient_err],
        )
        self.assertFalse(res["success"])
        self.assertIn("after 3 attempts", res["error"])
        self.assertEqual(len(calls), 3)

    # --- Don't retry on permanent failures ---------------------------
    def test_does_not_retry_on_non_transient(self):
        # 404, auth required, missing file - shouldn't burn a retry
        permanent_err = {
            "success": False, "media_id": "000048796",
            "error": "media not found: 404 Not Found",
        }
        res, calls = _run_download(
            "000048796", self.tmp,
            side_effects=[permanent_err],
        )
        self.assertFalse(res["success"])
        self.assertEqual(len(calls), 1,
                         "Permanent errors must NOT retry")
        # The original error message is preserved (no "after N attempts" wrap)
        self.assertEqual(res["error"], permanent_err["error"])

    # --- Partial-zip cleanup between attempts -----------------------
    def test_cleans_partial_zip_between_attempts(self):
        # Drop a partial zip into dest before the retry
        (self.tmp / "morphosource_media-id-000048796_download-abc.zip"
         ).write_bytes(b"PARTIAL DATA")
        (self.tmp / "morphosource_media-id-000048796_download-abc.zip.part"
         ).write_bytes(b"MORE PARTIAL")

        ok = {"success": True, "media_id": "000048796",
              "downloaded_file": "x.zip", "file_size": 100}
        transient = {"success": False, "media_id": "000048796",
                     "error": "Connection broken: IncompleteRead"}
        res, calls = _run_download(
            "000048796", self.tmp,
            side_effects=[transient, ok],
        )
        self.assertTrue(res["success"])
        # After cleanup, no partial zips should remain (the success path
        # would normally write a fresh one but our stub doesn't).
        remaining = list(self.tmp.glob("morphosource_media-id-*.zip*"))
        self.assertEqual(remaining, [],
                         f"Partial zips should be cleaned between attempts; "
                         f"found: {remaining}")

    # --- Cache hit short-circuits the whole thing --------------------
    def test_cache_hit_skips_network(self):
        # A cache hit needs both the zip AND an extracted sibling dir
        z = (self.tmp /
             "morphosource_media-id-000048796_download-abc.zip")
        z.write_bytes(b"cached zip")
        d = self.tmp / "morphosource_media-id-000048796_download-abc"
        d.mkdir()
        (d / "Media 000048796 - foo" / "lacerta.tif").parent.mkdir()
        (d / "Media 000048796 - foo" / "lacerta.tif").write_bytes(b"")

        # If `download_media` gets called, the test will fail because
        # the side_effects iterator is empty.
        res, calls = _run_download(
            "000048796", self.tmp, side_effects=[],
        )
        self.assertTrue(res["success"])
        self.assertTrue(res.get("from_cache"))
        self.assertEqual(len(calls), 0)


if __name__ == "__main__":
    unittest.main()
