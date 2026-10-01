"""Tests for the calibration tool. Each test is named after the audit finding it guards.

Run:  python3 -m pytest tests -q
GUI tests need a display (they use a hidden Tk root) and are skipped without one.
Integration tests use tests/data/synthetic_flight (python3 tests/make_synthetic_flight.py) and
tests/data/real_m4t (python3 tests/convert_rjpeg.py …) when present.
"""
import datetime as dt
import json
import os
import shutil
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import thermal_calibration_tool as tct  # noqa: E402

DATA = HERE / "data"
T0 = dt.datetime(2026, 6, 1, 10, 0, 0)
TARGETS = {"T1": ((20, 20), 15.0), "T2": ((80, 20), 30.0), "T3": ((140, 20), 45.0)}
SIZE = 30


# ----------------------------------------------------------------------------- helpers
def scene(drift=0.0, gain=1.0, blur=0.0):
    img = np.full((160, 200), 25.0, np.float32)
    for (x, y), t in TARGETS.values():
        img[y:y + SIZE, x:x + SIZE] = t
    img[100:150, 20:180] = 30.0          # "canopy"
    if blur:
        img = cv2.GaussianBlur(img, (0, 0), blur)
    return (gain * img + drift).astype(np.float32)


def write_tif(path, arr, when=None, crs="EPSG:4326"):
    with rasterio.open(path, "w", driver="GTiff", height=arr.shape[0], width=arr.shape[1], count=1, dtype="float32",
                       transform=from_origin(5.0, 52.0, 1e-5, 1e-5), crs=crs) as d:
        d.write(arr, 1)
        if when:
            d.update_tags(TIFFTAG_DATETIME=when.strftime("%Y:%m:%d %H:%M:%S"))
    return str(path)


def poly(name, pad=0):
    (x, y), _ = TARGETS[name]
    return [(x - pad, y - pad), (x + SIZE + pad, y - pad), (x + SIZE + pad, y + SIZE + pad), (x - pad, y + SIZE + pad)]


def make_event(path, img=None, gt=TARGETS, inset=3):
    img = tct.load_thermal_image(path) if img is None else img
    ts = tct.extract_timestamp(path)
    ev = tct.CalibrationEvent(path, ts.value, ts.source, ts.tz, targets=[])
    ev.targets = []
    for i, name in enumerate(gt):
        t = tct.CalibrationTarget(name, tct.COLOR_PALETTE[i], poly(name))
        st = tct.extract_roi_statistics(img, t.polygon, inset)
        t.drone_mean, t.drone_std, t.pixel_count, t.inset_applied = st.mean, st.std, st.count, st.inset_applied
        t.ground_truth = gt[name][1] if not isinstance(gt[name], float) else gt[name]
        ev.targets.append(t)
    tct.recompute_fit(ev, False, True, None)
    return ev


def read_px(path, xy):
    with rasterio.open(path) as s:
        return float(s.read(1)[xy[1], xy[0]])


def run(job, timestamps=None):
    logs = []
    res = tct.run_batch(job, logs.append, lambda i, n, name: None, lambda: False, timestamps)
    return res, logs


@pytest.fixture
def tk_root():
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    root.withdraw()
    yield root
    root.destroy()


@pytest.fixture
def app(tk_root):
    a = tct.ThermalCalibrationApp(tk_root)
    tk_root.update_idletasks()
    return a


# ----------------------------------------------------------------------------- C1
def test_c1_mid_flight_calibration_is_used(tmp_path):
    drifts = {0: 0.0, 10: 3.0, 20: 4.0}
    events = []
    for mnt, d in drifts.items():
        p = write_tif(tmp_path / f"cal_{mnt:02d}.tif", scene(d), T0 + dt.timedelta(minutes=mnt))
        events.append(make_event(p))
    assert [round(e.fit.intercept, 3) for e in events] == [0.0, -3.0, -4.0]
    m, b, pos, pair = tct.coefficients_at(T0 + dt.timedelta(minutes=10), events)
    assert pos == 'between' and abs(b + 3.0) < 1e-6, "the mid event must be hit exactly"
    m, b, pos, _ = tct.coefficients_at(T0 + dt.timedelta(minutes=15), events)
    assert abs(b + 3.5) < 1e-6
    assert tct.coefficients_at(T0 - dt.timedelta(minutes=5), events)[2] == 'before'
    assert tct.coefficients_at(T0 + dt.timedelta(minutes=50), events)[2] == 'after'
    sur = tmp_path / "survey"; sur.mkdir()
    write_tif(sur / "s10.tif", scene(3.0), T0 + dt.timedelta(minutes=10))
    job = tct.BatchJob(tct.MODE_THERMAL, False, events, [sur / "s10.tif"], tmp_path / "out")
    res, _ = run(job)
    assert res['ok'] == 1
    assert abs(read_px(tmp_path / "out" / "s10_calibrated.tif", (100, 125)) - 30.0) < 0.01


# ----------------------------------------------------------------------------- C2
def test_c2_emissivity_is_consistent(tmp_path):
    p = write_tif(tmp_path / "cal.tif", scene(), T0)
    sur = tmp_path / "s"; sur.mkdir()
    write_tif(sur / "copy.tif", scene(), T0)
    em = tct.EmissivitySettings(True, 0.95, 0.95, -40.0)
    ev = make_event(p)
    tct.recompute_fit(ev, False, True, em)
    job = tct.BatchJob(tct.MODE_THERMAL, False, [ev], [sur / "copy.tif"], tmp_path / "out", em)
    run(job)
    out = tmp_path / "out" / "copy_calibrated.tif"
    for name, ((x, y), t) in TARGETS.items():
        assert abs(read_px(out, (x + 15, y + 15)) - t) < 0.02, f"{name}: perfect sensor must come back unchanged"
    # physically consistent case: the sensor reports *apparent* temperatures (ε = 1 conversion) of
    # panels with ε = 0.95 and a canopy with ε = 0.98; the IR gun reports true panel temperatures.
    def apparent(t_true, eps, t_sky=-40.0):
        return ((eps * (t_true + 273.15) ** 4 + (1 - eps) * (t_sky + 273.15) ** 4) ** 0.25) - 273.15
    app_img = np.full((160, 200), apparent(25.0, 0.98), np.float32)
    for (x, y), t in TARGETS.values():
        app_img[y:y + SIZE, x:x + SIZE] = apparent(t, 0.95)
    app_img[100:150, 20:180] = apparent(30.0, 0.98)
    p2 = write_tif(tmp_path / "cal2.tif", app_img, T0)
    sur2 = tmp_path / "s2"; sur2.mkdir()
    write_tif(sur2 / "copy.tif", app_img, T0)
    em2 = tct.EmissivitySettings(True, 0.95, 0.98, -40.0)
    ev2 = make_event(p2)
    tct.recompute_fit(ev2, False, True, em2)
    assert abs(ev2.fit.slope - 1.0) < 1e-3 and abs(ev2.fit.intercept) < 0.05, "panel correction makes the sensor ideal"
    run(tct.BatchJob(tct.MODE_THERMAL, False, [ev2], [sur2 / "copy.tif"], tmp_path / "out2", em2))
    canopy_on = read_px(tmp_path / "out2" / "copy_calibrated.tif", (100, 125))
    assert abs(canopy_on - 30.0) < 0.05, "scene corrected with its own emissivity gives the true temperature"
    tct.recompute_fit(ev2, False, True, None)
    run(tct.BatchJob(tct.MODE_THERMAL, False, [ev2], [sur2 / "copy.tif"], tmp_path / "out3"))
    canopy_off = read_px(tmp_path / "out3" / "copy_calibrated.tif", (100, 125))
    assert 1.0 < canopy_off - 30.0 < 2.5, "without the correction the ELM over-corrects the higher-ε canopy"


# ----------------------------------------------------------------------------- C3
def test_c3_colour_jpeg_is_refused(tmp_path):
    grad = np.tile(np.linspace(20, 40, 200, dtype=np.float32), (160, 1))
    rgb = cv2.applyColorMap(cv2.normalize(grad, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8), cv2.COLORMAP_INFERNO)
    jpg = tmp_path / "DJI_0001_T.JPG"
    cv2.imwrite(str(jpg), rgb)
    info = tct.inspect_image(str(jpg))
    assert info.bands == 3 and not info.is_single_band
    with pytest.raises(ValueError, match="3 bands"):
        tct.load_thermal_image(str(jpg))
    p = write_tif(tmp_path / "cal.tif", scene(), T0)
    ev = make_event(p)
    job = tct.BatchJob(tct.MODE_THERMAL, False, [ev], [jpg], tmp_path / "out")
    rep = tct.preflight(job)
    assert rep.blocking and any("colour" in t for _, t in rep.lines)


def test_c3_8bit_warns(tmp_path):
    cv2.imwrite(str(tmp_path / "eight.tif"), np.full((160, 200), 128, np.uint8))
    p = write_tif(tmp_path / "cal.tif", scene(), T0)
    job = tct.BatchJob(tct.MODE_THERMAL, False, [make_event(p)], [tmp_path / "eight.tif"], tmp_path / "out")
    rep = tct.preflight(job)
    assert not rep.blocking and any("8-bit" in t for l, t in rep.lines if l == 'warn')


# ----------------------------------------------------------------------------- C4
def test_c4_comma_decimal_and_zero():
    assert tct.parse_number("15,0") == 15.0
    assert tct.parse_number(" 15.5 ") == 15.5
    assert tct.parse_number("0") == 0.0
    assert tct.parse_number("") is None
    with pytest.raises(ValueError):
        tct.parse_number("abc")
    t = tct.CalibrationTarget("ice", polygon=poly("T1"), drone_mean=0.4, ground_truth=0.0)
    assert t.is_complete(), "0.0 C (ice bath) is a valid reference"
    t.ground_truth = None
    assert not t.is_complete()


def test_c4_gui_entry(app, tmp_path):
    p = write_tif(tmp_path / "cal.tif", scene(), T0)
    app._add_calibration_images([p])
    row = app.target_rows[0]
    row.gt_var.set("15,0")
    assert app.calibration_events[0].targets[0].ground_truth == 15.0
    row.gt_var.set("abc")
    assert app.calibration_events[0].targets[0].ground_truth is None
    assert row.gt_entry.cget('bg') == '#f5b7b1'
    row.gt_var.set("0")
    assert app.calibration_events[0].targets[0].ground_truth == 0.0


# ----------------------------------------------------------------------------- C5
def test_c5_mode_switch_recomputes_all_and_keeps_targets(app, tmp_path):
    p1 = write_tif(tmp_path / "a.tif", scene() * 10, T0)
    p2 = write_tif(tmp_path / "b.tif", scene(2.0) * 11, T0 + dt.timedelta(minutes=20))
    app.sensor_mode.set(tct.MODE_MSI); app.msi_single_point.set(False); app._on_mode_switch()
    app._add_calibration_images([p1, p2])
    for i in range(2):
        app._load_image(i)
        ev = app.calibration_events[i]
        img = app.current_image
        for j, name in enumerate(TARGETS):
            t = ev.targets[j] if j < len(ev.targets) else ev.add_target()
            t.polygon = poly(name)
            st = tct.extract_roi_statistics(img, t.polygon, 3)
            t.drone_mean, t.drone_std, t.pixel_count = st.mean, st.std, st.count
            t.ground_truth = TARGETS[name][1] / 100
        app._recompute_fit(ev)
    assert all(e.fit is not None and not e.fit.single_point for e in app.calibration_events)
    app.msi_single_point.set(True); app._on_single_point_toggle()
    assert all(e.fit is not None and e.fit.single_point for e in app.calibration_events), "every event recomputed"
    assert all(len(e.targets) == 3 for e in app.calibration_events), "targets are hidden, not deleted"
    assert len(app.target_rows) == 1
    app.msi_single_point.set(False); app._on_single_point_toggle()
    assert all(not e.fit.single_point for e in app.calibration_events) and len(app.target_rows) == 3


# ----------------------------------------------------------------------------- C6
def test_c6_timestamp_source_is_recorded(tmp_path):
    p = write_tif(tmp_path / "no_meta.tif", scene())
    os.utime(p, (1e9, 1e9))
    ts = tct.extract_timestamp(p)
    assert ts.source == "file mtime" and not ts.reliable
    p2 = write_tif(tmp_path / "tag.tif", scene(), T0)
    assert tct.extract_timestamp(p2).source == "TIFF tag"
    assert tct._parse_datetime_string("2026-06-01T10:00:00+02:00") == (T0, "+02:00")
    assert tct._parse_datetime_string("2026-06-01T08:00:00Z") == (T0 - dt.timedelta(hours=2), "Z")
    assert tct._parse_datetime_string("2026:06:01 10:00:00") == (T0, "")
    cal = write_tif(tmp_path / "cal.tif", scene(), T0)
    job = tct.BatchJob(tct.MODE_THERMAL, False, [make_event(cal)], [Path(p)], tmp_path / "out")
    rep = tct.preflight(job)
    assert any("modification" in t for l, t in rep.lines if l == 'warn')
    res, _ = run(job, rep.timestamps)
    assert res['rows'][0]['timestamp_source'] == 'file mtime'


@pytest.mark.skipif(not shutil.which("exiftool"), reason="exiftool needed to write XMP")
def test_c6_xmp_with_offset_and_utc(tmp_path):
    import subprocess
    p = write_tif(tmp_path / "xmp.tif", scene())
    subprocess.run(["exiftool", "-overwrite_original", "-q", "-XMP:CreateDate=2026:06:01 10:00:00+02:00", p], check=True)
    ts = tct.extract_timestamp(p)
    assert (ts.value, ts.source, ts.tz) == (T0, "XMP", "+02:00")
    z = write_tif(tmp_path / "z.tif", scene())
    subprocess.run(["exiftool", "-overwrite_original", "-q", "-XMP:CreateDate=2026:06:01 08:00:00Z", z], check=True)
    assert tct.extract_timestamp(z).tz == "Z"


# ----------------------------------------------------------------------------- H1
def test_h1_fixed_pixel_inset_removes_edge_bias():
    img = scene(blur=1.2)
    old_style = tct.extract_roi_statistics(img, poly("T1", pad=2), inset_px=0)
    new = tct.extract_roi_statistics(img, poly("T1", pad=2), inset_px=3)
    assert abs(old_style.mean - 15.0) > 1.0, "without inset the 2 px click error biases the mean"
    assert abs(new.mean - 15.0) < 0.5
    assert new.count > 0 and new.inset_applied
    tiny = tct.extract_roi_statistics(img, [(20, 20), (24, 20), (24, 24), (20, 24)], inset_px=3)
    assert not tiny.inset_applied and tiny.count == 25
    assert tct.inner_roi_contour(poly("T1"), img.shape, 3)


# ----------------------------------------------------------------------------- H2
def test_h2_zoom_canvas_transforms(tk_root):
    clicks = []
    zc = tct.ZoomCanvas(tk_root, on_click=lambda x, y: clicks.append((x, y)))
    zc.canvas.config(width=800, height=600)
    zc.canvas.pack()
    tk_root.update_idletasks()
    zc.set_image(np.zeros((160, 200, 3), np.uint8))
    assert zc.scale > 1.0, "a small image is enlarged to fit"
    cx, cy = zc.to_canvas(50, 40)
    assert np.allclose(zc.to_image(cx, cy), (50, 40))
    ix, iy = zc.to_image(300, 200)
    zc.zoom_at(300, 200, 2.0)
    assert np.allclose(zc.to_image(300, 200), (ix, iy)), "zoom keeps the point under the cursor fixed"
    zc.one_to_one()
    assert zc.scale == 1.0
    zc._on_click(SimpleNamespace(x=-50, y=-50))
    assert clicks == [], "clicks outside the image are ignored (M8)"
    zc.fit()
    px, py = zc.to_canvas(10, 10)
    zc._on_click(SimpleNamespace(x=px, y=py))
    assert clicks == [(10, 10)]


# ----------------------------------------------------------------------------- H3
def test_h3_fit_statistics():
    fit = tct.compute_linear_regression([33.66, 28.74, 35.12], [34.3, 29.1, 36.6])
    assert abs(fit.slope - 1.1454) < 1e-3 and abs(fit.intercept + 3.9006) < 1e-3
    assert abs(fit.r_squared - 0.9930) < 1e-3
    assert abs(fit.se_slope - 0.0962) < 1e-3 and abs(fit.se_intercept - 3.138) < 1e-2
    assert abs(fit.rmse - 0.2626) < 1e-3 and abs(fit.loo_rmse - 1.573) < 0.01
    assert abs(fit.predict_uncertainty(31.0) - 0.545) < 1e-2
    two = tct.compute_linear_regression([20.0, 30.0], [5.0, 60.0])
    assert two.r_squared == pytest.approx(1.0) and two.rmse is None and two.predict_uncertainty(25) is None
    with pytest.raises(ValueError):
        tct.compute_linear_regression([20.0, 20.0], [1.0, 2.0])


# ----------------------------------------------------------------------------- H4
def test_h4_metadata_survives(tmp_path):
    src = write_tif(tmp_path / "src.tif", scene(), T0, crs="EPSG:32631")
    out = tmp_path / "out.tif"
    tct.save_calibrated_geotiff(scene(), str(out), src, {"CALIBRATION_SLOPE": "1.0"})
    with rasterio.open(out) as s:
        assert s.tags()["TIFFTAG_DATETIME"] == "2026:06:01 10:00:00"
        assert s.tags()["CALIBRATION_SLOPE"] == "1.0"
        assert s.crs.to_epsg() == 32631
        assert s.profile["compress"].lower() == "lzw"
    assert tct.extract_timestamp(str(out)).source == "TIFF tag", "re-running on an output still finds the time"


@pytest.mark.skipif(not shutil.which("exiftool"), reason="exiftool")
def test_h4_exif_gps_copied_with_exiftool(tmp_path):
    import subprocess
    src = write_tif(tmp_path / "src.tif", scene(), T0)
    subprocess.run(["exiftool", "-overwrite_original", "-q", "-EXIF:DateTimeOriginal=2026:06:01 10:00:00",
                    "-GPSLatitude=51.98", "-GPSLatitudeRef=N", "-XMP:CreateDate=2026:06:01 10:00:00+02:00", src], check=True)
    out = tmp_path / "out.tif"
    tct.save_calibrated_geotiff(scene(), str(out), src)
    r = subprocess.run(["exiftool", "-s", "-s", "-s", "-EXIF:DateTimeOriginal", "-GPSLatitude", "-XMP:CreateDate", str(out)],
                       capture_output=True, text=True).stdout
    assert "2026:06:01 10:00:00" in r and "51" in r and "+02:00" in r
    assert tct.extract_timestamp(str(out)).source == "XMP"
    with rasterio.open(out) as s:
        assert s.read(1).dtype == np.float32 and s.tags()["TIFFTAG_DATETIME"]


# ----------------------------------------------------------------------------- H5
def test_h5_summary_is_utf8(tmp_path):
    p = write_tif(tmp_path / "cal.tif", scene(), T0)
    sur = tmp_path / "s"; sur.mkdir()
    write_tif(sur / "a.tif", scene(), T0)
    em = tct.EmissivitySettings(True, 0.95, 0.98, -40)
    ev = make_event(p); tct.recompute_fit(ev, False, True, em)
    run(tct.BatchJob(tct.MODE_THERMAL, False, [ev], [sur / "a.tif"], tmp_path / "out", em))
    text = (tmp_path / "out" / "processing_summary.txt").read_bytes().decode("utf-8")
    assert "ε" in text and "±" not in text[:0]


# ----------------------------------------------------------------------------- H6
def test_h6_license_present():
    assert "GNU GENERAL PUBLIC LICENSE" in (HERE.parent / "LICENSE").read_text()


# ----------------------------------------------------------------------------- H7
def test_h7_switching_image_cancels_drawing(app, tmp_path):
    a = write_tif(tmp_path / "a.tif", scene(), T0)
    b = write_tif(tmp_path / "b.tif", scene(), T0 + dt.timedelta(minutes=20))
    app._add_calibration_images([a, b])
    ev_a, ev_b = app.calibration_events
    ev_a.targets[0].polygon = poly("T1")
    app._start_polygon_selection(0)
    app._on_image_click(20, 20); app._on_image_click(50, 20)
    assert app.drawing_polygon and ev_a.targets[0].polygon == poly("T1"), "old polygon kept while drawing"
    app.calib_listbox.selection_clear(0, 'end'); app.calib_listbox.selection_set(1)
    app._on_calib_select(None)
    assert not app.drawing_polygon and app.current_event_index == 1
    app._on_image_click(170, 50); app._on_image_click(140, 50)
    assert ev_b.targets[0].polygon == [] and ev_a.targets[0].polygon == poly("T1")
    app._start_polygon_selection(0)
    app._on_image_click(1, 1)
    app._cancel_drawing()
    assert not app.drawing_polygon and ev_b.targets[0].polygon == []
    app._start_polygon_selection(0)
    for pt in poly("T1"):
        app._on_image_click(*pt)
    assert ev_b.targets[0].polygon == poly("T1") and abs(ev_b.targets[0].drone_mean - 15.0) < 0.01


# ----------------------------------------------------------------------------- M1 / M2 / M6
def test_m1_batch_runs_in_worker_thread(app, tmp_path):
    cal = write_tif(tmp_path / "cal.tif", scene(), T0)
    sur = tmp_path / "s"; sur.mkdir()
    for i in range(3):
        write_tif(sur / f"s{i}.tif", scene(1.0), T0 + dt.timedelta(minutes=i))
    app._add_calibration_images([cal])
    ev = app.calibration_events[0]
    for j, name in enumerate(TARGETS):
        t = ev.targets[j] if j < len(ev.targets) else ev.add_target()
        t.polygon = poly(name)
        st = tct.extract_roi_statistics(app.current_image, t.polygon, 3)
        t.drone_mean, t.drone_std, t.pixel_count = st.mean, st.std, st.count
        t.ground_truth = TARGETS[name][1]
    app._recompute_fit(ev)
    app.survey_entry.insert(0, str(sur)); app.output_entry.insert(0, str(tmp_path / "out"))
    import tkinter.messagebox as mb
    app.root.after(0, lambda: None)
    mb.askokcancel = lambda *a, **k: True
    app._start_batch_processing()
    assert app._batch_thread is not None and app.start_btn.instate(['disabled'])
    for _ in range(300):
        app.root.update()
        if not app._batch_thread.is_alive() and app.start_btn.instate(['!disabled']):
            break
        time.sleep(0.02)
    assert app.start_btn.instate(['!disabled'])
    assert sorted(p.name for p in (tmp_path / "out").glob("*_calibrated.tif")) == ["s0_calibrated.tif", "s1_calibrated.tif", "s2_calibrated.tif"]
    assert (tmp_path / "out" / "untitled_project.calib.json").exists(), "project saved next to the outputs"
    assert "Done" in app.progress_label.cget("text")


def test_m2_project_roundtrip_and_cli(tmp_path):
    cal = write_tif(tmp_path / "cal.tif", scene(1.5), T0)        # sensor reads 1.5 C too warm
    sur = tmp_path / "s"; sur.mkdir()
    write_tif(sur / "a.tif", scene(1.5), T0 + dt.timedelta(minutes=1))
    ev = make_event(cal)
    project = tct.events_to_project([ev], tct.MODE_THERMAL, False, tct.EmissivitySettings(), 3,
                                    {"folder": str(sur), "output_folder": str(tmp_path / "out")})
    pp = tmp_path / "flight.calib.json"
    pp.write_text(json.dumps(project), encoding="utf-8")
    events, problems = tct.events_from_project(json.loads(pp.read_text()), str(tmp_path), 3)
    assert not problems and events[0].targets[0].drone_mean == pytest.approx(ev.targets[0].drone_mean)
    logs = []
    res = tct.run_project_headless(str(pp), log=logs.append)
    assert res['ok'] == 1
    assert abs(read_px(tmp_path / "out" / "a_calibrated.tif", (100, 125)) - 30.0) < 0.01
    assert tct.main(["--project", str(pp), "--run", "--output", str(tmp_path / "out2")]) is None
    assert (tmp_path / "out2" / "processing_log.csv").exists()


def test_m6_output_names_and_overwrite_warning(tmp_path):
    a, b = tmp_path / "X.tif", tmp_path / "X.png"
    names = tct.output_filenames([a, b, tmp_path / "Y.tif"])
    assert names[a] != names[b] and names[tmp_path / "Y.tif"] == "Y_calibrated.tif"
    cal = write_tif(tmp_path / "cal.tif", scene(), T0)
    sur = tmp_path / "s"; sur.mkdir()
    write_tif(sur / "a.tif", scene(), T0)
    write_tif(sur / "a_calibrated.tif", scene(), T0)
    files = tct.collect_survey_files(str(sur), None, "", [cal])
    assert [f.name for f in files] == ["a.tif"], "earlier outputs are never re-calibrated"
    out = tmp_path / "out"; out.mkdir()
    write_tif(out / "a_calibrated.tif", scene(), T0)
    job = tct.BatchJob(tct.MODE_THERMAL, False, [make_event(cal)], files, out, skip_existing=True)
    rep = tct.preflight(job)
    assert any("already exist" in t and "skipped" in t for _, t in rep.lines)
    res, _ = run(job, rep.timestamps)
    assert res['skipped'] == 1


def test_preflight_window_warnings(tmp_path):
    evs = [make_event(write_tif(tmp_path / f"c{i}.tif", scene(i), T0 + dt.timedelta(minutes=10 * i))) for i in range(2)]
    sur = tmp_path / "s"; sur.mkdir()
    write_tif(sur / "early.tif", scene(), T0 - dt.timedelta(minutes=3))
    write_tif(sur / "mid.tif", scene(), T0 + dt.timedelta(minutes=5))
    write_tif(sur / "late.tif", scene(), T0 + dt.timedelta(minutes=30))
    job = tct.BatchJob(tct.MODE_THERMAL, False, evs, sorted(sur.glob("*.tif")), tmp_path / "out")
    rep = tct.preflight(job)
    txt = rep.text()
    assert "1 image(s) taken before" in txt and "1 image(s) taken after" in txt
    res, _ = run(job, rep.timestamps)
    pos = {r['input_filename']: r['position_vs_calibration'] for r in res['rows']}
    assert pos == {"early.tif": "before", "mid.tif": "between", "late.tif": "after"}
    assert all(r['u_fit_1sigma'] is not None for r in res['rows'])


# ----------------------------------------------------------------------------- integration on generated data
SYN = DATA / "synthetic_flight"


@pytest.mark.skipif(not (SYN / "ground_truth.json").exists(), reason="run tests/make_synthetic_flight.py first")
def test_synthetic_flight_recovers_the_sensor_model(tmp_path):
    truth = json.loads((SYN / "ground_truth.json").read_text())
    events = []
    for rec in [r for r in truth["images"] if r["kind"] == "calibration"]:
        path = str(SYN / "calibration" / rec["file"])
        img = tct.load_thermal_image(path)
        ts = tct.extract_timestamp(path)
        assert ts.source == "XMP" and ts.tz == "+02:00"
        ev = tct.CalibrationEvent(path, ts.value, ts.source, ts.tz, targets=[])
        ev.targets = []
        for i, (name, (cx, cy, hs)) in enumerate(rec["panel_px"].items()):
            # a realistic, slightly sloppy polygon: corners 2 px outside the panel
            pad = 2
            t = tct.CalibrationTarget(name, tct.COLOR_PALETTE[i], [(cx - hs - pad, cy - hs - pad), (cx + hs + pad, cy - hs - pad),
                                                                  (cx + hs + pad, cy + hs + pad), (cx - hs - pad, cy + hs + pad)])
            st = tct.extract_roi_statistics(img, t.polygon, 3)
            t.drone_mean, t.drone_std, t.pixel_count, t.inset_applied = st.mean, st.std, st.count, st.inset_applied
            t.ground_truth = rec["ir_gun_readings_C"][name]
            ev.targets.append(t)
        tct.recompute_fit(ev, False, True, None)
        # the simulated IR gun has 0.3 C noise over a 23 C span, so m is only known to ~0.03 per shot;
        # the real check is the error on the survey images below
        assert abs(ev.fit.slope - rec["expected_m"]) < 0.1, (ev.fit.slope, rec["expected_m"])
        assert abs(ev.fit.intercept - rec["expected_b"]) < 3.0
        assert ev.fit.rmse is not None and ev.fit.rmse < 0.6
        events.append(ev)
    files = tct.collect_survey_files(str(SYN / "survey"), None, "", [e.image_path for e in events])
    job = tct.BatchJob(tct.MODE_THERMAL, False, events, files, tmp_path / "out")
    rep = tct.preflight(job)
    assert not rep.blocking and len(rep.files) == 24
    res, _ = run(job, rep.timestamps)
    assert res['ok'] == 24
    errs = []
    for rec in [r for r in truth["images"] if r["kind"] == "survey"]:
        true = np.load(SYN / "survey" / rec["true_npy"])
        with rasterio.open(tmp_path / "out" / rec["file"].replace(".tif", "_calibrated.tif")) as s:
            cal = s.read(1)
        inner = (slice(40, 472), slice(40, 600))
        errs.append(float(np.mean(cal[inner]) - np.mean(true[inner])))
    assert max(abs(e) for e in errs) < 0.6, errs       # blur + vignetting + noise remain; drift is gone
    assert np.mean(np.abs(errs)) < 0.3
    row = res['rows'][0]
    assert row['timestamp_source'] == 'XMP' and row['u_fit_1sigma'] is not None


REAL = DATA / "real_m4t"


@pytest.mark.skipif(not list(REAL.glob("*_T_deg.tif")) if REAL.exists() else True, reason="run tests/convert_rjpeg.py first")
def test_real_m4t_files_load_and_process(tmp_path):
    files = sorted(REAL.glob("*_T_deg.tif"))
    for f in files:
        info = tct.inspect_image(str(f))
        assert info.is_single_band and info.is_float and (info.width, info.height) == (640, 512)
        assert tct.extract_timestamp(str(f)).source in ("EXIF", "XMP", "TIFF tag")
    img = tct.load_thermal_image(str(files[3]))      # the warmest scene; use two patches as mock panels
    ev = tct.CalibrationEvent(str(files[3]), tct.extract_timestamp(str(files[3])).value, targets=[])
    ev.targets = []
    for i, (x, y) in enumerate([(100, 100), (300, 250), (500, 400)]):
        t = tct.CalibrationTarget(f"T{i + 1}", tct.COLOR_PALETTE[i], [(x, y), (x + 40, y), (x + 40, y + 40), (x, y + 40)])
        st = tct.extract_roi_statistics(img, t.polygon, 3)
        t.drone_mean, t.drone_std, t.pixel_count = st.mean, st.std, st.count
        t.ground_truth = round(st.mean * 1.02 + 0.5, 1)
        ev.targets.append(t)
    tct.recompute_fit(ev, False, True, None)
    assert ev.fit is not None and 0.9 < ev.fit.slope < 1.1
    job = tct.BatchJob(tct.MODE_THERMAL, False, [ev], [f for f in files if f != files[3]], tmp_path / "out")
    rep = tct.preflight(job)
    res, _ = run(job, rep.timestamps)
    assert res['ok'] == 4
    for r in res['rows']:
        assert r['status'] == 'ok'
