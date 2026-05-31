#!/usr/bin/env python3
"""Convert a segmentation composite labelmap into release-ready 3D visuals.

Input: a *multi-label* composite labelmap (``.nii.gz`` / ``.nrrd``) such as the
bright-seed composite committed by the Jetstream ECU
(``results/<run>/bright_seed/artifacts/composite.nii.gz``). Each segment in the
volume has a distinct integer label (written by Slicer's
``ExportAllSegmentsToLabelmapNode``).

Outputs (under ``--out-dir``):
  <name>.glb       - glTF binary, one colored node per segment (renders inline
                     on GitHub + AR-on-Android, colors preserved)
  <name>.usdz      - Apple Quick Look / AR, one colored mesh per segment
  <name>.obj (+ .mtl) - OBJ with one material (``usemtl``) per segment, so DCC
                     tools (Blender/Maya/MeshLab) show the individual colored
                     segments
  <name>.stl       - universal mesh interchange (geometry only, no color)
  <name>.png       - hero render (best-effort, off-screen, colored)
  <name>_turntable.gif - rotating preview (best-effort)
  <name>.visuals.json  - provenance + per-segment stats

Each segment is surfaced with marching cubes over ``label == value`` (cropped to
the label bounding box for speed), smoothed and decimated to a per-segment
budget, then assigned a distinct color from a golden-angle palette so the
individual segments are visually separable. Rendering steps are best-effort: if
no GL context is available the geometry assets are still produced.
"""

from __future__ import annotations

import argparse
import colorsys
import json
import sys
import time
from pathlib import Path


def _log(msg: str) -> None:
    print(f"[visuals] {msg}", flush=True)


# A neutral bone color used when the labelmap has a single segment (or is a
# binary union with no per-segment information to color).
BONE_RGBA = [220, 213, 196, 255]


def load_labelmap(path: Path):
    """Return (labelmap[z,y,x] int ndarray, spacing_xyz mm tuple)."""
    import SimpleITK as sitk

    img = sitk.ReadImage(str(path))
    arr = sitk.GetArrayFromImage(img)  # (z, y, x)
    spacing = img.GetSpacing()  # (x, y, z)
    return arr, spacing


def segment_palette(n: int):
    """``n`` visually-distinct RGBA colors via golden-angle hue rotation."""
    if n <= 1:
        return [list(BONE_RGBA)]
    cols = []
    for i in range(n):
        h = (i * 0.6180339887498949) % 1.0          # golden-angle hue
        s = 0.55 + 0.25 * ((i * 0.37) % 1.0)         # gentle saturation jitter
        v = 0.82 + 0.16 * ((i * 0.13) % 1.0)         # gentle value jitter
        r, g, b = colorsys.hsv_to_rgb(h, s, v)
        cols.append([int(round(r * 255)), int(round(g * 255)),
                     int(round(b * 255)), 255])
    return cols


def extract_label_meshes(arr, spacing, *, smooth_iter: int,
                         per_segment_faces: int, min_voxels: int,
                         max_segments: int):
    """Surface every label>0 as its own colored ``trimesh.Trimesh``.

    Returns a list of dicts: ``{"label", "mesh", "rgba", "voxels"}`` sorted by
    descending voxel count (largest segments first / most stable colors).
    """
    import numpy as np
    from skimage import measure
    import trimesh

    sx, sy, sz = spacing
    labels = [int(v) for v in np.unique(arr) if v != 0]
    if not labels:
        raise SystemExit("ERROR: labelmap is empty (no label > 0)")

    # Order by size so the biggest, most meaningful structures get the first
    # (most saturated/stable) palette entries, and so --max-segments keeps the
    # largest ones.
    sizes = {l: int((arr == l).sum()) for l in labels}
    labels = [l for l in sorted(labels, key=lambda l: sizes[l], reverse=True)
              if sizes[l] >= min_voxels]
    if max_segments and len(labels) > max_segments:
        _log(f"capping {len(labels)} -> {max_segments} largest segments")
        labels = labels[:max_segments]

    palette = segment_palette(len(labels))
    segs = []
    for idx, l in enumerate(labels):
        m = arr == l
        zz, yy, xx = np.where(m)
        z0, z1 = int(zz.min()), int(zz.max())
        y0, y1 = int(yy.min()), int(yy.max())
        x0, x1 = int(xx.min()), int(xx.max())
        sub = m[z0:z1 + 1, y0:y1 + 1, x0:x1 + 1]
        sub = np.pad(sub, 1, mode="constant", constant_values=False)
        try:
            verts, faces, _, _ = measure.marching_cubes(
                sub.astype(np.float32), level=0.5, spacing=(sz, sy, sx))
        except (ValueError, RuntimeError):
            continue
        if len(faces) == 0:
            continue
        # verts are (z, y, x) world-mm within the padded sub-volume -> reorder
        # to (x, y, z) and offset back to the global volume origin.
        verts = verts[:, ::-1].copy()
        verts[:, 0] += (x0 - 1) * sx
        verts[:, 1] += (y0 - 1) * sy
        verts[:, 2] += (z0 - 1) * sz

        mesh = trimesh.Trimesh(vertices=verts, faces=faces, process=True)
        if smooth_iter > 0:
            try:
                trimesh.smoothing.filter_taubin(mesh, iterations=smooth_iter)
            except Exception:  # pragma: no cover
                pass
        if per_segment_faces and len(mesh.faces) > per_segment_faces:
            try:
                mesh = mesh.simplify_quadric_decimation(
                    face_count=per_segment_faces)
            except Exception:  # pragma: no cover
                pass
        if len(mesh.faces) == 0:
            continue
        mesh.fix_normals()
        rgba = palette[idx]
        mesh.visual = trimesh.visual.ColorVisuals(
            mesh, vertex_colors=np.tile(rgba, (len(mesh.vertices), 1)))
        segs.append({"label": l, "mesh": mesh, "rgba": rgba,
                     "voxels": sizes[l]})

    if not segs:
        raise SystemExit("ERROR: no segment produced a surface mesh")
    _log(f"surfaced {len(segs)} segment(s); "
         f"total faces={sum(len(s['mesh'].faces) for s in segs):,}")
    return segs


def combine_colored(segs):
    """Concatenate all segment meshes into one vertex-colored ``Trimesh``."""
    import trimesh
    if len(segs) == 1:
        return segs[0]["mesh"]
    return trimesh.util.concatenate([s["mesh"] for s in segs])


def export_geometry(segs, combined, out_dir: Path, name: str) -> dict:
    """Write GLB (per-segment colored scene), OBJ+MTL (materials), and STL."""
    import trimesh

    glb = out_dir / f"{name}.glb"
    stl = out_dir / f"{name}.stl"

    # GLB: a Scene with one named, colored node per segment so viewers can
    # distinguish (and toggle) the individual segments.
    scene = trimesh.Scene()
    for s in segs:
        scene.add_geometry(s["mesh"].copy(),
                           geom_name=f"segment_{s['label']:03d}")
    try:
        scene.export(glb)
    except Exception as e:  # pragma: no cover - fall back to a single mesh
        _log(f"scene GLB export failed ({e!r}); exporting combined mesh")
        combined.export(glb)

    # STL: geometry only (no color in the format).
    combined.export(stl)

    # OBJ + MTL: hand-written so every segment gets its own material and shows
    # up as a distinct color in DCC tools, independent of trimesh's version.
    obj_name = _write_obj_mtl(segs, out_dir, name)

    out = {"glb": glb.name, "stl": stl.name, "obj": obj_name,
           "mtl": f"{name}.mtl"}
    sizes = {k: (out_dir / v).stat().st_size / 1e6
             for k, v in (("glb", glb.name), ("stl", stl.name),
                          ("obj", obj_name))}
    _log("wrote " + ", ".join(f"{v} ({sizes[k]:.2f} MB)"
                              for k, v in (("glb", glb.name), ("stl", stl.name),
                                           ("obj", obj_name))))
    return out


def _write_obj_mtl(segs, out_dir: Path, name: str) -> str:
    """Write ``<name>.obj`` + ``<name>.mtl`` with one material per segment."""
    obj = [f"# {name} - {len(segs)} colored segments", f"mtllib {name}.mtl"]
    mtl = [f"# {name} - one material per segment"]
    voff = 0
    for i, s in enumerate(segs):
        mesh = s["mesh"]
        r, g, b = (c / 255.0 for c in s["rgba"][:3])
        mname = f"seg{i:03d}"
        mtl += [f"newmtl {mname}",
                f"Kd {r:.4f} {g:.4f} {b:.4f}",
                "Ka 0.0000 0.0000 0.0000",
                "Ks 0.0000 0.0000 0.0000",
                "d 1.0",
                "illum 1",
                ""]
        obj.append(f"o segment_{s['label']:03d}")
        obj.append(f"usemtl {mname}")
        for v in mesh.vertices:
            obj.append(f"v {v[0]:.4f} {v[1]:.4f} {v[2]:.4f}")
        for f in mesh.faces:
            obj.append(f"f {f[0] + 1 + voff} {f[1] + 1 + voff} {f[2] + 1 + voff}")
        voff += len(mesh.vertices)
    (out_dir / f"{name}.obj").write_text("\n".join(obj) + "\n")
    (out_dir / f"{name}.mtl").write_text("\n".join(mtl) + "\n")
    return f"{name}.obj"


def export_usdz(segs, out_dir: Path, name: str) -> str | None:
    """Build a USD stage with one colored mesh per segment, package as .usdz."""
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
    root = UsdGeom.Xform.Define(stage, "/Skull")
    stage.SetDefaultPrim(root.GetPrim())

    for s in segs:
        mesh = s["mesh"]
        gmesh = UsdGeom.Mesh.Define(stage, f"/Skull/segment_{s['label']:03d}")
        verts = mesh.vertices.astype(np.float32)
        faces = mesh.faces.astype(np.int32)
        gmesh.CreatePointsAttr(Vt.Vec3fArray.FromNumpy(verts))
        gmesh.CreateFaceVertexCountsAttr(
            Vt.IntArray.FromNumpy(np.full(len(faces), 3, dtype=np.int32)))
        gmesh.CreateFaceVertexIndicesAttr(
            Vt.IntArray.FromNumpy(faces.reshape(-1)))
        gmesh.CreateSubdivisionSchemeAttr(UsdGeom.Tokens.none)
        gmesh.CreateDisplayColorAttr(
            Vt.Vec3fArray([(s["rgba"][0] / 255.0, s["rgba"][1] / 255.0,
                            s["rgba"][2] / 255.0)]))
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


def render_previews(segs, combined, out_dir: Path, name: str, *,
                    frames: int) -> dict:
    """Best-effort off-screen colored hero render + turntable GIF via pyvista."""
    out: dict = {}
    try:
        import os
        os.environ.setdefault("PYVISTA_OFF_SCREEN", "true")
        import numpy as np
        import pyvista as pv
        pv.OFF_SCREEN = True

        pl = pv.Plotter(off_screen=True, window_size=[1200, 1200])
        pl.set_background("white")
        # One actor per segment keeps colors crisp and avoids giant scalar
        # arrays; fall back to the combined mesh if something goes sideways.
        try:
            for s in segs:
                mesh = s["mesh"]
                faces = np.hstack(
                    [np.full((len(mesh.faces), 1), 3, dtype=np.int64),
                     mesh.faces.astype(np.int64)]).reshape(-1)
                pmesh = pv.PolyData(mesh.vertices, faces)
                pl.add_mesh(pmesh, color=[c / 255 for c in s["rgba"][:3]],
                            smooth_shading=True, specular=0.2,
                            specular_power=12)
        except Exception:
            faces = np.hstack(
                [np.full((len(combined.faces), 1), 3, dtype=np.int64),
                 combined.faces.astype(np.int64)]).reshape(-1)
            pmesh = pv.PolyData(combined.vertices, faces)
            pl.add_mesh(pmesh, color=[c / 255 for c in BONE_RGBA[:3]],
                        smooth_shading=True)

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
    p.add_argument("--decimate-faces", type=int, default=300_000,
                   help="total face budget across all segments")
    p.add_argument("--per-segment-faces", type=int, default=0,
                   help="explicit per-segment face cap (0 = derive from "
                        "--decimate-faces / segment count)")
    p.add_argument("--min-segment-voxels", type=int, default=20,
                   help="drop segments smaller than this many voxels (noise)")
    p.add_argument("--max-segments", type=int, default=400,
                   help="keep at most this many (largest) segments")
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
    arr, spacing = load_labelmap(lm)
    import numpy as np
    n_labels = int((np.unique(arr) != 0).sum())
    _log(f"volume {arr.shape} spacing(mm)={[round(s,4) for s in spacing]} "
         f"labels={n_labels}")

    per_seg = args.per_segment_faces
    if per_seg <= 0:
        per_seg = max(400, args.decimate_faces // max(1, n_labels))

    segs = extract_label_meshes(
        arr, spacing, smooth_iter=args.smooth_iter,
        per_segment_faces=per_seg, min_voxels=args.min_segment_voxels,
        max_segments=args.max_segments)
    combined = combine_colored(segs)
    _log(f"combined: {len(combined.vertices):,} verts, "
         f"{len(combined.faces):,} faces across {len(segs)} segments")

    assets = {}
    assets.update(export_geometry(segs, combined, out_dir, args.name))
    usdz = export_usdz(segs, out_dir, args.name)
    if usdz:
        assets["usdz"] = usdz
    assets.update(render_previews(segs, combined, out_dir, args.name,
                                  frames=args.turntable_frames))

    total_voxels = int(sum(s["voxels"] for s in segs))
    meta = {
        "name": args.name,
        "source_labelmap": str(lm),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "volume_shape_zyx": list(arr.shape),
        "spacing_mm_xyz": [round(s, 6) for s in spacing],
        "n_segments": len(segs),
        "label_voxels": total_voxels,
        "mesh_vertices": int(len(combined.vertices)),
        "mesh_faces": int(len(combined.faces)),
        "bounds_mm": [[round(float(v), 3) for v in combined.bounds[0]],
                      [round(float(v), 3) for v in combined.bounds[1]]],
        "segments": [{"label": s["label"], "voxels": s["voxels"],
                      "rgba": s["rgba"], "faces": int(len(s["mesh"].faces))}
                     for s in segs[:200]],
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
