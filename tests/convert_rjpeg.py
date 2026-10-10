#!/usr/bin/env python3
"""Convert DJI R-JPEGs to float32 temperature TIFFs with the DJI Thermal SDK.

Mirrors the pre-processing step the README describes: R-JPEG -> DJI SDK ->
32-bit float TIFF in degrees C, with the JPEG's EXIF/XMP copied over by
exiftool so capture time and GPS survive.

Usage: python3 tests/convert_rjpeg.py IN_DIR OUT_DIR [--emissivity 1.0] [--distance 5] [--humidity 70]
Needs the SDK under tests/vendor/thermal_parser (see tests/README.md) and exiftool on PATH.
"""
import argparse
import glob
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dji_sdk  # noqa: E402
dji_sdk.ensure_library_path()

import rasterio      # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("in_dir"); ap.add_argument("out_dir")
ap.add_argument("--emissivity", type=float, default=None, help="override SDK emissivity (default: value stored in the R-JPEG)")
ap.add_argument("--distance", type=float, default=None); ap.add_argument("--humidity", type=float, default=None)
a = ap.parse_args()
os.makedirs(a.out_dir, exist_ok=True)
sdk = dji_sdk.DjiThermal(verbose=0)
for jpg in sorted(glob.glob(os.path.join(a.in_dir, "*_R.JP*G")) + glob.glob(os.path.join(a.in_dir, "*_T.JP*G"))):
    temp, used = sdk.temperature(jpg, emissivity=a.emissivity, distance=a.distance, humidity=a.humidity)
    out = os.path.join(a.out_dir, os.path.splitext(os.path.basename(jpg))[0].replace("_R", "_T") + "_deg.tif")
    with rasterio.open(out, "w", driver="GTiff", width=temp.shape[1], height=temp.shape[0], count=1,
                       dtype="float32", compress="lzw", predictor=3) as d:
        d.write(temp, 1)
    subprocess.run(["exiftool", "-overwrite_original", "-q", "-tagsFromFile", jpg, "-all:all", "-xmp", out], check=True)
    print(f"{os.path.basename(jpg)} -> {os.path.basename(out)}  {temp.min():.1f}..{temp.max():.1f} C  params={used}")

# The DJI library crashes while the interpreter unloads it; everything is written and flushed by now.
sys.stdout.flush()
os._exit(0)
