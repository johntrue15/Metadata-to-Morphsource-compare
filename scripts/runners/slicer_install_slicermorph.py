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


_METADATA_REFRESHED = False


def _ensure_metadata_refreshed(em) -> bool:
    """Slicer 5.10 requires an explicit metadata fetch before installs work."""
    global _METADATA_REFRESHED
    if _METADATA_REFRESHED:
        return True
    print("Refreshing extensions metadata from server ...")
    try:
        # signature: updateExtensionsMetadataFromServer(force, waitForCompletion) -> bool
        ok = em.updateExtensionsMetadataFromServer(True, True)
    except Exception as exc:
        print(f"[ERROR] metadata refresh crashed: {exc}")
        return False
    if not ok:
        print("[ERROR] metadata refresh returned False (no network? wrong server URL?).")
        return False
    _METADATA_REFRESHED = True
    print("  metadata refreshed.")
    return True


def install_extension(name: str) -> bool:
    em = slicer.app.extensionsManagerModel()
    if em.isExtensionInstalled(name):
        path = em.extensionInstallPath(name) if hasattr(em, "extensionInstallPath") else "(installed)"
        print(f"[OK] {name} already installed at: {path}")
        return True

    if not _ensure_metadata_refreshed(em):
        return False

    print(f"Installing {name} ...")
    try:
        # Slicer 5.10 signature:
        #   downloadAndInstallExtensionByName(name, installDependencies, waitForCompletion) -> bool
        ok = em.downloadAndInstallExtensionByName(name, True, True)
    except Exception as exc:
        print(f"[ERROR] {name}: install crashed: {exc}")
        return False

    if ok and em.isExtensionInstalled(name):
        print(f"[OK] {name} installed.")
        return True
    print(f"[ERROR] {name}: install returned {ok}, isExtensionInstalled={em.isExtensionInstalled(name)}")
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
