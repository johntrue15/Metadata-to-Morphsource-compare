#!/usr/bin/env python3
"""
Bright-spot greedy nnInteractive segmentation, driven from this Mac.

A deterministic counterpart to slicer_remote_loop.py — no LLM, no API
costs. The algorithm is:

  1. Threshold the volume at a chosen percentile (e.g. 99th) and collect
     every voxel above the threshold, sorted by intensity descending.
     This is the candidate seed list.
  2. While we still have budget AND candidates left:
       a. Read the current segmentation mask from the active segmentation
          node.
       b. Skip past any candidate that's already inside the mask.
       c. Click a positive point at the next candidate via
          ``plugin.point_prompt``.
       d. Re-read the mask, compute the voxel delta.
       e. Record the step. If the last few deltas are tiny we stop early
          (segmentation has saturated).

All heavy lifting (volume read, mask read, candidate selection, prompt
call) happens server-side inside Slicer over /slicer/exec; the script
sends a small Python recipe and gets back ~hundreds of bytes of JSON
per step. That's important: a 259x258x421 IMPC volume is ~28 MB, and
streaming it across the proxy every step would be wasteful.

Per-step artifacts go to ``runs/<name>/step_NN/``:
  red.png, yellow.png, green.png, threeD.png, state.json

Env vars
--------
  SLICER_WEBSERVER_URL  e.g. https://http-149-...-2016.proxy-js2-iu.exosphere.app/

Usage
-----
  set -a && source .env && set +a
  python3 .github/scripts/slicer_remote_bright_seed.py \\
      --volume IMPC_sample_data \\
      --reset-first \\
      --intensity-percentile 99 \\
      --max-steps 20 \\
      --out-dir runs/impc_bright_seed
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import textwrap
import time
import urllib.error
import urllib.request
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR))
# Add repo root so the optional jetstream_replay package is importable
# without changing the consumer's invocation contract. The package
# only kicks in when JETSTREAM_RECORD / JETSTREAM_REPLAY are set;
# otherwise it's a thin pass-through.
_REPO_ROOT = _SCRIPT_DIR.parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from run_telemetry import (  # noqa: E402  (sibling module)
    CAPTURE_REMOTE_ENV_SRC,
    CAPTURE_REMOTE_RESOURCES_SRC,
    EXPORT_SEGMENTATION_SRC,
    HASH_ACTIVE_VOLUME_SRC,
    PING_SLICER_SRC,
    RunLogger,
)

try:
    from metadata_to_morphsource.jetstream_replay.recorder import (  # noqa: E402
        urlopen_via_session,
    )
except Exception:  # pragma: no cover  (offline-replay package optional)
    def urlopen_via_session(request, data=None, timeout=60):
        return urllib.request.urlopen(request, data=data, timeout=timeout)


# ---------------------------------------------------------------------------
# HTTP helpers
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


def http_get(url: str, timeout: float = 20) -> bytes:
    with urlopen_via_session(url, timeout=timeout) as resp:
        if resp.status != 200:
            raise RuntimeError(f"GET {url} -> HTTP {resp.status}")
        return resp.read()


def _http_defaults() -> tuple[int, float, float]:
    """(retries, retry_sleep_s, default_timeout_s) from env."""
    return (
        int(os.environ.get("MORPHOCLAW_HTTP_RETRIES", "8")),
        float(os.environ.get("MORPHOCLAW_HTTP_RETRY_SLEEP", "20")),
        float(os.environ.get("MORPHOCLAW_STEP_TIMEOUT", "180")),
    )


_RETRYABLE_HTTP = frozenset({502, 503, 504})


def post_python(base_url: str, source: str, timeout: float | None = None,
                retries: int | None = None, retry_sleep: float | None = None) -> dict:
    """POST to /slicer/exec with retries on 5xx and transport errors."""
    dr, ds, dt = _http_defaults()
    if timeout is None:
        timeout = dt
    if retries is None:
        retries = dr
    if retry_sleep is None:
        retry_sleep = ds

    body = source.encode("utf-8")
    last_exc: BaseException | None = None
    t0 = time.time()
    for attempt in range(retries + 1):
        req = urllib.request.Request(
            base_url + "/slicer/exec", data=body, method="POST",
            headers={"Content-Type": "text/plain"},
        )
        try:
            with urlopen_via_session(req, timeout=timeout) as resp:
                content = resp.read()
                status = resp.status
        except urllib.error.HTTPError as e:
            content = e.read()
            status = e.code
            if status in _RETRYABLE_HTTP and attempt < retries:
                last_exc = RuntimeError(
                    f"/slicer/exec -> HTTP {status}: {content[:300]!r}"
                )
                time.sleep(retry_sleep)
                continue
            if status != 200:
                raise RuntimeError(
                    f"/slicer/exec -> HTTP {status}: {content[:300]!r}"
                )
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if attempt < retries:
                last_exc = e
                time.sleep(retry_sleep)
                continue
            raise RuntimeError(
                f"/slicer/exec transport failure after {attempt + 1} attempt(s): {e!r}"
            ) from e
        else:
            if status != 200:
                if status in _RETRYABLE_HTTP and attempt < retries:
                    last_exc = RuntimeError(
                        f"/slicer/exec -> HTTP {status}: {content[:300]!r}"
                    )
                    time.sleep(retry_sleep)
                    continue
                raise RuntimeError(
                    f"/slicer/exec -> HTTP {status}: {content[:300]!r}"
                )
            try:
                result = json.loads(content)
            except Exception:
                raise RuntimeError(f"non-JSON exec reply: {content[:300]!r}")
            result["_dt_s"] = round(time.time() - t0, 3)
            result["_retries_used"] = attempt
            return result

    if last_exc is not None:
        raise last_exc
    raise RuntimeError("/slicer/exec failed with no response")


READ_TOTAL_VOXELS_SRC = """\
import slicer, numpy as np
out = {}
try:
    sel = slicer.app.applicationLogic().GetSelectionNode()
    vol = slicer.mrmlScene.GetNodeByID(sel.GetActiveVolumeID()) if sel else None
    if vol is None:
        out["status"] = "no_volume"
    else:
        shape = tuple(slicer.util.arrayFromVolume(vol).shape)
        total = np.zeros(shape, dtype=bool)
        n_seg = 0
        for sn in slicer.util.getNodesByClass("vtkMRMLSegmentationNode"):
            if "do not touch" in sn.GetName().lower():
                continue
            seg = sn.GetSegmentation()
            for ii in range(seg.GetNumberOfSegments()):
                sid = seg.GetNthSegmentID(ii)
                try:
                    a = slicer.util.arrayFromSegmentBinaryLabelmap(sn, sid)
                except Exception:
                    a = None
                if a is None or a.shape != shape:
                    continue
                total |= (a > 0)
                n_seg += 1
        out["status"] = "ok"
        out["voxels"] = int(total.sum())
        out["n_segments"] = n_seg
except Exception as e:
    out["status"] = "exception"
    out["error"] = repr(e)
__execResult.update(out)
"""


def _recover_after_timeout(base_url: str, voxels_before: int,
                           logger: RunLogger, step: int) -> dict | None:
    """If the proxy timed out, the click may still have landed — probe voxels."""
    waits = (15, 25, 40)
    for i, delay in enumerate(waits, 1):
        logger.log(f"  504 recovery wait {i}/{len(waits)} ({delay}s)…")
        time.sleep(delay)
        try:
            probe = post_python(base_url, READ_TOTAL_VOXELS_SRC,
                                timeout=90, retries=3, retry_sleep=10)
        except Exception as exc:
            logger.log(f"  recovery probe failed: {exc!r}")
            continue
        after = int(probe.get("voxels", 0))
        if probe.get("status") == "ok" and after > voxels_before:
            logger.log(f"  recovered: voxels {voxels_before:,} -> {after:,} "
                       f"(+{after - voxels_before:,})")
            return {
                "status": "ok",
                "recovered_after_timeout": True,
                "voxels_before": voxels_before,
                "voxels_after": after,
                "delta": after - voxels_before,
                "n_segments_after": probe.get("n_segments"),
                "picked_ijk": [0, 0, 0],
                "intensity": 0.0,
                "skipped_inside": 0,
                "candidates_left": -1,
                "click_seconds": 0.0,
                "made_new_segment": True,
                "segment_id": "recovered",
                "segment_voxels": after - voxels_before,
            }
    return None


def _run_step_with_recovery(base_url: str, logger: RunLogger, step: int,
                            voxels_before_hint: int,
                            new_segment_per_click: bool) -> dict:
    """One bright-seed step; survive 504 via retries + voxel probe."""
    step_src = STEP_SRC_TEMPLATE.format(
        click_positive=True,
        new_segment=new_segment_per_click,
    )
    try:
        r = post_python(base_url, step_src)
    except RuntimeError as exc:
        logger.log(f"  step transport error: {exc!r}")
        logger.event("step_transport_error", step=step, error=repr(exc))
        recovered = _recover_after_timeout(
            base_url, voxels_before_hint, logger, step
        )
        if recovered is not None:
            logger.event("step_recovered", step=step, **recovered)
            return recovered
        raise

    if r.get("status") in ("ok", "no_more_candidates"):
        return r

    logger.log(f"  step returned {r.get('status')!r}, probing voxels…")
    recovered = _recover_after_timeout(
        base_url, voxels_before_hint, logger, step
    )
    if recovered is not None:
        logger.event("step_recovered", step=step, **recovered)
        return recovered
    return r


BATCH_STATE_PROBE_SRC = textwrap.dedent("""
    import slicer
    import numpy as np
    out = {}
    try:
        state = globals().get("_BS_STATE")
        if state is None:
            out["status"] = "not_initialized"
        else:
            hist = state.get("history", [])
            union = state.get("union_mask")
            out["status"] = "ok"
            out["history_len"] = int(len(hist))
            out["next_idx"] = int(state.get("next_idx", 0))
            out["voxels"] = int(union.sum()) if union is not None else None
            # Return the tail of history so the caller can resync the clicks
            # that completed server-side before the proxy timed out.
            n = int(globals().get("__probe_since", 0))
            out["tail"] = list(hist[n:])
    except Exception as e:
        out["status"] = "error"
        out["error"] = repr(e)
    __execResult.update(out)
""").strip()


def _run_batch_with_recovery(base_url: str, logger: RunLogger, step0: int,
                             batch_size: int, new_segment_per_click: bool,
                             history_len_before: int) -> dict:
    """Issue up to ``batch_size`` clicks in one exec; survive proxy timeouts.

    On a transport timeout the server may have already completed some (or all)
    of the clicks — server-side ``_BS_STATE.history`` is authoritative — so we
    probe it and reconstruct the per-click ``results`` from the history tail.
    """
    batch_src = STEP_BATCH_SRC_TEMPLATE.format(
        batch_size=batch_size,
        click_positive=True,
        new_segment=new_segment_per_click,
    )
    timeout = max(180, batch_size * 45)
    try:
        r = post_python(base_url, batch_src, timeout=timeout)
    except RuntimeError as exc:
        logger.log(f"  batch transport error: {exc!r}  probing server state…")
        logger.event("batch_transport_error", step=step0, error=repr(exc))
        probe_src = BATCH_STATE_PROBE_SRC.replace(
            'globals().get("__probe_since", 0)', str(int(history_len_before))
        )
        try:
            probe = post_python(base_url, probe_src, timeout=60, retries=3,
                                retry_sleep=10)
        except RuntimeError:
            raise exc
        if probe.get("status") != "ok":
            raise exc
        tail = probe.get("tail", [])
        results = []
        for h in tail:
            results.append({
                "status": "ok",
                "picked_ijk": h.get("ijk"),
                "intensity": h.get("intensity"),
                "voxels_before": h.get("voxels_before"),
                "voxels_after": h.get("voxels_after"),
                "delta": h.get("delta"),
                "click_positive": h.get("click_positive", True),
                "skipped_inside": h.get("skipped_inside", 0),
                "made_new_segment": h.get("made_new_segment"),
                "segment_id": h.get("segment_id"),
                "segment_voxels": h.get("segment_voxels", 0),
                "n_segments_after": probe.get("history_len"),
                "recovered_after_timeout": True,
            })
        logger.event("batch_recovered", step=step0, recovered=len(results),
                     voxels=probe.get("voxels"))
        return {
            "status": "ok" if results else "no_more_candidates",
            "results": results,
            "voxels": probe.get("voxels"),
            "candidates_left": -1,
            "batch_clicks": len(results),
            "recovered_after_timeout": True,
        }
    return r


def _run_batched_clicks(base_url: str, logger: "RunLogger", args,
                        total_voxels: int, explosion_threshold: int,
                        new_segment_per_click: bool, resource_cfg: dict):
    """Greedy click loop using server-side batching (``--batch-size`` > 1).

    Mirrors the per-step loop's bookkeeping (logging, state.json, history,
    stop rules) but issues ``args.batch_size`` clicks per /slicer/exec call
    and skips per-click screenshots. Returns (history, stop_reason,
    consecutive_small).
    """
    history: list[dict] = []
    consecutive_small = 0
    stop_reason = None
    clicks_done = 0
    batch_idx = 0

    while clicks_done < args.max_steps:
        if not args.no_resource_monitor:
            exit_reason = _check_resource_exit(
                base_url, logger, clicks_done, resource_cfg,
            )
            if exit_reason is not None:
                stop_reason = exit_reason
                break

        this_batch = min(args.batch_size, args.max_steps - clicks_done)
        logger.log(f"=== Batch {batch_idx:02d}  (clicks {clicks_done}.."
                   f"{clicks_done + this_batch - 1}, size {this_batch}) ===")
        logger.event("batch_begin", batch=batch_idx, start_click=clicks_done,
                     size=this_batch)
        t_batch0 = time.time()
        try:
            br = _run_batch_with_recovery(
                base_url, logger, clicks_done, this_batch,
                new_segment_per_click, len(history),
            )
        except RuntimeError as exc:
            logger.log(f"  BATCH unrecoverable: {exc!r}")
            logger.event("batch_failed", batch=batch_idx, error=repr(exc))
            if args.skip_failed_steps:
                logger.log("  (continuing — --skip-failed-steps)")
                batch_idx += 1
                continue
            stop_reason = {"reason": "batch_failed", "batch": batch_idx,
                           "error": repr(exc)}
            return history, stop_reason, consecutive_small

        batch_status = br.get("status")
        results = br.get("results", [])
        batch_wall = round(time.time() - t_batch0, 3)
        per_click_wall = round(batch_wall / max(len(results), 1), 3)
        logger.log(f"  batch returned {len(results)} clicks in {batch_wall}s "
                   f"({per_click_wall}s/click incl. round-trip)"
                   + ("  [recovered]" if br.get("recovered_after_timeout") else "")
                   + ("  [union rebuilt]" if br.get("rebuilt_union") else ""))

        if not results and batch_status == "no_more_candidates":
            logger.log(f"  no more bright candidates outside the mask "
                       f"(voxels={br.get('voxels')})")
            stop_reason = {"reason": "no_more_candidates",
                           "step": clicks_done, "voxels": br.get("voxels")}
            break

        if not results and batch_status not in ("ok", "no_more_candidates"):
            logger.log(f"  BATCH FAILED: {br}")
            logger.event("batch_status_bad", batch=batch_idx, **{
                k: v for k, v in br.items() if k != "results"})
            if args.skip_failed_steps:
                batch_idx += 1
                continue
            stop_reason = {"reason": "batch_failed", "batch": batch_idx,
                           "details": {k: v for k, v in br.items()
                                       if k != "results"}}
            return history, stop_reason, consecutive_small

        stop_now = False
        for r in results:
            step = clicks_done
            step_dir = args.out_dir / f"step_{step:02d}"
            step_dir.mkdir(parents=True, exist_ok=True)
            r["step"] = step
            r["batch"] = batch_idx
            r["batch_per_click_s"] = per_click_wall

            ijk = r.get("picked_ijk")
            intensity = r.get("intensity") or 0.0
            before = r.get("voxels_before", 0)
            after = r.get("voxels_after", 0)
            delta = r.get("delta", 0)
            skipped = r.get("skipped_inside", 0)
            cand_left = r.get("candidates_left", -1)
            click_s = r.get("click_seconds", 0.0)
            seg_id = r.get("segment_id")
            seg_vox = r.get("segment_voxels", 0)
            n_segs = r.get("n_segments_after", "?")
            new_seg = "new-seg" if r.get("made_new_segment") else "same-seg"
            logger.log(f"  [{step:02d}] ijk={ijk} I={intensity:.1f} {new_seg} "
                       f"segs={n_segs} skip={skipped} left={cand_left:,}  "
                       f"vox {before:,}->{after:,} d={delta:+,} "
                       f"seg={seg_vox:,} ({click_s}s gpu)")

            (step_dir / "state.json").write_text(json.dumps(r, indent=2))
            logger.event("step_end", **r)
            history.append(r)
            clicks_done += 1

            if delta >= explosion_threshold:
                logger.log(f"  RUNAWAY: delta={delta:,} >= "
                           f"{explosion_threshold:,}. Stopping.")
                stop_reason = {"reason": "runaway", "step": step,
                               "delta": delta,
                               "explosion_threshold": explosion_threshold}
                stop_now = True
                break
            if delta <= args.min_delta:
                consecutive_small += 1
                if consecutive_small >= args.patience:
                    logger.log(f"  saturated (last {consecutive_small} deltas "
                               f"<= {args.min_delta}). Stopping.")
                    stop_reason = {"reason": "saturated", "step": step,
                                   "consecutive_small": consecutive_small,
                                   "min_delta": args.min_delta,
                                   "patience": args.patience}
                    stop_now = True
                    break
            else:
                consecutive_small = 0

        if stop_now:
            break
        if batch_status == "no_more_candidates":
            logger.log("  candidate list exhausted mid-batch. Stopping.")
            stop_reason = {"reason": "no_more_candidates",
                           "step": clicks_done, "voxels": br.get("voxels")}
            break
        batch_idx += 1

    return history, stop_reason, consecutive_small


def _resource_config() -> dict:
    return {
        "min_available_gb": float(
            os.environ.get("MORPHOCLAW_MIN_AVAILABLE_GB", "1.5")
        ),
        "max_slicer_family_mb": float(
            os.environ.get("MORPHOCLAW_MAX_SLICER_FAMILY_MB", "0")
        ),  # 0 = disabled
        "ping_timeout_s": float(
            os.environ.get("MORPHOCLAW_PING_TIMEOUT", "20")
        ),
        "ping_retries": int(os.environ.get("MORPHOCLAW_PING_RETRIES", "3")),
        "ping_retry_sleep_s": float(
            os.environ.get("MORPHOCLAW_PING_RETRY_SLEEP", "10")
        ),
    }


def _format_resource_line(snap: dict) -> str:
    parts = [
        f"avail={snap.get('sys_available_mem_gb', '?')} GB",
        f"slicer={snap.get('slicer_family_rss_mb', '?')} MB",
    ]
    if snap.get("fastapi_rss_mb") is not None:
        parts.append(f"nnI={snap['fastapi_rss_mb']} MB")
    if snap.get("sys_load_1") is not None:
        parts.append(f"load={snap['sys_load_1']}")
    if snap.get("n_scene_nodes") is not None:
        parts.append(f"nodes={snap['n_scene_nodes']}")
    return "  resources: " + "  ".join(parts)


def _snapshot_resources(base_url: str, logger: RunLogger,
                        step: int | None = None) -> dict:
    try:
        snap = post_python(
            base_url, CAPTURE_REMOTE_RESOURCES_SRC,
            timeout=30, retries=1, retry_sleep=5,
        )
    except RuntimeError as exc:
        snap = {"status": "transport_error", "error": repr(exc)}
    if step is not None:
        snap["step"] = step
    logger.event("resource_snapshot", **snap)
    if snap.get("status") == "ok":
        logger.log(_format_resource_line(snap))
    else:
        logger.log(f"  resources: probe failed ({snap.get('error', snap.get('status'))})")
    return snap


def _ping_slicer(base_url: str, timeout: float) -> bool:
    try:
        post_python(
            base_url, PING_SLICER_SRC,
            timeout=timeout, retries=0,
        )
        return True
    except Exception:
        return False


def _ensure_slicer_responsive(base_url: str, logger: RunLogger,
                              cfg: dict) -> bool:
    """Ping Slicer; retry briefly on transient proxy timeouts."""
    for attempt in range(1, cfg["ping_retries"] + 1):
        if _ping_slicer(base_url, cfg["ping_timeout_s"]):
            if attempt > 1:
                logger.log(f"  slicer responsive (attempt {attempt})")
            return True
        if attempt < cfg["ping_retries"]:
            logger.log(
                f"  slicer ping failed — retry in {cfg['ping_retry_sleep_s']:.0f}s "
                f"({attempt}/{cfg['ping_retries']})…"
            )
            logger.event(
                "slicer_ping_failed",
                attempt=attempt,
                max_attempts=cfg["ping_retries"],
            )
            time.sleep(cfg["ping_retry_sleep_s"])
    return False


def _check_resource_exit(base_url: str, logger: RunLogger, step: int,
                         cfg: dict) -> dict | None:
    """Return a stop_reason when Jetstream is out of headroom; else None."""
    if not _ensure_slicer_responsive(base_url, logger, cfg):
        logger.log("  EXIT: Slicer unresponsive (resource / overload)")
        logger.event("resource_exit", kind="slicer_unresponsive", step=step)
        return {"reason": "resource_exhausted", "kind": "slicer_unresponsive",
                "step": step}

    snap = _snapshot_resources(base_url, logger, step=step)
    if snap.get("status") != "ok":
        logger.log("  EXIT: resource probe failed (Slicer overloaded)")
        logger.event("resource_exit", kind="probe_failed", step=step, **snap)
        return {"reason": "resource_exhausted", "kind": "probe_failed",
                "step": step, "details": snap}

    avail_raw = snap.get("sys_available_mem_gb")
    family_raw = snap.get("slicer_family_rss_mb")
    if avail_raw is not None:
        avail = float(avail_raw)
        if avail < cfg["min_available_gb"]:
            logger.log(
                f"  EXIT: available RAM {avail:.1f} GB "
                f"< {cfg['min_available_gb']} GB"
            )
            logger.event("resource_exit", kind="low_memory", step=step, **snap)
            return {"reason": "resource_exhausted", "kind": "low_memory",
                    "step": step, "available_gb": avail,
                    "threshold_gb": cfg["min_available_gb"], **snap}

    if family_raw is not None and cfg["max_slicer_family_mb"] > 0:
        family = float(family_raw)
        if family > cfg["max_slicer_family_mb"]:
            logger.log(
                f"  EXIT: Slicer RSS {family:.0f} MB "
                f"> {cfg['max_slicer_family_mb']:.0f} MB"
            )
            logger.event("resource_exit", kind="high_slicer_rss", step=step, **snap)
            return {"reason": "resource_exhausted", "kind": "high_slicer_rss",
                    "step": step, "slicer_family_mb": family,
                    "threshold_mb": cfg["max_slicer_family_mb"], **snap}

    return None


# Recipe that returns base64-encoded PNGs of each Slicer widget's *actual*
# rendered output, so segmentation overlays are included. The default
# /slicer/slice endpoint returns the underlying volume only (no
# segmentation overlay), which makes it useless for visual feedback.
GRAB_VIEWS_SRC = """\
import slicer, base64, os, tempfile, traceback
def _grab_widget(w):
    if w is None:
        return None
    pm = w.grab()
    fd, path = tempfile.mkstemp(suffix=".png", prefix="bs_grab_")
    os.close(fd)
    try:
        ok = pm.save(path, "PNG")
        if not ok:
            return None
        with open(path, "rb") as f:
            data = f.read()
        return base64.b64encode(data).decode("ascii")
    finally:
        try:
            os.unlink(path)
        except Exception:
            pass
out = {}
try:
    # Make sure 3D rendering exists for every segmentation that has voxels.
    # Closed-surface reps are needed for the 3D view to actually show
    # something; binary labelmaps alone are 2D-only.
    for sn in slicer.util.getNodesByClass("vtkMRMLSegmentationNode"):
        if "do not touch" in sn.GetName().lower():
            continue
        try:
            sn.CreateClosedSurfaceRepresentation()
        except Exception:
            pass
        d = sn.GetDisplayNode()
        if d:
            d.SetVisibility(True)
            d.SetVisibility2DFill(True)
            d.SetVisibility2DOutline(True)
            d.SetVisibility3D(True)
    lm = slicer.app.layoutManager()
    for color, name in (("Red", "red"), ("Yellow", "yellow"), ("Green", "green")):
        try:
            sw = lm.sliceWidget(color)
            view = sw.sliceView() if sw else None
            if view is not None:
                view.scheduleRender()
                slicer.app.processEvents()
            out[name + "_png_b64"] = _grab_widget(view)
        except Exception as e:
            out[name + "_err"] = repr(e)
    try:
        if lm.threeDViewCount > 0:
            tw = lm.threeDWidget(0)
            tv = tw.threeDView() if tw else None
            if tv is not None:
                tv.resetFocalPoint()
                tv.resetCamera()
                tv.scheduleRender()
                slicer.app.processEvents()
                tv.forceRender()
            out["threeD_png_b64"] = _grab_widget(tv)
    except Exception as e:
        out["threeD_err"] = repr(e)
    out["status"] = "ok"
except Exception as e:
    out["status"] = "exception"
    out["error"] = repr(e)
    out["traceback"] = traceback.format_exc()
__execResult.update(out)
"""


def capture_views(base_url: str, step_dir: Path) -> None:
    """Save red.png / yellow.png / green.png / threeD.png plus a
    ``window.png`` of the full Slicer main window for context."""
    step_dir.mkdir(parents=True, exist_ok=True)
    try:
        r = post_python(base_url, GRAB_VIEWS_SRC, timeout=30)
    except Exception as e:
        (step_dir / "grab.err").write_text(repr(e))
        r = {}
    import base64
    for color in ("red", "yellow", "green", "threeD"):
        b64 = r.get(f"{color}_png_b64")
        if b64:
            try:
                (step_dir / f"{color}.png").write_bytes(base64.b64decode(b64))
            except Exception as e:
                (step_dir / f"{color}.err").write_text(repr(e))
        elif r.get(f"{color}_err"):
            (step_dir / f"{color}.err").write_text(r[f"{color}_err"])
    try:
        (step_dir / "window.png").write_bytes(
            http_get(f"{base_url}/slicer/screenshot", timeout=15)
        )
    except Exception as e:
        (step_dir / "window.err").write_text(repr(e))


# ---------------------------------------------------------------------------
# Server-side recipes (run inside Slicer's Python via /slicer/exec)
# ---------------------------------------------------------------------------

# Set the active volume by name and recenter slice views on it.
SET_ACTIVE_VOLUME_SRC_TEMPLATE = textwrap.dedent("""
    import slicer
    target_name = {target_name!r}
    found = None
    for v in slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode"):
        if v.GetName() == target_name:
            found = v
            break
    if found is None:
        __execResult["status"] = "not_found"
        __execResult["available"] = [
            v.GetName() for v in slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode")
        ]
    else:
        sel = slicer.app.applicationLogic().GetSelectionNode()
        sel.SetActiveVolumeID(found.GetID())
        slicer.app.applicationLogic().PropagateVolumeSelection(0)
        slicer.util.setSliceViewerLayers(background=found, fit=True)
        __execResult["status"] = "ok"
        __execResult["volume_id"] = found.GetID()
        img = found.GetImageData()
        __execResult["dimensions_ijk"] = list(img.GetDimensions())
        __execResult["spacing_mm"] = [round(s, 4) for s in found.GetSpacing()]
""").strip()


# Reset segmentation: clear scene + tell the server to forget interactions.
RESET_SEGMENTATION_SRC = textwrap.dedent("""
    import slicer, io, gzip
    import numpy as np
    import requests
    out = {}
    try:
        mod = slicer.modules.slicernninteractive
        plugin = mod.widgetRepresentation().self()
        vol = plugin.get_volume_node()
        if vol is None:
            __execResult["status"] = "no_active_volume"
        else:
            arr = slicer.util.arrayFromVolume(vol)
            empty = np.zeros(arr.shape, dtype=np.uint8)
            buf = io.BytesIO()
            np.save(buf, empty, allow_pickle=False)
            r = requests.post(
                f"{plugin.server}/upload_segment",
                files={"file": ("seg.npy.gz",
                                io.BytesIO(gzip.compress(buf.getvalue())),
                                "application/octet-stream")},
                timeout=120,
            )
            cleared = []
            for sn in slicer.util.getNodesByClass("vtkMRMLSegmentationNode"):
                if "do not touch" in sn.GetName().lower():
                    continue
                seg = sn.GetSegmentation()
                seg.RemoveAllSegments()
                cleared.append(sn.GetName())
            try:
                plugin.setup_prompts()
            except Exception:
                pass
            out["status"] = "ok"
            out["upload_segment_status"] = r.status_code
            out["cleared_nodes"] = cleared
    except Exception as e:
        out["status"] = "error"
        out["error"] = repr(e)
    __execResult.update(out)
""").strip()


# Build the candidate seed list: voxels above ``intensity_percentile`` of
# the volume's intensity distribution, sorted by intensity descending.
# Stash the result in globals() under ``_BS_STATE`` so subsequent /exec
# calls can read it without re-computing or re-uploading the volume.
INIT_CANDIDATES_SRC_TEMPLATE = textwrap.dedent("""
    import slicer
    import numpy as np
    out = {{}}
    try:
        sel = slicer.app.applicationLogic().GetSelectionNode()
        vol = slicer.mrmlScene.GetNodeByID(sel.GetActiveVolumeID())
        if vol is None:
            __execResult["status"] = "no_active_volume"
        else:
            arr = slicer.util.arrayFromVolume(vol)  # (k, j, i)
            arr_f = arr.astype(np.float32, copy=False)
            t = float(np.percentile(arr_f, {percentile}))
            mask = arr_f >= t
            ks, js, is_ = np.where(mask)
            if len(ks) == 0:
                __execResult["status"] = "no_bright_voxels"
            else:
                intensities = arr_f[ks, js, is_]
                order = np.argsort(-intensities)  # descending
                cap = int({max_candidates})
                if cap > 0 and len(order) > cap:
                    order = order[:cap]
                # Encode candidates compactly: parallel arrays of int.
                cand_kji = np.stack([ks[order], js[order], is_[order]], axis=1).astype(np.int32)
                cand_int = intensities[order].astype(np.float32)
                globals()["_BS_STATE"] = {{
                    "volume_id": vol.GetID(),
                    "volume_name": vol.GetName(),
                    "shape_kji": list(arr.shape),
                    "threshold": t,
                    "percentile": float({percentile}),
                    "next_idx": 0,
                    "history": [],
                    "candidates_kji": cand_kji,
                    "candidates_intensity": cand_int,
                }}
                out["status"] = "ok"
                out["volume_name"] = vol.GetName()
                out["shape_kji"] = list(arr.shape)
                out["threshold"] = t
                out["n_candidates"] = int(len(cand_int))
                out["intensity_min"] = float(cand_int.min())
                out["intensity_max"] = float(cand_int.max())
                out["scalar_type"] = str(arr.dtype)
    except Exception as e:
        out["status"] = "error"
        out["error"] = repr(e)
    __execResult.update(out)
""").strip()


# Step recipe: skim past candidates already inside the mask, click the
# next one, return mask deltas + intensity. ``click_positive`` lets the
# caller occasionally click negative if needed (e.g. cleanup).
#
# Each click goes into its OWN segment (so structures stay separable):
#   - For the first step, we use whatever segment is currently selected
#     (after --reset-first there's an empty default segment, or the
#     plugin auto-creates one when point_prompt fires).
#   - For every subsequent step, we call ``plugin.make_new_segment()``
#     BEFORE the click so nnInteractive paints into a fresh segment
#     instead of refining the previous one.
#
# IMPORTANT: this string is written flush-left (no leading whitespace
# anywhere) so the eventual /slicer/exec call sees valid top-level
# Python. Don't reindent.
STEP_SRC_TEMPLATE = """\
import slicer, time, traceback
import numpy as np
def _bs_read_total_mask(shape):
    total = np.zeros(shape, dtype=bool)
    per_seg = []
    for sn in slicer.util.getNodesByClass("vtkMRMLSegmentationNode"):
        if "do not touch" in sn.GetName().lower():
            continue
        seg = sn.GetSegmentation()
        for ii in range(seg.GetNumberOfSegments()):
            sid = seg.GetNthSegmentID(ii)
            try:
                a = slicer.util.arrayFromSegmentBinaryLabelmap(sn, sid)
            except Exception:
                a = None
            if a is None or a.shape != shape:
                continue
            ab = a > 0
            total |= ab
            per_seg.append({{
                "node": sn.GetName(), "sid": sid,
                "voxels": int(ab.sum()),
            }})
    return total, per_seg
try:
    state = globals().get("_BS_STATE")
    if state is None:
        __execResult["status"] = "not_initialized"
    else:
        shape = tuple(state["shape_kji"])
        cand_kji = state["candidates_kji"]
        cand_int = state["candidates_intensity"]
        idx = int(state["next_idx"])
        mask_before, segs_before = _bs_read_total_mask(shape)
        voxels_before = int(mask_before.sum())
        picked = None
        skipped_inside = 0
        while idx < len(cand_int):
            k = int(cand_kji[idx, 0])
            j = int(cand_kji[idx, 1])
            i = int(cand_kji[idx, 2])
            if mask_before[k, j, i]:
                idx += 1
                skipped_inside += 1
                continue
            picked = (k, j, i, float(cand_int[idx]))
            idx += 1
            break
        if picked is None:
            state["next_idx"] = idx
            __execResult["status"] = "no_more_candidates"
            __execResult["candidates_left"] = 0
            __execResult["voxels"] = voxels_before
            __execResult["skipped_inside"] = skipped_inside
        else:
            k, j, i, intensity = picked
            click_positive = bool({click_positive})
            # Honor --new-segment-per-click except for the very first click
            # of a *fresh* scene (no existing segments → let the plugin
            # auto-create the first one). On continuation runs there will
            # already be segments in the scene, so always make a new one
            # to avoid silently refining the previously-active segment.
            n_existing_segments = len(segs_before)
            new_segment = bool({new_segment}) and (
                len(state["history"]) > 0 or n_existing_segments > 0
            )
            mod = slicer.modules.slicernninteractive
            plugin = mod.widgetRepresentation().self()
            new_segment_id = None
            if new_segment:
                plugin.make_new_segment()
                try:
                    new_segment_id = plugin.get_current_segment_id()
                except Exception:
                    new_segment_id = None
            if click_positive:
                plugin.on_prompt_type_positive_clicked()
            else:
                plugin.on_prompt_type_negative_clicked()
            t0 = time.time()
            plugin.point_prompt(xyz=[i, j, k], positive_click=click_positive)
            t1 = time.time()
            mask_after, segs_after = _bs_read_total_mask(shape)
            voxels_after = int(mask_after.sum())
            delta = voxels_after - voxels_before
            new_voxels_at_picked = int(mask_after[k, j, i])
            current_segment_id = None
            try:
                current_segment_id = plugin.get_current_segment_id()
            except Exception:
                pass
            current_segment_voxels = 0
            for s in segs_after:
                if s["sid"] == current_segment_id:
                    current_segment_voxels = s["voxels"]
                    break
            state["next_idx"] = idx
            state["history"].append({{
                "ijk": [i, j, k], "intensity": intensity,
                "voxels_before": voxels_before, "voxels_after": voxels_after,
                "delta": delta, "click_positive": click_positive,
                "skipped_inside": skipped_inside,
                "made_new_segment": new_segment,
                "segment_id": current_segment_id,
                "segment_voxels": current_segment_voxels,
                "n_segments_before": len(segs_before),
                "n_segments_after": len(segs_after),
            }})
            __execResult["status"] = "ok"
            __execResult["picked_ijk"] = [i, j, k]
            __execResult["intensity"] = intensity
            __execResult["voxels_before"] = voxels_before
            __execResult["voxels_after"] = voxels_after
            __execResult["delta"] = delta
            __execResult["new_voxels_at_picked"] = new_voxels_at_picked
            __execResult["click_positive"] = click_positive
            __execResult["skipped_inside"] = skipped_inside
            __execResult["candidates_left"] = int(len(cand_int) - idx)
            __execResult["click_seconds"] = round(t1 - t0, 3)
            __execResult["made_new_segment"] = new_segment
            __execResult["segment_id"] = current_segment_id
            __execResult["segment_voxels"] = current_segment_voxels
            __execResult["n_segments_before"] = len(segs_before)
            __execResult["n_segments_after"] = len(segs_after)
except Exception as e:
    __execResult["status"] = "exception"
    __execResult["error"] = repr(e)
    __execResult["traceback"] = traceback.format_exc()
"""


# Batched step recipe: issue up to {batch_size} bright-seed clicks inside a
# SINGLE /slicer/exec call. This is the big throughput lever — it amortises
# the exec/HTTP round-trip across M clicks instead of paying it per click.
#
# It also fixes the growing per-step cost: instead of rebuilding the union
# mask from EVERY segment twice per click (O(n_segments x volume) and getting
# slower as segments accumulate), we keep the union mask in ``_BS_STATE`` and
# update it INCREMENTALLY — after each click we read only the freshly created
# segment's labelmap and OR it into the cached union.
#
# Returns ``__execResult["results"]`` = list of per-click dicts whose keys
# match the single-click recipe, so the Mac-side loop can treat each entry
# exactly like a normal step.
#
# Written flush-left (top-level Python for /slicer/exec). Don't reindent.
STEP_BATCH_SRC_TEMPLATE = """\
import slicer, time, traceback
import numpy as np
def _bs_iter_segnodes():
    for sn in slicer.util.getNodesByClass("vtkMRMLSegmentationNode"):
        if "do not touch" in sn.GetName().lower():
            continue
        yield sn
def _bs_build_union(shape):
    total = np.zeros(shape, dtype=bool)
    n_seg = 0
    for sn in _bs_iter_segnodes():
        seg = sn.GetSegmentation()
        for ii in range(seg.GetNumberOfSegments()):
            sid = seg.GetNthSegmentID(ii)
            try:
                a = slicer.util.arrayFromSegmentBinaryLabelmap(sn, sid)
            except Exception:
                a = None
            if a is None or a.shape != shape:
                continue
            total |= (a > 0)
            n_seg += 1
    return total, n_seg
def _bs_node_for_sid(sid):
    for sn in _bs_iter_segnodes():
        if sn.GetSegmentation().GetSegment(sid) is not None:
            return sn
    return None
try:
    state = globals().get("_BS_STATE")
    if state is None:
        __execResult["status"] = "not_initialized"
    else:
        shape = tuple(state["shape_kji"])
        cand_kji = state["candidates_kji"]
        cand_int = state["candidates_intensity"]
        idx = int(state["next_idx"])
        union = state.get("union_mask")
        if union is None or getattr(union, "shape", None) != shape:
            union, n_existing = _bs_build_union(shape)
        else:
            n_existing = int(state.get("n_segments", 0))
        union_count = int(union.sum())
        B = int({batch_size})
        click_positive = bool({click_positive})
        make_new = bool({new_segment})
        mod = slicer.modules.slicernninteractive
        plugin = mod.widgetRepresentation().self()
        results = []
        total_skipped = 0
        made_any = False
        rebuilt = False
        for _b in range(B):
            picked = None
            skipped_inside = 0
            while idx < len(cand_int):
                k = int(cand_kji[idx, 0]); j = int(cand_kji[idx, 1]); i = int(cand_kji[idx, 2])
                if union[k, j, i]:
                    idx += 1; skipped_inside += 1; total_skipped += 1; continue
                picked = (k, j, i, float(cand_int[idx])); idx += 1; break
            if picked is None:
                break
            k, j, i, intensity = picked
            voxels_before = union_count
            new_segment = make_new and (
                len(state["history"]) > 0 or union_count > 0
                or n_existing > 0 or made_any
            )
            if new_segment:
                plugin.make_new_segment()
            if click_positive:
                plugin.on_prompt_type_positive_clicked()
            else:
                plugin.on_prompt_type_negative_clicked()
            t0 = time.time()
            plugin.point_prompt(xyz=[i, j, k], positive_click=click_positive)
            t1 = time.time()
            made_any = True
            sid = None
            try:
                sid = plugin.get_current_segment_id()
            except Exception:
                sid = None
            seg_vox = 0
            added = 0
            ab = None
            if sid is not None:
                node = _bs_node_for_sid(sid)
                if node is not None:
                    try:
                        a = slicer.util.arrayFromSegmentBinaryLabelmap(node, sid)
                    except Exception:
                        a = None
                    if a is not None and a.shape == shape:
                        ab = (a > 0)
                        seg_vox = int(ab.sum())
            if ab is not None:
                added = int(np.count_nonzero(ab & ~union))
                union |= ab
            else:
                # Fallback: couldn't read just the new segment — rebuild the
                # full union (slow path) so the running count stays correct.
                new_union, n_existing = _bs_build_union(shape)
                added = int(new_union.sum()) - union_count
                union = new_union
                rebuilt = True
            union_count = voxels_before + added
            delta = added
            state["history"].append({{
                "ijk": [i, j, k], "intensity": intensity,
                "voxels_before": voxels_before, "voxels_after": union_count,
                "delta": delta, "click_positive": click_positive,
                "skipped_inside": skipped_inside,
                "made_new_segment": new_segment,
                "segment_id": sid, "segment_voxels": seg_vox,
            }})
            results.append({{
                "status": "ok",
                "picked_ijk": [i, j, k], "intensity": intensity,
                "voxels_before": voxels_before, "voxels_after": union_count,
                "delta": delta, "new_voxels_at_picked": int(bool(ab is not None)),
                "click_positive": click_positive, "skipped_inside": skipped_inside,
                "candidates_left": int(len(cand_int) - idx),
                "click_seconds": round(t1 - t0, 3),
                "made_new_segment": new_segment,
                "segment_id": sid, "segment_voxels": seg_vox,
                "n_segments_after": len(state["history"]),
            }})
        state["next_idx"] = idx
        state["union_mask"] = union
        state["n_segments"] = int(n_existing) + len([r for r in results if r["made_new_segment"]])
        __execResult["status"] = "ok" if results else "no_more_candidates"
        __execResult["results"] = results
        __execResult["voxels"] = union_count
        __execResult["candidates_left"] = int(len(cand_int) - idx)
        __execResult["skipped_inside"] = total_skipped
        __execResult["batch_clicks"] = len(results)
        __execResult["rebuilt_union"] = rebuilt
except Exception as e:
    __execResult["status"] = "exception"
    __execResult["error"] = repr(e)
    __execResult["traceback"] = traceback.format_exc()
"""


# One-time configuration to make the segmentation actually visible in
# slice views and the 3D view. Without this, the screenshots show only
# the underlying CT and an empty 3D box.
ENABLE_VISIBILITY_SRC = """\
import slicer, traceback
try:
    nodes = slicer.util.getNodesByClass("vtkMRMLSegmentationNode")
    for sn in nodes:
        if "do not touch" in sn.GetName().lower():
            continue
        sn.CreateDefaultDisplayNodes()
        sn.CreateClosedSurfaceRepresentation()
        d = sn.GetDisplayNode()
        if d:
            d.SetVisibility(True)
            d.SetVisibility2DFill(True)
            d.SetVisibility2DOutline(True)
            d.SetVisibility3D(True)
            d.SetOpacity(0.6)
            d.SetOpacity2DFill(0.5)
            d.SetOpacity2DOutline(1.0)
    lm = slicer.app.layoutManager()
    for vi in range(lm.threeDViewCount):
        v = lm.threeDWidget(vi).threeDView()
        v.resetFocalPoint()
        v.resetCamera()
    __execResult["nodes"] = [n.GetName() for n in nodes]
    __execResult["status"] = "ok"
except Exception as e:
    __execResult["status"] = "exception"
    __execResult["error"] = repr(e)
    __execResult["traceback"] = traceback.format_exc()
"""


# Headless: keep the segmentation display OFF during the click loop. The 3D
# closed-surface representation is what gets expensive once 100+ segments
# accumulate (Slicer re-renders every surface at each batch boundary), so we
# never create surfaces and never turn on 2D/3D visibility while clicking.
HEADLESS_HIDE_SRC = """\
import slicer, traceback
try:
    nodes = slicer.util.getNodesByClass("vtkMRMLSegmentationNode")
    for sn in nodes:
        if "do not touch" in sn.GetName().lower():
            continue
        sn.CreateDefaultDisplayNodes()
        d = sn.GetDisplayNode()
        if d:
            d.SetVisibility(False)
            d.SetVisibility2DFill(False)
            d.SetVisibility2DOutline(False)
            d.SetVisibility3D(False)
    __execResult["nodes"] = [n.GetName() for n in nodes]
    __execResult["status"] = "ok"
except Exception as e:
    __execResult["status"] = "exception"
    __execResult["error"] = repr(e)
    __execResult["traceback"] = traceback.format_exc()
"""


# View the completed result as a SINGLE combined surface. After a headless
# run we don't want to pay for rendering hundreds of per-click segments, so
# we collapse the final union mask into one binary segment + one closed
# surface and show only that. Uses the cached _BS_STATE["union_mask"] when
# available, else rebuilds the union from the in-scene segments.
VIEW_COMPLETED_SRC = """\
import slicer, vtk, traceback
import numpy as np
try:
    sel = slicer.app.applicationLogic().GetSelectionNode()
    vol = slicer.mrmlScene.GetNodeByID(sel.GetActiveVolumeID())
    if vol is None:
        __execResult["status"] = "no_active_volume"
    else:
        arr = slicer.util.arrayFromVolume(vol)
        shape = arr.shape
        st = globals().get("_BS_STATE") or {}
        union = st.get("union_mask")
        if union is None or getattr(union, "shape", None) != shape:
            union = np.zeros(shape, dtype=bool)
            for sn in slicer.util.getNodesByClass("vtkMRMLSegmentationNode"):
                if "do not touch" in sn.GetName().lower():
                    continue
                seg = sn.GetSegmentation()
                for ii in range(seg.GetNumberOfSegments()):
                    sid = seg.GetNthSegmentID(ii)
                    try:
                        a = slicer.util.arrayFromSegmentBinaryLabelmap(sn, sid)
                    except Exception:
                        a = None
                    if a is not None and a.shape == shape:
                        union |= (a > 0)
        m = vtk.vtkMatrix4x4()
        vol.GetIJKToRASMatrix(m)
        # Hide the noisy per-click segmentation node(s) so only the clean
        # combined mask is rendered.
        for sn in slicer.util.getNodesByClass("vtkMRMLSegmentationNode"):
            if "do not touch" in sn.GetName().lower():
                continue
            d = sn.GetDisplayNode()
            if d:
                d.SetVisibility(False)
        lm = slicer.util.addVolumeFromArray(
            union.astype("uint8"), ijkToRAS=m, name="completed_mask_lm",
            nodeClassName="vtkMRMLLabelMapVolumeNode",
        )
        seg = slicer.mrmlScene.AddNewNodeByClass(
            "vtkMRMLSegmentationNode", "completed_mask")
        seg.SetReferenceImageGeometryParameterFromVolumeNode(vol)
        slicer.modules.segmentations.logic().ImportLabelmapToSegmentationNode(lm, seg)
        slicer.mrmlScene.RemoveNode(lm)
        seg.CreateClosedSurfaceRepresentation()
        d = seg.GetDisplayNode()
        if d:
            d.SetVisibility(True)
            d.SetVisibility3D(True)
            d.SetVisibility2DFill(True)
            d.SetVisibility2DOutline(True)
            d.SetOpacity(0.7)
        lmgr = slicer.app.layoutManager()
        for vi in range(lmgr.threeDViewCount):
            v = lmgr.threeDWidget(vi).threeDView()
            v.resetFocalPoint()
            v.resetCamera()
        __execResult["status"] = "ok"
        __execResult["voxels"] = int(union.sum())
except Exception as e:
    __execResult["status"] = "exception"
    __execResult["error"] = repr(e)
    __execResult["traceback"] = traceback.format_exc()
"""


# Recenter slice views on a picked voxel so the next screenshot shows
# the action.
RECENTER_SRC_TEMPLATE = textwrap.dedent("""
    import slicer, vtk
    i, j, k = {i}, {j}, {k}
    sel = slicer.app.applicationLogic().GetSelectionNode()
    vol = slicer.mrmlScene.GetNodeByID(sel.GetActiveVolumeID())
    if vol:
        m = vtk.vtkMatrix4x4()
        vol.GetIJKToRASMatrix(m)
        ras4 = [0.0]*4
        m.MultiplyPoint([float(i), float(j), float(k), 1.0], ras4)
        ras = ras4[:3]
        for color in ("Red", "Yellow", "Green"):
            sw = slicer.app.layoutManager().sliceWidget(color)
            if sw:
                sw.sliceLogic().GetSliceNode().JumpSliceByCentering(*ras)
        __execResult["ras"] = [round(r, 2) for r in ras]
""").strip()


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--volume",
                   help="Name of the vtkMRMLScalarVolumeNode to segment "
                        "(default: keep whatever is currently active)")
    p.add_argument("--reset-first", action="store_true",
                   help="reset any existing segmentation on the active volume "
                        "before starting")
    p.add_argument("--intensity-percentile", type=float, default=99.0,
                   help="threshold percentile for bright voxels "
                        "(default 99 — top 1%% intensities)")
    p.add_argument("--max-candidates", type=int, default=200_000,
                   help="hard cap on the number of bright voxels to track "
                        "(default 200k; keeps the state small)")
    p.add_argument("--max-steps", type=int, default=20,
                   help="maximum number of clicks to issue (default 20)")
    p.add_argument("--min-delta", type=int, default=50,
                   help="if the last --patience deltas are all below this "
                        "many voxels, stop early (default 50 voxels)")
    p.add_argument("--patience", type=int, default=3,
                   help="number of consecutive small-delta steps before "
                        "early stopping (default 3)")
    p.add_argument("--max-explosion-frac", type=float, default=0.5,
                   help="if a single click adds more than this fraction "
                        "of the total volume voxels to the mask, treat it "
                        "as runaway and stop (default 0.5)")
    p.add_argument("--no-stop-rules", action="store_true",
                   help="disable ALL early-stopping heuristics: always "
                        "issue --max-steps clicks unless the bright "
                        "candidate list is exhausted. Implemented as "
                        "min_delta=0, patience=10**9, "
                        "max_explosion_frac=1.0 — recorded that way in "
                        "manifest.json so the run is reproducible.")
    p.add_argument("--no-new-segment-per-click", action="store_true",
                   help="put every click into the SAME segment (default: "
                        "each click after the first creates a fresh segment "
                        "via plugin.make_new_segment(), so structures "
                        "stay separable)")
    p.add_argument("--no-screenshots", action="store_true",
                   help="skip per-step view captures (faster)")
    p.add_argument("--batch-size", type=int, default=1,
                   help="number of clicks to issue per /slicer/exec call "
                        "(default 1). Values >1 enable server-side click "
                        "batching: M point_prompts run inside a single exec "
                        "and the union mask is maintained INCREMENTALLY "
                        "(only the new segment is read each click instead of "
                        "re-scanning every segment). Amortises HTTP/exec "
                        "overhead and removes the growing per-step mask "
                        "rebuild. Per-click screenshots are skipped in batch "
                        "mode.")
    p.add_argument("--fast", action="store_true",
                   help="convenience preset for maximum throughput: implies "
                        "--batch-size 8 (unless --batch-size given) and "
                        "--no-screenshots.")
    p.add_argument("--headless", action="store_true",
                   help="keep the segmentation display OFF during the click "
                        "loop (no 2D/3D visibility, no closed surfaces). "
                        "Avoids Slicer re-rendering hundreds of per-click "
                        "segments at every batch boundary — render cost no "
                        "longer grows with segment count. Implies "
                        "--no-screenshots. At the end, the completed mask is "
                        "collapsed into ONE combined surface and shown (unless "
                        "--no-view-result).")
    p.add_argument("--no-view-result", action="store_true",
                   help="in --headless mode, skip building/showing the final "
                        "combined surface (leave the scene non-rendered)")
    p.add_argument("--no-export-segmentation", action="store_true",
                   help="skip exporting per-segment + composite NIfTI "
                        "labelmaps as run artifacts (saves bandwidth, but "
                        "results then aren't independently reproducible)")
    p.add_argument("--skip-remote-env", action="store_true",
                   help="skip heavy remote env capture (FastAPI probes, "
                        "model hashes); use lightweight resource snapshot "
                        "instead — recommended for long continuation runs")
    p.add_argument("--skip-failed-steps", action="store_true",
                   help="on transport/remote step errors, log and continue "
                        "instead of aborting the run")
    p.add_argument("--skip-volume-hash", action="store_true",
                   help="skip hashing the active volume at startup (faster "
                        "when continuing an existing scene)")
    p.add_argument("--no-resource-monitor", action="store_true",
                   help="disable per-step Slicer ping + memory exit checks")
    p.add_argument("--min-available-gb", type=float, default=None,
                   help="exit when Jetstream available RAM drops below this "
                        "(default: MORPHOCLAW_MIN_AVAILABLE_GB or 1.5)")
    p.add_argument("--label", type=str, default=None,
                   help="optional label embedded in the run id "
                        "(e.g. 'mouse_skull')")
    p.add_argument("--out-dir", type=Path,
                   default=Path("runs") / time.strftime("bright_%Y%m%d_%H%M%S"))
    args = p.parse_args(argv)

    if args.fast:
        if args.batch_size <= 1:
            args.batch_size = 8
        args.no_screenshots = True

    if args.headless:
        args.no_screenshots = True

    if args.no_stop_rules:
        args.min_delta = 0
        args.patience = 10 ** 9
        args.max_explosion_frac = 1.0

    base_url = _read_url()
    resource_cfg = _resource_config()
    if args.min_available_gb is not None:
        resource_cfg["min_available_gb"] = args.min_available_gb
    args.out_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Provenance: open the run logger first, so anything that crashes
    # below this point leaves a partial-but-readable record.
    # ------------------------------------------------------------------
    logger = RunLogger.start(
        root=args.out_dir,
        args={k: (str(v) if isinstance(v, Path) else v)
              for k, v in vars(args).items()},
        label=args.label,
    )
    logger.log("=== Slicer remote bright-spot greedy segmentation ===")
    logger.log(f"server       : {base_url}")
    logger.log(f"run id       : {logger.run_id}")
    logger.log(f"out          : {args.out_dir}")
    logger.log(f"percentile   : {args.intensity_percentile}")
    logger.log(f"max steps    : {args.max_steps}")
    logger.log(f"min delta    : {args.min_delta}  (patience={args.patience})")
    if not args.no_resource_monitor:
        logger.log(
            f"resources    : exit if avail<{resource_cfg['min_available_gb']} GB  "
            f"ping_timeout={resource_cfg['ping_timeout_s']}s"
        )
    logger.log("")

    # Local environment (this Mac, git commit, package versions, env vars)
    local_env = logger.record_local_env()
    logger.log(f"git commit   : {local_env.get('git_commit')}  "
               f"dirty={local_env.get('git_dirty')}")
    if local_env.get("git_dirty"):
        logger.log("WARNING: working tree is dirty; the recorded git_commit "
                   "may not match the script that's running.")

    # Remote environment — full capture is slow and can wedge an overloaded
    # Slicer; continuation runs should pass --skip-remote-env.
    if args.skip_remote_env:
        logger.log("-> Lightweight remote resource snapshot (skip heavy env)…")
        try:
            snap = _snapshot_resources(base_url, logger)
            logger.record_remote_env({"lightweight": True, **snap})
        except Exception as e:
            logger.log(f"   resource snapshot failed: {e!r}")
            logger.event("resource_snapshot_failed", error=repr(e))
    else:
        logger.log("-> Capturing remote environment (Slicer/plugin/torch)…")
        try:
            remote_env = post_python(
                base_url, CAPTURE_REMOTE_ENV_SRC,
                timeout=60, retries=1, retry_sleep=10,
            )
            logger.record_remote_env(remote_env)
            logger.log(f"   slicer       : {remote_env.get('slicer_version')}")
            logger.log(f"   torch        : {remote_env.get('torch_version')}  "
                       f"cuda={remote_env.get('torch_cuda_available')}  "
                       f"mps={remote_env.get('torch_mps_available')}")
            logger.log(f"   nnInteractive: {remote_env.get('nninteractive_version')}")
            if "slicernninteractive_git_commit" in remote_env:
                logger.log(f"   plugin commit: {remote_env['slicernninteractive_git_commit']}")
            if "nninteractive_model_total_bytes" in remote_env:
                logger.log(f"   model bytes  : {remote_env['nninteractive_model_total_bytes']:,}")
            logger.log(_format_resource_line(remote_env))
        except Exception as e:
            logger.log(f"   remote env capture failed: {e!r}")
            logger.event("remote_env_failed", error=repr(e))
            try:
                snap = _snapshot_resources(base_url, logger)
                logger.log("   (fallback resource snapshot succeeded)")
            except Exception as e2:
                logger.log(f"   fallback resource snapshot also failed: {e2!r}")

    # ------------------------------------------------------------------
    # Volume selection + reset + visibility
    # ------------------------------------------------------------------
    if args.volume:
        logger.log(f"-> Setting active volume to {args.volume!r}")
        r = post_python(base_url,
                        SET_ACTIVE_VOLUME_SRC_TEMPLATE.format(target_name=args.volume),
                        timeout=20)
        if r.get("status") != "ok":
            logger.log(f"   FAILED: {r}")
            logger.event("volume_set_failed", **r)
            logger.finalize(stop_reason={"reason": "volume_not_found", "details": r})
            return 2
        logger.event("volume_set", **r)
        logger.log(f"   id={r['volume_id']}  dims={r.get('dimensions_ijk')}  "
                   f"spacing={r.get('spacing_mm')}")

    # Hash the input volume — the single most important provenance step
    vol_meta: dict = {}
    if args.skip_volume_hash:
        logger.log("-> Skipping volume hash (--skip-volume-hash)")
        logger.event("volume_hash_skipped")
    else:
        logger.log("-> Hashing active volume…")
        vol_meta = post_python(
            base_url, HASH_ACTIVE_VOLUME_SRC, timeout=120, retries=2, retry_sleep=10,
        )
        if vol_meta.get("status") != "ok":
            logger.log(f"   FAILED: {vol_meta}")
            logger.event("volume_hash_failed", **vol_meta)
            logger.finalize(stop_reason={"reason": "volume_hash_failed",
                                         "details": vol_meta})
            return 2
        logger.record_inputs(vol_meta)
        logger.log(f"   sha256(voxels) = {vol_meta['sha256_voxels'][:16]}…  "
                   f"shape={vol_meta['shape_kji']}  dtype={vol_meta['dtype']}")

    if args.reset_first:
        logger.log("-> Resetting segmentation (server + scene)…")
        r = post_python(base_url, RESET_SEGMENTATION_SRC, timeout=120)
        logger.log(f"   {r.get('status')}  cleared={r.get('cleared_nodes')}")
        logger.event("reset", **r)

    if args.headless:
        logger.log("-> HEADLESS: disabling segmentation display during loop…")
        r = post_python(base_url, HEADLESS_HIDE_SRC, timeout=30)
        logger.log(f"   {r.get('status')}  nodes={r.get('nodes')}")
        logger.event("visibility_disabled_headless", **r)
    else:
        logger.log("-> Enabling segmentation visibility (2D + 3D)…")
        r = post_python(base_url, ENABLE_VISIBILITY_SRC, timeout=30)
        logger.log(f"   {r.get('status')}  nodes={r.get('nodes')}")
        logger.event("visibility_enabled", **r)

    logger.log("-> Building bright-pixel candidate list…")
    init = post_python(
        base_url,
        INIT_CANDIDATES_SRC_TEMPLATE.format(
            percentile=args.intensity_percentile,
            max_candidates=args.max_candidates,
        ),
        timeout=60,
    )
    if init.get("status") != "ok":
        logger.log(f"   FAILED: {init}")
        logger.event("candidates_failed", **init)
        logger.finalize(stop_reason={"reason": "candidates_failed",
                                     "details": init})
        return 3
    total_voxels = 1
    for d in init.get("shape_kji", [1, 1, 1]):
        total_voxels *= int(d)
    logger.log(f"   volume       : {init['volume_name']}  shape(k,j,i)={init['shape_kji']}")
    logger.log(f"   threshold    : {init['threshold']:.2f}  ({init['scalar_type']})")
    logger.log(f"   candidates   : {init['n_candidates']:,}  "
               f"intensities=[{init['intensity_min']:.1f}, {init['intensity_max']:.1f}]")
    logger.log(f"   total voxels : {total_voxels:,}")
    logger.event("candidates_built", total_voxels=total_voxels, **init)

    # ------------------------------------------------------------------
    # Greedy click loop
    # ------------------------------------------------------------------
    history: list[dict] = []
    consecutive_small = 0
    explosion_threshold = int(args.max_explosion_frac * total_voxels)
    new_segment_per_click = not args.no_new_segment_per_click
    stop_reason = None

    use_batching = bool(args.batch_size and args.batch_size > 1)
    if use_batching:
        logger.log(f"-> Server-side click batching ENABLED "
                   f"(batch_size={args.batch_size}, per-click screenshots off)")
        history, stop_reason, consecutive_small = _run_batched_clicks(
            base_url, logger, args, total_voxels,
            explosion_threshold, new_segment_per_click, resource_cfg,
        )

    for step in range(args.max_steps):
        if use_batching:
            break
        step_dir = args.out_dir / f"step_{step:02d}"
        step_dir.mkdir(parents=True, exist_ok=True)

        logger.log(f"--- Step {step:02d} -----------------------------------------")
        logger.event("step_begin", step=step)

        if not args.no_resource_monitor:
            exit_reason = _check_resource_exit(
                base_url, logger, step, resource_cfg,
            )
            if exit_reason is not None:
                stop_reason = exit_reason
                break

        voxels_before_hint = int(history[-1]["voxels_after"]) if history else 0
        t_step0 = time.time()
        try:
            r = _run_step_with_recovery(
                base_url, logger, step, voxels_before_hint, new_segment_per_click,
            )
        except RuntimeError as exc:
            logger.log(f"  STEP unrecoverable: {exc!r}")
            logger.event("step_failed", step=step, error=repr(exc))
            if args.skip_failed_steps:
                logger.log("  (continuing — --skip-failed-steps)")
                continue
            stop_reason = {"reason": "step_failed", "step": step,
                           "error": repr(exc)}
            logger.finalize(stop_reason=stop_reason,
                            summary={"steps": len(history), "history": history})
            return 4
        r["step"] = step
        r["step_wallclock_s"] = round(time.time() - t_step0, 3)

        if r.get("status") == "no_more_candidates":
            logger.log(f"  no more bright candidates outside the mask "
                       f"(voxels={r.get('voxels')})")
            (step_dir / "state.json").write_text(json.dumps(r, indent=2))
            logger.event("step_end", **r)
            stop_reason = {"reason": "no_more_candidates",
                            "step": step,
                            "voxels": r.get("voxels")}
            break

        if r.get("status") != "ok":
            logger.log(f"  STEP FAILED: {r}")
            (step_dir / "state.json").write_text(json.dumps(r, indent=2))
            logger.event("step_failed", **r)
            if args.skip_failed_steps:
                logger.log("  (continuing — --skip-failed-steps)")
                history.append(r)
                continue
            stop_reason = {"reason": "step_failed", "step": step, "details": r}
            logger.finalize(stop_reason=stop_reason,
                            summary={"steps": len(history), "history": history})
            return 4

        ijk = r["picked_ijk"]
        intensity = r["intensity"]
        before = r["voxels_before"]
        after = r["voxels_after"]
        delta = r["delta"]
        skipped = r.get("skipped_inside", 0)
        cand_left = r.get("candidates_left", -1)
        click_s = r.get("click_seconds", 0.0)
        seg_id = r.get("segment_id")
        seg_vox = r.get("segment_voxels", 0)
        n_segs = r.get("n_segments_after", "?")
        new_seg = "new-seg" if r.get("made_new_segment") else "same-seg"
        if r.get("recovered_after_timeout"):
            logger.log(f"  recovered after proxy timeout: voxels {before:,} -> {after:,} "
                       f"(+{delta:,})  segments={n_segs}")
        else:
            logger.log(f"  picked ijk={ijk}  intensity={intensity:.1f}  {new_seg}  "
                       f"segments={n_segs}  skipped_inside={skipped}  candidates_left={cand_left:,}")
            logger.log(f"  voxels {before:>10,} -> {after:>10,}  delta={delta:+,}  "
                       f"this segment: {seg_id} = {seg_vox:,} vox  ({click_s}s remote, "
                       f"{r['step_wallclock_s']}s round-trip)")

        if not args.no_screenshots:
            try:
                post_python(
                    base_url,
                    RECENTER_SRC_TEMPLATE.format(i=ijk[0], j=ijk[1], k=ijk[2]),
                    timeout=15,
                )
            except Exception as e:
                logger.log(f"  recenter warning: {e}")
                logger.event("recenter_warning", step=step, error=repr(e))
            try:
                capture_views(base_url, step_dir)
            except Exception as e:
                logger.log(f"  screenshot warning: {e}")
                logger.event("screenshot_warning", step=step, error=repr(e))

        (step_dir / "state.json").write_text(json.dumps(r, indent=2))
        logger.event("step_end", **r)
        history.append(r)

        if delta >= explosion_threshold:
            logger.log(f"  RUNAWAY: delta={delta:,} >= {explosion_threshold:,} "
                       f"({100*args.max_explosion_frac:.0f}% of total volume). Stopping.")
            stop_reason = {"reason": "runaway", "step": step,
                           "delta": delta,
                           "explosion_threshold": explosion_threshold}
            break
        if delta <= args.min_delta:
            consecutive_small += 1
            if consecutive_small >= args.patience:
                logger.log(f"  saturated (last {consecutive_small} deltas "
                           f"<= {args.min_delta}). Stopping.")
                stop_reason = {"reason": "saturated", "step": step,
                               "consecutive_small": consecutive_small,
                               "min_delta": args.min_delta,
                               "patience": args.patience}
                break
        else:
            consecutive_small = 0

    if stop_reason is None:
        stop_reason = {"reason": "max_steps", "max_steps": args.max_steps}

    # ------------------------------------------------------------------
    # Export final segmentation as artifacts (NIfTI + checksums) so the
    # paper has hashable, redistributable outputs.
    # ------------------------------------------------------------------
    if not args.no_export_segmentation:
        logger.log("-> Exporting final segmentation as NIfTI artifacts…")
        try:
            export = post_python(base_url, EXPORT_SEGMENTATION_SRC, timeout=300)
            if export.get("status") == "ok":
                comp = export.get("composite") or {}
                if comp.get("data_b64"):
                    data = base64.b64decode(comp["data_b64"])
                    rec = logger.write_artifact(
                        f"artifacts/{comp['filename']}", data,
                        kind="composite_labelmap",
                        extra={"sha256_remote": comp.get("sha256")},
                    )
                    if rec["sha256"] != comp.get("sha256"):
                        logger.log("  WARNING: composite sha256 mismatch "
                                   "(local vs remote)")
                    logger.log(f"  composite     : {rec['path']}  "
                               f"{rec['size_bytes']:,} bytes  "
                               f"sha256={rec['sha256'][:16]}…")
                for seg in export.get("per_segment", []):
                    if not seg.get("data_b64"):
                        continue
                    data = base64.b64decode(seg["data_b64"])
                    rec = logger.write_artifact(
                        f"artifacts/per_segment/{seg['filename']}", data,
                        kind="per_segment_labelmap",
                        extra={"sid": seg["sid"], "name": seg["name"],
                               "color": seg.get("color"),
                               "sha256_remote": seg.get("sha256")},
                    )
                    logger.log(f"  segment {seg['sid']:>14s}: "
                               f"{rec['size_bytes']:>9,} bytes  "
                               f"sha256={rec['sha256'][:12]}…")
            else:
                logger.log(f"  export failed: {export}")
                logger.event("export_failed", **export)
        except Exception as e:
            logger.log(f"  export error: {e}")
            logger.event("export_error", error=repr(e))

    # ------------------------------------------------------------------
    # Headless runs render nothing during the loop. Collapse the final
    # union into ONE combined surface and show it (paid once, not per batch).
    # ------------------------------------------------------------------
    if args.headless and not args.no_view_result:
        logger.log("-> Building combined surface of completed mask for viewing…")
        try:
            v = post_python(base_url, VIEW_COMPLETED_SRC, timeout=300)
            if v.get("status") == "ok":
                logger.log(f"   completed_mask shown: {v.get('voxels'):,} voxels")
            else:
                logger.log(f"   view-result failed: {v}")
            logger.event("view_completed", **v)
        except Exception as e:
            logger.log(f"   view-result error: {e}")
            logger.event("view_completed_error", error=repr(e))

    # ------------------------------------------------------------------
    # Summary + replay script
    # ------------------------------------------------------------------
    final_voxels = (history[-1]["voxels_after"] if history else 0)
    summary = {
        "run_id": logger.run_id,
        "volume_name": init.get("volume_name"),
        "volume_sha256_voxels": vol_meta.get("sha256_voxels"),
        "shape_kji": init.get("shape_kji"),
        "spacing_mm": vol_meta.get("spacing_mm"),
        "threshold": init.get("threshold"),
        "n_candidates_initial": init.get("n_candidates"),
        "steps": len(history),
        "history": history,
        "args": {k: (str(v) if isinstance(v, Path) else v)
                  for k, v in vars(args).items()},
        "final_voxel_count": final_voxels,
        "final_voxel_fraction": round(final_voxels / max(total_voxels, 1), 4),
        "stop_reason": stop_reason,
    }
    logger.write_artifact(
        "summary.json", json.dumps(summary, indent=2, default=str).encode(),
        kind="summary",
    )
    # Also keep summary.json at the run root (legacy location).
    (args.out_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, default=str)
    )

    # Build the replay script. We strip --out-dir (it's relative anyway
    # and the user will pick a fresh directory) and substitute the
    # captured args verbatim.
    replay_cmd = [
        f'python3 "{Path(__file__).relative_to(Path.cwd().resolve()) if Path(__file__).is_relative_to(Path.cwd().resolve()) else Path(__file__).name}"',
    ]
    arg_pairs = [
        ("--volume", args.volume),
        ("--intensity-percentile", args.intensity_percentile),
        ("--max-candidates", args.max_candidates),
        ("--max-steps", args.max_steps),
        ("--min-delta", args.min_delta),
        ("--patience", args.patience),
        ("--max-explosion-frac", args.max_explosion_frac),
    ]
    for flag, val in arg_pairs:
        if val is not None:
            replay_cmd.append(f'{flag} {val}')
    if args.reset_first:
        replay_cmd.append("--reset-first")
    if args.no_stop_rules:
        replay_cmd.append("--no-stop-rules")
    if args.no_new_segment_per_click:
        replay_cmd.append("--no-new-segment-per-click")
    if args.no_screenshots:
        replay_cmd.append("--no-screenshots")
    if args.batch_size and args.batch_size > 1:
        replay_cmd.append(f"--batch-size {args.batch_size}")
    if args.headless:
        replay_cmd.append("--headless")
    replay_cmd.append(f'--out-dir runs/replay_{logger.run_id}')
    if args.label:
        replay_cmd.append(f'--label "{args.label}"')

    logger.write_replay(
        command=replay_cmd,
        env_keys=["SLICER_WEBSERVER_URL", "NNI_REMOTE_URL", "OPENAI_API_KEY"],
    )

    logger.log("")
    logger.log(f"DONE. final voxels = {final_voxels:,} / {total_voxels:,} "
               f"({100*final_voxels/max(total_voxels, 1):.2f}%)")
    logger.log(f"      stop reason = {stop_reason}")
    logger.log(f"      summary    -> {args.out_dir / 'summary.json'}")
    logger.log(f"      events     -> {logger.events_path}")
    logger.log(f"      artifacts  -> {logger.artifacts_dir}")
    logger.log(f"      replay     -> {logger.replay_path}")

    logger.finalize(stop_reason=stop_reason, summary=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
