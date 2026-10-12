#!/bin/bash
# macOS: double-click to install what is missing and start the Thermal Calibration Tool (see launch.py).
# First time after a download: right-click > Open (Gatekeeper), or run it from a terminal.
cd "$(dirname "$0")" || exit 1
exec ./Launch.sh "$@"
