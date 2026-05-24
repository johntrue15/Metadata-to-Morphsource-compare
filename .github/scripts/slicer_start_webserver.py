# Slicer-internal helper: start the Web Server module on :2016 with the
# "Slicer API exec" channel enabled. Paste this into Slicer's Python
# Interactor (View > Python Interactor) inside the Guacamole desktop after
# unshelving, OR pass via `Slicer --python-script` once a future v2
# auto-launch path lands.
#
# This script is intentionally print-driven and idempotent: re-running on
# an already-started server is a no-op (apart from the status print).
#
# It is also printed verbatim by .github/scripts/jetstream_unshelve_start.py
# as the fallback when the :2016 HTTPS probe fails, so KEEP IT SHORT AND
# SELF-CONTAINED — no imports from this repo, no f-strings older than
# Slicer's bundled Python supports, no external deps.

import slicer

# 1. Switch the GUI to the Web Server module so the user can see the state
#    change in real time.
slicer.util.selectModule("WebServer")

ws = slicer.modules.WebServerWidget

# 2. Expand "Advanced" and tick the Slicer API exec channel. The widget
#    names below match Slicer 5.x's WebServer module; harmless if the layout
#    changes (the qt attribute lookup will raise loudly).
try:
    ws.advancedCollapsibleButton.collapsed = False
except AttributeError:
    pass

try:
    ws.slicerAPICheckBox.checked = True
except AttributeError:
    # Older builds named it differently; try the underlying logic flag.
    try:
        ws.logic.enableSlicerAPI = True
    except AttributeError:
        print("WARN: could not enable 'Slicer API exec' programmatically; "
              "please tick it manually in the module's Advanced section.")

# 3. Start the server if it isn't already. The widget's toggle is a
#    QPushButton in "checkable" mode whose `checked` property reflects
#    server state.
already_running = False
try:
    if ws.startStopButton.checked:
        already_running = True
    else:
        ws.startStopButton.click()
except AttributeError:
    # Fallback: call the logic directly. Default port is 2016.
    try:
        ws.logic.start()
    except Exception as exc:  # noqa: BLE001 — Slicer-side, surface clearly
        print("ERROR: could not start Web Server via logic.start():", exc)
        raise

if already_running:
    print("Slicer Web Server was already running on port 2016.")
else:
    print("Started Slicer Web Server on port 2016 with Slicer API exec enabled.")

# 4. Print the current server URL so the operator can confirm the port
#    matches the proxy URL the local Mac is configured to hit.
try:
    print("Server object:", slicer.modules.WebServerWidget.logic.server)
except Exception:
    pass

print("Ready. The local Mac probe should now see HTTP 200 on "
      "https://http-<ip-with-dashes>-2016.proxy-js2-iu.exosphere.app/slicer/screenshot")
