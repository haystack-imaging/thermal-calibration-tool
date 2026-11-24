#!/usr/bin/env python3
"""Build a synthetic thermal drone flight over a wheat field trial, to try the tool without a drone.

Nothing here is real data. The field, the reference panels, the operator, the car, the trees and the
camera are all simulated. The images look like DJI Matrice 4T thermal images converted to temperature
TIFFs (640 x 512, float32, degrees C), and the camera drifts in a known way, so you can check that the
tool takes the drift out.

    python demo/make_demo_flight.py              # writes demo/flight/
    python demo/make_demo_flight.py OUT --seed 7
    python demo/make_demo_flight.py OUT --every 4   # every 4th survey image only (the demo in the app)

What it writes:
    calibration/            two low shots over the reference panels, at the start and at the end
    survey/                 the survey images, 35 m above ground, nadir, about 75 % / 70 % overlap
    field_sheet.csv         the panel readings of a handheld IR thermometer (emissivity 0.95) per shot
    ground_truth.json       the camera's gain and offset at every image, panel and plot temperatures
    demo.calib.json         a project with the panels drawn and the readings typed in: open it, press Run
    README.txt              what the folder is and how to open it
    screenshot_config.json  panel corners in pixels and the readings, for tests/screenshot_gui.py

The scene: 48 plots of wheat (12 columns, 4 replicates), irrigated on the west half and rainfed on the
east half, bare soil alleys, a mown headland with the three reference panels (black, grey, white), the
operator with the IR thermometer, a car on the gravel track, a row of trees along the east side with
their shadows. Sun from the south-south-east, 25 degC air, a dry July day around noon.

The camera: 45 deg diagonal field of view, mild barrel distortion, optical blur with a faint halo,
vignetting, column and row fixed-pattern noise, 45 mK temporal noise, shutter corrections every few
minutes, and a gain and offset that drift after take-off (the offset settles exponentially, so a
straight line between two calibration shots leaves a small error mid-flight, as in real flights).

Needs numpy, opencv-python and rasterio, which the tool needs anyway. With exiftool on the PATH the
images also get DJI-style EXIF/XMP capture times, GPS positions and gimbal angles; without it the
capture time goes into the TIFF DateTime tag only, which the tool reads as well.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import shutil
import subprocess
import sys
import warnings
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

import cv2
import numpy as np
import rasterio
import rasterio.errors

# ---------------------------------------------------------------------------------------------- camera
W, H = 640, 512
DFOV_DEG = 45.0
F = math.hypot(W, H) / 2 / math.tan(math.radians(DFOV_DEG / 2))   # focal length in pixels (~990)
K1 = 0.035                                   # barrel distortion
PIXEL_PITCH_MM = 0.012

# --------------------------------------------------------------------------------------- time and place
T0 = datetime(2026, 7, 2, 12, 40, 0)
TZ = "+02:00"
LAT0, LON0 = 52.0, 5.0                       # local frame origin; round numbers on purpose
GROUND_ALT = 2.0                             # m above sea level
SUN_AZ, SUN_EL = 165.0, 58.0                 # degrees; shadows fall to the north-north-west
SHADOW = (-math.sin(math.radians(SUN_AZ)) / math.tan(math.radians(SUN_EL)),
          -math.cos(math.radians(SUN_AZ)) / math.tan(math.radians(SUN_EL)))   # ground offset per metre of height
SUN_DIR = (math.sin(math.radians(SUN_AZ)), math.cos(math.radians(SUN_AZ)))

# ------------------------------------------------------------------------------------------ the trial
N_COLS, N_RANGES = 12, 4
PLOT_W, PLOT_L = 1.5, 6.0
COL_PITCH, RANGE_PITCH = 1.8, 7.5
ROW_SPACING = 0.125
PANEL_SIDE = 0.6
PANELS = {                                   # true apparent temperature at the start, warming per minute
    "black": dict(t0=51.4, rate=0.055),
    "grey": dict(t0=42.7, rate=0.035),
    "white": dict(t0=33.9, rate=0.025),
}

# -------------------------------------------------------------------------------------- the flight
SURVEY_ALT = 35.0
CAL_ALT = 8.0
LINES_X = (-2.0, 5.0, 12.0, 19.0, 26.0)
STATIONS_Y = tuple(-10.0 + 4.5 * i for i in range(11))
SHOT_INTERVAL_S = 12.0
TURN_S = 20.0


def smoothstep(e0, e1, x):
    t = np.clip((x - e0) / (e1 - e0), 0.0, 1.0)
    return t * t * (3 - 2 * t)


class Noise:
    """Tileable band-limited noise textures, sampled at any scale in metres (values about N(0, 1))."""

    def __init__(self, rng: np.random.Generator, count: int = 12, size: int = 1024):
        ky = np.fft.fftfreq(size)[:, None]
        kx = np.fft.rfftfreq(size)[None, :]
        filt = np.exp(-2 * math.pi ** 2 * 1.2 ** 2 * (kx ** 2 + ky ** 2))
        self.size = size
        self.fp = 0.0                         # metres per sample of the current render; finer detail fades out
        self.tex = []
        for _ in range(count):
            a = np.fft.irfft2(np.fft.rfft2(rng.standard_normal((size, size))) * filt, s=(size, size))
            self.tex.append(((a - a.mean()) / a.std()).astype(np.float32))

    def __call__(self, X, Y, cell, k=0, angle=0.0, stretch=1.0, shift=(0.0, 0.0)):
        """Features about 3 * cell metres wide; stretch > 1 elongates them along the rotated y axis."""
        shape = np.shape(X)
        n = int(np.prod(shape))
        fade = float(smoothstep(0.8, 2.0, 3 * cell / self.fp)) if self.fp else 1.0
        if n == 0 or fade == 0.0:
            return np.zeros(shape, np.float32)
        ca, sa = np.float32(math.cos(angle) / cell), np.float32(math.sin(angle) / cell)
        u = ca * X + sa * Y + np.float32(shift[0] + 131.7 * k)
        v = (ca * Y - sa * X) / np.float32(stretch) + np.float32(shift[1] + 57.3 * k)
        size = np.float32(self.size)
        u, v = np.mod(u, size).ravel(), np.mod(v, size).ravel()
        cols = 4096                                       # cv2.remap wants each side below 32767
        pad = (-n) % cols
        if pad:
            u, v = np.concatenate([u, np.zeros(pad, np.float32)]), np.concatenate([v, np.zeros(pad, np.float32)])
        out = cv2.remap(self.tex[k % len(self.tex)], u.reshape(-1, cols), v.reshape(-1, cols),
                        cv2.INTER_LINEAR, borderMode=cv2.BORDER_WRAP)
        out = out.ravel()[:n].reshape(shape)
        return out if fade == 1.0 else out * np.float32(fade)

    def fbm(self, X, Y, cell, octaves=3, k=0, gain=0.55, **kw):
        out, amp, norm = 0.0, 1.0, 0.0
        for o in range(octaves):
            out = out + amp * self(X, Y, cell / 2 ** o, k=k + o, shift=(37.1 * o, 91.7 * o), **kw)
            norm += amp * amp
            amp *= gain
        return out / math.sqrt(norm)


def local(X, Y, cx, cy, heading_deg):
    """Coordinates along (f) and across (l, to the left) an object's forward axis; heading from north, clockwise."""
    a = math.radians(heading_deg)
    fx, fy = math.sin(a), math.cos(a)
    dx, dy = X - cx, Y - cy
    return dx * fx + dy * fy, -dx * fy + dy * fx


def superellipse(f, l, rf, rl, p=6.0):
    return (np.abs(f / rf) ** p + np.abs(l / rl) ** p) <= 1.0


def ellipse(f, l, rf, rl):
    return (f / rf) ** 2 + (l / rl) ** 2


# ------------------------------------------------------------------------------------------- objects
@dataclass
class Part:
    """A flat piece of an object at a height above ground: mask and temperature at world (X, Y)."""
    h: float
    fn: Callable                              # (X, Y, minute) -> (mask, temperature)
    cx: float = 0.0                           # bounding circle in world metres, for culling
    cy: float = 0.0
    rad: float = 1.0
    shadow: float = 0.0                       # cooling of the ground in its shadow, degC (0 = none)
    draw: bool = True                         # False: only casts a shadow (hidden from above)


@dataclass
class Scene:
    noise: Noise
    plot_dt: np.ndarray                       # canopy minus air temperature per plot [range, col]
    plot_cover: np.ndarray
    parts: list = field(default_factory=list)
    trees: list = field(default_factory=list)


def make_scene(rng: np.random.Generator, operator_at: tuple, case_at: tuple) -> Scene:
    noise = Noise(rng)
    dt = np.empty((N_RANGES, N_COLS))
    cover = np.empty((N_RANGES, N_COLS))
    geno = rng.normal(0, 0.7, N_COLS)
    for c in range(N_COLS):
        irrigated = c < N_COLS // 2
        for r in range(N_RANGES):
            dt[r, c] = (-1.5 if irrigated else 4.2) + 0.9 * geno[(c * 5 + r * 3) % N_COLS] + rng.normal(0, 0.4)
            cover[r, c] = rng.uniform(0.78, 0.95) if irrigated else rng.uniform(0.6, 0.86)
    cover[1, 9] = 0.42                        # poor emergence
    cover[3, 2] = 0.5
    sc = Scene(noise=noise, plot_dt=dt, plot_cover=cover)
    sc.parts += panel_parts(noise)
    sc.parts += person_parts(noise, *operator_at)
    sc.parts += car_parts(noise, 3.2, -7.0, 92.0)
    sc.parts += case_parts(noise, *case_at)
    sc.parts += hose_parts(noise)
    # tree row along the east side
    y = -14.0
    while y < 40:
        sc.trees.append(dict(x=29.6 + rng.normal(0, 0.5), y=y, r=rng.uniform(2.5, 3.3), h=rng.uniform(9.5, 12.5),
                             k=int(rng.integers(0, 12))))
        y += rng.uniform(5.2, 6.8)
    return sc


PANEL_CENTRE = (7.0, -3.4)
PANEL_HEADING = 98.0                          # the row of panels runs roughly east, turned by 8 deg
PANEL_GAP = 0.14
EDGE = 0.015                                  # the boards' sawn edges


def panel_centres():
    out = {}
    a = math.radians(PANEL_HEADING)
    for i, name in enumerate(PANELS):
        s = (i - 1) * (PANEL_SIDE + PANEL_GAP)
        out[name] = (PANEL_CENTRE[0] + s * math.sin(a), PANEL_CENTRE[1] + s * math.cos(a))
    return out


def panel_temp(name, minute):
    p = PANELS[name]
    return p["t0"] + p["rate"] * minute


def panel_parts(noise):
    parts = []
    for name, (cx, cy) in panel_centres().items():
        def fn(X, Y, minute, cx=cx, cy=cy, name=name):
            f, l = local(X, Y, cx, cy, PANEL_HEADING)
            half = PANEL_SIDE / 2
            outer = (np.abs(f) <= half + EDGE) & (np.abs(l) <= half + EDGE)
            inner = (np.abs(f) <= half) & (np.abs(l) <= half)
            edge = np.minimum(half - np.abs(f), half - np.abs(l))
            t = panel_temp(name, minute) - 0.5 * np.exp(-np.clip(edge, 0, None) / 0.02) \
                + 0.08 * noise(X, Y, 0.03, k=9) + 0.12 * noise(X, Y, 0.12, k=10)
            wood = 36.0 + 0.6 * noise(X, Y, 0.01, k=11)          # unpainted plywood edge
            return outer, np.where(inner, t, wood)
        parts.append(Part(0.03, fn, cx, cy, 0.5))
    return parts


def person_parts(noise, px, py, heading):
    """The operator with the IR thermometer, built from horizontal slices so that parallax and the
    shadow come out right. Sunlit dark hair and shoulders are the warmest parts; recent shadow."""
    parts = []

    def slice_(fc, lc, h, rf, rl, temp, shadow=3.0):
        def fn(X, Y, minute):
            f, l = local(X, Y, px, py, heading)
            e = ellipse(f - fc, l - lc, rf, rl)
            return e <= 1, temp(X, Y, e)
        parts.append(Part(h, fn, px, py, 0.9, shadow))

    for side in (-1, 1):
        slice_(0.06, side * 0.11, 0.05, 0.14, 0.055, lambda X, Y, e: 39.0 + 0 * X)
        for h in np.arange(0.12, 0.95, 0.1):
            slice_(0.0, side * 0.1, h, 0.075, 0.07, lambda X, Y, e: 38.5 + 0.3 * noise(X, Y, 0.02, k=1))
    for h in np.arange(0.95, 1.46, 0.07):
        w = 0.17 + 0.05 * min(1.0, (h - 0.95) / 0.4)
        slice_(0.0, 0.0, h, 0.12, w, lambda X, Y, e, h=h: 37.5 + 3.0 * (h - 0.95) / 0.5
               + 0.5 * noise(X, Y, 0.015, k=2) - 1.0 * e)
    for h in np.arange(0.85, 1.38, 0.07):                          # left arm hanging
        slice_(0.0, 0.24, h, 0.045, 0.045, lambda X, Y, e: 34.6 + 0 * X)
    for t in np.linspace(0, 1, 9):                                 # right arm forward to the thermometer
        slice_(0.05 + 0.4 * t, -0.2 + 0.04 * t, 1.38 - 0.22 * t, 0.05, 0.045, lambda X, Y, e: 34.8 + 0 * X)
    slice_(0.53, -0.16, 1.2, 0.08, 0.025, lambda X, Y, e: 38.0 + 0 * X)
    slice_(0.0, 0.0, 1.52, 0.055, 0.06, lambda X, Y, e: 35.0 + 0 * X)
    for h, r in ((1.58, 0.085), (1.64, 0.098), (1.70, 0.095), (1.75, 0.08), (1.79, 0.05)):
        slice_(0.01, 0.0, h, r * 1.12, r, lambda X, Y, e, h=h: 35.5 + 3.5 * (h - 1.58) / 0.21
               + 0.3 * noise(X, Y, 0.01, k=3) - 0.8 * e)
    return parts


def car_parts(noise, cx, cy, heading):
    parts = []

    def piece(h, shape, temp, shadow=12.0):
        def fn(X, Y, minute):
            f, l = local(X, Y, cx, cy, heading)
            return shape(f, l), temp(X, Y, f, l, minute)
        parts.append(Part(h, fn, cx, cy, 2.6, shadow))

    body = lambda f, l: superellipse(f, l, 2.2, 0.9, 5)
    for h in (0.3, 0.5):
        piece(h, body, lambda X, Y, f, l, m: 36.0 + 0.3 * noise(X, Y, 0.05, k=5))
    piece(0.7, body, lambda X, Y, f, l, m: 38.5 + 0.3 * noise(X, Y, 0.05, k=6))
    piece(1.0, lambda f, l: body(f, l) & (f > 0.9),
          lambda X, Y, f, l, m: 47.0 + 6.0 * np.exp(-((f - 1.5) ** 2 / 0.25 + l ** 2 / 0.3)) + 0.3 * noise(X, Y, 0.04, k=7))
    piece(1.3, lambda f, l: (f > 0.25) & (f <= 0.9) & (np.abs(l) < 0.78 - 0.15 * (f - 0.25)),
          lambda X, Y, f, l, m: 11.5 + 4.0 * (f - 0.25) + 0.8 * noise(X, Y, 0.1, k=8))
    piece(1.46, lambda f, l: (f > -1.2) & (f <= 0.25) & (np.abs(l) < 0.72),
          lambda X, Y, f, l, m: 40.5 - 1.2 * np.abs(l) + 0.25 * noise(X, Y, 0.06, k=9))
    piece(1.3, lambda f, l: (f > -1.75) & (f <= -1.2) & (np.abs(l) < 0.7),
          lambda X, Y, f, l, m: 14.5 + 0.8 * noise(X, Y, 0.1, k=10))
    piece(1.0, lambda f, l: body(f, l) & (f <= -1.75),
          lambda X, Y, f, l, m: 39.5 + 0 * X)
    for side in (-1, 1):
        piece(1.05, lambda f, l, s=side: ellipse(f - 0.75, l - s * 1.0, 0.09, 0.12) <= 1,
              lambda X, Y, f, l, m: 31.0 + 0 * X)
    return parts


def case_parts(noise, cx, cy, heading):
    def fn(X, Y, minute):
        f, l = local(X, Y, cx, cy, heading)
        m = superellipse(f, l, 0.31, 0.24, 8)
        ribs = 0.9 * (np.sin(f / 0.03) > 0.6)
        return m, 53.0 + ribs + 0.3 * noise(X, Y, 0.02, k=11)
    return [Part(0.28, fn, cx, cy, 0.5, 6.0)]


def hose_parts(noise):
    """The supply hose of the irrigated half, lying on the headland; the water in it keeps it cool."""
    x0, x1, y = -0.4, 10.8, -1.8

    def fn(X, Y, minute):
        wiggle = 0.06 * np.sin(X / 1.3) + 0.03 * np.sin(X / 0.47 + 1.0)
        m = (X > x0) & (X < x1) & (np.abs(Y - y - wiggle) < 0.022)
        return m, 23.5 + 0.4 * noise(X, Y, 0.05, k=2)
    return [Part(0.02, fn, (x0 + x1) / 2, y, (x1 - x0) / 2 + 0.2, 1.5)]


# -------------------------------------------------------------------------------------------- ground
def soil_temp(X, Y, n: Noise, air, warm):
    """Sunlit bare soil: moisture patches, clods and crumbs."""
    return air + 18.0 + warm + 1.6 * n.fbm(X, Y, 1.2, 3, k=0) + 0.8 * n.fbm(X, Y, 0.04, 2, k=1) + 0.6 * n(X, Y, 0.006, k=2)


def wheat(X, Y, n: Noise, air, warm, dt, cover, var, row_x0=0.0625):
    """Wheat rows along y at grain filling: sunlit and shaded leaf clusters, warm ears, shaded soil
    between the rows with sunflecks. var is the field's large-scale variation (soil, water)."""
    q = (X - row_x0) / ROW_SPACING
    frac = q - np.floor(q) - 0.5
    prof = np.cos(np.pi * frac) ** 2
    clump = n.fbm(X, Y, 0.018, 3, k=5, angle=0.3, stretch=1.5)
    fine = n(X, Y, 0.0035, k=6, angle=-0.5, stretch=3)
    dens = 0.9 * prof + 0.55 * clump + 0.3 * fine + 3.2 * (cover + 0.05 * var - 0.7) - 0.3
    L = smoothstep(-0.25, 0.25, dens)
    sun = smoothstep(-0.6, 0.9, clump + 0.3 * fine)
    ears = smoothstep(0.8, 1.5, n(X, Y, 0.005, k=7))
    t_leaf = air + dt + 0.7 * var + 1.6 * (sun - 0.5) + 1.2 * ears + 0.3 * n.fbm(X, Y, 0.3, 2, k=9)
    under = air + 6.0 + 1.0 * n(X, Y, 0.04, k=10)
    fleck = smoothstep(0.9, 1.5, n(X, Y, 0.012, k=11))
    under = under + 0.35 * fleck * (soil_temp(X, Y, n, air, warm) - under)
    return L * t_leaf + (1 - L) * under


GRASS, LONG_GRASS, GRAVEL, FIELD, TRIAL = range(5)
TRIAL_BOX = (-0.75, N_COLS * COL_PITCH - 0.3 + 0.75, -0.75, N_RANGES * RANGE_PITCH - 1.5 + 0.75)


def ground(X, Y, minute, sc: Scene):
    """True apparent temperature of the ground at world points X, Y (any shape), shadows included."""
    n = sc.noise
    air = np.float32(25.0 + 0.04 * minute)
    warm = np.float32(0.06 * minute)
    edge = 0.05 * n.fbm(X, Y, 0.12, 2, k=3)
    xe, yt = X + edge, Y + edge

    region = np.full(X.shape, FIELD, np.int8)
    x0, x1, y0, y1 = TRIAL_BOX
    region[(xe > x0) & (xe < x1) & (yt > y0) & (yt < y1)] = TRIAL
    region[xe > 25.0] = LONG_GRASS
    region[yt < -1.6] = GRASS
    region[(yt < -5.5) & (yt >= -8.5)] = GRAVEL
    region[(yt < -8.5) & (xe > 25.0)] = LONG_GRASS

    def var(x, y):                # soil and water differences across the field, 5 to 20 m
        return n.fbm(x, y, 2.0, 3, k=16)

    def grass_t(x, y):            # mown headland, faint mowing stripes along x
        return air + 6.0 + 0.6 * warm + 1.0 * n.fbm(x, y, 0.04, 3, k=3) + 0.3 * n(x, y, 0.004, k=4, angle=0.7, stretch=5) \
            + 1.0 * n.fbm(x, y, 0.8, 2, k=5) + 0.3 * np.tanh(3 * np.sin(np.pi * y / 1.1))

    def grass(x, y, yt):
        return grass_t(x, y)

    def long_grass(x, y, yt):
        return air + 7.0 + 0.6 * warm + 1.6 * n.fbm(x, y, 0.05, 3, k=6, angle=-0.4, stretch=1.5) \
            + 0.6 * n(x, y, 0.004, k=7, angle=-0.4, stretch=3) + 1.0 * n.fbm(x, y, 0.8, 2, k=8)

    def gravel(x, y, yt):         # two compacted wheel ruts, grass in the middle and on the verges
        stones = n(x, y, 0.006, k=8) + 0.6 * n(x, y, 0.02, k=9)
        g = air + 21.0 + warm + 1.0 * n.fbm(x, y, 0.6, 2, k=7) + 1.0 * stones
        rut = (np.abs(yt + 7.75) < 0.22) | (np.abs(yt + 6.25) < 0.22)
        g = np.where(rut, g + 1.6 - 0.6 * stones, g)
        mid = smoothstep(0.1, -0.1, np.abs(yt + 7.0) - 0.26 - 0.06 * n.fbm(x, y, 0.05, 2, k=10))
        verge = smoothstep(0.45, 0.0, np.minimum(np.abs(yt + 5.5), np.abs(yt + 8.5)))
        veg = np.maximum(mid, (verge > 0) * smoothstep(0.25, 0.55, verge + 0.45 * n.fbm(x, y, 0.025, 3, k=11) - 0.3))
        return g + veg * (grass_t(x, y) + 2.0 - g)

    def field(x, y, yt):          # the farmer's wheat around the trial, with two tramlines
        t = wheat(x, y, n, air, warm, 1.2, 0.92, var(x, y), row_x0=0.03)
        tram = np.zeros(x.shape, bool)
        for xc in (-7.5, -9.3):
            tram |= np.abs(x - xc + 0.04 * n.fbm(x, y, 0.1, 2, k=10)) < 0.22
        tram &= n(x, y, 0.03, k=6) < 1.3                              # a few weeds in the tramlines
        if tram.any():
            t[tram] = soil_temp(x[tram], y[tram], n, air, warm) + 1.5
        return t

    def trial(x, y, yt):          # plots on bare soil alleys, wheel marks across the alleys
        xe_ = x + 0.03 * n(x, y, 0.06, k=11)
        ye_ = y + 0.06 * n.fbm(x, y, 0.08, 2, k=0)
        col = np.floor(xe_ / COL_PITCH).astype(int)
        rng_ = np.floor(ye_ / RANGE_PITCH).astype(int)
        in_plot = (col >= 0) & (col < N_COLS) & (rng_ >= 0) & (rng_ < N_RANGES) \
            & (xe_ - col * COL_PITCH < PLOT_W) & (ye_ - rng_ * RANGE_PITCH < PLOT_L)
        t = soil_temp(x, y, n, air, warm)
        ry = np.mod(ye_, RANGE_PITCH)
        t = np.where((np.abs(ry - 6.45) < 0.18) | (np.abs(ry - 7.05) < 0.18), t + 1.3, t)
        weeds = smoothstep(2.1, 2.5, n.fbm(x, y, 0.1, 2, k=4))
        t = t + weeds * (air + 5.0 - t)
        if in_plot.any():
            ci, ri = col[in_plot], rng_[in_plot]
            xp, yp = x[in_plot], y[in_plot]
            t[in_plot] = wheat(xp, yp, n, air, warm, sc.plot_dt[ri, ci].astype(np.float32),
                               sc.plot_cover[ri, ci].astype(np.float32), var(xp, yp))
        return t

    T = np.empty(X.shape, np.float32)
    for code, fn in ((GRASS, grass), (LONG_GRASS, long_grass), (GRAVEL, gravel), (FIELD, field), (TRIAL, trial)):
        idx = np.nonzero(region == code)
        if idx[0].size:
            T[idx] = fn(X[idx], Y[idx], yt[idx])

    # shadows: trees (old, full cooling) and objects (each with its own cooling)
    shade = np.zeros(X.shape, np.float32)
    trunk_dir = math.degrees(math.atan2(SHADOW[0], SHADOW[1]))
    for t in sc.trees:
        ends = [(t["x"], t["y"])] + [(t["x"] + SHADOW[0] * hh, t["y"] + SHADOW[1] * hh) for hh in (t["h"] - 1, t["h"] - 3)]
        pad = 1.2 * t["r"]
        bx0, bx1 = min(e[0] for e in ends) - pad, max(e[0] for e in ends) + pad
        by0, by1 = min(e[1] for e in ends) - pad, max(e[1] for e in ends) + pad
        idx = np.nonzero((X > bx0) & (X < bx1) & (Y > by0) & (Y < by1))
        if not idx[0].size:
            continue
        x, y = X[idx], Y[idx]
        s = np.zeros(x.shape, np.float32)
        for hh in (t["h"] - 1.0, t["h"] - 3.0):
            gx, gy = x - np.float32(SHADOW[0] * hh), y - np.float32(SHADOW[1] * hh)
            d = np.hypot(gx - t["x"], gy - t["y"]) / (t["r"] * (1 + 0.12 * n(gx, gy, 0.25, k=t["k"])))
            s = np.maximum(s, smoothstep(1.02, 0.94, d))
        f, l = local(x, y, t["x"], t["y"], trunk_dir)
        trunk_len = (t["h"] - 4.0) * math.hypot(*SHADOW)
        s = np.maximum(s, ((f > 0) & (f < trunk_len) & (np.abs(l) < 0.18)).astype(np.float32))
        shade[idx] = np.maximum(shade[idx], s)
    obj = np.zeros(X.shape, np.float32)
    for p in sc.parts:
        if not p.shadow:
            continue
        sx, sy = p.cx + SHADOW[0] * p.h, p.cy + SHADOW[1] * p.h
        idx = np.nonzero((np.abs(X - sx) < p.rad) & (np.abs(Y - sy) < p.rad))
        if not idx[0].size:
            continue
        m, _ = p.fn(X[idx] - np.float32(SHADOW[0] * p.h), Y[idx] - np.float32(SHADOW[1] * p.h), minute)
        obj[idx] = np.maximum(obj[idx], m * np.float32(p.shadow))
    bare = T > air + 15                                               # bare ground cools more than grass
    T -= shade * np.where(bare, 13.0, 7.0).astype(np.float32)
    T -= obj * np.where(bare, 1.0, 0.55).astype(np.float32)
    return T


# -------------------------------------------------------------------------------------------- render
@dataclass
class Pose:
    x: float
    y: float
    alt: float
    heading: float                            # image top points this way, degrees from north
    roll: float = 0.0
    pitch: float = 0.0

    def R(self):
        a = math.radians(self.heading)
        fw = np.array([math.sin(a), math.cos(a), 0.0])
        rw = np.array([math.cos(a), -math.sin(a), 0.0])
        R0 = np.stack([rw, -fw, np.array([0.0, 0.0, -1.0])], axis=1)
        p, r = math.radians(self.pitch), math.radians(self.roll)
        Rx = np.array([[1, 0, 0], [0, math.cos(p), -math.sin(p)], [0, math.sin(p), math.cos(p)]])
        Ry = np.array([[math.cos(r), 0, math.sin(r)], [0, 1, 0], [-math.sin(r), 0, math.cos(r)]])
        return R0 @ Rx @ Ry


def render_true(pose: Pose, minute: float, sc: Scene, ss: int = 2) -> np.ndarray:
    """True apparent temperature seen by each pixel (supersampled, area-averaged)."""
    v, u = np.mgrid[0:H * ss, 0:W * ss].astype(np.float64)
    u = (u + 0.5) / ss - 0.5
    v = (v + 0.5) / ss - 0.5
    xd, yd = (u - (W - 1) / 2) / F, (v - (H - 1) / 2) / F
    r2 = xd * xd + yd * yd
    xu, yu = xd * (1 + K1 * r2), yd * (1 + K1 * r2)
    R = pose.R()
    dz = R[2, 0] * xu + R[2, 1] * yu + R[2, 2]
    dx = ((R[0, 0] * xu + R[0, 1] * yu + R[0, 2]) / dz).astype(np.float32)   # ground offset per metre of drop
    dy = ((R[1, 0] * xu + R[1, 1] * yu + R[1, 2]) / dz).astype(np.float32)

    def hit(h):
        t = np.float32(h - pose.alt)
        return np.float32(pose.x) + t * dx, np.float32(pose.y) + t * dy

    X, Y = hit(0.0)
    n = sc.noise
    n.fp = pose.alt / F / ss                                   # metres per sample at nadir
    T = ground(X, Y, minute, sc)
    layers = [(p.h, "part", p) for p in sc.parts if p.draw] + [(t["h"], "tree", t) for t in sc.trees]
    for h, kind, obj in sorted(layers, key=lambda x: x[0]):
        Xh, Yh = hit(h)
        cx, cy, rad = (obj.cx, obj.cy, obj.rad) if kind == "part" else (obj["x"], obj["y"], obj["r"] * 1.2)
        idx = np.nonzero((np.abs(Xh - cx) < rad) & (np.abs(Yh - cy) < rad))
        if not idx[0].size:
            continue
        x, y = Xh[idx], Yh[idx]
        if kind == "part":
            m, t = obj.fn(x, y, minute)
        else:
            ox, oy = x - obj["x"], y - obj["y"]
            d = np.hypot(ox, oy) / (obj["r"] * (1 + 0.12 * n(x, y, 0.25, k=obj["k"])))
            m = d < 1.0
            sunward = (ox * SUN_DIR[0] + oy * SUN_DIR[1]) / obj["r"]
            dome = np.sqrt(np.clip(1 - d * d, 0, 1))
            t = 28.0 + 0.04 * minute + 1.8 * sunward * dome - 2.0 * (1 - dome) \
                + 1.3 * n.fbm(x, y, 0.15, 3, k=obj["k"] + 1) + 0.5 * n(x, y, 0.02, k=obj["k"] + 2)
        T[idx] = np.where(m, t, T[idx])
    return cv2.resize(T, (W, H), interpolation=cv2.INTER_AREA)


def project(pose: Pose, X, Y, Z=0.0):
    """World point -> pixel (u, v), the inverse of the ray model in render_true."""
    R = pose.R()
    d = R.T @ np.array([X - pose.x, Y - pose.y, Z - pose.alt])
    xu, yu = d[0] / d[2], d[1] / d[2]
    ru = math.hypot(xu, yu)
    rd = ru
    for _ in range(20):
        rd = ru / (1 + K1 * rd * rd)
    s = rd / ru if ru else 1.0
    return xu * s * F + (W - 1) / 2, yu * s * F + (H - 1) / 2


class Camera:
    """Microbolometer response with a known drift. T_drone = gain(t) * T_true + offset(t) + fixed patterns + noise."""

    def __init__(self, rng: np.random.Generator):
        self.rng = rng                        # only for the fixed patterns; each image brings its own noise
        yy, xx = np.mgrid[0:H, 0:W]
        r2 = ((xx - W / 2) ** 2 + (yy - H / 2) ** 2) / ((W / 2) ** 2 + (H / 2) ** 2)
        self.vignette = (-0.3 * r2 + 0.06 * np.exp(-r2 / 0.05)).astype(np.float32)
        self.fpn = self._pattern()
        self.ffc_times = [3.1, 6.4, 9.6, 12.9]
        self.ffc_jumps = rng.normal(0, 0.07, len(self.ffc_times))

    def _pattern(self):
        r = self.rng
        cols = r.normal(0, 0.035, (1, W))
        rows = r.normal(0, 0.015, (H, 1))
        pix = r.normal(0, 0.025, (H, W))
        return (cols + rows + pix).astype(np.float32)

    def gain(self, minute):
        return 0.975 - 0.001 * minute

    def offset(self, minute):
        """Reads warm right after take-off, then cools in the wind; small steps at each shutter event."""
        jumps = sum(j for t, j in zip(self.ffc_times, self.ffc_jumps) if minute >= t)
        return 3.0 - 6.0 * (1 - math.exp(-minute / 25.0)) + jumps

    def capture(self, t_true: np.ndarray, minute: float, rng: np.random.Generator) -> np.ndarray:
        core = cv2.GaussianBlur(t_true, (0, 0), 0.75)
        halo = cv2.GaussianBlur(t_true, (0, 0), 5.0)
        img = 0.96 * core + 0.04 * halo
        noise = rng.normal(0, 0.045, (H, W)).astype(np.float32)
        noise += cv2.GaussianBlur(rng.normal(0, 0.08, (H, W)).astype(np.float32), (0, 0), 2.0)
        img = self.gain(minute) * img + self.offset(minute) + self.vignette + self.fpn + noise
        return img.astype(np.float32)


# ---------------------------------------------------------------------------------------------- files
def gps(x, y):
    lat = LAT0 + y / 111_320.0
    lon = LON0 + x / (111_320.0 * math.cos(math.radians(LAT0)))
    return lat, lon


def write_tif(path: Path, img: np.ndarray, when: datetime):
    """Like the DJI SDK's output: no georeferencing in the TIFF itself (GPS goes into EXIF)."""
    warnings.filterwarnings("ignore", category=rasterio.errors.NotGeoreferencedWarning)
    with rasterio.open(path, "w", driver="GTiff", width=W, height=H, count=1, dtype="float32",
                       compress="lzw", predictor=3) as d:
        d.write(img, 1)
        d.update_tags(TIFFTAG_DATETIME=when.strftime("%Y:%m:%d %H:%M:%S"))


def exif_args(path: Path, when: datetime, pose: Pose, rng) -> list:
    lat, lon = gps(pose.x + rng.normal(0, 0.3), pose.y + rng.normal(0, 0.3))
    ts = when.strftime("%Y:%m:%d %H:%M:%S")
    f35 = 43.27 / 2 / math.tan(math.radians(DFOV_DEG / 2))
    return [
        f"-EXIF:DateTimeOriginal={ts}", f"-EXIF:CreateDate={ts}", f"-EXIF:OffsetTimeOriginal={TZ}",
        f"-XMP:CreateDate={ts}{TZ}", f"-XMP:DateTimeOriginal={ts}{TZ}",
        "-EXIF:Make=DJI", "-EXIF:Model=M4T",
        f"-EXIF:FocalLength={F * PIXEL_PITCH_MM:.2f}", f"-EXIF:FocalLengthIn35mmFormat={round(f35)}",
        f"-GPSLatitude={lat:.8f}", "-GPSLatitudeRef=N", f"-GPSLongitude={lon:.8f}", "-GPSLongitudeRef=E",
        f"-GPSAltitude={GROUND_ALT + pose.alt:.2f}", "-GPSAltitudeRef=above",
        f"-XMP-drone-dji:RelativeAltitude=+{pose.alt:.2f}", f"-XMP-drone-dji:AbsoluteAltitude=+{GROUND_ALT + pose.alt:.2f}",
        f"-XMP-drone-dji:GimbalPitchDegree={-90 + pose.pitch:+.2f}", f"-XMP-drone-dji:GimbalRollDegree={pose.roll:+.2f}",
        f"-XMP-drone-dji:GimbalYawDegree={((pose.heading + 180) % 360) - 180:+.2f}",
        f"-XMP-drone-dji:FlightYawDegree={((pose.heading + 180) % 360) - 180:+.2f}",
        str(path), "-execute",
    ]


def write_project(out: Path, shots: list, when_of: dict):
    """demo.calib.json: the panels already drawn and the readings typed in, ready to open and run.
    All paths are relative to the project file, so the folder works wherever it is copied or unpacked
    (the tool looks for images and folders next to the project when the stored path is not found)."""
    root = out.resolve()
    colours = {"black": "#E63946", "grey": "#2A9D8F", "white": "#E9C46A"}     # the tool's first three
    images = []
    for c in shots:
        path = (Path.cwd() / c["file"]).resolve()
        when = when_of[path.name]
        rel = path.relative_to(root).as_posix()
        images.append({"path": rel, "path_relative": rel,
                       "timestamp": when[:19], "timestamp_source": "XMP", "timestamp_tz": when[19:],
                       "targets": [{"name": k, "color": colours[k], "polygon": poly, "ground_truth": c["gun"][k]}
                                   for k, poly in c["panels"].items()]})
    project = {"tool": "thermal_calibration_tool", "version": "0.2.0", "saved": datetime.now().isoformat(timespec="seconds"),
               "title": "Demo flight (synthetic)", "mode": "thermal", "msi_single_point": False, "roi_inset_px": 3,
               "emissivity": {"enabled": False, "target_emissivity": 0.95, "scene_emissivity": 0.98, "sky_temperature": -40.0},
               "calibration_images": images,
               "survey": {"folder": "", "folder_relative": "survey", "filter": "",
                          "output_folder": "", "output_folder_relative": "calibrated",
                          "copy_metadata": True, "skip_existing": False}}
    (out / "demo.calib.json").write_text(json.dumps(project, indent=2), encoding="utf-8")
    (out / "README.txt").write_text(README_TXT, encoding="utf-8")


README_TXT = """Demo flight for the Thermal Calibration Tool
https://github.com/haystack-imaging/thermal-calibration-tool

Nothing in this folder is real data. The field, the reference panels and the camera are simulated, and
the camera drifts in a known way, so you can see the tool take the drift out.

How to try it:
1. Open demo.calib.json (desktop app: Open, then Open project; Python tool: File, Open project).
   The panels are drawn and the thermometer readings are typed in already.
2. Look at the fits in 2 Targets, then go to 3 Process and press Run.
   The calibrated images go into the folder "calibrated" next to this file.

calibration/       the two shots over the reference panels, at the start and at the end of the flight
survey/            the survey images
field_sheet.csv    the panel readings of the handheld IR thermometer
ground_truth.json  the camera's true gain and offset at every image, to check the results against
"""


# ----------------------------------------------------------------------------------------------- main
def plan(rng):
    """Poses and times: start shot, serpentine survey, end shot."""
    shots = []
    jitter = lambda: dict(roll=rng.normal(0, 0.6), pitch=rng.normal(0, 0.6))
    shots.append(("calibration", 0.0, Pose(7.55 + rng.normal(0, 0.1), -3.25, CAL_ALT, 6.0, **jitter())))
    minute = 1.6
    for i, x in enumerate(LINES_X):
        ys = STATIONS_Y if i % 2 == 0 else STATIONS_Y[::-1]
        heading = 0.0 if i % 2 == 0 else 180.0
        for y in ys:
            shots.append(("survey", minute, Pose(x + rng.normal(0, 0.25), y + rng.normal(0, 0.25),
                                                 SURVEY_ALT + rng.normal(0, 0.2), heading + rng.normal(0, 1.0), **jitter())))
            minute += SHOT_INTERVAL_S / 60
        minute += TURN_S / 60
    end = minute + 1.2
    shots.append(("calibration", end, Pose(7.2 + rng.normal(0, 0.1), -3.45, CAL_ALT + 0.4, -4.0, **jitter())))
    return shots


OPERATOR = {"start": (8.6, -3.85, 280.0), "survey": (4.6, -4.9, 200.0), "end": (5.6, -4.25, 60.0)}
CASE = (9.35, -2.45, 15.0)
_WORKER = {}


def scene_for(seed: int, key: str) -> Scene:
    """The same field every time; only the operator moves (next to the panels, by the car, elsewhere)."""
    return make_scene(np.random.default_rng(seed + 1), OPERATOR[key], CASE)


def _render_job(job):
    """Render, expose and write one image (runs in a worker process)."""
    seq, kind, key, minute, pose, path, when, seed, ss = job
    if _WORKER.get("seed") != seed:
        _WORKER.clear()
        _WORKER.update(seed=seed, cam=Camera(np.random.default_rng(seed + 2)))
    if key not in _WORKER:
        _WORKER[key] = scene_for(seed, key)
    img = _WORKER["cam"].capture(render_true(pose, minute, _WORKER[key], ss), minute,
                                 np.random.default_rng([seed, seq]))
    write_tif(Path(path), img, when)
    return seq


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("out", nargs="?", default=str(Path(__file__).resolve().parent / "flight"))
    ap.add_argument("--seed", type=int, default=2026)
    ap.add_argument("--supersample", type=int, default=2)
    ap.add_argument("--only", choices=("calibration", "survey"), help="render one kind only (for previews)")
    ap.add_argument("--limit", type=int, default=0, help="render at most N survey images (for previews)")
    ap.add_argument("--pick", default="", help="render only these survey images, e.g. 1,20,33 (for previews)")
    ap.add_argument("--every", type=int, default=1, help="keep every Nth survey image (1st, N+1th, ...), for a smaller flight")
    ap.add_argument("--jobs", type=int, default=os.cpu_count() or 1, help="parallel worker processes")
    args = ap.parse_args(argv)

    rng = np.random.default_rng(args.seed)
    out = Path(args.out)
    if out.exists() and any(out.iterdir()):
        if not (out / "ground_truth.json").exists():
            sys.exit(f"{out} is not empty and is not an earlier demo flight; choose another folder")
        shutil.rmtree(out)                    # an earlier demo flight: replace it
    (out / "calibration").mkdir(parents=True)
    (out / "survey").mkdir(parents=True)

    shots = plan(rng)
    cam = Camera(np.random.default_rng(args.seed + 2))
    have_exiftool = shutil.which("exiftool") is not None
    exif = []
    truth = {
        "note": "Synthetic data from demo/make_demo_flight.py. T_drone = gain * T_true + offset (+ fixed "
                "pattern, vignetting, noise); the tool should find m = 1/gain and b = -offset/gain.",
        "seed": args.seed, "start": T0.isoformat() + TZ, "camera": {"dfov_deg": DFOV_DEG, "k1": K1},
        "panels": {}, "plots": {}, "images": [],
    }
    sc0 = scene_for(args.seed, "survey")
    truth["plots"] = {"canopy_minus_air_C": np.round(sc0.plot_dt, 2).tolist(), "cover": np.round(sc0.plot_cover, 2).tolist(),
                      "layout": f"{N_RANGES} ranges (south to north) x {N_COLS} columns (west to east); columns 0-5 irrigated"}
    sheet = []
    shot_cfg = []
    n_survey = 0
    jobs = []
    for seq, (kind, minute, pose) in enumerate(shots, start=1):
        if args.only and kind != args.only:
            continue
        if kind == "survey":
            n_survey += 1
            if args.limit and n_survey > args.limit:
                continue
            if args.pick and n_survey not in {int(v) for v in args.pick.split(",")}:
                continue
            if args.every > 1 and (n_survey - 1) % args.every:
                continue
        when = T0 + timedelta(seconds=round(minute * 60))
        key = "survey" if kind == "survey" else ("start" if minute == 0 else "end")
        name = f"DJI_{when:%Y%m%d%H%M%S}_{seq:04d}_T.tif"
        path = out / kind / name
        jobs.append((seq, kind, key, minute, pose, str(path), when, args.seed, args.supersample))
        if have_exiftool:
            exif += exif_args(path, when, pose, rng)
        g, o = cam.gain(minute), cam.offset(minute)
        rec = {"file": f"{kind}/{name}", "kind": kind, "time": when.isoformat() + TZ, "minute": round(minute, 3),
               "gain": round(g, 6), "offset_C": round(o, 4), "expected_m": round(1 / g, 6), "expected_b": round(-o / g, 4),
               "pose": {"x_m": round(pose.x, 3), "y_m": round(pose.y, 3), "alt_m": round(pose.alt, 2),
                        "heading_deg": round(pose.heading, 2), "roll_deg": round(pose.roll, 2), "pitch_deg": round(pose.pitch, 2)}}
        if kind == "calibration":
            temps = {k: round(panel_temp(k, minute), 3) for k in PANELS}
            readings = {k: round(v + float(rng.normal(0, 0.1)), 1) for k, v in temps.items()}
            corners = {}
            a = math.radians(PANEL_HEADING)
            fx, fy = math.sin(a), math.cos(a)
            lx, ly = -fy, fx
            for k, (cx, cy) in panel_centres().items():
                s = PANEL_SIDE / 2
                pts = [(cx + sf * s * fx + sl * s * lx, cy + sf * s * fy + sl * s * ly)
                       for sf, sl in ((-1, 1), (1, 1), (1, -1), (-1, -1))]
                corners[k] = [[round(c, 1) for c in project(pose, px, py, 0.03)] for px, py in pts]
            rec.update({"panel_true_C": temps, "ir_thermometer_C": readings, "panel_corners_px": corners})
            for k in PANELS:
                sheet.append({"time": when.strftime("%H:%M:%S"), "image": name, "panel": k, "reading_C": readings[k]})
            shot_cfg.append({"file": str(path), "panels": {k: [[round(x), round(y)] for x, y in v] for k, v in corners.items()},
                             "gun": readings})
        truth["images"].append(rec)

    print(f"rendering {len(jobs)} images with {args.jobs} processes ...", flush=True)
    if args.jobs > 1 and len(jobs) > 1:
        with ProcessPoolExecutor(args.jobs) as ex:
            for i, _ in enumerate(ex.map(_render_job, jobs), start=1):
                print(f"\r  {i}/{len(jobs)}", end="", flush=True)
        print()
    else:
        for i, job in enumerate(jobs, start=1):
            _render_job(job)
            print(f"\r  {i}/{len(jobs)}", end="", flush=True)
        print()
    if exif:
        subprocess.run(["exiftool", "-@", "-", "-common_args", "-overwrite_original", "-q"], input="\n".join(exif) + "\n",
                       text=True, check=True)
    for k in PANELS:
        truth["panels"][k] = {"t_start_C": PANELS[k]["t0"], "warming_C_per_min": PANELS[k]["rate"]}
    (out / "ground_truth.json").write_text(json.dumps(truth, indent=2), encoding="utf-8")
    with open(out / "field_sheet.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["time", "image", "panel", "reading_C"])
        w.writeheader()
        w.writerows(sheet)
    if shot_cfg:
        cfg = {"title": "Demo flight (synthetic)", "calibration": shot_cfg, "survey_folder": str(out / "survey"),
               "output_folder": str(out / "calibrated"), "loupe_target": 2,
               "view": {"scale": 1.3, "center": [320, 256]}}
        ox, oy, _ = OPERATOR["start"]
        start_pose = next(p for k, m, p in shots if k == "calibration")
        cfg["hover"] = [round(c) for c in project(start_pose, ox, oy, 1.75)]        # the operator's head
        (out / "screenshot_config.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        write_project(out, shot_cfg, when_of={r["file"].split("/")[-1]: r["time"] for r in truth["images"]})
    print(f"done: {out}")


if __name__ == "__main__":
    sys.exit(main())
