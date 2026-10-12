#!/bin/bash
# Linux / macOS: install what is missing and start the Thermal Calibration Tool (see launch.py).
# Double-click it (GNOME Files: right-click > Run as a Program) or run  ./Launch.sh  in a terminal.
cd "$(dirname "$0")" || exit 1
if command -v python3 >/dev/null 2>&1; then
    python3 launch.py "$@"
else
    echo "Python 3 was not found."
    case "$(uname -s)" in
        Darwin) echo "Install it from https://www.python.org/downloads/macos/ then double-click this file again."
                open "https://www.python.org/downloads/macos/" 2>/dev/null ;;
        *)      echo "Debian/Ubuntu: sudo apt install python3 python3-tk python3-venv"
                echo "Fedora:        sudo dnf install python3 python3-tkinter" ;;
    esac
    read -r -p "Press Enter to close this window."
    exit 1
fi
