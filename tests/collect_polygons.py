#!/usr/bin/env python3
"""Open the GUI on a flight config, let a person draw the panel polygons, save them back to the config.

Usage: python3 tests/collect_polygons.py tests/data/<flight>/readme_flight.json

The calibration images and the reference readings come from the config; the polygons you draw replace
the ones stored there when the window is closed. Draw the targets in the order of the readings
(T1, T2, T3 ...) on every shot; use ‹ › to switch shots. Then close the window.
"""
import json
import sys
import tkinter as tk
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import thermal_calibration_tool as tct  # noqa: E402

cfg_path = Path(sys.argv[1])
cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
root = tk.Tk()
app = tct.ThermalCalibrationApp(root)
root.geometry("1500x950+40+40")
root.update()
app.project_var.set(cfg.get("title", "flight"))
app._add_calibration_images([c["file"] for c in cfg["calibration"]])
by_path = {str(Path(c["file"]).resolve()): c for c in cfg["calibration"]}
for i, ev in enumerate(app.calibration_events):           # readings pre-filled, polygons left to the person
    c = by_path[str(Path(ev.image_path).resolve())]
    names = list(c["gun"])
    while len(ev.targets) < len(names):
        ev.add_target()
    for t, name in zip(ev.targets, names):
        t.name, t.ground_truth = name, float(c["gun"][name])
app._load_image(0)
app.show_step(2)
app.status_var.set("Draw T1, T2, T3 on this shot, switch with ‹ ›, draw them there too, then close the window.")


def save_and_close():
    for ev in app.calibration_events:
        c = by_path[str(Path(ev.image_path).resolve())]
        c["panels"] = {t.name: [[int(x), int(y)] for x, y in t.polygon] for t in ev.targets if t.has_polygon()}
    cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    missing = [(Path(c["file"]).name, [n for n in c["gun"] if n not in c.get("panels", {})]) for c in cfg["calibration"]]
    for name, miss in missing:
        if miss:
            print(f"{name}: no polygon for {', '.join(miss)}")
    print("saved polygons to", cfg_path)
    root.destroy()


root.protocol("WM_DELETE_WINDOW", save_and_close)
root.mainloop()
