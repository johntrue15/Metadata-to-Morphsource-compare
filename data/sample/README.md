# MorphoSource Sample Data

This directory holds small, **URL-loadable** NRRD test assets derived from
public MorphoSource specimens. They serve the same purpose as
[`SlicerMorph/SampleData/IMPC_sample_data.nrrd`](https://github.com/SlicerMorph/SampleData/blob/master/IMPC_sample_data.nrrd):
a tiny, deterministic dataset that 3D Slicer (and the Jetstream Slicer
remote in this repo) can pull on demand for smoke tests, demos, and
single-specimen ground-truth pipeline runs.

Each asset is staged by
[`.github/scripts/stage_morphosource_sample.py`](../../.github/scripts/stage_morphosource_sample.py),
which downloads the original MorphoSource media bundles, crops to the
mesh bounding box (plus a small margin), stride-downsamples so the
largest axis stays `≤ --max-axis` voxels, and writes gzip-encoded
single-file NRRDs alongside a JSON provenance sidecar.

The pipeline is **split into two phases** that map onto the
runner topology in [`docs/RUNNER_TOPOLOGY.md`](../../docs/RUNNER_TOPOLOGY.md):

| Phase | Work | Host | Output |
| --- | --- | --- | --- |
| `--phase ct-only` | MorphoSource API, TIFF stream, crop + stride downsample, gzip NRRD | **Mac mini** driver (or any host) | `<slug>_ct.nrrd` + partial `<slug>.provenance.json` |
| `--phase voxelize-only` | Load staged CT NRRD as the reference grid, rasterize the mesh onto it with trimesh + rtree ray-casting | **Dell GPU runner** or **Jetstream2** (the script refuses to run this on macOS unless `MORPHOCLAW_FORCE_MAC_VOXELIZE=1`) | `<slug>_gt_labelmap.nrrd` + merged provenance |
| `--phase all` | Both phases in one process | GPU host only | All three files |

The split exists because rasterizing a multi-million-polygon
MorphoSource mesh onto the CT grid is genuinely heavy work (a 2.86 M-
poly mesh hung `vtkPolyDataToImageStencil` at 98 % CPU for **52 hours**
on the Mac mini before being killed; the trimesh fallback hit a similar
multi-hour stall). The CT phase, by contrast, takes ~30 s and is
exactly what the Mac mini is good at: compression + NRRD generation.

## Available samples

| Slug | Specimen | Files | CT source | Mesh source |
|------|----------|-------|-----------|-------------|
| `tuatara_skull_000358663` | *Sphenodon punctatus* (tuatara), skull | `*_ct.nrrd`, `*_gt_labelmap.nrrd`, `*.provenance.json` | [media 000011009](https://www.morphosource.org/concern/media/000011009?locale=en) | [media 000358663](https://www.morphosource.org/concern/media/000358663?locale=en) |
| `crotalus_skull_000445108` | *Crotalus intermedius* (Colors of Skull pilot) | same pattern | [media 000445159](https://www.morphosource.org/concern/media/000445159?locale=en) | [media 000691954](https://www.morphosource.org/concern/media/000691954?locale=en) |

URL manifest for the Crotalus sample (raw GitHub paths + pilot defaults):
[`colors_of_skull_urls.json`](colors_of_skull_urls.json).

Stage on the Mac, then voxelize on Dell/Jetstream:

```bash
make stage-colors-skull-ct    # Mac mini
make stage-colors-skull-gt    # GPU host (after CT is in data/sample/)
```

Jetstream smoke (10 bright-seed clicks after pushing to `main`):

```bash
export SLICER_WEBSERVER_URL=http://127.0.0.1:2016/
make jetstream-10click-smoke
```

For each slug:

- `<slug>_ct.nrrd` — scalar CT volume (single-file NRRD, gzip-encoded),
  cropped to the skull and downsampled. Load this in Slicer as a regular
  **Volume**.
- `<slug>_gt_labelmap.nrrd` — `uint8` binary labelmap voxelized from the
  source mesh onto the *same* grid as the CT NRRD. Load this in Slicer
  as a **LabelMap Volume**, then convert to a `Segmentation` node if you
  want segment-style overlays.
- `<slug>.provenance.json` — full provenance: media IDs, original voxel
  spacing, downsample stride, crop indices, output dimensions, output
  SHA-256s, mesh world bounds, and the origin convention that aligned
  the mesh with the CT.

## Load from URL in 3D Slicer

After this directory is pushed to `main`, the raw GitHub URL for any
file in `data/sample/` is:

```
https://raw.githubusercontent.com/<owner>/<repo>/main/data/sample/<filename>
```

In Slicer:

1. **File → Add Data → Choose File from URL…**
2. Paste the raw URL, e.g.
   `https://raw.githubusercontent.com/<owner>/<repo>/main/data/sample/tuatara_skull_000358663_ct.nrrd`
3. Pick the right "Description" (Volume for the CT, LabelMap for the GT)
   and click **OK**.

Or, headlessly from a Slicer Python console (this is what the Jetstream
remote will do):

```python
slicer.util.downloadAndLoadFromURL(
    "https://raw.githubusercontent.com/<owner>/<repo>/main/data/sample/tuatara_skull_000358663_ct.nrrd",
    "tuatara_skull_ct",
)
```

## Regenerating the samples

The recommended path is the GitHub Actions workflow
[`stage_morphosource_sample.yml`](../../.github/workflows/stage_morphosource_sample.yml),
which dispatches the CT phase to the Mac mini and the GT phase to the
Dell GPU runner, then optionally opens a PR with the refreshed
`data/sample/` files:

```bash
gh workflow run stage_morphosource_sample.yml \
    -f ct_media_id=000011009 \
    -f mesh_media_id=000358663 \
    -f slug=tuatara_skull_000358663 \
    -f commit_outputs=true
```

To reproduce locally, run the two phases on the appropriate hosts:

```bash
# On the Mac mini (or any driver host) -- fast, only writes the CT NRRD.
make stage-sample-ct

# On the Dell GPU runner / Jetstream2 -- after copying or rsyncing
# data/sample/<slug>_ct.nrrd + <slug>.provenance.json over from the
# Mac. Reads that CT NRRD as the grid and writes the GT labelmap.
make stage-sample-gt
```

If you really want to do both phases in one process on a GPU host:

```bash
# Dell or Jetstream only -- macOS will refuse without
# MORPHOCLAW_FORCE_MAC_VOXELIZE=1 in the environment.
make stage-sample
```

The script caches the raw MorphoSource downloads under
`data/morphosource-download-<media_id>/` (which is `.gitignore`d) so
re-runs are fast. Pass `--force-download` to refetch.

### Why two hosts?

Both the staged CT and the staged GT labelmap are tiny once they're
gzipped (~40 MB each), but *producing* them is asymmetric:

- CT generation (TIFF z-stack → cropped, downsampled, gzipped NRRD) is
  pure numpy + SimpleITK I/O. The Mac mini handles a 5 GB stack in
  ~30 s.
- GT generation (multi-million-polygon mesh → labelmap on the CT grid)
  is per-voxel geometric work. On the Mac mini both
  `vtkPolyDataToImageStencil` and `trimesh.contains()` stalled for
  hours; on the Dell runner the trimesh+rtree path is ~minutes.

See [`docs/RUNNER_TOPOLOGY.md`](../../docs/RUNNER_TOPOLOGY.md) for the
canonical reasoning and the workflow routing rules.

## Why not check in the full-resolution CT?

The original tuatara CT (`000011009`) is a ~5 GB TIFF z-stack at
~30 µm isotropic voxels — far above GitHub's 100 MB file-size hard
limit. We keep that bundle in a local cache and ship only the cropped,
downsampled NRRD here, which is small enough to fetch over plain HTTPS
without Git LFS. If you need the native resolution for a paper-grade
analysis, regenerate locally with `--max-axis 4096` (or download from
MorphoSource directly via the script above) and run the analysis from
the cache directory.
