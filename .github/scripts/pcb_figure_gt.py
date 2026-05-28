#!/usr/bin/env python3
"""
Build PCB copper-layer ground truth from reference figure images.

This script supports two stages:
1) Extract binary copper masks from layer figure screenshots.
2) Register masks to a CT volume grid and emit NIfTI labelmaps for scoring.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np


def _require_imaging_deps():
    try:
        import PIL  # noqa: F401
        import SimpleITK as sitk  # noqa: F401
    except Exception as exc:
        raise RuntimeError(
            "pcb_figure_gt requires Pillow + SimpleITK. "
            "Install requirements in your active environment."
        ) from exc


def _load_rgb(path: Path) -> np.ndarray:
    from PIL import Image

    arr = np.asarray(Image.open(path).convert("RGB"), dtype=np.uint8)
    if arr.ndim != 3 or arr.shape[2] != 3:
        raise ValueError(f"Expected RGB image at {path}")
    return arr


def _largest_bbox(mask: np.ndarray) -> tuple[int, int, int, int]:
    ys, xs = np.where(mask)
    if ys.size == 0:
        raise ValueError("No foreground pixels to derive board bbox")
    return int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())


def _estimate_board_crop(rgb: np.ndarray) -> tuple[int, int, int, int]:
    # Gray background in screenshots is low-saturation; board region is saturated.
    r = rgb[:, :, 0].astype(np.int16)
    g = rgb[:, :, 1].astype(np.int16)
    b = rgb[:, :, 2].astype(np.int16)
    sat = np.maximum.reduce([r, g, b]) - np.minimum.reduce([r, g, b])
    board_mask = sat > 18
    x0, y0, x1, y1 = _largest_bbox(board_mask)
    # Pad inward minimally to avoid caption text.
    return x0, y0, x1, y1


def _rgb_to_hsv_like(rgb: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    # Lightweight HSV-like decomposition for robust hue thresholds.
    arr = rgb.astype(np.float32) / 255.0
    r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
    cmax = np.maximum.reduce([r, g, b])
    cmin = np.minimum.reduce([r, g, b])
    delta = cmax - cmin
    sat = np.where(cmax <= 1e-6, 0.0, delta / np.maximum(cmax, 1e-6))

    hue = np.zeros_like(cmax)
    idx = delta > 1e-6
    ridx = idx & (cmax == r)
    gidx = idx & (cmax == g)
    bidx = idx & (cmax == b)
    hue[ridx] = ((g[ridx] - b[ridx]) / delta[ridx]) % 6.0
    hue[gidx] = ((b[gidx] - r[gidx]) / delta[gidx]) + 2.0
    hue[bidx] = ((r[bidx] - g[bidx]) / delta[bidx]) + 4.0
    hue = (hue / 6.0) % 1.0
    return hue, sat, cmax


def extract_copper_mask(rgb: np.ndarray) -> np.ndarray:
    hue, sat, val = _rgb_to_hsv_like(rgb)
    # Copper fills in provided figures are highly saturated and dominate area.
    candidate = (sat > 0.5) & (val > 0.35)
    if candidate.sum() == 0:
        raise ValueError("No copper candidate pixels found")

    # Pick dominant hue among high-saturation pixels.
    hvals = hue[candidate]
    bins = np.linspace(0.0, 1.0, 49)
    hist, edges = np.histogram(hvals, bins=bins)
    peak = int(hist.argmax())
    h0 = float(edges[peak])
    h1 = float(edges[peak + 1])
    center = 0.5 * (h0 + h1)

    # Circular hue distance threshold.
    dh = np.abs(hue - center)
    dh = np.minimum(dh, 1.0 - dh)
    copper = (dh < 0.07) & (sat > 0.35) & (val > 0.2)

    # Remove tiny dot-like noise from overlays.
    try:
        from scipy import ndimage as ndi  # type: ignore

        labels, n = ndi.label(copper)
        if n > 0:
            sizes = np.bincount(labels.ravel())
            keep = sizes >= 64
            keep[0] = False
            copper = keep[labels]
    except Exception:
        pass
    return copper.astype(np.uint8)


def _save_mask_png(mask: np.ndarray, path: Path) -> None:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.fromarray((mask > 0).astype(np.uint8) * 255)
    img.save(path)


@dataclass
class FigureLayer:
    name: str
    source: str
    mask_png: str
    board_crop_xyxy: list[int]
    mask_shape_yx: list[int]
    copper_pixels: int


def cmd_extract(args: argparse.Namespace) -> int:
    out_dir = args.out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    layers: list[FigureLayer] = []

    for item in args.figure:
        if "=" not in item:
            raise SystemExit(f"--figure requires name=path, got: {item!r}")
        name, raw_path = item.split("=", 1)
        src = Path(raw_path).expanduser().resolve()
        rgb = _load_rgb(src)
        x0, y0, x1, y1 = _estimate_board_crop(rgb)
        board = rgb[y0 : y1 + 1, x0 : x1 + 1]
        mask = extract_copper_mask(board)

        mask_path = out_dir / f"{name}_mask.png"
        _save_mask_png(mask, mask_path)
        layers.append(
            FigureLayer(
                name=name,
                source=str(src),
                mask_png=str(mask_path),
                board_crop_xyxy=[x0, y0, x1, y1],
                mask_shape_yx=[int(mask.shape[0]), int(mask.shape[1])],
                copper_pixels=int(mask.sum()),
            )
        )

    manifest = {
        "version": 1,
        "mode": "figures_only",
        "layers": [layer.__dict__ for layer in layers],
    }
    manifest_path = out_dir / "figure_gt_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"Wrote {manifest_path}")
    return 0


def _resize_mask(mask: np.ndarray, out_h: int, out_w: int) -> np.ndarray:
    from PIL import Image

    img = Image.fromarray((mask > 0).astype(np.uint8) * 255)
    resized = img.resize((out_w, out_h), resample=Image.Resampling.NEAREST)
    arr = np.asarray(resized, dtype=np.uint8)
    return (arr > 127).astype(np.uint8)


def _phase_corr_shift(a: np.ndarray, b: np.ndarray) -> tuple[int, int]:
    # Return (dy, dx), aligning b to a.
    fa = np.fft.rfft2(a.astype(np.float32))
    fb = np.fft.rfft2(b.astype(np.float32))
    cps = fa * np.conj(fb)
    denom = np.maximum(np.abs(cps), 1e-6)
    cps /= denom
    corr = np.fft.irfft2(cps)
    y, x = np.unravel_index(np.argmax(corr), corr.shape)
    if y > corr.shape[0] // 2:
        y -= corr.shape[0]
    if x > corr.shape[1] // 2:
        x -= corr.shape[1]
    return int(y), int(x)


def _shift_2d(mask: np.ndarray, dy: int, dx: int) -> np.ndarray:
    out = np.zeros_like(mask)
    h, w = mask.shape
    ys0 = max(0, -dy)
    ys1 = min(h, h - dy)  # source
    yd0 = max(0, dy)
    yd1 = yd0 + (ys1 - ys0)
    xs0 = max(0, -dx)
    xs1 = min(w, w - dx)
    xd0 = max(0, dx)
    xd1 = xd0 + (xs1 - xs0)
    if ys1 > ys0 and xs1 > xs0:
        out[yd0:yd1, xd0:xd1] = mask[ys0:ys1, xs0:xs1]
    return out


def _embed_on_volume_plane(
    mask_yx: np.ndarray,
    volume_shape_zyx: tuple[int, int, int],
    layer_axis: int,
    layer_index: int,
) -> np.ndarray:
    z, y, x = volume_shape_zyx
    vol = np.zeros((z, y, x), dtype=np.uint8)
    if layer_axis == 2:  # k-axis
        vol[layer_index, :, :] = mask_yx
    elif layer_axis == 1:  # j-axis
        vol[:, layer_index, :] = mask_yx
    else:  # i-axis
        vol[:, :, layer_index] = mask_yx
    return vol


def _read_ct_for_layer(path: Path):
    import SimpleITK as sitk

    img = sitk.ReadImage(str(path))
    arr = sitk.GetArrayFromImage(img)  # z,y,x
    dims_ijk = list(img.GetSize())
    axis = int(np.argmin(dims_ijk))
    layer_idx = dims_ijk[axis] // 2
    if axis == 2:
        layer = arr[layer_idx, :, :]
    elif axis == 1:
        layer = arr[:, layer_idx, :]
    else:
        layer = arr[:, :, layer_idx]
    # Normalize for registration.
    layer_f = layer.astype(np.float32)
    p1, p99 = np.percentile(layer_f, [1, 99])
    if p99 > p1:
        layer_f = np.clip((layer_f - p1) / (p99 - p1), 0.0, 1.0)
    else:
        layer_f = np.zeros_like(layer_f)
    return img, arr.shape, axis, layer_idx, layer_f


def cmd_register(args: argparse.Namespace) -> int:
    _require_imaging_deps()
    import SimpleITK as sitk
    from PIL import Image

    manifest = json.loads(Path(args.manifest).read_text())
    ct_img, ct_shape_zyx, layer_axis, layer_index_auto, ct_layer = _read_ct_for_layer(
        Path(args.ct_volume).resolve()
    )
    layer_index = int(args.layer_index) if args.layer_index is not None else layer_index_auto
    out_dir = args.out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for layer in manifest.get("layers", []):
        src_mask = np.asarray(Image.open(layer["mask_png"]).convert("L"), dtype=np.uint8)
        src_mask = (src_mask > 127).astype(np.uint8)
        target_h, target_w = ct_layer.shape
        mask_resized = _resize_mask(src_mask, target_h, target_w)

        dy = dx = 0
        if args.refine_translation:
            dy, dx = _phase_corr_shift(ct_layer, mask_resized.astype(np.float32))
            mask_resized = _shift_2d(mask_resized, dy, dx)

        vol_mask = _embed_on_volume_plane(
            mask_resized,
            ct_shape_zyx,
            layer_axis=layer_axis,
            layer_index=layer_index,
        )
        out_img = sitk.GetImageFromArray(vol_mask.astype(np.uint8))
        out_img.CopyInformation(ct_img)
        nii_path = out_dir / f"{layer['name']}_gt_registered.nii.gz"
        sitk.WriteImage(out_img, str(nii_path), useCompression=True)

        # QC overlay
        overlay = np.zeros((target_h, target_w, 3), dtype=np.uint8)
        base = (ct_layer * 255.0).astype(np.uint8)
        overlay[:, :, :] = base[:, :, None]
        overlay[:, :, 1] = np.maximum(overlay[:, :, 1], mask_resized * 255)
        overlay_path = out_dir / f"{layer['name']}_overlay.png"
        Image.fromarray(overlay).save(overlay_path)

        results.append(
            {
                "name": layer["name"],
                "registered_labelmap": str(nii_path),
                "overlay_png": str(overlay_path),
                "translation_yx": [dy, dx],
                "ct_layer_axis": layer_axis,
                "ct_layer_index": layer_index,
                "copper_voxels": int(vol_mask.sum()),
            }
        )

    out_manifest = {
        "version": 1,
        "ct_volume": str(Path(args.ct_volume).resolve()),
        "ct_shape_zyx": list(ct_shape_zyx),
        "ct_layer_axis": layer_axis,
        "ct_layer_index": layer_index,
        "layers": results,
    }
    out_manifest_path = out_dir / "registered_gt_manifest.json"
    out_manifest_path.write_text(json.dumps(out_manifest, indent=2), encoding="utf-8")
    print(f"Wrote {out_manifest_path}")
    return 0


def _parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    pe = sub.add_parser("extract", help="extract binary copper masks from figures")
    pe.add_argument(
        "--figure",
        action="append",
        required=True,
        help="Layer figure mapping: layer_name=/abs/or/rel/path.png (repeatable)",
    )
    pe.add_argument("--out-dir", type=Path, required=True)
    pe.set_defaults(func=cmd_extract)

    pr = sub.add_parser("register", help="register extracted masks to CT grid/layer")
    pr.add_argument("--manifest", type=Path, required=True,
                    help="figure_gt_manifest.json from extract")
    pr.add_argument("--ct-volume", type=Path, required=True)
    pr.add_argument("--out-dir", type=Path, required=True)
    pr.add_argument("--layer-index", type=int, default=None,
                    help="Override plane index on thinnest IJK axis")
    pr.add_argument("--refine-translation", action="store_true",
                    help="Use phase-correlation translation refinement")
    pr.set_defaults(func=cmd_register)
    return p.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> int:
    args = _parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())

