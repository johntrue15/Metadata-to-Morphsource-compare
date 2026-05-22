# MorphoClaw self-hosted runner scripts

Idempotent bootstrap scripts for adding a new self-hosted GitHub Actions
runner to this repository. The headline use case is turning a Windows host
with an NVIDIA GPU into a GPU-capable runner for the nnInteractive and
SlicerMorph workflows, but the same scripts work on any Debian/Ubuntu Linux
box with CUDA passthrough (bare metal, WSL2, or LXD/Proxmox VM).

## Files

| Path | Runs on | Purpose |
| --- | --- | --- |
| `setup-windows-host.ps1` | Windows host (Admin PowerShell) | Verifies the NVIDIA driver, enables WSL2 + Virtual Machine Platform features, installs the Ubuntu 24.04 distro. |
| `setup-wsl-runner.sh` | Inside Ubuntu / WSL2 | Installs apt prereqs, Miniforge, downloads and configures the actions-runner, writes a per-host `.env`, optionally pre-warms nnInteractive and installs Slicer. |
| `install-slicer-linux.sh` | Inside Ubuntu / WSL2 | Downloads 3D Slicer, installs it under `/opt/slicer`, writes an `xvfb-run` wrapper at `~/bin/Slicer`, and invokes the SlicerMorph installer. |
| `slicer_install_slicermorph.py` | Inside Slicer (via `--python-script`) | Headlessly installs SlicerMorph + a few common companion extensions. |
| `test-runner.sh` | Inside Ubuntu / WSL2 | Local end-to-end smoke test: nvidia-smi, runner config, PyTorch + CUDA, `nnInteractiveInferenceSession` constructed on the GPU, synthetic forward pass, Slicer headless, `import GPA`. Exits non-zero on the first failure. |
| `gpu_smoke.py` | Inside the nnInteractive venv | Python helper called by `test-runner.sh` and by `.github/workflows/runner-smoke.yml`. Does the real PyTorch + nnInteractive work. |
| `runner-env.example` | Reference | Sample of the `.env` file the actions-runner reads. Useful for hand-configuring the Mac mini or any other host. |

## One-shot bring-up (Dell XPS + GTX 1650 Ti, but applies to any Windows + NVIDIA box)

1. **Windows host (elevated PowerShell):**

   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\runners\setup-windows-host.ps1
   ```

   Reboot if the script tells you to, then re-run.

2. **Inside WSL Ubuntu:**

   ```bash
   bash /mnt/c/Users/<you>/.../MorphoClaw/scripts/runners/setup-wsl-runner.sh
   ```

   You will be prompted (once) for a runner registration token. Get one from:

   <https://github.com/johntrue15/MorphoClaw/settings/actions/runners/new>

   (Or `gh auth login` first and the script will fetch the token for you.)

3. **Smoke-test the box BEFORE attaching it to real workloads:**

   ```bash
   bash scripts/runners/test-runner.sh
   ```

   This runs 6 end-to-end checks in ~2-3 minutes: nvidia-smi, the runner's
   `.env` schema, the nnInteractive venv, a real `nnInteractiveInferenceSession`
   on CUDA + a synthetic 32^3 forward pass, Slicer headless launch, and
   `import GPA` (SlicerMorph) inside Slicer. Exits non-zero on the first
   failure with a pointer to the relevant log in `/tmp/`.

4. **Start the runner:**

   ```bash
   cd ~/actions-runner-morphoclaw
   ./run.sh
   ```

5. **Verify routing + secrets via GitHub:** trigger the
   "Runner GPU smoke test" workflow from the Actions tab. It runs the same
   checks as step 3 but through GitHub Actions, proving the registration,
   labels, and `runs-on: [self-hosted, gpu]` routing all work.

   See `docs/self-hosted-gpu-runner.md` for the full operations guide,
   including how to label the existing Mac mini runner so the two hosts
   split work cleanly.

## Re-running

All four scripts are safe to re-run. They detect existing installs and only
do the work that is missing. To force a clean reinstall:

```bash
# Remove the runner from GitHub first (it needs a one-time removal token):
cd ~/actions-runner-morphoclaw
./config.sh remove --token <removal-token>
cd ~ && rm -rf actions-runner-morphoclaw

# Then re-run setup-wsl-runner.sh.
```
