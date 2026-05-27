# Runner topology and compute planning

Canonical plan for where work runs. Read this before wiring workflows,
local `.env`, or Cursor sessions.

> **The Mac mini self-hosted runner is a driver.** It orchestrates jobs,
> talks to MorphoSource, parses manifests, and issues HTTP/SSH to remote
> tools. It does **not** host nnInteractive, 3D Slicer, or heavy
> PyTorch/MONAI training anymore.
>
> **Jetstream2** is the heavy GPU tool (Slicer Web Server + nnInteractive
> over Exosphere). **The Dell GPU box** is the light GPU tool (mesh
> morphometrics, embeddings, smaller training). **GitHub `ubuntu-latest`**
> exercises offline replay with committed JSONL fixtures.

## Why we pivoted

We initially used the Mac mini as a workstation that could run
nnInteractive, Slicer, MONAI, SimpleITK, and the iterative training loop
locally (including the 10-click bright-seed demo driven from Cursor).

That model failed in practice:

1. **Fragile local Python** — Anaconda on Apple Silicon has repeatedly
   SIGSEGV'd on `numpy` import, blocking SimpleITK / vtk / anything in the
   seg-train stack.
2. **RAM and proxy limits** — A 16 GB Mac cannot hold nnU-Net inference
   comfortably; driving Slicer over the Exosphere proxy from the Mac still
   works, but **compute must stay on Jetstream** (chunked `/slicer/exec`,
   not local Slicer).
3. **Duplicate work** — We already pay for Jetstream CUDA + a Dell GPU;
   nursing a third stack on the Mac mini has no payoff.

**New rule:** if a step needs CUDA, vtk, or Slicer, it runs on Jetstream
or the Dell — never on the Mac mini runner.

### 2026-05-24 cautionary tale: local pilot runs are forbidden

The doctrine says "no nnInteractive / Slicer / heavy SimpleITK on the Mac
mini." It did *not* say "do not launch the pilot *orchestrator* in a local
Mac terminal." On 2026-05-24 we learned why that's the same rule.

A `python3 .github/scripts/eval_project358382_pilot.py --specimens 3
--budgets 10,25,50,100` was pasted into a Cursor terminal on the Mac
mini. The script's discovery + manifest stages were genuinely
driver-only work, but the subsequent stages each landed an artifact
on the driver's disk:

- `data/morphosource-download-<id>/` — 17 GB of CT TIFFs + meshes
  cached across two specimens
- `runs/pilot_project358382_<ts>/<specimen>/preprocessing/ct_cropped.nii.gz` —
  multi-hundred-MB cropped CT per specimen
- `runs/pilot_project358382_<ts>/<specimen>/gt_voxelized.nii.gz` — same

By specimen 2 of 3 the workspace volume was at 100 %. The pilot died
at `OSError(28, 'No space left on device')`, the workflow output never
landed in `events.jsonl`, and the third specimen was lost.

The fix is enforcement, not a stern reminder:

1. **`eval_project358382_pilot.py` and `nninteractive_compare.py` now
   refuse to run on `sys.platform == "darwin"`** unless one of the
   explicitly-driver-only paths is in use (`--dry-run`, `--replay-from`,
   `--cached-specimens --no-download`, `--from-fixture`, or
   `--skip-paint-loop`). Override is `--force-local` /
   `MORPHOCLAW_FORCE_MAC_PILOT=1`.
2. **`morphosource_api_download.download_media()` checks free disk
   space** (`MORPHOSOURCE_MIN_FREE_GB`, default 5 GiB) before issuing
   the API call, so the script aborts before the first byte hits disk
   when there isn't headroom.
3. **`make pilot-dell` / `make pilot-jetstream`** wrappers dispatch to
   the right GHA runner via `gh workflow run`, with the same arg
   surface as the local script. These replace the temptation to paste
   the raw `python ...` command into a Mac terminal.

If you are an AI agent reading this and the user asks you to run the
pilot, dispatch `make pilot-dell` (or, once Phase 3 lands,
`make pilot-jetstream`). Never paste the `python ...` command into a
Mac terminal — the doctrine and the script-level guard both forbid it,
and the morning of 2026-05-24 is the reason.

### 2026-05 reinforcement: mesh voxelization belongs on a GPU host

`stage_morphosource_sample.py` was the next thing to learn this the hard
way. Producing the **CT** NRRD (TIFF streaming → crop → stride
downsample → gzipped NRRD) on the Mac mini takes ~30 s for a 39 MB
output and is exactly the kind of "compression + NRRD generation" job
the Mac is good at. Producing the paired **GT labelmap** (rasterizing
a multi-million-polygon MorphoSource mesh onto that grid) is *not*:

- ``vtkPolyDataToImageStencil`` stalled at 98 % CPU / 38 MB RSS for
  **52 hours** on the tuatara skull mesh before being killed.
- ``trimesh.mesh.contains()`` with rtree-accelerated rays hit a similar
  multi-hour hang on the same mesh, with Python's cyclic GC walking the
  AABB tree the entire time.
- Even after `vtkQuadricDecimation` cut the mesh from 2.86 M → 200 K
  triangles, both algorithms still budgeted in *hours* per specimen on
  this host.

So **anything that rasterizes a mesh into a labelmap belongs on the Dell
WSL runner (or Jetstream)**, not the Mac mini. The Mac drives, downloads,
streams the CT, and uploads/commits the result; the GPU host does the
voxelization.

### What is OK on the Mac mini

- MorphoSource API calls, manifest parsing, JSONL/CSV munging
- Streaming TIFF / DICOM slice reads + crop + stride downsample
- Gzipped NRRD / NIfTI **writing** (compression on already-cropped
  arrays is fine; it's the per-voxel geometric work that isn't)
- SHA256 / provenance JSON / git ops / `make unshelve` / GH issue API
- HTTP/SSH clients to Jetstream + Dell (the "driver" pattern in
  `slicer_remote_*`, `nninteractive_remote.py`, `eval_project358382_pilot.py`)
- Lightweight `numpy` over already-cropped arrays (≤ a few hundred MB)
- Pytest tiers 1 + 4 of `Tests/test_eval_project358382.sh`

### What is NOT OK on the Mac mini

- `vtkPolyDataToImageStencil`, `vtkSelectEnclosedPoints`,
  `trimesh.mesh.contains`, or any per-voxel mesh→labelmap pipeline on a
  > ~1 M-poly mesh or > ~100³-voxel grid
- nnInteractive / nnU-Net inference, MONAI training, any CUDA/MPS
  inference loop
- Headless / GUI Slicer compute (`vtkSegmentation` ops, Markups paint
  loops). HTTP *to* a remote Slicer is fine; doing the work locally is
  not.
- `numpy` / `SimpleITK` operations on full-resolution multi-GB volumes
  (Anaconda on Apple Silicon has SIGSEGV'd on plain `import numpy` in
  the past; assume any heavy SITK pipeline here is fragile)
- Anything that needs the `nninteractive` venv to do real work — keep
  that venv on the Mac only for *read-only* introspection (manifest
  parsing, fixture sanity), not inference

## The four roles

| Role | Where | Runs | Must not run |
| --- | --- | --- | --- |
| **Driver** | Mac mini self-hosted runner (`m4-morphosource`) + optional local Cursor for the same scripts | `eval_project358382_pilot.py` orchestration, `export_session.py` / `replay_session.py` as HTTP clients, MorphoSource API, research agent, pytest tiers 1+4, `make unshelve`, manifest/JSONL tooling, **`stage_morphosource_sample.py --phase ct-only`** (TIFF→cropped→downsampled CT NRRD), gzip / NRRD / NIfTI writing | Local nnInteractive inference, local Slicer GUI/headless, MONAI training, heavy `numpy`/`SimpleITK` in CI on the Mac, **`stage_morphosource_sample.py --phase voxelize-only`** (mesh → labelmap rasterization) |
| **Heavy GPU tool** | Jetstream2 / MorphoCloud (reachable via `SLICER_WEBSERVER_URL`, `NNI_REMOTE_URL`) | Slicer + SlicerNNInteractive, bright-seed 10-click pilot, volume push/load, full-res segmentation export | MorphoSource downloads, repo git ops, reading Mac `.env` |
| **Light GPU tool** | Dell self-hosted runner (`DellXPS-wsl-gpu`) | Mesh-only morphometrics, embedding inference, fixture regeneration when faster than mock, bootstrap smoke for Linux CUDA layout, **`stage_morphosource_sample.py --phase voxelize-only`** (mesh → labelmap rasterization onto a Mac-built CT grid) | Full interactive Slicer + nnInteractive paint loop (CUDA stack and ops live on Jetstream) |
| **Offline replay tester** | GitHub `ubuntu-latest` | `metadata_to_morphsource.jetstream_replay`, Tier 1 + Tier 4 of `Tests/test_eval_project358382.sh`, mock fixture record | Egress to Jetstream, Dell, or MorphoSource |

### Driver → tool wire format

All remote compute goes through one of:

- **HTTP** — `urllib` → Exosphere proxy → `/slicer/exec`, nnInteractive FastAPI
- **SSH** — `make unshelve`, `jetstream_remote_restart.sh` (infra only)
- **Recorded JSONL** — `JETSTREAM_RECORD` / `JETSTREAM_REPLAY` for CI and offline dev

```
   ┌────────────────────────────┐
   │ Mac mini (driver)          │
   │  GH Actions + local .env   │
   └─────────────┬──────────────┘
                 │  urllib / SSH
                 ▼
   ┌────────────────────────────┐     ┌────────────────────────────┐
   │ Jetstream2 (heavy tool)    │     │ Dell WSL (light GPU)       │
   │ Slicer :2016 + nnI :1527   │     │ mesh / embed / train smoke │
   └────────────────────────────┘     └────────────────────────────┘
                 │
                 │  fixtures only
                 ▼
   ┌────────────────────────────┐
   │ ubuntu-latest (CI replay)  │
   └────────────────────────────┘
```

## Workflow routing (target state)

| Workflow | `runs-on` | Role |
| --- | --- | --- |
| `eval_project358382_replay.yml` | `ubuntu-latest` | Offline replay; no GPU |
| `eval_project358382_record_mock.yml` | `ubuntu-latest` | Refresh JSONL from mock server |
| `eval_project358382_jetstream.yml` | `[self-hosted, jetstream]` | Live pilot; **driver may run on Mac**, Slicer/nnI on JS2 |
| `eval_project358382_dellgpu.yml` | `[self-hosted, gpu]` (Dell only) | Mesh/embed leg; explicit labels so Mac does not pick it up |
| `nninteractive_compare.yml` | `[self-hosted, gpu, nninteractive]` | **Dell** — batch compare, not interactive Jetstream pilot |
| `iterative_segmentation_training.yml` | `[self-hosted, gpu]` | Prefer **Dell** for training rounds; Jetstream TBD for paint-heavy rounds |
| `bootstrap_nninteractive.yml` | `[self-hosted, gpu]` | Validates venv on **Dell**; not a Mac-mini bootstrap target |
| `autoresearchclaw.yml` | `[self-hosted, morphosource]` or Mac labels | Driver-only research; enable nnI only via remote URLs in `.env` |
| `runner-smoke.yml` / `runner-liveness.yml` | configurable | Infra health per host |
| `stage_morphosource_sample.yml` | `ct-on-mac`: `[self-hosted, morphosource]`; `gt-on-dell`: `[self-hosted, gpu, nninteractive]` | Two-job pipeline: Mac downloads + writes CT NRRD; Dell voxelizes the mesh onto that grid; either commits to `data/sample/` |

Until a `jetstream` Actions runner is registered beside Slicer, live Jetstream
jobs are driven **from the Mac mini** (or a developer laptop) with
`SLICER_WEBSERVER_URL` / `NNI_REMOTE_URL` in `.env` — still driver/tool split,
just without GH Actions on the JS2 box yet.

## 10-click demo and session export (driver pattern)

These scripts are the reference driver implementation:

| Script | Driver does | Tool does |
| --- | --- | --- |
| `slicer_remote_bright_seed.py` | Candidate logic, logging, `/slicer/exec` recipes | Mask read, `point_prompt`, per-step screenshots |
| `export_session.py` | Merge `runs/`, chunked download client | `saveNode`, segment export, 4 MB file chunks |
| `replay_session.py` | Replay `clicks.jsonl` in order | Apply clicks, export final seg |
| `make unshelve IP=...` | SSH, rewrite `.env`, probe proxies | Start nnI tmux, print Web Server snippet |
| `make pilot-dell` / `make pilot-jetstream` | `gh workflow run`, print run id, optional `make tail RUN_ID=…` | Whole pilot (discovery → download → crop → voxelize → push → bright-seed → score) runs on the chosen self-hosted GPU runner |

**Do not** install nnInteractive or Slicer on the Mac mini for this path.
Use `make unshelve IP=…` then run bright-seed/export from the repo with
sourced `.env`.

**Do not** paste `python .github/scripts/eval_project358382_pilot.py …`
into a Mac terminal. The script's Darwin guard refuses unless you also
pass `--dry-run`, `--replay-from`, `--cached-specimens --no-download`,
or the escape hatch (`--force-local` / `MORPHOCLAW_FORCE_MAC_PILOT=1`).
For live runs use `make pilot-dell` (or `make pilot-jetstream` after
Phase 3).

## Per-package notes

### `eval_project358382_pilot.py`

- Stays a driver script; HTTP to Slicer is correct.
- Heavy imports (`SimpleITK`, `vtk`, `numpy`) must be **lazy** so `--help`,
  `--dry-run`, and `--replay-from` work on stock `python3` on the Mac.
- Live CT staging that needs SimpleITK should run on Jetstream (or use cached
  fixtures + replay).

### `seg_train` / iterative segmentation

- **Training / student inference:** Dell GPU runner (or Jetstream when paint
  rounds need the same box as Slicer).
- **Unit tests / smoke:** `ubuntu-latest` or synthetic fixtures; not Mac-mini
  full stack.

### `stage_morphosource_sample.py`

The script that produces the paired CT + GT-labelmap NRRDs under
`data/sample/` for URL-loadable Jetstream/Slicer ingestion. It splits
cleanly into two phases:

| Phase | Work | Runs on | Outputs |
| --- | --- | --- | --- |
| `ct-only` | Resolve MorphoSource metadata, download CT + (optionally) mesh into the `data/morphosource-download-*/` cache, stream TIFF slices, crop around the mesh bbox in world coords, stride-downsample, gzip-write the CT NRRD, write `*.provenance.json` minus the GT block | **Mac mini** (or any host) | `<slug>_ct.nrrd`, `<slug>.provenance.json` |
| `voxelize-only` | Read the staged CT NRRD as the reference grid, load the mesh, apply the auto-detected signed-permutation transform, rasterize onto the CT grid, gzip-write the labelmap NRRD, merge into `*.provenance.json` | **Dell** (or Jetstream) | `<slug>_gt_labelmap.nrrd`, merged provenance |
| `all` (default) | Both phases in one process | **Dell** or **Jetstream** | All three files |

Implementation rules:

- `--phase` defaults to `all`, but the script refuses to run the
  voxelization step on `sys.platform == "darwin"` unless
  `MORPHOCLAW_FORCE_MAC_VOXELIZE=1` is set. The `--phase ct-only`
  invocation has no such guard.
- The CT NRRD is fully self-describing (origin / spacing / direction in
  the gzipped header), so the `voxelize-only` step on the Dell does
  *not* need to re-resolve the CT's MorphoSource metadata — it reads
  the staged NRRD and the mesh straight from disk / cache.
- Both phases write to the same `--out-dir` so the GitHub Actions split
  is "Mac uploads `data/sample/*_ct.nrrd` as artifact → Dell downloads
  it → Dell runs `--phase voxelize-only` against it → Dell uploads
  `*_gt_labelmap.nrrd` → bot opens a PR with both."

### `metadata_to_morphsource.jetstream_replay`

- Single seam: `urlopen_via_session`.
- Committed `Tests/fixtures/jetstream_replay/sessions/*.jsonl` let CI prove
  the wire contract without GPUs.

## Self-hosted runner labels (target)

| Runner name | Labels (intent) | Role |
| --- | --- | --- |
| `m4-morphosource` | `self-hosted`, `macOS`, `morphosource`, **`driver`** | Orchestration, research, unshelve, remote HTTP clients |
| `DellXPS-wsl-gpu` | `self-hosted`, `gpu`, `cuda`, `nninteractive`, … | Light/medium GPU jobs **without** Jetstream Slicer |
| *(future)* Jetstream agent | `jetstream` | Optional: run pilot workflow on-box; Slicer already local |

**Remove from Mac mini (when relabeling):** `nninteractive`, `slicer`, `gpu`
if present — those invite workflows to schedule heavy work on the driver.

Workflows that target Dell or Jetstream must use **specific** label sets;
`runs-on: self-hosted` alone is wrong.

## Migration checklist

### Done / in progress

- [x] `docs/RUNNER_TOPOLOGY.md` (this file) as canonical planning doc
- [x] `eval_project358382_replay.yml` on `ubuntu-latest`
- [x] `eval_project358382_jetstream.yml` + `eval_project358382_dellgpu.yml` stubs (`workflow_dispatch`)
- [x] `metadata_to_morphsource.jetstream_replay` package + cached fixtures
- [x] `export_session.py` / `replay_session.py` with chunked Jetstream download
- [x] `make unshelve`, `make test-eval-replay`, `make test-eval358382`
- [x] **Phase 1 — Stop the bleeding (2026-05-24)** — Darwin guard in
      `eval_project358382_pilot.py` + `nninteractive_compare.py`; free-space
      pre-check in `morphosource_api_download.download_media()` (default
      5 GiB, `MORPHOSOURCE_MIN_FREE_GB` override); `make pilot-dell` /
      `make pilot-jetstream` / `make tail` dispatch wrappers; see the
      "2026-05-24 cautionary tale" section above

### Still to do

#### Phase 2 — Convert remaining manual paths to dispatch

- [ ] `make seg-train-dell` / `make seg-train-jetstream` wrappers
      (dispatch `iterative_segmentation_training.yml` / `seg_train_live_chameleon.yml`)
- [ ] `make nni-compare-dell` wrapper (dispatch `nninteractive_compare.yml`)
- [ ] Audit top-level `import SimpleITK` / `import vtk` / `import numpy` in
      `.github/scripts/` — lazy-import inside functions so `--help` /
      `--dry-run` work on stock `python3` on the Mac (belt + suspenders
      for the Phase 1 Darwin guard)
- [ ] Update `docs/self-hosted-gpu-runner.md` diagram (Dell = light GPU;
      Mac = driver)
- [ ] `Tests/test_eval_project358382.sh` Tier 1 + Tier 4 on stock `python3`
      (no nninteractive venv on Mac)

#### Phase 3 — Register a Jetstream GHA runner

- [ ] `jetstream_runner_install.sh` installs the `actions/runner` agent
      on JS2 under `/media/volume/MyData/actions-runner/` (survives unshelve),
      labels `[self-hosted, jetstream, Linux, X64]`
- [ ] Extend `jetstream_remote_restart.sh` with `--with-runner` mode
      (idempotent restart of the runner agent in tmux)
- [ ] Extend `jetstream_unshelve_start.py` with `--with-runner` flag;
      surface as `make unshelve UNSHELVE_FLAGS=--with-runner`
- [ ] Drop the `JETSTREAM_PILOT_FORCE=1` requirement from
      `make pilot-jetstream` once the runner is live
- [ ] Smoke: dispatch `eval_project358382_jetstream.yml` and confirm it
      lands on the JS2 runner

#### Phase 4 — Cache hygiene (optional)

- [ ] `MORPHOSOURCE_CACHE_DIR` env in `download_media()`; runner-specific
      defaults (`$RUNNER_TEMP/morphosource-cache` on GHA,
      `data/morphosource-download-*` on dev)
- [ ] Route `~/.autoresearchclaw/specimens` off the Mac (same env, or
      delete-after-success policy)
- [ ] Relabel Mac mini runner: add `driver`, drop `nninteractive` /
      `slicer` from Mac if listed

#### Sample staging (carry-over)

- [x] Split `stage_morphosource_sample.py` into `--phase {ct-only, voxelize-only, all}` so the Mac mini can ship the CT NRRD without ever touching the voxelizer
- [x] Add `make stage-sample-ct` (Mac-safe) / `make stage-sample-gt` (Dell/Jetstream-only) targets
- [x] Add `.github/workflows/stage_morphosource_sample.yml` with `ct-on-mac` + `gt-on-dell` jobs
- [ ] Once Dell has produced the GT labelmap for the tuatara sample, commit both NRRDs under `data/sample/` and reference them from `data/sample/README.md`

## Related docs

- [JETSTREAM_UNSHELVE.md](JETSTREAM_UNSHELVE.md) — driver-side `.env` + SSH after unshelve
- [PROJECT358382_REPLAY.md](PROJECT358382_REPLAY.md) — offline tier on `ubuntu-latest`
- [self-hosted-gpu-runner.md](self-hosted-gpu-runner.md) — Dell WSL bring-up (light GPU host)
- [ITERATIVE_SEGMENTATION.md](ITERATIVE_SEGMENTATION.md) — methodology (compute host-agnostic)
