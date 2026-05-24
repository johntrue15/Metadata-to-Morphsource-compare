#!/usr/bin/env python3
"""
Replay a paint session bundle against a remote 3D Slicer + SlicerNNInteractive.

Given a bundle produced by ``export_session.py`` (or any directory with
``manifest.json`` + ``clicks.jsonl`` in the documented schema), this
script:

  1. Verifies the target Slicer has a volume whose ``sha256_voxels``
     matches the bundle's source volume hash. (Refuses to proceed if
     not — that's the cardinal rule for reproducibility.)
  2. Optionally resets any pre-existing segmentation on that volume.
  3. Replays every click from ``clicks.jsonl`` in order, honoring the
     original ``made_new_segment`` and ``click_positive`` flags.
  4. After each click, records the resulting voxel delta and per-segment
     count, so we can compare against the bundle's recorded trajectory.
  5. At the end, exports the resulting composite + per-segment NIfTIs
     and writes ``replay_log.jsonl`` + ``replay_summary.json`` next to
     them. If the bundle includes a composite, we also report whether
     SHA256 matches.

Usage
-----
  set -a && source .env && set +a
  python3 .github/scripts/replay_session.py \\
      --bundle paper_artifacts/mouse_skull_session_001 \\
      --target-volume IMPC_sample_data \\
      --reset-first \\
      --out-dir paper_artifacts/mouse_skull_session_001/replay_<timestamp>

The bundle ``replay.sh`` is a thin wrapper that calls this with sensible
defaults.
"""
from __future__ import annotations

import argparse
import base64
import datetime
import hashlib
import json
import os
import socket
import sys
import textwrap
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = Path(__file__).resolve().parent

sys.path.insert(0, str(SCRIPTS_DIR))


# ---------------------------------------------------------------------------
# HTTP + recipes
# ---------------------------------------------------------------------------

def _read_url() -> str:
    url = (
        os.environ.get("SLICER_WEBSERVER_URL", "").strip()
        or os.environ.get("NNI_REMOTE_URL", "").strip()
    )
    if not url:
        sys.exit("ERROR: set SLICER_WEBSERVER_URL or NNI_REMOTE_URL")
    if url.startswith("ws://"):
        url = "http://" + url[len("ws://"):]
    elif url.startswith("wss://"):
        url = "https://" + url[len("wss://"):]
    return url.rstrip("/")


def post_python(base_url: str, source: str, timeout: float = 180,
                retries: int = 3, retry_sleep: float = 4.0) -> dict:
    body = source.encode("utf-8")
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                base_url + "/slicer/exec", data=body, method="POST",
                headers={"Content-Type": "text/plain"},
            )
            t0 = time.time()
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                content = resp.read()
                status = resp.status
            if status != 200:
                raise RuntimeError(f"/slicer/exec -> HTTP {status}: {content[:300]!r}")
            result = json.loads(content)
            result["_dt_s"] = round(time.time() - t0, 3)
            return result
        except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
            last_err = e
            print(f"  [exec retry {attempt+1}/{retries}] {e!r}; sleeping {retry_sleep}s")
            time.sleep(retry_sleep)
    raise RuntimeError(f"/slicer/exec exhausted retries: {last_err!r}")


# Recipes imported from siblings.
from run_telemetry import (  # noqa: E402
    CAPTURE_REMOTE_ENV_SRC,
    HASH_ACTIVE_VOLUME_SRC,
    EXPORT_SEGMENTATION_SRC,
)
from slicer_remote_bright_seed import (  # noqa: E402
    SET_ACTIVE_VOLUME_SRC_TEMPLATE,
    RESET_SEGMENTATION_SRC,
    ENABLE_VISIBILITY_SRC,
)


# Apply a single click. Identical semantics to slicer_remote_bright_seed's
# STEP_SRC_TEMPLATE, minus the candidate-list bookkeeping (replay already
# knows what to click). Returns delta voxels + segment id.
APPLY_CLICK_SRC_TEMPLATE = textwrap.dedent("""
    import slicer, numpy as np, time, traceback
    i, j, k = {i}, {j}, {k}
    click_positive  = bool({click_positive})
    make_new_segment = bool({make_new_segment})
    out = {{}}
    try:
        mod = slicer.modules.slicernninteractive
        plugin = mod.widgetRepresentation().self()
        # Read mask BEFORE
        sel = slicer.app.applicationLogic().GetSelectionNode()
        vol = slicer.mrmlScene.GetNodeByID(sel.GetActiveVolumeID()) if sel else None
        ref_shape = None
        if vol is not None:
            ref_shape = tuple(slicer.util.arrayFromVolume(vol).shape)
        def _read_total_mask(shape):
            total = np.zeros(shape, dtype=bool)
            n_seg = 0
            for sn in slicer.util.getNodesByClass("vtkMRMLSegmentationNode"):
                if "do not touch" in sn.GetName().lower():
                    continue
                s = sn.GetSegmentation()
                for ii in range(s.GetNumberOfSegments()):
                    sid = s.GetNthSegmentID(ii)
                    try:
                        a = slicer.util.arrayFromSegmentBinaryLabelmap(sn, sid)
                    except Exception:
                        a = None
                    if a is None or a.shape != shape:
                        continue
                    total |= (a > 0)
                    n_seg += 1
            return total, n_seg
        mask_before, n_seg_before = _read_total_mask(ref_shape) if ref_shape else (None, 0)
        voxels_before = int(mask_before.sum()) if mask_before is not None else 0
        # Optionally create a new segment to paint into
        new_segment_id = None
        if make_new_segment:
            plugin.make_new_segment()
            try:
                new_segment_id = plugin.get_current_segment_id()
            except Exception:
                new_segment_id = None
        # Toggle prompt sign in the GUI to keep state consistent
        if click_positive:
            plugin.on_prompt_type_positive_clicked()
        else:
            plugin.on_prompt_type_negative_clicked()
        t0 = time.time()
        plugin.point_prompt(xyz=[int(i), int(j), int(k)],
                            positive_click=click_positive)
        click_seconds = round(time.time() - t0, 3)
        # Read mask AFTER
        mask_after, n_seg_after = _read_total_mask(ref_shape) if ref_shape else (None, 0)
        voxels_after = int(mask_after.sum()) if mask_after is not None else 0
        seg_id_after = None
        try:
            seg_id_after = plugin.get_current_segment_id()
        except Exception:
            pass
        out["status"] = "ok"
        out["voxels_before"] = voxels_before
        out["voxels_after"] = voxels_after
        out["delta"] = voxels_after - voxels_before
        out["n_segments_before"] = n_seg_before
        out["n_segments_after"] = n_seg_after
        out["made_new_segment"] = make_new_segment
        out["new_segment_id"] = new_segment_id
        out["segment_id"] = seg_id_after
        out["click_seconds"] = click_seconds
    except Exception as e:
        out["status"] = "exception"
        out["error"] = repr(e)
        out["traceback"] = traceback.format_exc()
    __execResult.update(out)
""").strip()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_bundle(bundle_dir: Path) -> dict:
    bundle_dir = Path(bundle_dir).resolve()
    if not (bundle_dir / "manifest.json").exists():
        sys.exit(f"no manifest.json in {bundle_dir}")
    manifest = json.loads((bundle_dir / "manifest.json").read_text())
    clicks_path = bundle_dir / "clicks.jsonl"
    if not clicks_path.exists():
        sys.exit(f"no clicks.jsonl in {bundle_dir}")
    clicks: list[dict] = []
    for line in clicks_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        clicks.append(json.loads(line))
    # Sort defensively by global_step then run_id
    clicks.sort(key=lambda c: (c.get("global_step", 0), c.get("step", 0)))
    return {"dir": bundle_dir, "manifest": manifest, "clicks": clicks}


def expected_volume_hash(manifest: dict) -> str | None:
    sv = manifest.get("source_volume") or {}
    return sv.get("sha256_voxels")


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while True:
            b = f.read(1 << 20)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--bundle", type=Path, required=True,
                   help="Bundle directory produced by export_session.py")
    p.add_argument("--target-volume", type=str, required=True,
                   help="Name of the vtkMRMLScalarVolumeNode to paint on. "
                        "Its sha256_voxels MUST match the bundle's source "
                        "volume hash (override with --skip-hash-check at "
                        "your peril).")
    p.add_argument("--reset-first", action="store_true",
                   help="Clear any existing segmentation on the target "
                        "volume before replaying.")
    p.add_argument("--skip-hash-check", action="store_true",
                   help="Replay even if sha256_voxels differs (useful "
                        "when intentionally replaying on new data). The "
                        "mismatch is recorded in replay_summary.json.")
    p.add_argument("--out-dir", type=Path, default=None,
                   help="Where to write replay artifacts "
                        "(default: <bundle>/replay_<UTC>)")
    p.add_argument("--no-export", action="store_true",
                   help="Skip exporting the final composite + segments")
    p.add_argument("--max-clicks", type=int, default=None,
                   help="Limit replay to the first N clicks (for smoke "
                        "tests).")
    args = p.parse_args(argv)

    base_url = _read_url()
    bundle = load_bundle(args.bundle)
    out_dir = args.out_dir or (
        args.bundle / f"replay_{datetime.datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "replay_log.jsonl"
    summary_path = out_dir / "replay_summary.json"

    def jlog(rec: dict) -> None:
        rec.setdefault("t", datetime.datetime.utcnow().isoformat() + "Z")
        with open(log_path, "a") as f:
            f.write(json.dumps(rec, default=str) + "\n")

    print(f"=== Replay session: {bundle['manifest'].get('bundle_label')!r} ===")
    print(f"  bundle    : {bundle['dir']}")
    print(f"  clicks    : {len(bundle['clicks'])}")
    print(f"  out_dir   : {out_dir}")
    print(f"  server    : {base_url}")

    jlog({"event": "replay_start",
          "bundle_dir": str(bundle["dir"]),
          "bundle_manifest": bundle["manifest"],
          "target_volume": args.target_volume,
          "reset_first": args.reset_first,
          "skip_hash_check": args.skip_hash_check,
          "host": socket.gethostname()})

    # 1. Pick the target volume + hash-check
    print("\n• Setting active volume and hashing it…")
    r = post_python(base_url,
                    SET_ACTIVE_VOLUME_SRC_TEMPLATE.format(
                        target_name=args.target_volume),
                    timeout=30)
    if r.get("status") != "ok":
        jlog({"event": "set_volume_failed", **r})
        sys.exit(f"could not set active volume: {r}")
    jlog({"event": "volume_set", **r})

    meta = post_python(base_url, HASH_ACTIVE_VOLUME_SRC, timeout=120)
    if meta.get("status") != "ok":
        jlog({"event": "hash_failed", **meta})
        sys.exit(f"could not hash volume: {meta}")
    actual_sha = meta["sha256_voxels"]
    expected_sha = expected_volume_hash(bundle["manifest"])
    jlog({"event": "volume_hashed",
          "actual_sha256_voxels": actual_sha,
          "expected_sha256_voxels": expected_sha,
          "shape_kji": meta["shape_kji"],
          "dtype": meta["dtype"]})
    print(f"  actual sha256   = {actual_sha}")
    print(f"  expected sha256 = {expected_sha}")
    if expected_sha and actual_sha != expected_sha:
        msg = ("target volume hash does NOT match bundle's source volume "
               f"hash:\n  expected {expected_sha}\n  got      {actual_sha}")
        if not args.skip_hash_check:
            jlog({"event": "abort_hash_mismatch"})
            sys.exit("REFUSING TO REPLAY: " + msg + "\nUse --skip-hash-check to override.")
        else:
            print("  ⚠ continuing despite hash mismatch (--skip-hash-check)")

    # 2. Optional reset
    if args.reset_first:
        print("\n• Resetting existing segmentation on the target volume…")
        r = post_python(base_url, RESET_SEGMENTATION_SRC, timeout=180)
        jlog({"event": "reset", **r})
        print(f"  status={r.get('status')}  cleared={r.get('cleared_nodes')}")

    # 3. Visibility (so screenshots / 3D show the result)
    r = post_python(base_url, ENABLE_VISIBILITY_SRC, timeout=30)
    jlog({"event": "visibility_enabled", **r})

    # 4. Replay each click
    clicks = bundle["clicks"]
    if args.max_clicks:
        clicks = clicks[: args.max_clicks]
    print(f"\n• Replaying {len(clicks)} clicks…")
    trajectory: list[dict] = []
    for idx, c in enumerate(clicks):
        ijk = c["ijk"]
        i, j, k = int(ijk[0]), int(ijk[1]), int(ijk[2])
        src = APPLY_CLICK_SRC_TEMPLATE.format(
            i=i, j=j, k=k,
            click_positive=bool(c.get("click_positive", True)),
            make_new_segment=bool(c.get("made_new_segment", False)),
        )
        t0 = time.time()
        r = post_python(base_url, src, timeout=240)
        dt = time.time() - t0
        rec = {
            "event": "replay_click",
            "global_step": c.get("global_step", idx),
            "expected_step": c.get("step"),
            "ijk": [i, j, k],
            "click_positive": bool(c.get("click_positive", True)),
            "made_new_segment": bool(c.get("made_new_segment", False)),
            "expected_segment_id": c.get("segment_id"),
            "expected_delta": c.get("delta"),
            "expected_voxels_after": c.get("voxels_after"),
            "actual": r,
            "round_trip_s": round(dt, 3),
        }
        trajectory.append(rec)
        jlog(rec)
        print(f"  [{idx:>3}/{len(clicks)}] ijk={[i,j,k]}  "
              f"new_seg={c.get('made_new_segment')}  "
              f"Δ_expected={c.get('delta')}  "
              f"Δ_actual={r.get('delta')}  "
              f"voxels_after={r.get('voxels_after')}  "
              f"({dt:.1f}s rt, {r.get('click_seconds')}s remote)")
        if r.get("status") != "ok":
            print(f"  ! click failed: {r}")
            break

    # 5. Final segmentation export
    composite_match = None
    if not args.no_export:
        print("\n• Exporting final segmentation…")
        r = post_python(base_url, EXPORT_SEGMENTATION_SRC, timeout=900,
                        retries=3, retry_sleep=8.0)
        if r.get("status") != "ok":
            jlog({"event": "export_failed", **{k: v for k, v in r.items()
                                                if k != "per_segment"}})
            print(f"  ! export failed: {r}")
        else:
            (out_dir / "segments").mkdir(parents=True, exist_ok=True)
            for ps in r.get("per_segment", []):
                if ps.get("data_b64"):
                    (out_dir / "segments" / ps["filename"]).write_bytes(
                        base64.b64decode(ps["data_b64"])
                    )
            if (r.get("composite") or {}).get("data_b64"):
                data = base64.b64decode(r["composite"]["data_b64"])
                (out_dir / "composite.nii.gz").write_bytes(data)
                actual_comp_sha = hashlib.sha256(data).hexdigest()
                expected_comp_sha = ((bundle["manifest"].get("final_segmentation") or {})
                                      .get("composite") or {}).get("sha256")
                composite_match = (expected_comp_sha == actual_comp_sha) \
                                  if expected_comp_sha else None
                jlog({"event": "composite_exported",
                      "size_bytes": len(data),
                      "actual_sha256": actual_comp_sha,
                      "expected_sha256": expected_comp_sha,
                      "match": composite_match})
                print(f"  composite sha256: actual   = {actual_comp_sha}")
                print(f"                    expected = {expected_comp_sha}")
                print(f"                    match    = {composite_match}")

    # 6. Summary
    expected_deltas = [c.get("delta") for c in clicks]
    actual_deltas = [t["actual"].get("delta") for t in trajectory]
    n_match = sum(
        1 for e, a in zip(expected_deltas, actual_deltas)
        if e == a and e is not None
    )
    summary = {
        "bundle_label": bundle["manifest"].get("bundle_label"),
        "bundle_dir": str(bundle["dir"]),
        "target_volume": args.target_volume,
        "expected_sha256_voxels": expected_sha,
        "actual_sha256_voxels": actual_sha,
        "hash_match": (expected_sha == actual_sha) if expected_sha else None,
        "clicks_attempted": len(clicks),
        "clicks_succeeded": sum(1 for t in trajectory if t["actual"].get("status") == "ok"),
        "deltas_matching_exactly": n_match,
        "delta_exact_match_frac": (n_match / max(1, len(actual_deltas))),
        "composite_sha256_match": composite_match,
        "out_dir": str(out_dir),
        "finished_at_utc": datetime.datetime.utcnow().isoformat() + "Z",
    }
    summary_path.write_text(json.dumps(summary, indent=2, default=str))
    jlog({"event": "replay_end", **summary})

    print()
    print("DONE.")
    print(f"  clicks succeeded         : {summary['clicks_succeeded']} / {summary['clicks_attempted']}")
    print(f"  deltas matching exactly  : {summary['deltas_matching_exactly']} / "
          f"{len(actual_deltas)}  ({100*summary['delta_exact_match_frac']:.1f}%)")
    if composite_match is not None:
        print(f"  composite sha256 match   : {composite_match}")
    print(f"  log     : {log_path}")
    print(f"  summary : {summary_path}")
    return 0 if summary["clicks_succeeded"] == summary["clicks_attempted"] else 1


if __name__ == "__main__":
    sys.exit(main())
