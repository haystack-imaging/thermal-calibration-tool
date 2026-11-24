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


def update(root, limit=1000):
    """Handle the pending Tk events (at most `limit`), then the idle tasks.

    Used instead of `root.update()`, which returns only once the event queue is empty. On macOS, after
    many Tk windows in one test process, AppKit keeps that queue busy and `update()` never returns (seen
    on the GitHub macOS runner: Tk_UpdateObjCmd spinning in the CFRunLoop, no Python callback involved).
    The tool itself runs `mainloop()`, which does not wait for an empty queue."""
    import _tkinter
    flags = _tkinter.ALL_EVENTS | _tkinter.DONT_WAIT
    for _ in range(limit):
        if not root.tk.dooneevent(flags):
            break
    root.update_idletasks()


def pump(root, condition, timeout=10.0, step=0.02):
    """Run the Tk event loop until condition() is true or the timeout passes."""
    import time
    t_end = time.time() + timeout
    while time.time() < t_end:
        update(root)
        if condition():
            return True
        time.sleep(step)
    return condition()
