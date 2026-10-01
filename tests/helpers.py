"""Shared builders for the test suite: synthetic scenes, GeoTIFF writers, events, a deterministic
golden flight. Everything here is seeded, so two runs produce identical files."""
import datetime as dt
import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np
import rasterio
from rasterio.transform import from_origin

import thermal_calibration_tool as tct

T0 = dt.datetime(2026, 6, 1, 10, 0, 0)
TARGETS = {"T1": ((20, 20), 15.0), "T2": ((80, 20), 30.0), "T3": ((140, 20), 45.0)}
SIZE = 30


def scene(drift: float = 0.0, gain: float = 1.0, blur: float = 0.0, shape=(160, 200)) -> np.ndarray:
    """Three square panels (15/30/45 C) on a 25 C background with a 30 C 'canopy' strip."""
    img = np.full(shape, 25.0, np.float32)
    for (x, y), t in TARGETS.values():
        img[y:y + SIZE, x:x + SIZE] = t
    img[100:150, 20:180] = 30.0
    if blur:
        img = cv2.GaussianBlur(img, (0, 0), blur)
    return (gain * img + drift).astype(np.float32)


def apparent(t_true: float, eps: float, t_sky: float = -40.0) -> float:
    """Forward grey-body model: apparent temperature a sensor with ε = 1 reports."""
    return ((eps * (t_true + 273.15) ** 4 + (1 - eps) * (t_sky + 273.15) ** 4) ** 0.25) - 273.15


def write_tif(path, arr: np.ndarray, when: Optional[dt.datetime] = None, crs: str = "EPSG:4326",
              tags: Optional[dict] = None) -> str:
    with rasterio.open(path, "w", driver="GTiff", height=arr.shape[0], width=arr.shape[1], count=1, dtype="float32",
                       transform=from_origin(5.0, 52.0, 1e-5, 1e-5), crs=crs) as d:
        d.write(arr.astype(np.float32), 1)
        t = dict(tags or {})
        if when:
            t["TIFFTAG_DATETIME"] = when.strftime("%Y:%m:%d %H:%M:%S")
        if t:
            d.update_tags(**t)
    return str(path)


def poly(name: str, pad: int = 0) -> List[Tuple[int, int]]:
    (x, y), _ = TARGETS[name]
    return [(x - pad, y - pad), (x + SIZE + pad, y - pad), (x + SIZE + pad, y + SIZE + pad), (x - pad, y + SIZE + pad)]


def rect(x0: int, y0: int, x1: int, y1: int) -> List[Tuple[int, int]]:
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def make_event(path: str, img: Optional[np.ndarray] = None, gt: Optional[Dict[str, float]] = None, inset: int = 3,
               polygons: Optional[Dict[str, List[Tuple[int, int]]]] = None, fit: bool = True) -> tct.CalibrationEvent:
    """Event with the three standard targets measured on the image and reference values from `gt`."""
    img = tct.load_thermal_image(path) if img is None else img
    gt = gt or {k: v[1] for k, v in TARGETS.items()}
    ts = tct.extract_timestamp(path)
    ev = tct.CalibrationEvent(path, ts.value or T0, ts.source, ts.tz, targets=[])
    ev.targets = []
    for i, name in enumerate(gt):
        t = tct.CalibrationTarget(name, tct.COLOR_PALETTE[i % len(tct.COLOR_PALETTE)],
                                  (polygons or {}).get(name) or poly(name))
        st = tct.extract_roi_statistics(img, t.polygon, inset)
        t.drone_mean, t.drone_std, t.pixel_count, t.inset_applied = st.mean, st.std, st.count, st.inset_applied
        t.ground_truth = gt[name]
        ev.targets.append(t)
    if fit:
        tct.recompute_fit(ev, False, True, None)
    return ev


def read_px(path, xy: Tuple[int, int]) -> float:
    with rasterio.open(path) as s:
        return float(s.read(1)[xy[1], xy[0]])


def read_arr(path) -> np.ndarray:
    with rasterio.open(path) as s:
        return s.read(1)


def run_job(job: tct.BatchJob, timestamps=None):
    logs: List[str] = []
    res = tct.run_batch(job, logs.append, lambda i, n, name: None, lambda: False, timestamps)
    return res, logs


# ----------------------------------------------------------------------------- golden flight
GOLDEN_SEED = 7
GOLDEN_PANELS = {"P1": ((40, 40), 18.0), "P2": ((140, 40), 29.0), "P3": ((240, 40), 41.0)}
GOLDEN_SIZE = 40
GOLDEN_SHAPE = (256, 320)
GOLDEN_MINUTES = (0, 7, 14)
GOLDEN_A = (0.97, 0.95)      # gain start → end
GOLDEN_C = (1.0, 3.2)        # offset start → end


def build_golden_flight(out_dir: Path) -> dict:
    """Deterministic small flight (no exiftool): 3 calibration shots with 3 panels, 6 survey images with
    their true arrays. Returns the ground truth. Same seed → byte-identical files on one machine."""
    out_dir = Path(out_dir)
    (out_dir / "calibration").mkdir(parents=True, exist_ok=True)
    (out_dir / "survey").mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(GOLDEN_SEED)
    H, W = GOLDEN_SHAPE
    yy, xx = np.mgrid[0:H, 0:W]
    fpn = cv2.GaussianBlur(rng.normal(0, 0.06, (H, W)).astype(np.float32), (0, 0), 3)
    vignette = (-0.4 * (((xx - W / 2) / (W / 2)) ** 2 + ((yy - H / 2) / (H / 2)) ** 2)).astype(np.float32)

    def sensor(true, minute):
        f = minute / GOLDEN_MINUTES[-1]
        a = GOLDEN_A[0] + f * (GOLDEN_A[1] - GOLDEN_A[0])
        c = GOLDEN_C[0] + f * (GOLDEN_C[1] - GOLDEN_C[0])
        img = cv2.GaussianBlur(true, (0, 0), 1.0)
        img = a * img + c + vignette + fpn + rng.normal(0, 0.1, (H, W)).astype(np.float32)
        return img.astype(np.float32), a, c

    truth = {"seed": GOLDEN_SEED, "panels": {k: v[1] for k, v in GOLDEN_PANELS.items()}, "calibration": [], "survey": []}
    for i, minute in enumerate(GOLDEN_MINUTES):
        true = np.full(GOLDEN_SHAPE, 32.0, np.float32)
        for (x, y), t in GOLDEN_PANELS.values():
            true[y:y + GOLDEN_SIZE, x:x + GOLDEN_SIZE] = t
        img, a, c = sensor(true, minute)
        when = T0 + dt.timedelta(minutes=minute)
        name = f"cal_{i}.tif"
        write_tif(out_dir / "calibration" / name, img, when)
        gun = {k: round(v[1] + float(rng.normal(0, 0.25)), 1) for k, v in GOLDEN_PANELS.items()}
        truth["calibration"].append({"file": name, "minute": minute, "a": a, "c": c, "gun": gun,
                                     "polygons": {k: rect(x - 2, y - 2, x + GOLDEN_SIZE + 2, y + GOLDEN_SIZE + 2)
                                                  for k, ((x, y), _) in GOLDEN_PANELS.items()}})
    for j in range(6):
        minute = 1 + j * 2.4
        r = np.random.default_rng(100 + j)
        rows = ((yy + r.integers(0, 40)) % 40) < 22
        true = np.where(rows, 29.0 + cv2.GaussianBlur(r.normal(0, 1.0, GOLDEN_SHAPE).astype(np.float32), (0, 0), 8),
                        39.0 + cv2.GaussianBlur(r.normal(0, 2.0, GOLDEN_SHAPE).astype(np.float32), (0, 0), 15)).astype(np.float32)
        img, a, c = sensor(true, minute)
        when = T0 + dt.timedelta(minutes=minute)
        name = f"survey_{j}.tif"
        write_tif(out_dir / "survey" / name, img, when)
        np.save(out_dir / "survey" / f"survey_{j}_TRUE.npy", true)
        truth["survey"].append({"file": name, "minute": minute, "a": a, "c": c, "true": f"survey_{j}_TRUE.npy"})
    with open(out_dir / "ground_truth.json", "w", encoding="utf-8") as f:
        json.dump(truth, f, indent=2)
    return truth


def run_golden_pipeline(flight_dir: Path, out_dir: Path) -> dict:
    """Fit every calibration shot and process the survey images exactly as the GUI/CLI would."""
    flight_dir, out_dir = Path(flight_dir), Path(out_dir)
    truth = json.loads((flight_dir / "ground_truth.json").read_text())
    events = []
    for rec in truth["calibration"]:
        path = str(flight_dir / "calibration" / rec["file"])
        ev = make_event(path, gt=rec["gun"], polygons={k: [tuple(p) for p in v] for k, v in rec["polygons"].items()})
        events.append(ev)
    files = tct.collect_survey_files(str(flight_dir / "survey"), None, "", [e.image_path for e in events])
    job = tct.BatchJob(tct.MODE_THERMAL, False, events, files, out_dir)
    rep = tct.preflight(job)
    assert not rep.blocking, rep.text()
    res, _ = run_job(job, rep.timestamps)
    summary = {"events": [{"file": Path(e.image_path).name, "slope": e.fit.slope, "intercept": e.fit.intercept,
                           "r2": e.fit.r_squared, "rmse": e.fit.rmse, "se_slope": e.fit.se_slope,
                           "se_intercept": e.fit.se_intercept, "loo": e.fit.loo_rmse} for e in events],
               "rows": [{k: r[k] for k in ("input_filename", "slope_m", "intercept_b", "calibrated_mean_celsius",
                                           "u_fit_1sigma", "pct_pixels_outside_target_range", "position_vs_calibration")}
                        for r in res["rows"]]}
    errs = []
    for rec in truth["survey"]:
        true = np.load(flight_dir / "survey" / rec["true"])
        cal = read_arr(out_dir / rec["file"].replace(".tif", "_calibrated.tif"))
        inner = (slice(20, GOLDEN_SHAPE[0] - 20), slice(20, GOLDEN_SHAPE[1] - 20))
        errs.append(float(np.mean(cal[inner]) - np.mean(true[inner])))
    summary["survey_mean_errors"] = errs
    return summary
