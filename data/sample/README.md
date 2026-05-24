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

## Available samples

| Slug | Specimen | Files | CT source | Mesh source |
|------|----------|-------|-----------|-------------|
| `tuatara_skull_000358663` | *Sphenodon punctatus* (tuatara), skull | `*_ct.nrrd`, `*_gt_labelmap.nrrd`, `*.provenance.json` | [media 000011009](https://www.morphosource.org/concern/media/000011009?locale=en) | [media 000358663](https://www.morphosource.org/concern/media/000358663?locale=en) |

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

```bash
set -a && source .env && set +a
"$HOME/.autoresearchclaw/nninteractive/bin/python" \
    .github/scripts/stage_morphosource_sample.py \
    --ct-media-id 000011009 \
    --mesh-media-id 000358663 \
    --slug tuatara_skull_000358663 \
    --max-axis 512 \
    --margin-mm 2.0
```

The script caches the raw MorphoSource downloads under
`data/morphosource-download-<media_id>/` (which is `.gitignore`d) so
re-runs are fast. Pass `--force-download` to refetch.

## Why not check in the full-resolution CT?

The original tuatara CT (`000011009`) is a ~5 GB TIFF z-stack at
~30 µm isotropic voxels — far above GitHub's 100 MB file-size hard
limit. We keep that bundle in a local cache and ship only the cropped,
downsampled NRRD here, which is small enough to fetch over plain HTTPS
without Git LFS. If you need the native resolution for a paper-grade
analysis, regenerate locally with `--max-axis 4096` (or download from
MorphoSource directly via the script above) and run the analysis from
the cache directory.
