# PCB Layer Extraction And Iterative Scoring

This runbook automates:

1. CT preprocessing with explicit dewarp/stitch/flatten stages
2. Figure-derived layer GT mask extraction and CT registration
3. Layer-constrained nnInteractive copper segmentation on Jetstream
4. Iterative and final segmentation-vs-GT scoring

## 1) Build figure-derived GT masks

```bash
python3 .github/scripts/pcb_figure_gt.py extract \
  --figure top_copper="/abs/path/Figure28_top.png" \
  --figure layer2_copper="/abs/path/Figure29_l2.png" \
  --figure layer3_copper="/abs/path/Figure30_l3.png" \
  --figure bottom_copper="/abs/path/Figure31_bottom.png" \
  --out-dir data/pcb/reference/generated
```

Outputs:

- `data/pcb/reference/generated/figure_gt_manifest.json`
- `*_mask.png` (binary copper masks)

## 2) Preprocess CT (dewarp/stitch/flatten hooks)

From an existing NIfTI:

```bash
python3 .github/scripts/pcb_preprocess_layers.py \
  --input-volume .local/pcb_data/pcb_ti.nii.gz \
  --out-dir runs/pcb_preprocess_local \
  --dewarp-mode normalize \
  --stitch-mode identity \
  --flatten-layers 4
```

From a TIFF stack:

```bash
python3 .github/scripts/pcb_preprocess_layers.py \
  --input-stack-dir "/path/to/TI tiff stack" \
  --out-dir runs/pcb_preprocess_local \
  --max-axis 512 \
  --spacing-xyz 0.05 0.05 0.05 \
  --dewarp-mode normalize \
  --flatten-layers 4
```

Outputs:

- `pcb_preprocessed.nii.gz`
- `flattened_layers/layer_*.png`
- `preprocess_manifest.json`

## 3) Register GT masks onto CT grid

```bash
python3 .github/scripts/pcb_figure_gt.py register \
  --manifest data/pcb/reference/generated/figure_gt_manifest.json \
  --ct-volume runs/pcb_preprocess_local/pcb_preprocessed.nii.gz \
  --out-dir runs/pcb_gt_registered_local \
  --refine-translation
```

Outputs:

- `registered_gt_manifest.json`
- `<layer>_gt_registered.nii.gz`
- `<layer>_overlay.png`

## 4) Jetstream iterative segmentation + scoring

### Environment

```bash
set -a && source .env && set +a
export PCB_CT_VOLUME=/home/exouser/Desktop/pcb_ti_jetstream.nii.gz
export PCB_FIGURE_GT_MANIFEST=data/pcb/reference/generated/figure_gt_manifest.json
export PCB_GT_LABELMAP=runs/pcb_gt_registered_YYYYmmddTHHMMSS/top_copper_gt_registered.nii.gz
```

### Deploy scripts

```bash
python3 .github/scripts/jetstream_controller.py deploy --wait --restart
```

### Run presets

```bash
# Optional preprocessing run on ECU checkout
python3 .github/scripts/jetstream_controller.py preset pcb-preprocess-layers --wait

# Register GT on the Jetstream box
python3 .github/scripts/jetstream_controller.py preset pcb-register-gt --wait

# Run layer-constrained iterative copper loop with per-step scoring
python3 .github/scripts/jetstream_controller.py preset pcb-iterate-score --max-steps 20 --wait
```

### Artifacts

`pcb-iterate-score` writes:

- `step_NN/layer_view.png`, `step_NN/state.json`
- `artifacts/copper_composite.nii.gz`
- `summary.json`
- `final_metrics.json`
- `results.csv` (iteration trend)

## 5) Direct copper run with GT scoring

```bash
python3 .github/scripts/slicer_remote_pcb_copper.py \
  --phase copper \
  --volume pcb_ti_jetstream \
  --remote-volume-path /home/exouser/Desktop/pcb_ti_jetstream.nii.gz \
  --noise-manifest runs/pcb_noise_export_20260527/noise_manifest.json \
  --gt-labelmap runs/pcb_gt_registered_YYYYmmddTHHMMSS/top_copper_gt_registered.nii.gz \
  --score-each-step \
  --score-no-surface \
  --max-steps 20 \
  --out-dir runs/pcb_iterative_score_manual
```

## Notes

- Current preprocessing `dewarp` and `stitch` are explicit modular hooks with
  conservative defaults (`identity`, `normalize`). They are intentionally
  simple to keep the pipeline stable while stronger methods are added.
- The segmentation loop is constrained to a single layer plane to avoid
  side-view prompt drift.
