#!/usr/bin/env python3
"""Download a MorphoSource media file by media ID.

Standalone module that can be imported by slicer_tool.py or run via CLI.
"""

from __future__ import annotations

import json
import os
import re
import sys
import zipfile
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import unquote, urlparse

import requests

BASE = "https://www.morphosource.org/api"
TIMEOUT = (10, 120)


def _first(value: Any) -> str:
    if isinstance(value, list):
        return str(value[0]) if value else ""
    return str(value) if value is not None else ""


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
    url = f"{BASE}/media/{media_id}"
    headers: Dict[str, str] = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = api_key
    resp = session.get(url, headers=headers, timeout=TIMEOUT)
    if resp.status_code != 200:
        return {"error": f"HTTP {resp.status_code}"}
    return resp.json()


def get_visibility(api_data: Dict) -> str:
    response = api_data.get("response", api_data)
    if isinstance(response, dict):
        vis = response.get("visibility_ssi") or response.get("visibility")
        return _first(vis).lower() if vis else "unknown"
    return "unknown"


def request_download_url(session: requests.Session, api_key: str, media_id: str) -> Optional[str]:
    url = f"{BASE}/download/{media_id}"
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = api_key
    payload = {
        "use_statement": "Automated download for morphometric research",
        "use_categories": ["Research"],
        "agreements_accepted": True,
    }
    resp = session.post(url, headers=headers, json=payload, timeout=TIMEOUT)
    if resp.status_code < 200 or resp.status_code >= 300:
        return None
    data = resp.json()
    media_node = ((data.get("response") or {}).get("media") or {})
    dl = media_node.get("download_url") if isinstance(media_node, dict) else None
    if isinstance(dl, list) and dl:
        return dl[0]
    if isinstance(dl, str):
        return dl
    return (data.get("response") or {}).get("url") or data.get("url")


def download_file(session: requests.Session, signed_url: str, out_dir: Path,
                  media_id: str, api_key: str = "") -> Optional[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    headers: Dict[str, str] = {}
    if api_key:
        headers["Authorization"] = api_key
    try:
        with session.get(signed_url, headers=headers, stream=True,
                         allow_redirects=True, timeout=(10, 300)) as r:
            if r.status_code >= 300:
                return None
            fname = header_filename(r.headers.get("Content-Disposition"))
            if not fname:
                fname = Path(urlparse(signed_url).path).name or f"media_{media_id}"
            fname = Path(fname).name
            dest = out_dir / fname
            with open(dest, "wb") as f:
                for chunk in r.iter_content(1024 * 1024):
                    if chunk:
                        f.write(chunk)
        return dest
    except Exception:
        return None


def extract_archives(directory: Path) -> list[Path]:
    """Extract any ZIP files and return paths to mesh files found."""
    mesh_exts = {".ply", ".stl", ".obj", ".off", ".gltf", ".glb"}
    mesh_files = []
    for zf in directory.rglob("*.zip"):
        try:
            with zipfile.ZipFile(zf) as archive:
                archive.extractall(zf.parent / zf.stem)
        except zipfile.BadZipFile:
            continue
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

    with requests.Session() as session:
        # Check media exists
        api_data = check_media(session, api_key, media_id)
        if "error" in api_data:
            return {"success": False, "error": api_data["error"], "media_id": media_id}

        visibility = get_visibility(api_data)
        if visibility != "open":
            return {
                "success": False, "media_id": media_id,
                "visibility": visibility,
                "error": f"Media has visibility '{visibility}' — only 'open' media can be downloaded automatically",
            }

        # Request download URL
        signed_url = request_download_url(session, api_key, media_id)
        if not signed_url:
            return {"success": False, "media_id": media_id, "error": "Could not get download URL"}

        # Download
        dest = download_file(session, signed_url, out_path, media_id, api_key)
        if not dest:
            return {"success": False, "media_id": media_id, "error": "Download failed"}

        # Extract archives and find mesh files
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
    media_id = os.environ.get("MEDIA_ID", "") or (sys.argv[1] if len(sys.argv) > 1 else "")
    if not media_id:
        print("Usage: MEDIA_ID=000769445 python morphosource_api_download.py")
        sys.exit(1)
    out_dir = os.environ.get("OUT_DIR", "downloads")
    result = download_media(media_id, out_dir)
    print(json.dumps(result, indent=2))
