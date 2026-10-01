#!/usr/bin/env python3
"""Drive the real GUI on the synthetic flight and capture screenshots (Linux/X11 or XWayland).

Opens the actual window, loads the three calibration shots, draws the panels through the same
handlers the mouse uses (plus a few real Tk events), enters the IR-gun readings, zooms, runs the
pre-flight check and the batch, and saves PNGs with ImageMagick's `import`.

Usage: python3 tests/screenshot_gui.py [out_dir]      (default: GUI_screenshots/)
Needs: tests/data/synthetic_flight (make_synthetic_flight.py), `import` (ImageMagick) on PATH.
"""
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

SYN = HERE / "data" / "synthetic_flight"
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE.parent / "GUI_screenshots"
OUT.mkdir(parents=True, exist_ok=True)
truth = json.loads((SYN / "ground_truth.json").read_text())
cal_recs = [r for r in truth["images"] if r["kind"] == "calibration"]


def settle(root, n=6, dt=0.08):
    for _ in range(n):
        root.update()
        time.sleep(dt)


def shoot(root, name, window=None):
    settle(root, 8)
    wid = (window or root).winfo_id()
    path = OUT / name
    subprocess.run(["import", "-window", str(wid), str(path)], check=True)
    print("saved", path)


def main():
    root = tk.Tk()
    app = tct.ThermalCalibrationApp(root)
    root.geometry("1500x950+40+40")
    settle(root, 10)
    paths = [str(SYN / "calibration" / r["file"]) for r in cal_recs]
    app._add_calibration_images(paths)
    settle(root)

    # --- draw the panels on every calibration shot through the click handler (corners 2 px outside)
    for i, rec in enumerate(cal_recs):
        app.calib_listbox.selection_clear(0, 'end'); app.calib_listbox.selection_set(i)
        app._on_calib_select(None)
        settle(root, 3)
        for j, (name, (cx, cy, hs)) in enumerate(rec["panel_px"].items()):
            if j >= len(app.calibration_events[i].targets):
                app._add_target()
            app._start_polygon_selection(j)
            pad = 2
            for (x, y) in [(cx - hs - pad, cy - hs - pad), (cx + hs + pad, cy - hs - pad),
                           (cx + hs + pad, cy + hs + pad), (cx - hs - pad, cy + hs + pad)]:
                if i == 0 and j == 0:            # the first polygon via real mouse events on the canvas
                    px, py = app.view.to_canvas(x + 0.5, y + 0.5)
                    app.view.canvas.event_generate('<Button-1>', x=int(px), y=int(py))
                else:
                    app._on_image_click(x, y)
            app.target_rows[j].gt_var.set(str(rec["ir_gun_readings_C"][name]).replace('.', ','))  # comma decimal
            settle(root, 2)

    # --- back to the first shot: zoom with a real wheel event, hover for the readout, screenshot
    app.calib_listbox.selection_clear(0, 'end'); app.calib_listbox.selection_set(0)
    app._on_calib_select(None)
    settle(root, 4)
    c = app.view.canvas
    cx, cy = app.view.to_canvas(320, 300)
    for _ in range(3):
        c.event_generate('<Button-4>', x=int(cx), y=int(cy))
    settle(root, 3)
    c.event_generate('<Motion>', x=int(cx) + 40, y=int(cy) + 30)
    shoot(root, "GUI_Home.png")

    # --- drawing in progress with the loupe
    app._start_polygon_selection(2)
    rec = cal_recs[0]
    cx_, cy_, hs = rec["panel_px"]["T3_black"]
    app._on_image_click(cx_ - hs - 2, cy_ - hs - 2)
    app._on_image_click(cx_ + hs + 2, cy_ - hs - 2)
    px, py = app.view.to_canvas(cx_ + hs + 2, cy_ + hs + 2)
    c.event_generate('<Motion>', x=int(px), y=int(py))
    shoot(root, "GUI_Drawing_Loupe.png")
    app._cancel_drawing()

    # --- regression plot
    settle(root, 3)
    dlg = tct.RegressionPlotDialog(root, app.calibration_events[0], app.calibration_events[0].targets, "°C",
                                   "Fit for " + os.path.basename(paths[0]))
    settle(root, 10)
    shoot(root, "GUI_Regression_Plot.png", dlg.win)
    dlg.win.destroy()

    # --- step 4: pre-flight + run
    app.survey_entry.insert(0, str(SYN / "survey"))
    app.output_entry.insert(0, str(SYN / "survey" / "calibrated"))
    shutil.rmtree(SYN / "survey" / "calibrated", ignore_errors=True)
    app._on_survey_changed()
    for _ in range(100):                      # wait for the timeline scan
        root.update()
        if len(app._survey_timestamps) >= 24:
            break
        time.sleep(0.05)
    app.show_step(2)
    settle(root, 4)
    shoot(root, "GUI_Timeline.png")
    app.show_step(4)
    app._run_preflight()
    settle(root, 4)
    shoot(root, "GUI_Batch_Preflight.png")
    import tkinter.messagebox as mb
    mb.askokcancel = lambda *a, **k: True
    app._start_batch_processing()
    for _ in range(600):
        root.update()
        if app._batch_thread and not app._batch_thread.is_alive() and app.start_btn.instate(['!disabled']):
            break
        time.sleep(0.03)
    settle(root, 6)
    shoot(root, "GUI_Batch.png")

    # --- project save for the README example
    proj = SYN / "flight.calib.json"
    with open(proj, "w", encoding="utf-8") as f:
        json.dump(app._project_dict(), f, indent=2)
    print("project saved", proj)

    # --- a real DJI M4T frame (converted with the DJI SDK), if available
    real = sorted((HERE / "data" / "real_m4t").glob("*_T_deg.tif"))
    if real:
        app.calibration_events = []
        app._survey_timestamps = {}
        app.survey_entry.delete(0, 'end')
        app.show_step(2)
        app._add_calibration_images([str(real[3])])
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
        c.event_generate('<Motion>', x=400, y=300)
        shoot(root, "GUI_Real_M4T.png")
    root.destroy()


if __name__ == "__main__":
    main()
