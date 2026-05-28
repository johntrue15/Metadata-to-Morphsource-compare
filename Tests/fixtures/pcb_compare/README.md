# PCB Compare Fixtures

This fixture namespace is for deterministic PCB layer registration + scoring tests.

Current tests generate synthetic arrays in-memory (no large binary fixtures
checked into git yet), but this directory reserves the schema and paths for
future real PCB mini-fixtures.

## Planned fixture layout

- `ct.nii.gz` — reference CT crop
- `gt_layer.nii.gz` — registered GT labelmap on same grid
- `pred_layer.nii.gz` — known prediction sample
- `baseline_metrics.json` — expected metric baseline
- `fixture.json` — manifest used by smoke/regression scripts
