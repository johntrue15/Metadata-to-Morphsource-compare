# PCB Figure Ground Truth Bundle

This directory is for figure-derived PCB layer reference artifacts.

## Expected layer names

- `top_copper`
- `layer2_copper`
- `layer3_copper`
- `bottom_copper`

## Build masks from figures

```bash
python3 .github/scripts/pcb_figure_gt.py extract \
  --figure top_copper="/abs/path/top.png" \
  --figure layer2_copper="/abs/path/layer2.png" \
  --figure layer3_copper="/abs/path/layer3.png" \
  --figure bottom_copper="/abs/path/bottom.png" \
  --out-dir data/pcb/reference/generated
```

This writes:

- `data/pcb/reference/generated/*_mask.png`
- `data/pcb/reference/generated/figure_gt_manifest.json`

## Register masks to CT grid

```bash
python3 .github/scripts/pcb_figure_gt.py register \
  --manifest data/pcb/reference/generated/figure_gt_manifest.json \
  --ct-volume .local/pcb_data/pcb_ti.nii.gz \
  --out-dir data/pcb/reference/registered \
  --refine-translation
```

This writes per-layer registered NIfTI labelmaps and overlay PNGs for QC.
