#!/usr/bin/env python3
"""Download a MorphoSource media file by media ID.

Uses the MorphoSource download API:
  POST /api/download/{media_id}
  - Requires API key as Authorization header
  - Requires use_statement (>= 50 chars), use_categories, agreements_accepted
  - Returns a signed download URL
  - Download URL also requires the API key

Standalone module importable by slicer_tool.py or run via CLI.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import zipfile
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import unquote, urlparse

import requests

from _helpers import safe_first, load_dotenv as _do_load_dotenv, MORPHOSOURCE_API_BASE

log = logging.getLogger("MorphoDownload")

BASE = MORPHOSOURCE_API_BASE
TIMEOUT = (10, 120)

USE_STATEMENT = (
    "Automated download for comparative morphometric research analysis "
    "using AutoResearchClaw autonomous research agent workflow."
)


def header_filename(cd: Optional[str]) -> Optional[str]:
    if not cd:
        return None
    m = re.search(r'filename\*\s*=\s*[^\'"]*\'[^\'"]*\'([^;]+)', cd, re.I)
    if m:
        return unquote(m.group(1).strip())
    m = re.search(r'filename\s*=\s*"([^"]+)"', cd, re.I)
    if m:
        return m.group(1).strip()
    m = re.search(r'filename\s*=\s*([^;]+)', cd, re.I)
    if m:
        return m.group(1).strip().strip("'")
    return None


def check_media(session: requests.Session, api_key: str, media_id: str) -> Dict:
    """GET /api/media/{id} to verify media exists."""
    url = f"{BASE}/media/{media_id}"
    headers: Dict[str, str] = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = api_key
    resp = session.get(url, headers=headers, timeout=TIMEOUT)
    if resp.status_code != 200:
        log.warning("Media check %s: HTTP %d", media_id, resp.status_code)
        return {"error": f"HTTP {resp.status_code}"}
    return resp.json()


def get_visibility(api_data: Dict) -> str:
    response = api_data.get("response", api_data)
    if isinstance(response, dict):
        # Check both the media record and top-level
        media = response.get("media", response)
        if isinstance(media, dict):
            vis = media.get("visibility_ssi") or media.get("visibility")
            if vis:
                return safe_first(vis).lower()
        vis = response.get("visibility_ssi") or response.get("visibility")
        if vis:
            return safe_first(vis).lower()
    return "unknown"


def request_download_url(session: requests.Session, api_key: str, media_id: str) -> tuple[Optional[str], str]:
    """POST /api/download/{id} to obtain a signed download URL.

    Returns (download_url, error_message). One will be None.
    """
    if not api_key:
        return None, "MORPHOSOURCE_API_KEY not set — cannot request download"

    url = f"{BASE}/download/{media_id}"
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": api_key,
    }
    payload = {
        "use_statement": USE_STATEMENT,
        "use_categories": ["Research"],
        "agreements_accepted": True,
    }

    log.info("POST %s", url)
    resp = session.post(url, headers=headers, json=payload, timeout=TIMEOUT)

    if resp.status_code < 200 or resp.status_code >= 300:
        error_detail = ""
        try:
            error_detail = json.dumps(resp.json(), ensure_ascii=False)[:500]
        except Exception:
            error_detail = resp.text[:500] if resp.text else ""
        error_msg = f"Download URL request failed: HTTP {resp.status_code} — {error_detail}"
        log.warning(error_msg)
        return None, error_msg

    data = resp.json()
    log.debug("Download API response: %s", json.dumps(data)[:500])

    # Extract download URL from response
    media_node = (data.get("response") or {}).get("media") or {}
    if isinstance(media_node, dict):
        dl = media_node.get("download_url")
        if isinstance(dl, list) and dl:
            return dl[0], ""
        if isinstance(dl, str) and dl:
            return dl, ""

    # Fallback paths
    dl = (data.get("response") or {}).get("url") or data.get("url")
    if dl:
        return dl, ""

    return None, f"No download URL in response: {json.dumps(data)[:400]}"


def download_file(session: requests.Session, signed_url: str, out_dir: Path,
                  media_id: str, api_key: str = "") -> tuple[Optional[Path], str]:
    """Stream the file from the signed URL to disk.

    Returns (file_path, error_message).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    headers: Dict[str, str] = {}
    if api_key:
        headers["Authorization"] = api_key

    log.info("Downloading from signed URL...")
    try:
        with session.get(signed_url, headers=headers, stream=True,
                         allow_redirects=True, timeout=(10, 300)) as r:
            if r.status_code < 200 or r.status_code >= 300:
                return None, f"Download HTTP {r.status_code}: {r.text[:200]}"

            fname = header_filename(r.headers.get("Content-Disposition"))
            if not fname:
                fname = Path(urlparse(signed_url).path).name or f"media_{media_id}"
            fname = Path(fname).name
            if not fname:
                fname = f"media_{media_id}"

            dest = out_dir / fname
            size = 0
            with open(dest, "wb") as f:
                for chunk in r.iter_content(1024 * 1024):
                    if chunk:
                        f.write(chunk)
                        size += len(chunk)

        log.info("Downloaded %d bytes -> %s", size, dest)
        return dest, ""
    except Exception as exc:
        return None, f"Download error: {exc}"


def extract_archives(directory: Path) -> list[Path]:
    """Extract any ZIP files and return paths to mesh files found."""
    mesh_exts = {".ply", ".stl", ".obj", ".off", ".gltf", ".glb"}
    mesh_files = []
    for zf in directory.rglob("*.zip"):
        try:
            extract_dir = zf.parent / zf.stem
            extract_dir.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(zf) as archive:
                archive.extractall(extract_dir)
            log.info("Extracted: %s", zf.name)
        except zipfile.BadZipFile:
            log.warning("Bad ZIP: %s", zf)
    for f in directory.rglob("*"):
        if f.is_file() and f.suffix.lower() in mesh_exts:
            mesh_files.append(f)
    return mesh_files


def download_media(media_id: str, out_dir: str = "downloads") -> Dict[str, Any]:
    """Download a MorphoSource media file. Returns result dict.

    This is the main entry point for slicer_tool.py integration.
    """
    api_key = os.environ.get("MORPHOSOURCE_API_KEY", "")
    out_path = Path(out_dir)

    if not api_key:
        return {
            "success": False, "media_id": media_id,
            "error": "MORPHOSOURCE_API_KEY not set — cannot download files",
        }

    with requests.Session() as session:
        # Step 1: Check media exists and get visibility
        log.info("Checking media %s...", media_id)
        api_data = check_media(session, api_key, media_id)
        if "error" in api_data:
            return {"success": False, "error": api_data["error"], "media_id": media_id}

        visibility = get_visibility(api_data)
        log.info("Media %s visibility: %s", media_id, visibility)

        if visibility not in ("open", "open download", "open_download"):
            return {
                "success": False, "media_id": media_id,
                "visibility": visibility,
                "error": f"Media visibility is '{visibility}' — only open media can be downloaded",
            }

        # Step 2: Request signed download URL
        log.info("Requesting download URL for %s...", media_id)
        signed_url, error = request_download_url(session, api_key, media_id)
        if not signed_url:
            return {"success": False, "media_id": media_id, "error": error}
        log.info("Got download URL")

        # Step 3: Download the file (with API key in header)
        dest, error = download_file(session, signed_url, out_path, media_id, api_key)
        if not dest:
            return {"success": False, "media_id": media_id, "error": error}

        # Step 4: Extract archives and find mesh files
        mesh_files = extract_archives(out_path)

        return {
            "success": True,
            "media_id": media_id,
            "visibility": visibility,
            "downloaded_file": str(dest),
            "file_size": dest.stat().st_size,
            "mesh_files": [str(f) for f in mesh_files],
            "download_dir": str(out_path),
        }


if __name__ == "__main__":
    _do_load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    media_id = os.environ.get("MEDIA_ID", "") or (sys.argv[1] if len(sys.argv) > 1 else "")
    if not media_id:
        print("Usage: MEDIA_ID=000769445 python morphosource_api_download.py")
        print("   or: python morphosource_api_download.py 000769445")
        sys.exit(1)
    out_dir = os.environ.get("OUT_DIR", "downloads")
    result = download_media(media_id, out_dir)
    print(json.dumps(result, indent=2))
    if not result.get("success"):
        sys.exit(1)
