"""GUI behaviour of the four-step layout, driven through the same handlers the mouse uses.
Skipped automatically without a display."""
import datetime as dt
import json
import time
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
    assert app.active_step == 1, "adding images stays in step 1: the user may have more shots to add"
    assert app.images_guide_label.cget("text").startswith("1 shot loaded. Add the other calibration shots")
    assert app.images_guide_btn.winfo_manager() and app.images_guide_btn.cget("text") == "Open 2 · Targets ›"
    app.images_guide_btn.invoke()
    assert app.active_step == 2
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
    assert [len(e.targets) for e in app.calibration_events] == [1, 1], "× works down to the last target"
    assert app.target_rows[0].remove_btn.instate(['disabled']), "the last target's × is greyed out"
    app._remove_target(0)
    assert [len(e.targets) for e in app.calibration_events] == [1, 1] and "last target stays" in app.status_var.get()
    assert app.guide_label.cget("text").startswith("Draw T1"), "the remaining target is guided like any other"
    app._add_target()
    assert len(app.target_rows) == 2 and not app.target_rows[0].remove_btn.instate(['disabled'])


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
    assert all(b.instate(['!disabled']) for b in app.mode_buttons), "sensor type is free while no image is loaded"
    w, chain = app.mode_buttons[0], []
    while w is not None and w is not app.steps[1].body:
        chain.append(w); w = w.master
    assert w is app.steps[1].body, "the sensor selector lives in step 1, not in the top bar"
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
    assert texts and "T1: corner 4 of 4" in texts[0] and "15.00 °C" in texts[0]
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


# ----------------------------------------------------------------------------- UX round 2 (12 Oct 2026)
def test_sensor_type_locks_once_images_are_loaded(app, tmp_path):
    p = write_tif(tmp_path / "cal.tif", scene(), T0)
    app._add_calibration_images([p])
    assert all(b.instate(['disabled']) for b in app.mode_buttons)
    assert "Locked" in app.mode_lock_label.cget("text") and app.mode_lock_label.winfo_manager()
    app.calib_listbox.selection_set(0)
    app._remove_calibration_image()
    assert all(b.instate(['!disabled']) for b in app.mode_buttons) and not app.mode_lock_label.winfo_manager()
    app._add_calibration_images([p])
    import tkinter.messagebox as mb
    mb.askyesno = lambda *a, **k: True
    app._new_project()
    assert all(b.instate(['!disabled']) for b in app.mode_buttons) and app.sensor_mode.get() == tct.MODE_THERMAL


def test_guidance_walks_a_first_time_user_through_step_2(app, tmp_path):
    guide = lambda: app.guide_label.cget("text")
    assert "Add calibration images" in guide()
    a = write_tif(tmp_path / "a.tif", scene(), T0)
    b = write_tif(tmp_path / "b.tif", scene(), T0 + dt.timedelta(minutes=20))
    app._add_calibration_images([a, b])
    assert guide().startswith("Draw T1 on shot 1 of 2") and app.target_rows[0].highlighted and not app.target_rows[1].highlighted
    app._start_polygon_selection(0)
    assert guide() == "Click corner 1 of 4 for T1."
    app._on_image_click(*poly("T1")[0])
    assert guide() == "Click corner 2 of 4 for T1."
    for pt in poly("T1")[1:]:
        app._on_image_click(*pt)
    assert guide().startswith("Type the thermometer reading for T1") and app.target_rows[0].highlighted
    assert app.root.focus_get() in (app.target_rows[0].gt_entry, None), "focus jumps into the reading field"
    app.target_rows[0].gt_var.set("15")
    assert guide().startswith("Draw T2 on shot 1 of 2") and app.target_rows[1].highlighted and not app.target_rows[0].highlighted
    app._start_polygon_selection(1)
    for pt in poly("T2"):
        app._on_image_click(*pt)
    app.target_rows[1].gt_var.set("25")
    assert guide().startswith("Shot 1 is done (2 targets). Press › and draw the same targets on shot 2")
    assert not app.guide_btn.winfo_manager(), "no step button while shots are still to do"
    assert not any(r.highlighted for r in app.target_rows)
    app._step_image(1)
    assert app.current_event_index == 1 and guide().startswith("Draw T1 on shot 2 of 2")
    for j, name in enumerate(("T1", "T2")):
        app._start_polygon_selection(j)
        for pt in poly(name):
            app._on_image_click(*pt)
        app.target_rows[j].gt_var.set(str(15 + 10 * j))
    assert guide().startswith("All 2 shots done, 2 targets each. More panels in the images? Press + Add target.")
    assert app.guide_btn.winfo_manager() and app.guide_btn.cget("text") == "Open 4 · Process ›"
    app.target_rows[0].gt_var.set("")                      # a reading removed again → back to that step
    assert "Type the thermometer reading for T1" in guide()


def test_redraw_hides_the_old_polygon_at_once_and_esc_restores_it(app, tmp_path):
    p = write_tif(tmp_path / "cal.tif", scene(), T0)
    app._add_calibration_images([p])
    app.root.update()
    t = app.calibration_events[0].targets[0]
    app._start_polygon_selection(0)
    for pt in poly("T1"):
        app._on_image_click(*pt)
    c = app.view.canvas
    labels = lambda: [c.itemcget(i, 'text') for i in c.find_withtag('overlay') if c.type(i) == 'text']
    assert any(s.startswith("T1 ") for s in labels()), "polygon label on the image"
    app._start_polygon_selection(0)                        # redraw: the old outline is gone immediately
    assert not any(s.startswith("T1") for s in labels())
    assert t.polygon == poly("T1"), "data kept until the new polygon is complete"
    assert "redrawing" in app.target_rows[0].stats_label.cget("text")
    app._on_image_click(1, 1)
    app._cancel_drawing()                                  # Esc: the old polygon is back
    assert any(s.startswith("T1 ") for s in labels()) and t.polygon == poly("T1")
    assert "mean 15.00" in app.target_rows[0].stats_label.cget("text")
    app._start_polygon_selection(0)
    for pt in poly("T2"):
        app._on_image_click(*pt)
    assert t.polygon == poly("T2") and t.drone_mean == pytest.approx(30.0)


def test_enter_moves_to_the_next_reading_field(app, tmp_path):
    p = write_tif(tmp_path / "cal.tif", scene(), T0)
    app._add_calibration_images([p])
    app.root.update()
    app.target_rows[0].gt_var.set("15")
    app._on_gt_enter(0)
    assert app.root.focus_get() in (app.target_rows[1].gt_entry, None)
    app._on_gt_enter(1)                                    # last field: focus leaves the table
    assert app.root.focus_get() in (app.root, None)


def test_targets_can_be_named_and_names_are_shared(app, tmp_path):
    a = write_tif(tmp_path / "a.tif", scene(), T0)
    b = write_tif(tmp_path / "b.tif", scene(), T0 + dt.timedelta(minutes=20))
    app._add_calibration_images([a, b])
    app.root.update()
    row = app.target_rows[0]
    row.begin_rename()
    row.name_entry.delete(0, 'end'); row.name_entry.insert(0, "black")
    row.end_rename(True)
    assert all(e.targets[0].name == "black" for e in app.calibration_events)
    assert row.alias_label.cget("text") == "black" and row.alias_label.winfo_manager()
    assert "T1 black" in app.guide_label.cget("text")
    app._start_polygon_selection(0)
    for pt in poly("T1"):
        app._on_image_click(*pt)
    c = app.view.canvas
    assert any(c.itemcget(i, 'text').startswith("T1 black") for i in c.find_withtag('overlay') if c.type(i) == 'text')
    assert tct.target_label(0, "Target 1") == "T1" and tct.target_label(2, "white") == "T3 white"
    app._add_target()
    assert app.calibration_events[0].targets[2].name == "Target 3"
    app._remove_target(1)                                  # renumbering keeps the custom name
    assert [t.name for t in app.calibration_events[0].targets] == ["black", "Target 2"]
    row = app.target_rows[0]                               # the table was rebuilt (count changed)
    assert row.alias_label.cget("text") == "black"
    row.begin_rename(); row.name_entry.delete(0, 'end'); row.end_rename(True)
    assert all(e.targets[0].name == "Target 1" for e in app.calibration_events) and not row.alias_label.winfo_manager()


def test_empty_canvas_shows_a_hint_and_window_title_follows_the_project(app):
    app.root.update()
    c = app.view.canvas
    texts = [c.itemcget(i, 'text') for i in c.find_withtag('placeholder')]
    assert texts and "No calibration image yet" in texts[0]
    assert app.root.title() == tct.TOOL_NAME
    app.project_var.set("Flight 7")
    assert app.root.title().startswith("Flight 7")
    app.project_var.set("untitled project")
    assert app.root.title() == tct.TOOL_NAME


def test_step_1_guidance_duplicates_and_the_done_message_allow_for_more_targets(app, tmp_path, monkeypatch):
    g1 = lambda: app.images_guide_label.cget("text")
    assert g1().startswith("Choose the sensor type, then press Add images…") and not app.images_guide_btn.winfo_manager()
    assert "temperature TIFFs" in app.images_guide_detail.cget("text")
    a = write_tif(tmp_path / "a.tif", scene(), T0)
    b = write_tif(tmp_path / "b.tif", scene(), T0 + dt.timedelta(minutes=20))
    app._add_calibration_images([a])
    assert app.active_step == 1 and g1().startswith("1 shot loaded")
    assert "then open 2 · Targets" in app.status_var.get()
    # the same file chosen again: one warning, nothing added, the list unchanged
    warnings = []
    monkeypatch.setattr(mb, "showwarning", lambda title, msg, **k: warnings.append((title, msg)))
    app._add_calibration_images([a])
    assert len(app.calibration_events) == 1 and warnings and "Already in the list" in warnings[0][1] and "a.tif" in warnings[0][1]
    assert app.status_var.get().startswith("Nothing added: 1 image(s) were loaded already")
    warnings.clear()
    app._add_calibration_images([a, b, b])              # one old, one new (listed twice): one warning, one added
    assert len(app.calibration_events) == 2 and len(warnings) == 1 and "a.tif" in warnings[0][1] and "b.tif" in warnings[0][1]
    assert g1().startswith("2 shots loaded. Add any further calibration shots")
    assert app.images_guide_btn.winfo_manager()
    # a third target on a single shot: the 'done' message still offers + Add target and the Process button
    monkeypatch.setattr(mb, "askyesno", lambda *a, **k: True)
    app.calib_listbox.selection_clear(0, "end"); app.calib_listbox.selection_set(1); app._remove_calibration_image()
    assert len(app.calibration_events) == 1
    ev = app.calibration_events[0]
    complete_targets(app, ev)                            # three complete targets (T1..T3)
    app._update_step_summaries()
    g2 = app.guide_label.cget("text")
    assert g2.startswith("3 targets done on the shot. More panels in the image? Press + Add target.") and "4 · Process" in g2
    assert app.guide_btn.winfo_manager() and app.guide_btn.cget("text") == "Open 4 · Process ›"
    app.guide_btn.invoke()
    assert app.active_step == 4


def test_process_step_guidance_and_empty_table_hint(app, tmp_path, monkeypatch):
    g4 = lambda: app.process_guide_label.cget("text")
    btn = app.process_guide_btn
    assert g4() == "Add the calibration images first." and btn.cget("text") == "Open 1 · Images ›"
    assert app.process_view.placeholder.winfo_manager() == "place", "the empty results table says what to do"
    p = write_tif(tmp_path / "cal.tif", scene(), T0)
    app._add_calibration_images([p])
    assert g4().startswith("Finish the targets first") and btn.cget("text") == "Open 2 · Targets ›"
    complete_targets(app, app.calibration_events[0])
    assert g4().startswith("Choose the survey folder: press Browse") and btn.cget("text") == "Browse…"
    app.show_step(4)
    assert app.process_view.placeholder.winfo_manager() == "place"
    survey = tmp_path / "survey"
    survey.mkdir()
    for k in range(3):
        write_tif(survey / f"s{k}.tif", scene(), T0 + dt.timedelta(minutes=k + 1))
    app.survey_entry.insert(0, str(survey))
    app._on_survey_changed()
    for _ in range(300):
        app.root.update()
        if len(app._survey_timestamps) >= 3 and app._last_preflight is not None:
            break
        time.sleep(0.03)
    assert g4() == "3 images ready. Press Run." and btn.cget("text") == "Run"
    assert not app.process_view.placeholder.winfo_manager(), "pending rows replace the hint"
    monkeypatch.setattr(mb, "askokcancel", lambda *a, **k: True)
    btn.invoke()
    for _ in range(1500):
        app.root.update()
        if app._batch_thread and not app._batch_thread.is_alive() and app.start_btn.instate(['!disabled']):
            break
        time.sleep(0.02)
    assert g4().startswith("Done. The calibrated images") and btn.cget("text") == "Open output folder"
    app.survey_entry.delete(0, "end")                     # another folder: the done state no longer applies
    app._on_survey_changed()
    assert g4().startswith("Choose the survey folder") and app.process_view.placeholder.winfo_manager() == "place"
