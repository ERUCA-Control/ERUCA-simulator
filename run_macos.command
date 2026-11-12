#!/bin/bash
set -e
cd "$(dirname "$0")"

fail() {
    printf '\n%s\n' "$1" >&2
    printf '%s\n' "See README.md for manual setup instructions." >&2
    read -r -p "Press Return to close... " unused_reply || true
    exit 1
}

if [ ! -x ".venv/bin/python" ]; then
    if ! command -v python3 >/dev/null 2>&1; then
        fail "Python 3.10+ was not found. Install Python 3.11 or 3.12 from python.org."
    fi
    python3 -c 'import sys; assert sys.version_info >= (3, 10)' >/dev/null 2>&1 \
        || fail "Python 3.10+ is required. Install Python 3.11 or 3.12 from python.org."
    python3 -m venv .venv || fail "Could not create the .venv environment."
fi

.venv/bin/python -c 'import sys; assert sys.version_info >= (3, 10)' >/dev/null 2>&1 \
    || fail "The existing .venv uses an unsupported or missing Python. Rename .venv and run again."

if ! .venv/bin/python -c 'from importlib.metadata import version; assert version("rocketpy") == "1.13.0"; import rocketpy, numpy, matplotlib' >/dev/null 2>&1; then
    printf '%s\n' "Installing required packages. Internet access is needed for the first setup."
    .venv/bin/python -m pip install --disable-pip-version-check -r requirements.txt \
        || fail "Package installation failed. Check the internet connection and Python version."
fi

.venv/bin/python -c 'import tkinter' >/dev/null 2>&1 \
    || fail "Tkinter is missing. Use a Python distribution with Tcl/Tk (for example python.org)."

.venv/bin/python app.py || fail "The application exited with an error."
