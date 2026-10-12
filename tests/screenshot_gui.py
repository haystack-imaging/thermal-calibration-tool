#!/usr/bin/env python3
"""Drive the real GUI on a flight and capture screenshots (Linux/X11 or XWayland).

Opens the actual window, loads the calibration shots, draws the panels through the same handlers
the mouse uses (plus a few real Tk events), enters the reference readings, zooms, runs the pre-flight
check and the batch, and saves PNGs with ImageMagick's `import`.

Usage:
    python3 tests/screenshot_gui.py                         # synthetic flight (tests/make_synthetic_flight.py)
    python3 tests/screenshot_gui.py --config flight.json    # a real flight described by a config file
    python3 tests/screenshot_gui.py --out some/dir          # where the PNGs go (default GUI_screenshots/)

Config file (JSON):
    {"title": "...", "calibration": [{"file": "...tif", "panels": {"black": [[x,y],...4 corners], ...},
                                       "gun": {"black": 45.6, ...}}, ...],
     "survey_folder": "...", "output_folder": "...", "zoom": {"at": [x, y], "notches": 1} or
     "view": {"scale": 1.6, "center": [x, y]}, "loupe_target": 2}
Needs `import` (ImageMagick) on PATH.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import tkinter as tk
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import thermal_calibration_tool as tct  # noqa: E402


def synthetic_config() -> dict:
    syn = HERE / "data" / "synthetic_flight"
    truth = json.loads((syn / "ground_truth.json").read_text())
    cal = []
    for rec in [r for r in truth["images"] if r["kind"] == "calibration"]:
        panels = {name: [[cx - hs - 2, cy - hs - 2], [cx + hs + 2, cy - hs - 2], [cx + hs + 2, cy + hs + 2], [cx - hs - 2, cy + hs + 2]]
                  for name, (cx, cy, hs) in rec["panel_px"].items()}
        cal.append({"file": str(syn / "calibration" / rec["file"]), "panels": panels, "gun": rec["ir_gun_readings_C"]})
    return {"title": "synthetic flight", "calibration": cal, "survey_folder": str(syn / "survey"),
            "output_folder": str(syn / "survey" / "calibrated"), "zoom": {"at": [320, 300], "notches": 3}, "loupe_target": 2,
            "real_m4t": str(HERE / "data" / "real_m4t")}


def settle(root, n=6, dt=0.08):
    for _ in range(n):
        root.update()
        time.sleep(dt)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", help="flight config JSON (default: the synthetic flight)")
    ap.add_argument("--out", default=str(HERE.parent / "GUI_screenshots"))
    args = ap.parse_args()
    cfg = json.loads(Path(args.config).read_text()) if args.config else synthetic_config()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    def shoot(root, name, window=None):
        settle(root, 8)
        subprocess.run(["import", "-window", str((window or root).winfo_id()), str(out / name)], check=True)
        print("saved", out / name)

    root = tk.Tk()
    app = tct.ThermalCalibrationApp(root)
    root.geometry("1500x950+40+40")
    settle(root, 10)
    app.project_var.set(cfg.get("title", "flight"))
    app._add_calibration_images([c["file"] for c in cfg["calibration"]])
    settle(root)
    app.show_step(2)                                   # loading no longer switches the step by itself
    by_path = {os.path.abspath(c["file"]): c for c in cfg["calibration"]}

    # --- draw the panels on every calibration shot through the click handler
    for i, ev in enumerate(app.calibration_events):
        c = by_path[os.path.abspath(ev.image_path)]
        app.calib_listbox.selection_clear(0, 'end')
        app.calib_listbox.selection_set(i)
        app._on_calib_select(None)
        settle(root, 3)
        for j, (name, corners) in enumerate(c["panels"].items()):
            if j >= len(app.target_rows):
                app._add_target()
            app._start_polygon_selection(j)
            for (x, y) in corners:
                if i == 0 and j == 0:            # the first polygon via real mouse events on the canvas
                    px, py = app.view.to_canvas(x + 0.5, y + 0.5)
                    app.view.canvas.event_generate('<Button-1>', x=int(px), y=int(py))
                else:
                    app._on_image_click(x, y)
            app.target_rows[j].gt_var.set(str(c["gun"][name]).replace('.', ','))   # comma decimal
            settle(root, 2)

    # --- first shot, zoomed with real wheel events, hover for the readout
    app.calib_listbox.selection_clear(0, 'end')
    app.calib_listbox.selection_set(0)
    app._on_calib_select(None)
    settle(root, 4)
    cv = app.view.canvas
    if "view" in cfg:                          # explicit scale and image-space centre
        W, H = app.view.size()
        app.view.scale = float(cfg["view"]["scale"])
        vcx, vcy = cfg["view"]["center"]
        app.view.ox, app.view.oy = vcx - W / (2 * app.view.scale), vcy - H / (2 * app.view.scale)
        app.view.fitted = False
        app.view._clamp()
        app.view.redraw()
        cx, cy = app.view.to_canvas(vcx, vcy)
    else:                                      # wheel notches at a point, through real events
        zx, zy = cfg.get("zoom", {}).get("at", [320, 256])
        cx, cy = app.view.to_canvas(zx, zy)
        for _ in range(int(cfg.get("zoom", {}).get("notches", 1))):
            cv.event_generate('<Button-4>', x=int(cx), y=int(cy))
    settle(root, 3)
    cv.event_generate('<Motion>', x=int(cx) + 40, y=int(cy) + 30)
    shoot(root, "GUI_Home.png")

    # --- drawing in progress with the loupe (two corners of one panel of the first shot)
    t_idx = int(cfg.get("loupe_target", 2))
    first = cfg["calibration"][0]
    corners = list(first["panels"].values())[t_idx]
    app._start_polygon_selection(t_idx)
    app._on_image_click(*corners[0])
    app._on_image_click(*corners[1])
    px, py = app.view.to_canvas(*corners[2])
    cv.event_generate('<Motion>', x=int(px), y=int(py))
    shoot(root, "GUI_Drawing_Loupe.png")
    app._cancel_drawing()

    # --- detailed regression plot
    settle(root, 3)
    ev0 = app.calibration_events[0]
    dlg = tct.RegressionPlotDialog(root, ev0, ev0.targets, "°C", "Fit for " + os.path.basename(ev0.image_path))
    settle(root, 10)
    shoot(root, "GUI_Regression_Plot.png", dlg.win)
    dlg.win.destroy()

    # --- survey folder: timeline, automatic pre-flight, pending rows
    app.survey_entry.insert(0, cfg["survey_folder"])
    app.output_entry.insert(0, cfg["output_folder"])
    shutil.rmtree(cfg["output_folder"], ignore_errors=True)
    app._on_survey_changed()
    n_survey = len(list(Path(cfg["survey_folder"]).glob("*.tif")))
    for _ in range(200):                      # wait for the timeline scan
        root.update()
        if len(app._survey_timestamps) >= n_survey:
            break
        time.sleep(0.05)
    app.show_step(2)
    settle(root, 4)
    shoot(root, "GUI_Timeline.png")
    app.show_step(4)
    settle(root, 6)
    shoot(root, "GUI_Batch_Preflight.png")
    import tkinter.messagebox as mb
    mb.askokcancel = lambda *a, **k: True
    app._start_batch_processing()
    for _ in range(1200):
        root.update()
        if app._batch_thread and not app._batch_thread.is_alive() and app.start_btn.instate(['!disabled']):
            break
        time.sleep(0.03)
    settle(root, 6)
    shoot(root, "GUI_Batch.png")

    # --- project file for the README example
    proj = Path(cfg["survey_folder"]).parent / "flight.calib.json"
    with open(proj, "w", encoding="utf-8") as f:
        json.dump(app._project_dict(), f, indent=2)
    print("project saved", proj)

    # --- (synthetic default only) a real DJI M4T frame converted with the DJI SDK
    real = sorted(Path(cfg["real_m4t"]).glob("*_T_deg.tif")) if cfg.get("real_m4t") else []
    if real:
        app.calibration_events = []
        app._survey_timestamps = {}
        app.survey_entry.delete(0, 'end')
        app.show_step(2)
        app._add_calibration_images([str(real[3])])
        app.show_step(2)
        settle(root, 4)
        for j, (x, y) in enumerate([(96, 96), (300, 250), (500, 400)]):
            if j >= len(app.calibration_events[0].targets):
                app._add_target()
            app._start_polygon_selection(j)
            for pt in [(x, y), (x + 44, y), (x + 44, y + 44), (x, y + 44)]:
                app._on_image_click(*pt)
            t = app.calibration_events[0].targets[j]
            app.target_rows[j].gt_var.set(f"{t.drone_mean * 1.02 + 0.5:.1f}")
        settle(root, 3)
        cv.event_generate('<Motion>', x=400, y=300)
        shoot(root, "GUI_Real_M4T.png")
    root.destroy()


if __name__ == "__main__":
    main()
