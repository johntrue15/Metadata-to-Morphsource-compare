# Jetstream ECU — compute on the box, control from Mac

MorphoClaw splits **where work runs** from **who orchestrates it**:

| Role | Where | What |
|------|--------|------|
| **Controller** | Mac Mini | `.env`, OpenAI keys, `jetstream_controller.py`, job submit/poll |
| **ECU** | Jetstream | Git clone, `jetstream_ecu_server.py`, `slicer_remote_*` via **localhost** Slicer |

Long nnInteractive clicks use `http://127.0.0.1:2016/` on the box. The Mac only talks to the ECU on port **18765** (short HTTP — no Slicer proxy timeouts).

## One-line install (Guacamole terminal on Jetstream)

```bash
curl -fsSL https://raw.githubusercontent.com/johntrue15/MorphoClaw/main/scripts/jetstream/install_ecu.sh | bash
```

This will:

1. Clone/update `johntrue15/MorphoClaw` → `/media/volume/MyData/MorphoClaw` (or `~/MorphoClaw`)
2. Start the ECU job server in tmux session `morphoclaw-ecu`
3. Print the Exosphere proxy URL for your Mac

**Prerequisite:** 3D Slicer GUI running with Web Server on `:2016` and **Slicer API exec** enabled.

## Mac setup

After unshelve, add to `.env` (or run `make unshelve IP=...` which sets NNI/Slicer URLs; add ECU manually until unshelve v2):

```bash
MORPHOCLAW_ECU_URL=https://http-149-165-170-184-18765.proxy-js2-iu.exosphere.app/
JETSTREAM_PUBLIC_IP=149.165.170.184
```

Optional shared secret:

```bash
MORPHOCLAW_ECU_TOKEN=your-random-token   # same on Mac and Jetstream
```

## Controller commands (Mac)

```bash
set -a && source .env && set +a

# Health check
python3 .github/scripts/jetstream_controller.py health

# Run any script ON the Jetstream ECU (localhost Slicer inside)
python3 .github/scripts/jetstream_controller.py run --wait -- \
  python3 .github/scripts/slicer_remote_pcb_copper.py \
  --phase copper --volume pcb_ti_jetstream --max-steps 8 \
  --noise-manifest runs/pcb_noise_export_20260527/noise_manifest.json \
  --out-dir runs/pcb_copper_ecu_test

# Presets
python3 .github/scripts/jetstream_controller.py preset pcb-export-noise --wait
python3 .github/scripts/jetstream_controller.py preset pcb-copper-test --wait
```

LLM presets forward `OPENAI_API_KEY` from the Mac environment when set.

## Architecture

```mermaid
flowchart LR
  mac[Mac Mini controller]
  proxy[Exosphere :18765]
  ecu[jetstream_ecu_server.py]
  slicer[Slicer localhost :2016]
  nni[nnInteractive plugin]

  mac -->|POST /v1/jobs short| proxy
  proxy --> ecu
  ecu -->|exec localhost| slicer
  slicer --> nni
  mac -->|GET /v1/jobs/id poll| proxy
```

## Operations on Jetstream

```bash
tmux attach -t morphoclaw-ecu    # live ECU logs
tail -f ~/morphoclaw_ecu.log
bash ~/MorphoClaw/scripts/jetstream/restart_ecu.sh
```

Re-run the one-line install to update code from `main`.

## Install from Mac over SSH (optional)

```bash
ssh exouser@149.165.170.184 'curl -fsSL https://raw.githubusercontent.com/johntrue15/MorphoClaw/main/scripts/jetstream/install_ecu.sh | bash'
```

Requires key-based SSH (`ssh-copy-id`).

## Related

- [JETSTREAM_UNSHELVE.md](JETSTREAM_UNSHELVE.md) — IP change + nnInteractive `:1527`
- `scripts/dev/run_pcb_on_jetstream_localhost.sh` — manual localhost runner (superseded by ECU for Mac-driven workflows)
