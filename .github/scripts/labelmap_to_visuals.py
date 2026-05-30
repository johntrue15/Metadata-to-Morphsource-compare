#!/usr/bin/env python3
"""Convert a segmentation composite labelmap into release-ready 3D visuals.

Input: a labelmap volume (``.nii.gz`` / ``.nrrd``) such as the bright-seed
composite committed by the Jetstream ECU
(``results/<run>/bright_seed/artifacts/composite.nii.gz``).

Outputs (under ``--out-dir``):
  <name>.glb       - glTF binary mesh (GitHub renders this inline + AR-on-Android)
  <name>.usdz      - Apple Quick Look / AR (USD packaged, metres scale)
  <name>.stl       - universal mesh interchange
  <name>.png       - hero render (best-effort, off-screen)
  <name>_turntable.gif - rotating preview (best-effort)
  <name>.visuals.json  - provenance + mesh stats

The mesh is extracted with marching cubes over the binary union (label > 0),
in millimetre world coordinates derived from the volume spacing, optionally
smoothed and decimated for a lean release asset. Rendering steps are
best-effort: if no GL context is available the geometry assets are still
produced.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


def _log(msg: str) -> None:
    print(f"[visuals] {msg}", flush=True)


def load_mask(path: Path):
    """Return (binary_mask[z,y,x] bool, spacing_xyz mm tuple)."""
    import SimpleITK as sitk
    import numpy as np

    img = sitk.ReadImage(str(path))
    arr = sitk.GetArrayFromImage(img)  # (z, y, x)
    spacing = img.GetSpacing()  # (x, y, z)
    mask = arr > 0
    return mask, spacing, arr


def extract_mesh(mask, spacing, *, smooth_iter: int, decimate_faces: int):
    """Marching-cubes surface in mm; returns a trimesh.Trimesh."""
    import numpy as np
    from skimage import measure
    import trimesh

    if not mask.any():
        raise SystemExit("ERROR: labelmap is empty (no label > 0)")

    # Pad so surfaces on the volume border close cleanly.
    mask = np.pad(mask, 1, mode="constant", constant_values=False)
    # skimage spacing order matches array order (z, y, x).
    sx, sy, sz = spacing
    verts, faces, normals, _ = measure.marching_cubes(
        mask.astype(np.float32), level=0.5, spacing=(sz, sy, sx))
    # verts are (z, y, x) -> reorder to (x, y, z) world mm.
    verts = verts[:, ::-1]
    mesh = trimesh.Trimesh(vertices=verts, faces=faces, process=True)

    # Keep only the largest connected component(s) to drop floating speckles.
    comps = mesh.split(only_watertight=False)
    if len(comps) > 1:
        comps = sorted(comps, key=lambda m: m.area, reverse=True)
        keep = [c for c in comps if c.area >= 0.02 * comps[0].area]
        mesh = trimesh.util.concatenate(keep) if keep else comps[0]
        _log(f"kept {len(keep) or 1}/{len(comps)} connected components")

    if smooth_iter > 0:
        try:
            trimesh.smoothing.filter_taubin(mesh, iterations=smooth_iter)
        except Exception as e:  # pragma: no cover
            _log(f"smoothing skipped: {e!r}")

    if decimate_faces and len(mesh.faces) > decimate_faces:
        before = len(mesh.faces)
        try:
            mesh = mesh.simplify_quadric_decimation(face_count=decimate_faces)
            _log(f"decimated {before} -> {len(mesh.faces)} faces")
        except Exception as e:  # pragma: no cover
            _log(f"decimation skipped ({e!r}); keeping {before} faces")

    mesh.fix_normals()
    return mesh


BONE_RGBA = [220, 213, 196, 255]


def export_geometry(mesh, out_dir: Path, name: str) -> dict:
    import numpy as np
    import trimesh

    mesh.visual = trimesh.visual.ColorVisuals(
        mesh, vertex_colors=np.tile(BONE_RGBA, (len(mesh.vertices), 1)))

    glb = out_dir / f"{name}.glb"
    stl = out_dir / f"{name}.stl"
    obj = out_dir / f"{name}.obj"
    mesh.export(glb)
    mesh.export(stl)
    mesh.export(obj)  # writes <name>.obj (+ <name>.mtl for colors)
    _log(f"wrote {glb.name} ({glb.stat().st_size/1e6:.2f} MB), "
         f"{stl.name} ({stl.stat().st_size/1e6:.2f} MB), "
         f"{obj.name} ({obj.stat().st_size/1e6:.2f} MB)")
    return {"glb": glb.name, "stl": stl.name, "obj": obj.name}


def export_usdz(mesh, out_dir: Path, name: str) -> str | None:
    """Build a USD mesh (mm geometry, metres scale) and package as .usdz."""
    try:
        from pxr import Usd, UsdGeom, Vt, UsdUtils
    except Exception as e:
        _log(f"usd-core unavailable, skipping USDZ: {e!r}")
        return None
    import numpy as np

    usdc = out_dir / f"{name}.usdc"
    usdz = out_dir / f"{name}.usdz"
    stage = Usd.Stage.CreateNew(str(usdc))
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.y)
    UsdGeom.SetStageMetersPerUnit(stage, 0.001)  # geometry is in millimetres
    xform = UsdGeom.Xform.Define(stage, "/Skull")
    stage.SetDefaultPrim(xform.GetPrim())
    gmesh = UsdGeom.Mesh.Define(stage, "/Skull/Mesh")

    verts = mesh.vertices.astype(np.float32)
    faces = mesh.faces.astype(np.int32)
    gmesh.CreatePointsAttr(Vt.Vec3fArray.FromNumpy(verts))
    gmesh.CreateFaceVertexCountsAttr(
        Vt.IntArray.FromNumpy(np.full(len(faces), 3, dtype=np.int32)))
    gmesh.CreateFaceVertexIndicesAttr(Vt.IntArray.FromNumpy(faces.reshape(-1)))
    gmesh.CreateSubdivisionSchemeAttr(UsdGeom.Tokens.none)
    gmesh.CreateDisplayColorAttr(
        Vt.Vec3fArray([(BONE_RGBA[0] / 255, BONE_RGBA[1] / 255,
                        BONE_RGBA[2] / 255)]))
    try:
        ext = UsdGeom.PointBased(gmesh).ComputeExtent(verts)
        gmesh.CreateExtentAttr(ext)
    except Exception:
        pass
    stage.GetRootLayer().Save()

    ok = UsdUtils.CreateNewUsdzPackage(str(usdc), str(usdz))
    try:
        usdc.unlink()
    except OSError:
        pass
    if not ok or not usdz.exists():
        _log("USDZ packaging failed")
        return None
    _log(f"wrote {usdz.name} ({usdz.stat().st_size/1e6:.2f} MB)")
    return usdz.name


def render_previews(mesh, out_dir: Path, name: str, *, frames: int) -> dict:
    """Best-effort off-screen hero render + turntable GIF via pyvista."""
    out: dict = {}
    try:
        import os
        os.environ.setdefault("PYVISTA_OFF_SCREEN", "true")
        import numpy as np
        import pyvista as pv
        pv.OFF_SCREEN = True

        faces = np.hstack(
            [np.full((len(mesh.faces), 1), 3, dtype=np.int64),
             mesh.faces.astype(np.int64)]).reshape(-1)
        pmesh = pv.PolyData(mesh.vertices, faces)
        pmesh = pmesh.compute_normals(auto_orient_normals=True)

        pl = pv.Plotter(off_screen=True, window_size=[1200, 1200])
        pl.set_background("white")
        pl.add_mesh(pmesh, color=[c / 255 for c in BONE_RGBA[:3]],
                    smooth_shading=True, specular=0.3, specular_power=15)
        pl.enable_eye_dome_lighting()
        pl.camera_position = "yz"
        pl.camera.azimuth = 30
        pl.camera.elevation = 20

        hero = out_dir / f"{name}.png"
        pl.screenshot(str(hero))
        out["png"] = hero.name
        _log(f"wrote {hero.name}")

        if frames > 0:
            try:
                import imageio.v2 as imageio
                gif = out_dir / f"{name}_turntable.gif"
                imgs = []
                for i in range(frames):
                    pl.camera.azimuth = 30 + (360.0 * i / frames)
                    pl.render()
                    imgs.append(np.asarray(pl.screenshot(return_img=True)))
                imageio.mimsave(gif, imgs, duration=0.08, loop=0)
                out["gif"] = gif.name
                _log(f"wrote {gif.name}")
            except Exception as e:
                _log(f"turntable gif skipped: {e!r}")
        pl.close()
    except Exception as e:
        _log(f"preview render skipped (no GL?): {e!r}")
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--labelmap", required=True)
    p.add_argument("--out-dir", required=True)
    p.add_argument("--name", required=True, help="asset basename, e.g. chameleon-200")
    p.add_argument("--decimate-faces", type=int, default=200_000)
    p.add_argument("--smooth-iter", type=int, default=10)
    p.add_argument("--turntable-frames", type=int, default=36)
    p.add_argument("--metadata-json", default=None,
                   help="optional summary.json to embed in <name>.visuals.json")
    args = p.parse_args(argv)

    lm = Path(args.labelmap)
    if not lm.exists():
        raise SystemExit(f"ERROR: labelmap not found: {lm}")
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    _log(f"reading {lm}")
    mask, spacing, arr = load_mask(lm)
    import numpy as np
    voxels = int(mask.sum())
    _log(f"volume {arr.shape} spacing(mm)={[round(s,4) for s in spacing]} "
         f"label-voxels={voxels:,}")

    mesh = extract_mesh(mask, spacing, smooth_iter=args.smooth_iter,
                        decimate_faces=args.decimate_faces)
    _log(f"mesh: {len(mesh.vertices):,} verts, {len(mesh.faces):,} faces, "
         f"watertight={mesh.is_watertight}")

    assets = {}
    assets.update(export_geometry(mesh, out_dir, args.name))
    usdz = export_usdz(mesh, out_dir, args.name)
    if usdz:
        assets["usdz"] = usdz
    assets.update(render_previews(mesh, out_dir, args.name,
                                  frames=args.turntable_frames))

    meta = {
        "name": args.name,
        "source_labelmap": str(lm),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "volume_shape_zyx": list(arr.shape),
        "spacing_mm_xyz": [round(s, 6) for s in spacing],
        "label_voxels": voxels,
        "mesh_vertices": int(len(mesh.vertices)),
        "mesh_faces": int(len(mesh.faces)),
        "bounds_mm": [[round(float(v), 3) for v in mesh.bounds[0]],
                      [round(float(v), 3) for v in mesh.bounds[1]]],
        "assets": assets,
    }
    if args.metadata_json and Path(args.metadata_json).exists():
        try:
            meta["run_summary"] = json.loads(Path(args.metadata_json).read_text())
        except Exception:
            pass
    (out_dir / f"{args.name}.visuals.json").write_text(
        json.dumps(meta, indent=2) + "\n")
    _log(f"assets: {assets}")
    _log("DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
