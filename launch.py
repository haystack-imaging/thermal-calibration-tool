#!/usr/bin/env python3
"""Install what the tool needs (once, into a private environment) and start it.

Double-click Launch.bat (Windows), Launch.command (macOS) or Launch.sh (Linux); they run this file.
From a terminal:  python3 launch.py [--update] [--no-update] [--setup-only] [tool arguments ...]

Every start:
  1. checks that Python is 3.10 or newer and has Tk (the window toolkit);
  2. pulls new commits when this folder is a git checkout without local changes (--no-update skips it);
  3. creates .venv next to this file if it is missing or broken, and installs the packages from
     requirements.txt when that file changed since the last install (--update forces an upgrade);
  4. starts thermal_calibration_tool.py from that environment and passes further arguments on,
     e.g.  python3 launch.py --project flight.calib.json --run
Nothing is installed outside this folder (or ~/.thermal_calibration_tool when the folder is read-only).
"""
import hashlib
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOL = HERE / "thermal_calibration_tool.py"
REQUIREMENTS = HERE / "requirements.txt"
MIN_PY = (3, 10)
IMPORT_CHECK = "import numpy, pandas, PIL, cv2, rasterio, scipy, matplotlib, tkinter"
WIN = os.name == "nt"
PYTHON_DOWNLOAD = "https://www.python.org/downloads/"
LAUNCHER_FLAGS = ("--update", "--no-update", "--setup-only")


def say(msg: str):
    print(f"[launch] {msg}", flush=True)


def pause():
    """Keep the console open so the message can be read when the launcher was double-clicked."""
    if sys.stdin and sys.stdin.isatty():
        try:
            input("\nPress Enter to close this window.")
        except EOFError:
            pass


def fail(msg: str, hint: str = ""):
    print(f"\n[launch] PROBLEM: {msg}", flush=True)
    if hint:
        print(f"         {hint}", flush=True)
    pause()
    sys.exit(1)


def tk_hint() -> str:
    if platform.system() == "Linux":
        return ("Install Tk for Python: sudo apt install python3-tk (Debian/Ubuntu), "
                "sudo dnf install python3-tkinter (Fedora), sudo pacman -S tk (Arch); then run the launcher again.")
    return f"Install Python from {PYTHON_DOWNLOAD} (it includes Tk), then run the launcher again."


def check_python():
    if sys.version_info < MIN_PY:
        fail(f"Python {MIN_PY[0]}.{MIN_PY[1]} or newer is needed; this is Python {platform.python_version()}.",
             f"Install a current Python from {PYTHON_DOWNLOAD} and run the launcher again.")
    try:
        __import__("tkinter")
    except ImportError:
        fail("This Python has no Tk (tkinter), so the window cannot be shown.", tk_hint())


def venv_dir() -> Path:
    if os.access(HERE, os.W_OK):
        return HERE / ".venv"
    return Path.home() / ".thermal_calibration_tool" / "venv"      # e.g. unpacked into Program Files


def venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if WIN else "bin/python")


def venv_ok(venv: Path) -> bool:
    """The environment exists and its interpreter still runs (a system Python upgrade can break it)."""
    py = venv_python(venv)
    if not py.exists():
        return False
    try:
        r = subprocess.run([str(py), "-c", "import sys"], capture_output=True, text=True, timeout=60)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def create_venv(venv: Path):
    say(f"Creating a private Python environment in {venv} (only the first time)")
    if venv.exists():
        shutil.rmtree(venv, ignore_errors=True)
    venv.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run([sys.executable, "-m", "venv", str(venv)], capture_output=True, text=True)
    if r.returncode != 0:
        out = r.stdout + r.stderr
        hint = ("On Debian/Ubuntu: sudo apt install python3-venv, then run the launcher again."
                if "ensurepip" in out else out.strip()[-800:])
        fail("Could not create the virtual environment.", hint)


def requirements_stamp(venv: Path) -> Path:
    return venv / "requirements.sha256"


def requirements_digest() -> str:
    h = hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest()
    return f"{h} python{sys.version_info[0]}.{sys.version_info[1]}"


def packages_ok(venv: Path) -> bool:
    r = subprocess.run([str(venv_python(venv)), "-c", IMPORT_CHECK], capture_output=True, text=True)
    return r.returncode == 0


def install_packages(venv: Path, upgrade: bool):
    py = str(venv_python(venv))
    say("Installing the Python packages the tool needs (a few minutes the first time; needs an internet connection)")
    subprocess.run([py, "-m", "pip", "install", "--quiet", "--upgrade", "pip"], check=False)
    cmd = [py, "-m", "pip", "install", "-r", str(REQUIREMENTS)] + (["--upgrade"] if upgrade else [])
    if subprocess.run(cmd).returncode != 0:
        fail("pip could not install the packages (the messages above say why).",
             "Check the internet connection and run the launcher again. A very new Python version without "
             "ready-made packages can also be the cause; Python 3.11 to 3.13 is a safe choice.")
    requirements_stamp(venv).write_text(requirements_digest(), encoding="utf-8")


def git_update():
    """Fast-forward a clean git checkout; quietly skipped for a ZIP download, a dirty tree or without network."""
    git = shutil.which("git")
    if not git or not (HERE / ".git").exists():
        return
    try:
        run = lambda *a, **k: subprocess.run([git, *a], cwd=HERE, capture_output=True, text=True, timeout=k.get("timeout", 30))
        st = run("status", "--porcelain", "--untracked-files=no")
        if st.returncode != 0:
            return
        if st.stdout.strip():
            say("Code update skipped: this checkout has local changes.")
            return
        before = run("rev-parse", "--short", "HEAD").stdout.strip()
        r = run("pull", "--ff-only", "--quiet", timeout=120)
        after = run("rev-parse", "--short", "HEAD").stdout.strip()
        if r.returncode != 0:
            say("Code update skipped (no connection, no remote, or the branch has diverged).")
        elif before != after:
            say(f"Code updated: {before} -> {after}")
        else:
            say("Code is up to date.")
    except (OSError, subprocess.SubprocessError):
        say("Code update skipped.")


def launch(venv: Path, args: list) -> int:
    py = venv_python(venv)
    if "--run" in args:                      # headless batch: keep its output in this window
        return subprocess.call([str(py), str(TOOL), *args], cwd=HERE)
    exe = py.with_name("pythonw.exe") if WIN and py.with_name("pythonw.exe").exists() else py
    log = venv / "last_launch.log"
    say(f"Starting the tool (its messages go to {log})")
    with open(log, "w", encoding="utf-8") as lf:
        extra = ({"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP} if WIN
                 else {"start_new_session": True})
        proc = subprocess.Popen([str(exe), str(TOOL), *args], cwd=HERE, stdout=lf, stderr=subprocess.STDOUT, **extra)
    time.sleep(4)
    code = proc.poll()
    if code is not None and code != 0:
        print(log.read_text(encoding="utf-8", errors="replace")[-3000:])
        fail(f"The tool stopped right away (exit code {code}); the messages above say why.")
    say("Running. This window can be closed.")
    return 0


def main(argv) -> int:
    args = list(argv)
    if "-h" in args or "--help" in args:
        print(__doc__)
        return 0
    force_update, no_update, setup_only = (f in args for f in LAUNCHER_FLAGS)
    args = [a for a in args if a not in LAUNCHER_FLAGS]
    os.chdir(HERE)
    check_python()
    if not no_update:
        git_update()
    venv = venv_dir()
    if not venv_ok(venv):
        create_venv(venv)
    stamp = requirements_stamp(venv)
    have = stamp.read_text(encoding="utf-8").strip() if stamp.exists() else ""
    if force_update or have != requirements_digest() or not packages_ok(venv):
        install_packages(venv, upgrade=force_update or bool(have))
    else:
        say("Packages are installed and current.")
    if setup_only:
        say("Setup done.")
        return 0
    return launch(venv, args)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        sys.exit(130)
