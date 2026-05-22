"""Install SlicerMorph (+ a few common companion extensions) into 3D Slicer.

This is the SlicerMorph counterpart of
``.github/scripts/slicer_install_nninteractive.py``. It is run **inside Slicer**
by ``scripts/runners/install-slicer-linux.sh`` on the WSL runner setup path:

    Slicer --no-splash --no-main-window --python-script \
        scripts/runners/slicer_install_slicermorph.py

The script is idempotent: each extension is skipped if already installed.
Exit code 0 means SlicerMorph itself installed (or was already present);
the optional extensions are best-effort.
"""

from __future__ import annotations

import sys

import slicer


REQUIRED_EXTS = ("SlicerMorph",)

OPTIONAL_EXTS = (
    "SegmentEditorExtraEffects",
    "SurfaceWrapSolidify",
    "MarkupsToModel",
)


def install_extension(name: str) -> bool:
    em = slicer.app.extensionsManagerModel()
    if em.isExtensionInstalled(name):
        path = em.extensionInstallPath(name)
        print(f"[OK] {name} already installed at: {path}")
        return True

    print(f"Installing {name} ...")
    try:
        metadata = em.retrieveExtensionMetadataByName(name)
    except Exception as exc:
        print(f"[ERROR] {name}: lookup failed: {exc}")
        return False

    if not metadata or not metadata.get("extension_id"):
        print(f"[ERROR] {name}: not found in the extensions catalog "
              "(check internet / Slicer version).")
        return False

    print(f"  extension_id = {metadata['extension_id']}")
    try:
        ok = em.downloadAndInstallExtensionByName(name)
    except Exception as exc:
        print(f"[ERROR] {name}: install crashed: {exc}")
        return False

    if ok:
        print(f"[OK] {name} installed.")
        return True
    print(f"[ERROR] {name}: downloadAndInstallExtensionByName returned False.")
    return False


def main() -> int:
    overall_ok = True
    for ext in REQUIRED_EXTS:
        if not install_extension(ext):
            overall_ok = False

    for ext in OPTIONAL_EXTS:
        try:
            install_extension(ext)
        except Exception as exc:
            print(f"[WARN] Optional {ext}: {exc}")

    print("")
    if overall_ok:
        print("SlicerMorph installation finished. Restart Slicer to load the modules.")
        slicer.app.exit(0)
        return 0

    print("SlicerMorph installation failed; see messages above.")
    slicer.app.exit(1)
    return 1


sys.exit(main())
