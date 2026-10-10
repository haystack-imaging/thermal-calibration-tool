"""Regression tests from the independent science review of 10 October 2026.

Each test pins one way the tool could have handed a scientist a wrong number or a silently wrong file.
"""
import datetime as dt
import json
import math
import os
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

import thermal_calibration_tool as tct
from helpers import T0, TARGETS, apparent, make_event, poly, read_arr, rect, run_job, scene, write_tif

TOOL = Path(__file__).resolve().parent.parent / "thermal_calibration_tool.py"


# ----------------------------------------------------------------------------- 1. NaN never becomes "ok"
def test_target_with_nan_mean_is_not_complete():
    t = tct.CalibrationTarget("x", polygon=poly("T1"), drone_mean=float("nan"), drone_std=0.0, pixel_count=0, ground_truth=20.0)
    assert not t.is_complete()
    t.drone_mean, t.ground_truth = 20.0, float("nan")
    assert not t.is_complete()


def test_regression_rejects_nan_and_inf():
    for bad in ([float("nan"), 2.0, 3.0], [1.0, float("inf"), 3.0]):
        with pytest.raises(ValueError, match="not a number|NaN"):
            tct.compute_linear_regression(bad, [1.0, 2.0, 3.0])
        with pytest.raises(ValueError):
            tct.compute_linear_regression([1.0, 2.0, 3.0], bad)
    with pytest.raises(ValueError):
        tct.compute_single_point(float("nan"), 0.5)


def test_polygon_outside_image_blocks_instead_of_nan_outputs(tmp_path):
    """A project re-run on an image of another size: the polygons fall outside, the fit must not be NaN."""
    cal = write_tif(tmp_path / "cal.tif", scene(), T0)
    ev = make_event(cal, polygons={n: rect(900, 900, 950, 950) for n in TARGETS})
    assert ev.fit is None and all(not t.is_complete() for t in ev.targets)
    sur = tmp_path / "s"
    sur.mkdir()
    write_tif(sur / "a.tif", scene(), T0)
    rep = tct.preflight(tct.BatchJob(tct.MODE_THERMAL, False, [ev], [sur / "a.tif"], tmp_path / "out"))
    assert rep.blocking
    with pytest.raises(ValueError):
        tct.run_batch(tct.BatchJob(tct.MODE_THERMAL, False, [ev], [sur / "a.tif"], tmp_path / "out"),
                      lambda m: None, lambda i, n, name: None, lambda: False)


def test_two_targets_with_equal_references_report_no_r2():
    fit = tct.compute_linear_regression([10.0, 20.0], [5.0, 5.0])
    assert fit.slope == 0.0 and fit.r_squared is None and "n/a" in fit.summary_line("°C")


# ----------------------------------------------------------------------------- 2. nodata
def test_source_nodata_is_not_a_temperature(tmp_path):
    arr = scene()
    arr[:20, :20] = -9999.0
    p = tmp_path / "nd.tif"
    with rasterio.open(p, "w", driver="GTiff", height=160, width=200, count=1, dtype="float32",
                       transform=from_origin(5.0, 52.0, 1e-5, 1e-5), crs="EPSG:4326", nodata=-9999.0) as d:
        d.write(arr, 1)
        d.update_tags(TIFFTAG_DATETIME=T0.strftime("%Y:%m:%d %H:%M:%S"))
    img = tct.load_thermal_image(str(p))
    assert np.isnan(img[:20, :20]).all() and np.isfinite(img[25, 25])
    cal = write_tif(tmp_path / "cal.tif", scene(), T0)
    res, _ = run_job(tct.BatchJob(tct.MODE_THERMAL, False, [make_event(cal)], [p], tmp_path / "out"))
    row = res["rows"][0]
    assert row["status"] == "ok" and row["original_min_celsius"] > -100
    with rasterio.open(tmp_path / "out" / "nd_calibrated.tif") as s:
        assert s.nodata is not None and math.isnan(s.nodata)
        masked = s.read(1, masked=True)
        assert masked.mask[:20, :20].all() and not masked.mask[25, 25]
        assert math.isfinite(float(masked.mean()))


def test_output_without_georeference_gets_none_not_a_fake_transform(tmp_path):
    p = tmp_path / "plain.tif"
    with rasterio.open(p, "w", driver="GTiff", height=160, width=200, count=1, dtype="float32") as d:
        d.write(scene(), 1)
    out = tmp_path / "out.tif"
    tct.save_calibrated_geotiff(scene(), str(out), str(p))
    with rasterio.open(out) as s:
        assert s.crs is None and s.transform.is_identity
        assert "ModelTiepointTag" not in s.tags() and s.nodata is not None and math.isnan(s.nodata)
    geo = write_tif(tmp_path / "geo.tif", scene(), T0, crs="EPSG:32631")
    tct.save_calibrated_geotiff(scene(), str(out), geo)
    with rasterio.open(out) as s:
        assert s.crs.to_epsg() == 32631 and not s.transform.is_identity


# ----------------------------------------------------------------------------- 3. uncertainty from applied events only
def test_uncertainty_and_range_use_the_applied_shot_only(tmp_path):
    # event A: narrow span, tight fit; event B 20 min later: wide span, loose fit
    a = write_tif(tmp_path / "a.tif", scene(), T0)
    ev_a = make_event(a, gt={"T1": 15.1, "T2": 29.9, "T3": 45.1})
    b = write_tif(tmp_path / "b.tif", scene(), T0 + dt.timedelta(minutes=20))
    ev_b = make_event(b, gt={"T1": 10.0, "T2": 33.0, "T3": 50.0})
    assert ev_b.fit.predict_uncertainty(25.0) > 5 * ev_a.fit.predict_uncertainty(25.0)
    sur = tmp_path / "s"
    sur.mkdir()
    write_tif(sur / "early.tif", scene(), T0 - dt.timedelta(minutes=5))     # before A: only A applies
    write_tif(sur / "late.tif", scene(), T0 + dt.timedelta(minutes=30))     # after B: only B applies
    write_tif(sur / "mid.tif", scene(), T0 + dt.timedelta(minutes=10))      # between: both
    res, _ = run_job(tct.BatchJob(tct.MODE_THERMAL, False, [ev_a, ev_b], sorted(sur.glob("*.tif")), tmp_path / "out"))
    rows = {r["input_filename"]: r for r in res["rows"]}
    x0 = float(scene().mean())
    assert rows["early.tif"]["position_vs_calibration"] == "before"
    assert rows["early.tif"]["u_fit_1sigma"] == pytest.approx(ev_a.fit.predict_uncertainty(x0), abs=1e-3)
    assert rows["late.tif"]["u_fit_1sigma"] == pytest.approx(ev_b.fit.predict_uncertainty(x0), abs=1e-3)
    assert rows["mid.tif"]["u_fit_1sigma"] == pytest.approx(max(ev_a.fit.predict_uncertainty(x0), ev_b.fit.predict_uncertainty(x0)), abs=1e-3)


def test_image_at_exact_event_time_is_between_not_outside():
    e0 = tct.CalibrationEvent("a", T0, targets=[]); e0.fit = tct.compute_linear_regression([1, 2, 3], [1, 2, 3])
    e1 = tct.CalibrationEvent("b", T0 + dt.timedelta(minutes=10), targets=[]); e1.fit = tct.compute_linear_regression([1, 2, 3], [2, 3, 4])
    assert tct.coefficients_at(T0, [e0, e1])[2] == "between"
    m, b, pos, _ = tct.coefficients_at(T0 + dt.timedelta(minutes=10), [e0, e1])
    assert pos == "between" and (m, b) == pytest.approx((e1.fit.slope, e1.fit.intercept))
    assert tct.coefficients_at(T0 - dt.timedelta(seconds=1), [e0, e1])[2] == "before"
    assert tct.coefficients_at(T0 + dt.timedelta(minutes=10, seconds=1), [e0, e1])[2] == "after"


# ----------------------------------------------------------------------------- 4. no capture time
def test_missing_capture_time_is_an_error_with_several_shots(tmp_path):
    evs = [make_event(write_tif(tmp_path / f"c{i}.tif", scene(), T0 + dt.timedelta(minutes=10 * i))) for i in range(2)]
    sur = tmp_path / "s"
    sur.mkdir()
    write_tif(sur / "no_time.tif", scene())
    nothing = tct.Timestamp(None, "none", "")
    job = tct.BatchJob(tct.MODE_THERMAL, False, evs, [sur / "no_time.tif"], tmp_path / "out")
    rep = tct.preflight(job, {sur / "no_time.tif": nothing})
    assert rep.blocking and any("no capture time" in t for l, t in rep.lines if l == "block")
    res, _ = run_job(job, {sur / "no_time.tif": nothing})
    assert res["errors"] == 1 and "no capture time" in res["rows"][0]["status"]
    # a single shot needs no time: processed, with an empty timestamp column
    job1 = tct.BatchJob(tct.MODE_THERMAL, False, evs[:1], [sur / "no_time.tif"], tmp_path / "out1")
    res, _ = run_job(job1, {sur / "no_time.tif": nothing})
    assert res["ok"] == 1 and res["rows"][0]["timestamp"] == "" and res["rows"][0]["position_vs_calibration"] == "single"


# ----------------------------------------------------------------------------- 5. parsing
@pytest.mark.parametrize("text", ["2025-07-25", "2025:07:25", "2025-07-25+02:00", "2025", "25.07.2025"])
def test_date_only_strings_are_rejected(text):
    assert tct._parse_datetime_string(text) == (None, "")


def test_two_digit_offset_and_aware_input_become_naive():
    v, tz = tct._parse_datetime_string("2025-07-25 12:40:01+02")
    assert v == dt.datetime(2025, 7, 25, 12, 40, 1) and v.tzinfo is None and tz == "+02"
    v, tz = tct._parse_datetime_string("2025-07-25T12:40:01.5+0200")
    assert v.tzinfo is None and tz == "+0200"


def test_mixed_offsets_are_warned(tmp_path):
    cal = write_tif(tmp_path / "cal.tif", scene(), T0)
    ev = make_event(cal)
    ev.timestamp_tz = "+01:00"
    sur = tmp_path / "s"
    sur.mkdir()
    p = Path(write_tif(sur / "a.tif", scene(), T0 + dt.timedelta(minutes=1)))
    rep = tct.preflight(tct.BatchJob(tct.MODE_THERMAL, False, [ev], [p], tmp_path / "out"),
                        {p: tct.Timestamp(T0 + dt.timedelta(minutes=1), "XMP", "+02:00")})
    assert any("different UTC offsets" in t for l, t in rep.lines if l == "warn")


# ----------------------------------------------------------------------------- 6. polygons
def test_corners_in_any_order_give_the_same_simple_quad():
    square = [(20, 20), (50, 20), (50, 50), (20, 50)]
    bow_tie = [(20, 20), (50, 50), (50, 20), (20, 50)]      # clicked in Z order
    img = scene()
    assert tct.polygon_is_convex(tct.order_corners(bow_tie))
    assert tct.extract_roi_statistics(img, tct.order_corners(bow_tie), 3).count == tct.extract_roi_statistics(img, square, 3).count
    assert not tct.polygon_is_convex(bow_tie)
    pieces = tct.inner_roi_contours(bow_tie, img.shape, 3)
    assert len(pieces) == 2, "a bow-tie erodes to two lobes and both are drawn"
    assert len(tct.inner_roi_contours(square, img.shape, 3)) == 1


# ----------------------------------------------------------------------------- 7. emissivity order
def test_emissivity_fit_in_apparent_space_then_convert(tmp_path):
    """Sensor drift acts on apparent temperature: fit raw sensor vs apparent panel temperature, apply m, b
    to raw pixels, convert with the scene emissivity afterwards. A drifting sensor over panels at ε 0.95
    and a canopy at ε 0.98 must come back to the true canopy temperature."""
    a, c = 0.93, 2.5
    true = np.full((160, 200), 25.0, np.float32)
    eps = np.full((160, 200), 0.98, np.float32)
    for (x, y), t in TARGETS.values():
        true[y:y + 30, x:x + 30] = t
        eps[y:y + 30, x:x + 30] = 0.95
    true[100:150, 20:180] = 30.0
    app_img = np.array([[apparent(float(tv), float(ev)) for tv, ev in zip(rt, re_)] for rt, re_ in zip(true, eps)], np.float32)
    drone = (a * app_img + c).astype(np.float32)
    cal = write_tif(tmp_path / "cal.tif", drone, T0)
    em = tct.EmissivitySettings(True, 0.95, 0.98, -40.0)
    ev = make_event(cal, fit=False)
    assert tct.recompute_fit(ev, False, True, em) is None
    assert ev.fit.slope == pytest.approx(1 / a, rel=2e-3) and ev.fit.intercept == pytest.approx(-c / a, abs=0.1)
    sur = tmp_path / "s"
    sur.mkdir()
    write_tif(sur / "img.tif", drone, T0)
    run_job(tct.BatchJob(tct.MODE_THERMAL, False, [ev], [sur / "img.tif"], tmp_path / "out", em))
    out = read_arr(tmp_path / "out" / "img_calibrated.tif")
    assert abs(float(out[125, 100]) - 30.0) < 0.05, "canopy at its own emissivity"
    assert abs(float(out[35, 95]) - apparent(30.0, 0.95)) < 0.1 or abs(float(out[35, 95]) - 30.0) < 1.6, "panel pixels are converted with the scene ε, not the panel ε"


def test_apparent_and_correction_are_inverse():
    t = np.linspace(-20, 60, 50).astype(np.float32)
    back = tct.apply_emissivity_correction(tct.apparent_temperature(t, 0.9, -30.0), 0.9, -30.0)
    assert np.allclose(back, t, atol=2e-3)
    with pytest.raises(ValueError):
        tct.apply_emissivity_correction(t, 0.0, -30.0)
    e = tct.EmissivitySettings.from_dict({"target_emissivity": 9.5, "scene_emissivity": 0.0, "sky_temperature": -500})
    assert (e.target_emissivity, e.scene_emissivity, e.sky_temperature) == (1.0, 0.5, -100.0)


# ----------------------------------------------------------------------------- 9. project paths
def test_project_resolves_relative_paths_after_the_folder_moved(tmp_path):
    flight = tmp_path / "flight"
    (flight / "cal").mkdir(parents=True)
    cal = write_tif(flight / "cal" / "c.tif", scene(), T0)
    ev = make_event(cal)
    project = tct.events_to_project([ev], tct.MODE_THERMAL, False, tct.EmissivitySettings(), 3, {}, "t", str(flight))
    assert project["calibration_images"][0]["path_relative"] == os.path.join("cal", "c.tif")
    assert project["calibration_images"][0]["fit"]["slope"] == pytest.approx(ev.fit.slope)
    moved = tmp_path / "elsewhere" / "flight"
    shutil.move(str(flight), str(moved))
    events, problems = tct.events_from_project(project, str(moved), 3)
    assert problems == [] and events[0].image_path == str(moved / "cal" / "c.tif")


def test_project_copy_next_to_outputs_reopens_from_there(tmp_path):
    cal = write_tif(tmp_path / "cal.tif", scene(), T0)
    sur = tmp_path / "s"
    sur.mkdir()
    write_tif(sur / "a.tif", scene(), T0)
    ev = make_event(cal)
    project = tct.events_to_project([ev], tct.MODE_THERMAL, False, tct.EmissivitySettings(), 3, {"folder": str(sur)}, "Flight")
    out = tmp_path / "out"
    res, _ = run_job(tct.BatchJob(tct.MODE_THERMAL, False, [ev], [sur / "a.tif"], out, project=project))
    copy = json.loads(Path(res["project"]).read_text(encoding="utf-8"))
    assert copy["survey"]["output_folder"] == str(out.resolve()) and copy["calibration_images"][0]["path_relative"]
    logs = []
    again = tct.run_project_headless(res["project"], output_folder=str(tmp_path / "out2"), log=logs.append)
    assert again["ok"] == 1


# ----------------------------------------------------------------------------- 10. CLI
def test_cli_version_and_missing_project_message():
    r = subprocess.run([sys.executable, str(TOOL), "--version"], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and tct.__version__ in r.stdout + r.stderr
    r = subprocess.run([sys.executable, str(TOOL), "--project", "/nonexistent.calib.json", "--run"], capture_output=True, text=True, timeout=120)
    assert r.returncode != 0 and "not found" in r.stderr and "Traceback" not in r.stderr
