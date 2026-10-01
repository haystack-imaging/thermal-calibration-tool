#!/usr/bin/env python3
"""Generate a synthetic DJI-M4T-like thermal flight for testing the calibration tool.

Writes float32 single-band GeoTIFFs (640x512, like thermal_parser output) with
EXIF/XMP capture times and GPS written by exiftool, so the tool's metadata path
is exercised the same way as with real files.

The sensor model is  T_drone = a(t) * T_true + c(t) + noise,  with gain and
offset drifting linearly over the flight, plus blur, fixed-pattern noise and
vignetting. The inverse of that model is what the tool should recover.

Usage:  python3 tests/make_synthetic_flight.py [output_dir]
Default output: tests/data/synthetic_flight/
Needs: numpy, rasterio, opencv-python, exiftool on PATH.
"""
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta

import cv2
import numpy as np
import rasterio
from rasterio.transform import from_origin

OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "data", "synthetic_flight")
W, H = 640, 512
T0 = datetime(2026, 6, 1, 10, 0, 0)
TZ = "+02:00"
RNG = np.random.default_rng(42)

# Reference panels (true surface temperatures in C) and where they sit in a
# calibration shot (centre x, centre y, half-size in px).  Shot from lower
# altitude, so the panels are big.  A wet panel, a grey panel, a black panel.
PANELS = {
    "T1_wet":   dict(t=18.0, cx=190, cy=300, hs=34),
    "T2_grey":  dict(t=29.0, cx=320, cy=300, hs=34),
    "T3_black": dict(t=41.0, cx=450, cy=300, hs=34),
}

# Drift of the sensor over the flight (16 min)
FLIGHT_MIN = 16
A0, A1 = 0.97, 0.945      # gain at start / end
C0, C1 = 1.0, 3.6         # offset (C) at start / end
BLUR_SIGMA = 1.4          # microbolometer edge blur, px
NOISE_SIGMA = 0.12        # temporal NETD-like noise, C
FPN = cv2.GaussianBlur(RNG.normal(0, 0.08, (H, W)).astype(np.float32), (0, 0), 3)  # fixed pattern
yy, xx = np.mgrid[0:H, 0:W]
VIGNETTE = -0.6 * (((xx - W / 2) / (W / 2)) ** 2 + ((yy - H / 2) / (H / 2)) ** 2).astype(np.float32)


def sensor(t_true: np.ndarray, minute: float) -> np.ndarray:
    f = minute / FLIGHT_MIN
    a = A0 + f * (A1 - A0)
    c = C0 + f * (C1 - C0)
    img = cv2.GaussianBlur(t_true, (0, 0), BLUR_SIGMA)
    img = a * img + c + VIGNETTE + FPN + RNG.normal(0, NOISE_SIGMA, (H, W)).astype(np.float32)
    return img.astype(np.float32), a, c


def field_scene(seed: int) -> np.ndarray:
    """Crop rows (canopy ~29 C) on warm soil (~40 C) with some variation."""
    r = np.random.default_rng(seed)
    soil = 40.0 + cv2.GaussianBlur(r.normal(0, 2.5, (H, W)).astype(np.float32), (0, 0), 25)
    canopy = 29.0 + cv2.GaussianBlur(r.normal(0, 1.2, (H, W)).astype(np.float32), (0, 0), 12)
    rows = ((yy + r.integers(0, 60)) % 60) < 34          # row stripes, 60 px pitch
    gaps = cv2.GaussianBlur(r.random((H, W)).astype(np.float32), (0, 0), 6) > 0.45
    mask = rows & gaps
    scene = np.where(mask, canopy, soil).astype(np.float32)
    # a small cold feature (puddle) and a warm feature (path) for variety
    cv2.circle(scene, (int(r.integers(80, 560)), int(r.integers(80, 430))), 25, 24.0, -1)
    return scene


def calibration_scene() -> np.ndarray:
    scene = 33.0 + cv2.GaussianBlur(RNG.normal(0, 1.5, (H, W)).astype(np.float32), (0, 0), 20)
    for p in PANELS.values():
        scene[p["cy"] - p["hs"]:p["cy"] + p["hs"], p["cx"] - p["hs"]:p["cx"] + p["hs"]] = p["t"]
    return scene


def write_tif(path, arr, when, lat, lon, with_meta=True):
    tr = from_origin(lon, lat, 3.5e-7, 3.5e-7)   # ~3.5 cm GSD in degrees, roughly
    with rasterio.open(path, "w", driver="GTiff", width=W, height=H, count=1, dtype="float32",
                       crs="EPSG:4326", transform=tr, compress="lzw", predictor=3) as d:
        d.write(arr, 1)
    if not with_meta:
        return
    ts = when.strftime("%Y:%m:%d %H:%M:%S")
    subprocess.run([
        "exiftool", "-overwrite_original", "-q",
        f"-XMP:CreateDate={ts}{TZ}", f"-EXIF:DateTimeOriginal={ts}", f"-EXIF:CreateDate={ts}",
        f"-XMP:DateTimeOriginal={ts}{TZ}",
        "-EXIF:Make=DJI", "-EXIF:Model=M4T", "-XMP-drone-dji:RelativeAltitude=+30.0",
        f"-GPSLatitude={lat}", "-GPSLatitudeRef=N", f"-GPSLongitude={lon}", "-GPSLongitudeRef=E",
        "-GPSAltitude=52", "-GPSAltitudeRef=above",
        path], check=True)


def main():
    shutil.rmtree(OUT, ignore_errors=True)
    os.makedirs(os.path.join(OUT, "survey"))
    os.makedirs(os.path.join(OUT, "calibration"))
    os.makedirs(os.path.join(OUT, "edge_cases"))
    truth = {"sensor_model": "T_drone = a(t)*T_true + c(t)", "a_start": A0, "a_end": A1, "c_start": C0, "c_end": C1,
             "flight_minutes": FLIGHT_MIN, "panels": {k: v["t"] for k, v in PANELS.items()}, "images": []}

    def rec(name, kind, minute, a, c, extra=None):
        d = {"file": name, "kind": kind, "minute": minute, "a": round(a, 5), "c": round(c, 4),
             "expected_m": round(1 / a, 5), "expected_b": round(-c / a, 4)}
        if extra:
            d.update(extra)
        truth["images"].append(d)

    # --- calibration shots: start (0 min), mid (8 min), end (16 min)
    for i, minute in enumerate((0, 8, FLIGHT_MIN)):
        when = T0 + timedelta(minutes=minute)
        img, a, c = sensor(calibration_scene(), minute)
        name = f"DJI_{when:%Y%m%d%H%M%S}_{i + 1:04d}_T_deg.tif"
        write_tif(os.path.join(OUT, "calibration", name), img, when, 51.9870, 5.6650)
        # what the tool should measure inside each panel, and what the IR gun says
        gun = {k: round(v["t"] + float(RNG.normal(0, 0.3)), 1) for k, v in PANELS.items()}
        rec(name, "calibration", minute, a, c, {"ir_gun_readings_C": gun,
                                                 "panel_px": {k: [v["cx"], v["cy"], v["hs"]] for k, v in PANELS.items()}})

    # --- survey: 24 images between 1 and 15 min, two before the first shot would be unusual, so none
    for i in range(24):
        minute = 1 + i * (14 / 23)
        when = T0 + timedelta(minutes=minute)
        scene = field_scene(100 + i)
        img, a, c = sensor(scene, minute)
        name = f"DJI_{when:%Y%m%d%H%M%S}_{10 + i:04d}_T_deg.tif"
        lat, lon = 51.9870 + 0.0002 * (i // 6), 5.6650 + 0.0003 * (i % 6)
        write_tif(os.path.join(OUT, "survey", name), img, when, lat, lon)
        np.save(os.path.join(OUT, "survey", name.replace(".tif", "_TRUE.npy")), scene)
        rec(name, "survey", round(minute, 3), a, c, {"true_canopy_mean": 29.0, "true_npy": name.replace(".tif", "_TRUE.npy")})

    # --- edge cases
    ec = os.path.join(OUT, "edge_cases")
    when = T0 + timedelta(minutes=5)
    img, a, c = sensor(field_scene(999), 5)
    # 1. no date metadata at all (tool must warn, not silently use file mtime)
    write_tif(os.path.join(ec, "no_date_metadata.tif"), img, when, 51.987, 5.665, with_meta=False)
    # 2. colour "R-JPEG preview" (3 bands, 8 bit) - must be refused
    pal = cv2.applyColorMap(cv2.normalize(img, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8), cv2.COLORMAP_INFERNO)
    cv2.imwrite(os.path.join(ec, f"DJI_{when:%Y%m%d%H%M%S}_0099_T.JPG"), pal)
    # 3. 8-bit single band TIFF - must warn (not temperatures)
    cv2.imwrite(os.path.join(ec, "eight_bit_single_band.tif"), cv2.normalize(img, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8))
    # 4. UTC timestamp with 'Z' from a different converter (2 h off if treated as local)
    p = os.path.join(ec, "utc_z_timestamp.tif")
    write_tif(p, img, when, 51.987, 5.665, with_meta=False)
    subprocess.run(["exiftool", "-overwrite_original", "-q",
                    f"-XMP:CreateDate={(when - timedelta(hours=2)):%Y:%m:%d %H:%M:%S}Z", p], check=True)
    # 5. georeferenced output check source: a file with a real UTM transform
    tr = from_origin(683000.0, 5762000.0, 0.035, 0.035)
    with rasterio.open(os.path.join(ec, "utm_georef.tif"), "w", driver="GTiff", width=W, height=H, count=1,
                       dtype="float32", crs="EPSG:32631", transform=tr, compress="lzw") as d:
        d.write(img, 1)
        d.update_tags(TIFFTAG_DATETIME=when.strftime("%Y:%m:%d %H:%M:%S"))

    with open(os.path.join(OUT, "ground_truth.json"), "w", encoding="utf-8") as f:
        json.dump(truth, f, indent=2)
    n = sum(len(fs) for _, _, fs in os.walk(OUT))
    print(f"wrote {n} files to {OUT}")


if __name__ == "__main__":
    main()
