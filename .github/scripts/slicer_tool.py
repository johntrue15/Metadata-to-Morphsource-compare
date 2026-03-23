#!/usr/bin/env python3
"""
Unified SlicerMorph analysis tool for AutoResearchClaw.

Single entry point: given a MorphoSource media ID, downloads the specimen,
runs Layer 1 (3D Slicer morphometrics), and returns a structured analysis
summary that feeds back into the research agent's memory.

Usage:
    from slicer_tool import analyze_specimen
    result = analyze_specimen("000769445", topic="chameleon optic nerve")

Or CLI:
    python3 slicer_tool.py 000769445 --topic "chameleon optic nerve"
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

from _helpers import load_dotenv as _do_load_dotenv, SLICER_BIN, AUTORESEARCHCLAW_HOME, MORPHOSOURCE_API_BASE

log = logging.getLogger("SlicerTool")

DATA_DIR = AUTORESEARCHCLAW_HOME / "specimens"

_do_load_dotenv()


# ---------------------------------------------------------------------------
# Step 1: Download from MorphoSource
# ---------------------------------------------------------------------------


def _download_specimen(media_id: str) -> dict:
    """Download a specimen from MorphoSource. Returns result dict."""
    from morphosource_api_download import download_media

    out_dir = DATA_DIR / f"media_{media_id}"
    out_dir.mkdir(parents=True, exist_ok=True)

    log.info("Downloading media %s to %s", media_id, out_dir)
    result = download_media(media_id, str(out_dir))

    if result.get("success"):
        log.info("Downloaded: %s (%d bytes, %d mesh files)",
                 result.get("downloaded_file", "?"),
                 result.get("file_size", 0),
                 len(result.get("mesh_files", [])))
    else:
        log.warning("Download failed: %s", result.get("error", "unknown"))

    return result


# ---------------------------------------------------------------------------
# Step 2: Run Slicer analysis (Layer 1 deep morphometrics)
# ---------------------------------------------------------------------------

_SLICER_ANALYSIS_TEMPLATE = '''\
"""Auto-generated Layer 1 analysis script for {media_id}."""
import slicer
import vtk
import numpy as np
import json
import os
from collections import defaultdict

OUTPUT_DIR = "{output_dir}"
PLY_PATH = "{ply_path}"
os.makedirs(OUTPUT_DIR, exist_ok=True)

print("SlicerTool Layer 1: " + PLY_PATH)
success, node = slicer.util.loadModel(PLY_PATH, returnNode=True)
if not success or not node:
    json.dump({{"error": "Failed to load mesh"}}, open(os.path.join(OUTPUT_DIR, "analysis.json"), "w"))
    slicer.app.exit(1)

mesh = node.GetMesh()
bounds = [0]*6
node.GetBounds(bounds)
center = [(bounds[0]+bounds[1])/2, (bounds[2]+bounds[3])/2, (bounds[4]+bounds[5])/2]
npts = mesh.GetNumberOfPoints()
ncells = mesh.GetNumberOfCells()
print(f"  {{npts:,}} vertices, {{ncells:,}} faces")

# Sample points
stride = max(1, npts // 100000)
pts = np.array([mesh.GetPoint(i) for i in range(0, npts, stride)])

# Mass properties
mp = vtk.vtkMassProperties()
mp.SetInputData(mesh)
mp.Update()

# Curvature
cf = vtk.vtkCurvatures()
cf.SetInputData(mesh)
cf.SetCurvatureTypeToMean()
cf.Update()
cs = cf.GetOutput().GetPointData().GetScalars()
curv = np.array([cs.GetValue(i) for i in range(0, min(cs.GetNumberOfTuples(), 100000), stride)])
p5, p95 = np.percentile(curv, [5, 95])
clipped = curv[(curv >= p5) & (curv <= p95)]

# Landmarks
landmarks = []
for name, func in [
    ("snout_tip", lambda p: p[:, 1].argmin()),
    ("occipital", lambda p: p[:, 1].argmax()),
    ("dorsal_peak", lambda p: p[:, 2].argmax()),
    ("ventral", lambda p: p[:, 2].argmin()),
    ("left_max", lambda p: p[:, 0].argmin()),
    ("right_max", lambda p: p[:, 0].argmax()),
]:
    idx = func(pts)
    pt = pts[idx]
    landmarks.append({{"name": name, "position": [round(float(c), 2) for c in pt]}})

lm = {{l["name"]: np.array(l["position"]) for l in landmarks}}
distances = {{}}
for dname, p1, p2 in [("skull_length", "snout_tip", "occipital"),
                       ("skull_height", "ventral", "dorsal_peak"),
                       ("skull_width", "left_max", "right_max")]:
    if p1 in lm and p2 in lm:
        distances[dname] = round(float(np.linalg.norm(lm[p1] - lm[p2])), 2)

# Connectivity
conn = vtk.vtkConnectivityFilter()
conn.SetInputData(mesh)
conn.SetExtractionModeToAllRegions()
conn.ColorRegionsOn()
conn.Update()
n_regions = conn.GetNumberOfExtractedRegions()

# Sphericity
sa = mp.GetSurfaceArea()
vol = mp.GetVolume()
eq_r = (3 * vol / (4 * np.pi)) ** (1/3)
sphericity = (4 * np.pi * eq_r**2) / sa if sa > 0 else 0

# Screenshots
slicer.app.layoutManager().setLayout(slicer.vtkMRMLLayoutNode.SlicerLayoutOneUp3DView)
tw = slicer.app.layoutManager().threeDWidget(0)
tv = tw.threeDView()
rw = tv.renderWindow()
rw.SetOffScreenRendering(1)
rw.SetSize(1200, 900)
renderer = rw.GetRenderers().GetFirstRenderer()
renderer.SetBackground(0.05, 0.05, 0.1)
camera = renderer.GetActiveCamera()

screenshots = []
for vname, pos, vup in [
    ("anterior", (0, -120, 15), (0, 0, 1)),
    ("lateral", (120, 0, 15), (0, 0, 1)),
    ("dorsal", (0, 0, 120), (0, -1, 0)),
    ("oblique", (80, -80, 60), (0, 0, 1)),
]:
    camera.SetPosition(center[0]+pos[0], center[1]+pos[1], center[2]+pos[2])
    camera.SetFocalPoint(*center)
    camera.SetViewUp(*vup)
    renderer.ResetCameraClippingRange()
    rw.Render()
    w2i = vtk.vtkWindowToImageFilter()
    w2i.SetInput(rw)
    w2i.SetScale(1)
    w2i.ReadFrontBufferOff()
    w2i.Update()
    fpath = os.path.join(OUTPUT_DIR, f"{{vname}}.png")
    writer = vtk.vtkPNGWriter()
    writer.SetFileName(fpath)
    writer.SetInputConnection(w2i.GetOutputPort())
    writer.Write()
    screenshots.append(fpath)

result = {{
    "media_id": "{media_id}",
    "vertices": npts,
    "faces": ncells,
    "bounds": bounds,
    "extent_mm": [bounds[1]-bounds[0], bounds[3]-bounds[2], bounds[5]-bounds[4]],
    "surface_area_mm2": round(sa, 2),
    "volume_mm3": round(vol, 2),
    "sphericity": round(sphericity, 4),
    "sa_vol_ratio": round(sa/vol, 4) if vol > 0 else 0,
    "mean_curvature": round(float(np.mean(clipped)), 4),
    "curvature_std": round(float(np.std(clipped)), 4),
    "n_regions": n_regions,
    "landmarks": landmarks,
    "distances": distances,
    "screenshots": screenshots,
}}

with open(os.path.join(OUTPUT_DIR, "analysis.json"), "w") as f:
    json.dump(result, f, indent=2)
print("Analysis complete")
slicer.app.exit(0)
'''


def _run_slicer_analysis(media_id: str, ply_path: str) -> dict:
    """Run Layer 1 analysis in 3D Slicer headlessly."""
    output_dir = DATA_DIR / f"media_{media_id}" / "analysis"
    output_dir.mkdir(parents=True, exist_ok=True)

    script_content = _SLICER_ANALYSIS_TEMPLATE.format(
        media_id=media_id,
        ply_path=ply_path,
        output_dir=str(output_dir),
    )
    script_path = output_dir / "run_analysis.py"
    script_path.write_text(script_content)

    log.info("Running Slicer analysis on %s", ply_path)
    try:
        result = subprocess.run(
            [SLICER_BIN, "--no-splash", "--python-script", str(script_path)],
            capture_output=True, text=True, timeout=120,
        )
        log.info("Slicer exit code: %d", result.returncode)
    except subprocess.TimeoutExpired:
        log.error("Slicer timed out")
        return {"error": "Slicer timed out after 120s"}
    except Exception as exc:
        log.error("Slicer failed: %s", exc)
        return {"error": str(exc)}

    analysis_file = output_dir / "analysis.json"
    if analysis_file.exists():
        data = json.loads(analysis_file.read_text())
        log.info("Analysis complete: %d vertices, %s",
                 data.get("vertices", 0), data.get("distances", {}))
        return data
    return {"error": "No analysis output produced"}


# ---------------------------------------------------------------------------
# Step 3: Generate summary for AutoResearchClaw memory
# ---------------------------------------------------------------------------


def _build_summary(media_id: str, download_result: dict, analysis: dict) -> str:
    """Build a concise summary string for the research agent's memory."""
    if analysis.get("error"):
        return f"Specimen {media_id}: download={'OK' if download_result.get('success') else 'FAILED'}, analysis FAILED ({analysis['error']})"

    d = analysis.get("distances", {})
    lines = [
        f"## Specimen Analysis: media {media_id}",
        f"**Mesh:** {analysis.get('vertices', 0):,} vertices, {analysis.get('faces', 0):,} faces",
        f"**Dimensions:** {d.get('skull_length', 'N/A')} mm (L) x {d.get('skull_width', 'N/A')} mm (W) x {d.get('skull_height', 'N/A')} mm (H)",
        f"**Surface area:** {analysis.get('surface_area_mm2', 'N/A')} mm^2",
        f"**Volume:** {analysis.get('volume_mm3', 'N/A')} mm^3",
        f"**Sphericity:** {analysis.get('sphericity', 'N/A')}",
        f"**Connected regions:** {analysis.get('n_regions', 'N/A')}",
        f"**Curvature:** {analysis.get('mean_curvature', 'N/A')} +/- {analysis.get('curvature_std', 'N/A')}",
        f"**Screenshots:** {len(analysis.get('screenshots', []))} views captured",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def analyze_specimen(media_id: str, topic: str = "", skip_download: bool = False) -> dict:
    """Download and analyze a MorphoSource specimen.

    Returns a dict with: success, media_id, summary, analysis, download_result.
    This is what AutoResearchClaw calls as a tool.
    """
    t0 = time.time()
    log.info("=" * 50)
    log.info("SlicerTool: analyzing media %s", media_id)
    log.info("=" * 50)

    # Check if Slicer is available
    if not Path(SLICER_BIN).exists():
        return {
            "success": False, "media_id": media_id,
            "error": "3D Slicer not found at " + SLICER_BIN,
            "summary": f"Specimen {media_id}: Slicer not available on this runner",
        }

    # Check cache — reuse previously downloaded + analyzed specimens
    specimen_dir = DATA_DIR / f"media_{media_id}"
    existing_analysis = specimen_dir / "analysis" / "analysis.json"
    if existing_analysis.exists():
        log.info("CACHE HIT: reusing analysis for %s", media_id)
        analysis = json.loads(existing_analysis.read_text())
        summary = _build_summary(media_id, {"success": True}, analysis)
        return {
            "success": True, "media_id": media_id,
            "summary": summary, "analysis": analysis,
            "download_result": {"success": True, "cached": True},
            "duration_s": round(time.time() - t0, 1),
        }

    # Step 1: Download (not cached)
    log.info("CACHE MISS: downloading %s", media_id)
    download_result = _download_specimen(media_id)
    if not download_result.get("success"):
        summary = f"Specimen {media_id}: download FAILED — {download_result.get('error', 'unknown')}"
        return {
            "success": False, "media_id": media_id,
            "summary": summary, "download_result": download_result,
            "duration_s": round(time.time() - t0, 1),
        }

    # Find mesh file
    mesh_files = download_result.get("mesh_files", [])
    if not mesh_files:
        summary = f"Specimen {media_id}: downloaded but no mesh files found in archive"
        return {
            "success": False, "media_id": media_id,
            "summary": summary, "download_result": download_result,
            "duration_s": round(time.time() - t0, 1),
        }

    ply_path = mesh_files[0]
    log.info("Using mesh: %s", ply_path)

    # Step 2: Slicer analysis
    analysis = _run_slicer_analysis(media_id, ply_path)

    # Step 3: Build summary
    summary = _build_summary(media_id, download_result, analysis)

    duration = round(time.time() - t0, 1)
    log.info("SlicerTool complete in %.1fs", duration)

    return {
        "success": not bool(analysis.get("error")),
        "media_id": media_id,
        "summary": summary,
        "analysis": analysis,
        "download_result": download_result,
        "duration_s": duration,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    media_id = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("MEDIA_ID", "")
    if not media_id:
        print("Usage: python3 slicer_tool.py <media_id>")
        sys.exit(1)

    topic = sys.argv[2] if len(sys.argv) > 2 else ""
    result = analyze_specimen(media_id, topic)
    print("\n" + "=" * 60)
    print(result.get("summary", "No summary"))
    print("=" * 60)
    print(json.dumps({k: v for k, v in result.items() if k != "analysis"}, indent=2))
