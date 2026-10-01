"""Numerical correctness of the science core.

Every statistic the tool reports is checked against an independent closed-form computation, and the
calibration chain is checked for exact recovery of known sensor models, invariances and determinism.
These tests contain no Tk.
"""
import datetime as dt
import json
import math
from pathlib import Path

import numpy as np
import pytest

import thermal_calibration_tool as tct
from helpers import (T0, TARGETS, apparent, build_golden_flight, make_event, poly, read_arr, rect,
                     run_golden_pipeline, run_job, scene, write_tif)

HERE = Path(__file__).resolve().parent


# ----------------------------------------------------------------------------- regression statistics
def closed_form(x, y):
    """Ordinary least squares with the textbook formulas (Draper & Smith), independent of scipy."""
    x, y = np.asarray(x, float), np.asarray(y, float)
    n = len(x)
    xm, ym = x.mean(), y.mean()
    sxx = ((x - xm) ** 2).sum()
    sxy = ((x - xm) * (y - ym)).sum()
    m = sxy / sxx
    b = ym - m * xm
    resid = y - (m * x + b)
    sse = (resid ** 2).sum()
    sst = ((y - ym) ** 2).sum()
    out = {"m": m, "b": b, "r2": 1 - sse / sst, "resid": resid, "rmse": math.sqrt(sse / n)}
    if n > 2:
        s = math.sqrt(sse / (n - 2))
        out.update({"s": s, "se_m": s / math.sqrt(sxx), "se_b": s * math.sqrt(1 / n + xm ** 2 / sxx), "sxx": sxx, "xm": xm})
    return out


@pytest.mark.parametrize("seed", range(15))
def test_regression_matches_closed_form(seed):
    rng = np.random.default_rng(seed)
    n = int(rng.integers(3, 9))
    x = rng.uniform(5, 60, n)
    y = rng.uniform(0.8, 1.2) * x + rng.uniform(-8, 8) + rng.normal(0, 0.4, n)
    fit = tct.compute_linear_regression(list(x), list(y))
    ref = closed_form(x, y)
    assert fit.n == n
    assert fit.slope == pytest.approx(ref["m"], rel=1e-10)
    assert fit.intercept == pytest.approx(ref["b"], rel=1e-10, abs=1e-10)
    assert fit.r_squared == pytest.approx(ref["r2"], abs=1e-10)
    assert np.allclose(fit.residuals, ref["resid"], atol=1e-10)
    assert fit.rmse == pytest.approx(ref["rmse"], rel=1e-10)
    assert fit.resid_std == pytest.approx(ref["s"], rel=1e-10)
    assert fit.se_slope == pytest.approx(ref["se_m"], rel=1e-8)
    assert fit.se_intercept == pytest.approx(ref["se_b"], rel=1e-8)
    assert fit.x_mean == pytest.approx(ref["xm"]) and fit.sxx == pytest.approx(ref["sxx"])
    assert (fit.x_min, fit.x_max) == (x.min(), x.max())
    # prediction uncertainty: s * sqrt(1 + 1/n + (x0 - xm)^2 / sxx)
    for x0 in (x.min() - 10, ref["xm"], x.max() + 10):
        want = ref["s"] * math.sqrt(1 + 1 / n + (x0 - ref["xm"]) ** 2 / ref["sxx"])
        assert fit.predict_uncertainty(x0) == pytest.approx(want, rel=1e-10)
    assert fit.predict_uncertainty(ref["xm"]) <= fit.predict_uncertainty(x.max() + 10), "uncertainty grows away from x̄"
    # leave-one-out, by hand
    loo = []
    for i in range(n):
        keep = np.arange(n) != i
        r = closed_form(x[keep], y[keep])
        loo.append(y[i] - (r["m"] * x[i] + r["b"]))
    assert fit.loo_rmse == pytest.approx(math.sqrt(np.mean(np.square(loo))), rel=1e-10)


def test_regression_two_points_is_exact_and_undiagnosable():
    fit = tct.compute_linear_regression([10.0, 20.0], [12.0, 24.0])
    assert fit.slope == pytest.approx(1.2) and fit.intercept == pytest.approx(0.0, abs=1e-12)
    assert fit.r_squared == pytest.approx(1.0) and fit.n == 2
    assert fit.rmse is None and fit.resid_std is None and fit.se_slope is None and fit.loo_rmse is None
    assert fit.predict_uncertainty(15.0) is None
    assert np.allclose(fit.residuals, [0, 0], atol=1e-12)


def test_regression_rejects_degenerate_input():
    with pytest.raises(ValueError):
        tct.compute_linear_regression([1.0], [1.0])
    with pytest.raises(ValueError):
        tct.compute_linear_regression([5.0, 5.0, 5.0], [1.0, 2.0, 3.0])


@pytest.mark.parametrize("dn,rho", [(1000.0, 0.5), (2.5, 0.18), (65535.0, 0.99)])
def test_single_point_coefficient(dn, rho):
    fit = tct.compute_single_point(dn, rho)
    assert fit.single_point and fit.slope == pytest.approx(rho / dn, rel=1e-12) and fit.intercept == 0.0
    assert fit.predict(dn) == pytest.approx(rho) and fit.predict_uncertainty(dn) is None
    with pytest.raises(ValueError):
        tct.compute_single_point(0.0, 0.5)


# ----------------------------------------------------------------------------- exact recovery
@pytest.mark.parametrize("seed", range(10))
def test_linear_sensor_is_recovered_exactly(seed, tmp_path):
    """A noise-free sensor T_drone = a*T + c must give m = 1/a, b = -c/a and restore the scene exactly."""
    rng = np.random.default_rng(seed)
    a, c = float(rng.uniform(0.85, 1.15)), float(rng.uniform(-6, 6))
    temps = sorted(rng.uniform(5, 55, 3))
    gt = dict(zip(("T1", "T2", "T3"), temps))
    true = np.full((160, 200), 25.0, np.float32)
    for name, t in gt.items():
        (x, y), _ = TARGETS[name]
        true[y:y + 30, x:x + 30] = t
    true[100:150, 20:180] = float(rng.uniform(10, 50))
    drone = (a * true + c).astype(np.float32)
    cal = write_tif(tmp_path / "cal.tif", drone, T0)
    ev = make_event(cal, gt=gt)
    # images are float32: temperatures near 50 C carry an ulp of ~4e-6, which is the floor here
    assert ev.fit.slope == pytest.approx(1 / a, rel=1e-6)
    assert ev.fit.intercept == pytest.approx(-c / a, rel=1e-5, abs=1e-4)
    sur = tmp_path / "s"
    sur.mkdir()
    write_tif(sur / "img.tif", drone, T0)
    res, _ = run_job(tct.BatchJob(tct.MODE_THERMAL, False, [ev], [sur / "img.tif"], tmp_path / "out"))
    out = read_arr(tmp_path / "out" / "img_calibrated.tif")
    assert np.allclose(out, true, atol=2e-4), "every pixel, not just the targets"
    row = res["rows"][0]
    assert row["formula_applied"].startswith("Out = ") and row["status"] == "ok"
    assert abs(row["calibrated_mean_celsius"] - float(true.mean())) < 1e-3


def test_batch_applies_formula_pixelwise_and_keeps_nan(tmp_path):
    cal = write_tif(tmp_path / "cal.tif", scene(2.0), T0)
    ev = make_event(cal)
    m, b = ev.fit.slope, ev.fit.intercept
    img = scene(2.0)
    img[5:9, 5:9] = np.nan
    sur = tmp_path / "s"
    sur.mkdir()
    write_tif(sur / "nan.tif", img, T0)
    res, _ = run_job(tct.BatchJob(tct.MODE_THERMAL, False, [ev], [sur / "nan.tif"], tmp_path / "out"))
    out = read_arr(tmp_path / "out" / "nan_calibrated.tif")
    finite = np.isfinite(img)
    assert np.array_equal(np.isnan(out), ~finite), "NaN stays NaN and nothing else becomes NaN"
    assert np.allclose(out[finite], m * img[finite] + b, atol=1e-4)
    row = res["rows"][0]
    assert math.isfinite(row["original_mean_celsius"]) and math.isfinite(row["calibrated_mean_celsius"])


def test_roi_statistics_ignore_nan():
    img = scene()
    img[25, 25] = np.nan
    st = tct.extract_roi_statistics(img, poly("T1"), 3)
    assert st.mean == pytest.approx(15.0) and st.std == 0.0


# ----------------------------------------------------------------------------- ROI geometry
@pytest.mark.parametrize("seed", range(10))
def test_roi_mean_and_count_are_exact_on_constant_patches(seed):
    """A constant patch gives its value exactly, and the eroded count follows from the geometry:
    fillPoly includes the boundary, erosion by k removes k pixels on each side."""
    rng = np.random.default_rng(seed)
    img = np.full((200, 240), 20.0, np.float32)
    w, h = int(rng.integers(12, 60)), int(rng.integers(12, 60))
    x0, y0 = int(rng.integers(5, 240 - w - 5)), int(rng.integers(5, 200 - h - 5))
    val = float(rng.uniform(-10, 60))
    img[y0:y0 + h + 1, x0:x0 + w + 1] = val
    inset = int(rng.integers(0, 5))
    st = tct.extract_roi_statistics(img, rect(x0, y0, x0 + w, y0 + h), inset)
    assert st.mean == pytest.approx(val, abs=1e-5) and st.std == pytest.approx(0.0, abs=1e-5)
    assert st.count == (w + 1 - 2 * inset) * (h + 1 - 2 * inset) and st.inset_applied


def test_roi_inset_falls_back_when_polygon_too_small():
    img = scene()
    st = tct.extract_roi_statistics(img, rect(20, 20, 24, 24), 3)
    assert not st.inset_applied and st.count == 25 and st.mean == pytest.approx(15.0)
    assert tct.inner_roi_contour(rect(20, 20, 24, 24), img.shape, 3) == []


def test_roi_polygon_partly_outside_image_is_clipped():
    img = scene()
    st = tct.extract_roi_statistics(img, rect(-10, -10, 20, 20), 0)
    assert st.count == 21 * 21 and st.mean == pytest.approx(25.0 * (21 * 21 - 1) / (21 * 21) + 15.0 / (21 * 21), abs=1e-4)


def test_roi_inset_reduces_edge_bias_monotonically():
    img = scene(blur=1.5)
    biases = [abs(tct.extract_roi_statistics(img, poly("T1", pad=2), k).mean - 15.0) for k in (0, 1, 2, 3, 4)]
    assert all(b2 <= b1 + 1e-9 for b1, b2 in zip(biases, biases[1:])), biases
    assert biases[0] > 1.0, "a 2 px click error on a blurred edge costs more than a degree"
    assert biases[3] < 0.4 * biases[0], "the default 3 px inset removes most of it"


# ----------------------------------------------------------------------------- interpolation
@pytest.mark.parametrize("seed", range(8))
def test_interpolation_properties(seed):
    rng = np.random.default_rng(seed)
    k = int(rng.integers(2, 6))
    minutes = sorted(set(int(v) for v in rng.integers(0, 120, k)))
    events = []
    for mnt in minutes:
        e = tct.CalibrationEvent(f"e{mnt}", T0 + dt.timedelta(minutes=mnt), targets=[])
        e.fit = tct.compute_linear_regression([20, 30, 40], list(rng.uniform(0.9, 1.1) * np.array([20, 30, 40]) + rng.uniform(-3, 3)))
        events.append(e)
    shuffled = list(events)
    rng.shuffle(shuffled)
    for e in events:        # exact at the event times, independent of list order
        m, b, pos, _ = tct.coefficients_at(e.timestamp, shuffled)
        assert (m, b) == pytest.approx((e.fit.slope, e.fit.intercept))
    if len(events) > 1:
        e0, e1 = events[0], events[1]
        mid = e0.timestamp + (e1.timestamp - e0.timestamp) / 2
        m, b, pos, pair = tct.coefficients_at(mid, shuffled)
        assert pos == "between" and pair == (e0, e1)
        assert m == pytest.approx((e0.fit.slope + e1.fit.slope) / 2) and b == pytest.approx((e0.fit.intercept + e1.fit.intercept) / 2)
        m, b, pos, _ = tct.coefficients_at(events[0].timestamp - dt.timedelta(hours=1), shuffled)
        assert pos == "before" and (m, b) == pytest.approx((events[0].fit.slope, events[0].fit.intercept))
        m, b, pos, _ = tct.coefficients_at(events[-1].timestamp + dt.timedelta(hours=1), shuffled)
        assert pos == "after" and (m, b) == pytest.approx((events[-1].fit.slope, events[-1].fit.intercept))
    else:
        assert tct.coefficients_at(T0, events)[2] == "single"


def test_interpolation_ignores_unfitted_events_and_tolerates_duplicates():
    e0 = tct.CalibrationEvent("a", T0, targets=[]); e0.fit = tct.compute_linear_regression([1, 2], [1, 2])
    e1 = tct.CalibrationEvent("b", T0 + dt.timedelta(minutes=10), targets=[])          # no fit
    e2 = tct.CalibrationEvent("c", T0 + dt.timedelta(minutes=10), targets=[]); e2.fit = tct.compute_linear_regression([1, 2], [3, 5])
    e3 = tct.CalibrationEvent("d", T0 + dt.timedelta(minutes=10), targets=[]); e3.fit = tct.compute_linear_regression([1, 2], [3, 5])
    m, b, pos, _ = tct.coefficients_at(T0 + dt.timedelta(minutes=5), [e0, e1, e2, e3])
    assert pos == "between" and m == pytest.approx(1.5) and b == pytest.approx(0.5)
    with pytest.raises(ValueError):
        tct.coefficients_at(T0, [e1])


# ----------------------------------------------------------------------------- physics
@pytest.mark.parametrize("seed", range(12))
def test_emissivity_correction_inverts_the_forward_model(seed):
    rng = np.random.default_rng(seed)
    t_true = rng.uniform(-20, 60, 50)
    eps, t_sky = float(rng.uniform(0.85, 1.0)), float(rng.uniform(-60, 20))
    t_app = np.array([apparent(t, eps, t_sky) for t in t_true], np.float32)
    back = tct.apply_emissivity_correction(t_app, eps, t_sky)
    assert np.allclose(back, t_true, atol=2e-3)       # float32 in, float32 out
    assert tct.apply_emissivity_correction(np.array([30.0]), 1.0, -40.0)[0] == pytest.approx(30.0, abs=1e-4)


def test_emissivity_correction_is_monotonic_in_temperature():
    t = np.linspace(-30, 80, 200, dtype=np.float32)
    out = tct.apply_emissivity_correction(t, 0.9, -40.0)
    assert np.all(np.diff(out) > 0)


# ----------------------------------------------------------------------------- determinism and golden data
def test_batch_is_deterministic(tmp_path):
    truth = build_golden_flight(tmp_path / "flight")
    s1 = run_golden_pipeline(tmp_path / "flight", tmp_path / "out1")
    s2 = run_golden_pipeline(tmp_path / "flight", tmp_path / "out2")
    assert s1 == s2
    for rec in truth["survey"]:
        a = read_arr(tmp_path / "out1" / rec["file"].replace(".tif", "_calibrated.tif"))
        b = read_arr(tmp_path / "out2" / rec["file"].replace(".tif", "_calibrated.tif"))
        assert np.array_equal(a, b, equal_nan=True)
    csv1 = (tmp_path / "out1" / "processing_log.csv").read_text(encoding="utf-8")
    csv2 = (tmp_path / "out2" / "processing_log.csv").read_text(encoding="utf-8")
    assert csv1 == csv2


def test_golden_flight_matches_recorded_results(tmp_path):
    """Regression guard: the recorded coefficients, per-image results and survey errors must not change.
    Regenerate deliberately with `python tests/make_golden.py` after an intended change of the method."""
    expected = json.loads((HERE / "golden" / "expected.json").read_text(encoding="utf-8"))
    build_golden_flight(tmp_path / "flight")
    got = run_golden_pipeline(tmp_path / "flight", tmp_path / "out")
    for e_exp, e_got in zip(expected["events"], got["events"]):
        for key in ("slope", "intercept", "r2", "rmse", "se_slope", "se_intercept", "loo"):
            assert e_got[key] == pytest.approx(e_exp[key], rel=1e-4, abs=1e-5), (e_exp["file"], key)
    for r_exp, r_got in zip(expected["rows"], got["rows"]):
        assert r_got["input_filename"] == r_exp["input_filename"]
        for key in ("slope_m", "intercept_b", "calibrated_mean_celsius", "u_fit_1sigma", "pct_pixels_outside_target_range"):
            assert r_got[key] == pytest.approx(r_exp[key], rel=1e-4, abs=1e-4), (r_exp["input_filename"], key)
        assert r_got["position_vs_calibration"] == r_exp["position_vs_calibration"]
    assert np.allclose(got["survey_mean_errors"], expected["survey_mean_errors"], atol=1e-3)
    assert max(abs(e) for e in got["survey_mean_errors"]) < 0.5, "and the method itself works: drift removed"


def test_golden_flight_sensor_model_is_recovered(tmp_path):
    truth = build_golden_flight(tmp_path / "flight")
    got = run_golden_pipeline(tmp_path / "flight", tmp_path / "out")
    for rec, e in zip(truth["calibration"], got["events"]):
        assert abs(e["slope"] - 1 / rec["a"]) < 0.06           # gun noise 0.25 C over 23 C span
        assert abs(e["intercept"] + rec["c"] / rec["a"]) < 2.5
        assert e["rmse"] < 0.5
    assert np.mean(np.abs(got["survey_mean_errors"])) < 0.3
