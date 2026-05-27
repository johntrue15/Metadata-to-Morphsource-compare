# iMessage native 3D (USDZ)

Apple’s iMessage shows **inline 3D previews** for `.usdz` files (AR Quick Look). GLB is not native; convert first.

## One-time setup (Mac)

```bash
bash scripts/macos/install_usdz_tools.sh
```

This installs:

- `usd-core` — Pixar OpenUSD Python (Apple’s USDZ stack)
- `.local/usdzconvert/` — Apple `usdzconvert` CLI (GLB/OBJ → USDZ), patched for Python 3.9+

**Reality Converter** (optional GUI from [Apple AR resources](https://developer.apple.com/augmented-reality/resources/)) does the same job with drag-and-drop. The CLI path above is enough for automation.

## Export Colors of Skull bright-seed

```bash
bash scripts/dev/export_imessage_usdz.sh \
  --glb runs/colors_skull_viewer/crotalus_skull_bright.glb \
  --name crotalus_skull_bright
```

Or from the labelmap in one step:

```bash
bash scripts/dev/export_imessage_usdz.sh \
  --labelmap runs/colors_skull_viewer/composite.nii.gz
```

## Share

1. Finder opens with `crotalus_skull_bright.usdz` selected.
2. Drag the file into **Messages**.
3. On iPhone/iPad, tap the attachment → **View in AR**.

Mac Quick Look: select the `.usdz` and press **Space**.

## Pipeline

```
composite.nii.gz  →  labelmap_to_glb.py  →  .glb
.glb              →  usdzconvert         →  .usdz  →  iMessage
```
