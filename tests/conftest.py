import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import thermal_calibration_tool as tct  # noqa: E402


@pytest.fixture
def tk_root():
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    root.withdraw()
    yield root
    try:
        root.destroy()
    except tk.TclError:
        pass


@pytest.fixture(autouse=True)
def no_modal_dialogs(monkeypatch):
    """A stray message box would block the suite forever on a hidden root. Every dialog is recorded instead;
    questions answer "yes". Tests that care patch the function again themselves."""
    import tkinter.messagebox as mb
    shown = []
    for name in ("showerror", "showwarning", "showinfo"):
        monkeypatch.setattr(mb, name, lambda title, message="", _n=name, **k: shown.append((_n, title, message)))
    for name in ("askyesno", "askokcancel", "askquestion", "askretrycancel"):
        monkeypatch.setattr(mb, name, lambda *a, **k: True)
    return shown


@pytest.fixture
def app(tk_root):
    a = tct.ThermalCalibrationApp(tk_root)
    tk_root.update_idletasks()
    return a


def pump(root, condition, timeout=10.0, step=0.02):
    """Run the Tk event loop until condition() is true or the timeout passes."""
    import time
    t_end = time.time() + timeout
    while time.time() < t_end:
        root.update()
        if condition():
            return True
        time.sleep(step)
    return condition()
