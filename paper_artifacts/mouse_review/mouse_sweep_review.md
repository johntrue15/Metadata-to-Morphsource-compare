# Mouse sweep review vs Jetstream reference

Reference: `paper_artifacts/mouse_skull_session_001/composite.nii.gz`
Reference voxels: 434805

## Ranked unique configs (density collapsed)

| Rank | p | floor | clicks | union | Dice | IoU | coverage | score | dur |
|------|---|-------|--------|-------|------|-----|----------|-------|-----|
| 1 | 99.0 | 0.2 | 28 | 388,155 | 0.9424 | 0.8911 | 0.8918 | 0.8413 | 3424s |
| 2 | 95.0 | 0.5 | 13 | 303,121 | 0.8011 | 0.6682 | 0.6798 | 0.7169 | 548s |
| 3 | 97.0 | 0.5 | 12 | 287,661 | 0.7961 | 0.6613 | 0.6614 | 0.7130 | 338s |
| 4 | 99.0 | 0.5 | 8 | 229,216 | 0.6903 | 0.5271 | 0.5271 | 0.6025 | 164s |

## Failed / timed out

- `impc_mouse__intensity_drop_floor_frac_0p2__intensity_percentile_95p0__min_local_density_0p0` — failure
- `impc_mouse__intensity_drop_floor_frac_0p2__intensity_percentile_95p0__min_local_density_0p4` — failure
- `impc_mouse__intensity_drop_floor_frac_0p2__intensity_percentile_97p0__min_local_density_0p0` — failure
- `impc_mouse__intensity_drop_floor_frac_0p2__intensity_percentile_97p0__min_local_density_0p4` — failure
