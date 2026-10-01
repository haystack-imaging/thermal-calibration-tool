"""GUI behaviour of the four-step layout, driven through the same handlers the mouse uses.
Skipped automatically without a display."""
import datetime as dt
import json
import tkinter.messagebox as mb
from tkinter import ttk

import pytest

import thermal_calibration_tool as tct
from conftest import pump
from helpers import T0, TARGETS, poly, scene, write_tif


def complete_targets(app, ev, img=None):
    img = app.current_image if img is None else img
    for j, name in enumerate(TARGETS):
        t = ev.targets[j] if j < len(ev.targets) else ev.add_target()
        t.polygon = poly(name)
        st = tct.extract_roi_statistics(img, t.polygon, 3)
        t.drone_mean, t.drone_std, t.pixel_count, t.inset_applied = st.mean, st.std, st.count, st.inset_applied
        t.ground_truth = TARGETS[name][1]
    app._rebuild_target_rows()
    app._recompute_fit(ev)


def test_steps_and_center_views(app, tmp_path):
    assert app.active_step == 1 and app.steps[1].active and app.image_view.winfo_manager()
    app.show_step(4)
    assert app.process_view.frame.winfo_manager() and not app.image_view.winfo_manager()
    assert app.steps[4].active and not app.steps[1].active
    app.show_step(2)
    assert app.image_view.winfo_manager() and not app.process_view.frame.winfo_manager()
    p = write_tif(tmp_path / "cal.tif", scene(), T0)
    app.show_step(1)
    app._add_calibration_images([p])
    assert app.active_step == 2, "adding images moves on to the targets step"
    assert app.steps[1].done and "1 calibration shot" in app.steps[1].summary.cget("text")
    assert not app.steps[2].done and not app.steps[3].done
    complete_targets(app, app.calibration_events[0])
    assert app.steps[2].done and app.steps[3].done
    assert "RMSE" in app.steps[3].summary.cget("text")


def test_fit_panel_reflects_the_fit(app, tmp_path):
    p = write_tif(tmp_path / "cal.tif", scene(blur=0.4), T0)
    app._add_calibration_images([p])
    ev = app.calibration_events[0]
    fp = app.fit_panel
    assert fp.plot_btn.instate(['disabled']) and "2 complete" in fp._message
    complete_targets(app, ev)
    app.root.update_idletasks()
    assert fp.tiles['m'].cget("text").startswith(f"{ev.fit.slope:.4f}")
    assert fp.tiles['b'].cget("text").startswith(f"{ev.fit.intercept:+.3f}")
    assert f"{ev.fit.r_squared:.4f}" in fp.tiles['r2'].cget("text")
    assert "°C" in fp.tiles['u'].cget("text") and f"{ev.fit.x_mean:.1f}" in fp.tiles['u'].cget("text")
    assert fp.plot_btn.instate(['!disabled']) and fp.report_btn.instate(['!disabled'])
    assert len(fp.resid_canvas.find_withtag('bar')) == 3
    assert fp.plot.find_all(), "mini plot drew something"
    # drop to two targets: warning appears, uncertainty tile empty
    ev.targets[2].ground_truth = None
    app._recompute_fit(ev)
    assert "2 targets" in fp.warn_label.cget("text")
    assert "needs 3 targets" in fp.tiles['u'].cget("text")


def test_fit_report_export_from_gui(app, tmp_path):
    p = write_tif(tmp_path / "cal.tif", scene(blur=0.4), T0)
    app._add_calibration_images([p])
    complete_targets(app, app.calibration_events[0])
    out = tmp_path / "report.html"
    html = app._write_fit_report(str(out), app.calibration_events[0])
    assert out.exists() and "Calibration fit report" in html and "ROI edge inset" in html


def test_timeline_gets_survey_scan(app, tmp_path):
    cal = write_tif(tmp_path / "cal.tif", scene(), T0)
    app._add_calibration_images([cal])
    complete_targets(app, app.calibration_events[0])
    sur = tmp_path / "s"
    sur.mkdir()
    for i in range(4):
        write_tif(sur / f"s{i}.tif", scene(), T0 + dt.timedelta(minutes=i + 1))
    app.survey_entry.insert(0, str(sur))
    app._on_survey_changed()
    assert pump(app.root, lambda: len(app._survey_timestamps) == 4)
    assert len(app.timeline.survey) == 4 and "4 images" in app.timeline.summary.cget("text")
    assert app.timeline.events and app.timeline.canvas.find_all()
    assert "4 survey images, not processed yet" in app.steps[4].summary.cget("text")
    rows = app.process_view.tree.get_children()
    assert len(rows) == 4 and app.process_view.tree.item(rows[0])["values"][-1] == "not processed", "listed before any run"
    assert "passed" in app.process_view.pf_counts.cget("text"), "pre-flight ran by itself"
    app.filter_var.set("s1")
    app._on_survey_changed()
    assert pump(app.root, lambda: len(app._survey_timestamps) == 1)


def test_batch_from_gui_fills_results_table(app, tmp_path, monkeypatch):
    cal = write_tif(tmp_path / "cal.tif", scene(), T0)
    sur = tmp_path / "s"
    sur.mkdir()
    for i in range(3):
        write_tif(sur / f"s{i}.tif", scene(1.0), T0 + dt.timedelta(minutes=i))
    app._add_calibration_images([cal])
    complete_targets(app, app.calibration_events[0])
    app.survey_entry.insert(0, str(sur))
    app.output_entry.insert(0, str(tmp_path / "out"))
    monkeypatch.setattr(mb, "askokcancel", lambda *a, **k: True)
    app.show_step(4)
    rep = app._run_preflight()
    assert not rep.blocking and "passed" in app.process_view.pf_counts.cget("text")
    pending = app.process_view.tree.get_children()
    assert len(pending) == 3 and all(app.process_view.tree.item(i)["values"][-1] == "not processed" for i in pending)
    app._start_batch_processing()
    assert app.process_view.run_btn.instate(['disabled'])
    assert pump(app.root, lambda: app.process_view.run_btn.instate(['!disabled']), timeout=60)
    rows = app.process_view.tree.get_children()
    assert len(rows) == 3
    first = app.process_view.tree.item(rows[0])["values"]
    assert first[0] == "s0.tif" and "TIFF tag" in first[1] and first[-1] == "ok"
    assert app.process_view.open_btn.instate(['!disabled'])
    assert app.steps[4].done and "written to" in app.steps[4].summary.cget("text")
    assert "Done: 3 ok" in app.progress_label.cget("text")
    assert any("Done" in l for l in app.log_text)


def test_project_save_and_open_through_gui(app, tmp_path, monkeypatch):
    cal = write_tif(tmp_path / "cal.tif", scene(blur=0.3), T0)
    app._add_calibration_images([cal])
    complete_targets(app, app.calibration_events[0])
    app.roi_inset_var.set(2)
    app.project_var.set("Wheat June")
    app._on_title_edited(True)
    slope = app.calibration_events[0].fit.slope
    pp = tmp_path / "p.calib.json"
    app.project_path = str(pp)
    app._save_project()
    assert pp.exists() and app.saved_var.get().startswith("saved") and "p.calib.json" in app.saved_var.get()
    assert json.loads(pp.read_text(encoding="utf-8"))["title"] == "Wheat June"
    monkeypatch.setattr(mb, "askyesno", lambda *a, **k: True)
    app._new_project()
    assert app.calibration_events == [] and app.active_step == 1 and app.project_var.get() == "untitled project"
    app._open_project(str(pp))
    assert len(app.calibration_events) == 1 and app.active_step == 2
    assert app.calibration_events[0].fit.slope == pytest.approx(slope, rel=1e-6)
    assert app._roi_inset() == 2 and app.project_var.get() == "Wheat June"
    assert app.fit_panel.tiles['m'].cget("text").startswith(f"{slope:.4f}")


def test_targets_are_shared_across_shots(app, tmp_path, monkeypatch):
    a = write_tif(tmp_path / "a.tif", scene(), T0)
    b = write_tif(tmp_path / "b.tif", scene(), T0 + dt.timedelta(minutes=5))
    app._add_calibration_images([a])
    app._add_target()                                   # T3 on the only shot so far
    assert len(app.calibration_events[0].targets) == 3
    app._add_calibration_images([b])                    # a new shot gets the same three targets
    assert [len(e.targets) for e in app.calibration_events] == [3, 3]
    app._add_target()
    assert [len(e.targets) for e in app.calibration_events] == [4, 4] and len(app.target_rows) == 4
    ev_a, ev_b = app.calibration_events
    ev_b.targets[3].polygon = poly("T1")
    monkeypatch.setattr(mb, "askyesno", lambda *a, **k: True)
    app._remove_target(3)
    assert [len(e.targets) for e in app.calibration_events] == [3, 3]
    assert [t.name for t in ev_a.targets] == ["Target 1", "Target 2", "Target 3"]
    app._remove_target(0); app._remove_target(0)
    assert [len(e.targets) for e in app.calibration_events] == [2, 2], "never below two"


def test_title_entry_and_menu(app):
    assert app.project_var.get() == "untitled project"
    app.project_var.set("   ")
    app._on_title_edited(False)
    assert app.project_var.get() == "untitled project"
    app.project_var.set("Flight 3")
    app._on_title_edited(True)
    assert app.project_var.get() == "Flight 3"
    assert app.root.cget("menu"), "File/Help live in the menu bar only"
    assert not any(isinstance(w, ttk.Button) and w.cget("text") in ("Open", "Save", "Help")
                   for w in app.title_entry.master.winfo_children()), "no duplicate buttons in the top bar"


def test_mode_switch_updates_chrome(app, tmp_path):
    app.sensor_mode.set(tct.MODE_MSI)
    app._on_mode_switch()
    assert app.msi_method_frame.winfo_manager() and not app.emis_frame.winfo_manager()
    assert "Multispectral" in app.mode_status.cget("text") and app.add_target_btn.instate(['disabled'])
    app.msi_single_point.set(False)
    app._on_single_point_toggle()
    assert app.add_target_btn.instate(['!disabled']) and "multi point" in app.mode_status.cget("text")
    app.sensor_mode.set(tct.MODE_THERMAL)
    app._on_mode_switch()
    assert app.emis_frame.winfo_manager() and not app.msi_method_frame.winfo_manager()


def test_drawing_flow_with_readout_and_overlay(app, tmp_path):
    p = write_tif(tmp_path / "cal.tif", scene(), T0)
    app._add_calibration_images([p])
    app.root.update()
    app._start_polygon_selection(0)
    assert app.drawing_polygon and app.view.loupe_enabled and app.view.canvas.cget("cursor") == "crosshair"
    for pt in poly("T1")[:3]:
        app._on_image_click(*pt)
    app._on_image_motion(25, 25, 300, 200)
    readout = app.view.canvas.find_withtag('readout')
    assert readout, "value readout follows the cursor"
    texts = [app.view.canvas.itemcget(i, 'text') for i in readout if app.view.canvas.type(i) == 'text']
    assert texts and "T1: 3/4" in texts[0] and "15.00 °C" in texts[0]
    app._on_image_click(*poly("T1")[3])
    t = app.calibration_events[0].targets[0]
    assert not app.drawing_polygon and t.polygon == poly("T1") and t.drone_mean == pytest.approx(15.0)
    assert app.view.canvas.find_withtag('overlay'), "polygon, inner ROI and colour bar are drawn"
    assert "mean 15.00" in app.target_rows[0].stats_label.cget("text")
    rows_before = list(app.target_rows)
    app.calib_listbox.selection_clear(0, 'end'); app.calib_listbox.selection_set(0)
    app._load_image(0)
    assert app.target_rows == rows_before, "same target count: row widgets are reused, no blank flash"
    assert app.root.bind('<Escape>'), "Esc is bound to cancel drawing"


def test_remove_image_and_empty_state(app, tmp_path):
    a = write_tif(tmp_path / "a.tif", scene(), T0)
    b = write_tif(tmp_path / "b.tif", scene(), T0 + dt.timedelta(minutes=5))
    app._add_calibration_images([a, b])
    assert app.calib_listbox.size() == 2 and app.calib_listbox.get(0).startswith("[Start]")
    app.calib_listbox.selection_clear(0, 'end')
    app.calib_listbox.selection_set(0)
    app._remove_calibration_image()
    assert len(app.calibration_events) == 1 and app.calib_listbox.get(0).startswith("[Only ]")
    app.calib_listbox.selection_set(0)
    app._remove_calibration_image()
    assert app.calibration_events == [] and app.current_image is None
    assert app.image_name_label.cget("text") == "No image" and not app.steps[1].done
