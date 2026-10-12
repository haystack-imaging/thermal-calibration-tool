"""The double-click launcher: its decisions, not the installation itself (that needs network and minutes)."""
import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import launch  # noqa: E402


def test_wrappers_exist_and_point_at_the_launcher():
    for name in ("Launch.bat", "Launch.command", "Launch.sh"):
        p = ROOT / name
        assert p.exists(), name
        if os.name != "nt" and name != "Launch.bat":
            assert os.access(p, os.X_OK), f"{name} must be executable (git keeps the mode)"
    assert "launch.py" in (ROOT / "Launch.bat").read_text() and "launch.py" in (ROOT / "Launch.sh").read_text()
    assert "Launch.sh" in (ROOT / "Launch.command").read_text()


def test_digest_and_paths(tmp_path):
    d = launch.requirements_digest()
    assert re.fullmatch(r"[0-9a-f]{64} python\d+\.\d+", d), "sha256 of requirements.txt plus the Python version"
    py = launch.venv_python(tmp_path / "v")
    assert py.name == ("python.exe" if os.name == "nt" else "python")
    assert not launch.venv_ok(tmp_path / "missing")
    assert launch.requirements_stamp(tmp_path / "v") == tmp_path / "v" / "requirements.sha256"


def test_help_and_flags(capsys):
    assert launch.main(["--help"]) == 0
    assert "requirements.txt" in capsys.readouterr().out
    assert set(launch.LAUNCHER_FLAGS) == {"--update", "--no-update", "--setup-only"}


def test_python_check_passes_here_and_git_update_is_quiet_outside_a_checkout(tmp_path, monkeypatch, capsys):
    launch.check_python()                                   # the test environment has Python >= 3.10 with Tk
    monkeypatch.setattr(launch, "HERE", tmp_path)
    launch.git_update()                                     # no .git: nothing happens, nothing printed
    assert capsys.readouterr().out == ""


def test_old_python_is_refused(monkeypatch):
    monkeypatch.setattr(launch.sys, "version_info", (3, 9, 0, "final", 0))
    monkeypatch.setattr(launch, "pause", lambda: None)
    with pytest.raises(SystemExit):
        launch.check_python()
