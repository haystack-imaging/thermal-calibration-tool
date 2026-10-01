#!/usr/bin/env python3
"""
Image Calibration Tool (Thermal & Multispectral)
=================================================
Corrects sensor drift of drone thermal images (and single-band multispectral
images) with the Empirical Line Method, using field calibration targets whose
temperature (or reflectance) was measured with a reference instrument.

    T_corrected = m * T_drone + b

Coefficients are fitted per calibration image and interpolated in time across
all calibration images (start, any number of mid-flight shots, end).

Run the GUI:            python thermal_calibration_tool.py
Open a project:         python thermal_calibration_tool.py --project flight.calib.json
Headless batch run:     python thermal_calibration_tool.py --project flight.calib.json --run

The science (ROI statistics, regression, interpolation, batch processing) is in
plain functions with no Tk dependency so it can be tested and scripted; the GUI
sits on top.
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import os
import platform
import queue
import re
import shutil
import subprocess
import threading
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import cv2
import numpy as np
import pandas as pd
import rasterio
from PIL import Image, ImageTk
from rasterio.transform import from_bounds
from scipy import stats

try:  # optional: EXIF reading for JPEG/TIFF via piexif
    import piexif
    HAS_PIEXIF = True
except ImportError:  # pragma: no cover
    HAS_PIEXIF = False

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import tkinter.font as tkfont

try:  # optional: regression plot
    import matplotlib
    matplotlib.use("TkAgg")
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
    from matplotlib.figure import Figure
    HAS_MATPLOTLIB = True
except Exception:  # pragma: no cover
    HAS_MATPLOTLIB = False

__version__ = "0.2.0"
TOOL_NAME = "Thermal / MSI Calibration Tool"

# =============================================================================
# CONSTANTS
# =============================================================================

COLOR_PALETTE = [
    '#E63946',  # Red
    '#2A9D8F',  # Teal
    '#E9C46A',  # Yellow
    '#4C7BD9',  # Blue (lighter than before so it stays visible on dark areas)
    '#F4A261',  # Orange
    '#9B5DE5',  # Purple
    '#00F5D4',  # Cyan
    '#F15BB5',  # Pink
    '#00BBF9',  # Light Blue
    '#B8E986',  # Light green
]

IMAGE_EXTENSIONS = {'.tif', '.tiff', '.jpg', '.jpeg', '.png'}
DEFAULT_ROI_INSET_PX = 3        # pixels eroded from the polygon edge before taking statistics
MIN_ROI_PIXELS = 25             # fewer pixels than this gives a warning
MODE_THERMAL = "thermal"
MODE_MSI = "msi"
PROJECT_EXT = ".calib.json"


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class CalibrationTarget:
    """One reference target on one calibration image."""
    name: str
    color: str = '#FF0000'
    polygon: List[Tuple[int, int]] = field(default_factory=list)   # image pixel coordinates
    drone_mean: Optional[float] = None       # mean of the inner ROI, raw sensor units
    drone_std: Optional[float] = None
    pixel_count: int = 0
    ground_truth: Optional[float] = None     # None = not entered (0.0 is a valid value)
    inset_applied: bool = True               # False when the ROI was too small to erode

    def has_polygon(self) -> bool:
        return len(self.polygon) >= 3

    def is_complete(self) -> bool:
        return self.has_polygon() and self.drone_mean is not None and self.ground_truth is not None

    def to_dict(self) -> dict:
        return {"name": self.name, "color": self.color, "polygon": [list(p) for p in self.polygon],
                "ground_truth": self.ground_truth}

    @classmethod
    def from_dict(cls, d: dict) -> "CalibrationTarget":
        return cls(d.get("name", "Target"), d.get("color", COLOR_PALETTE[0]),
                   [tuple(int(v) for v in p) for p in d.get("polygon", [])], ground_truth=d.get("ground_truth"))


@dataclass
class FitResult:
    """Result of a calibration fit, with the numbers needed for an uncertainty statement."""
    slope: float
    intercept: float
    n: int
    single_point: bool = False
    r_squared: Optional[float] = None
    rmse: Optional[float] = None                 # root mean square of residuals (n-1 ... biased, descriptive)
    resid_std: Optional[float] = None            # residual standard error, s = sqrt(SSE / (n-2)), n > 2
    se_slope: Optional[float] = None
    se_intercept: Optional[float] = None
    loo_rmse: Optional[float] = None             # leave-one-out RMSE, n >= 3
    residuals: List[float] = field(default_factory=list)
    x_values: List[float] = field(default_factory=list)
    y_values: List[float] = field(default_factory=list)
    x_mean: float = 0.0
    sxx: float = 0.0
    x_min: float = 0.0
    x_max: float = 0.0

    def predict(self, x):
        return self.slope * x + self.intercept

    def predict_uncertainty(self, x0: float) -> Optional[float]:
        """1-sigma uncertainty of a calibrated value at sensor value x0 (prediction interval width).
        Needs at least 3 targets; with 2 the line passes through both points and nothing is left to
        estimate the scatter from."""
        if self.single_point or self.resid_std is None or self.n < 3 or self.sxx <= 0:
            return None
        return float(self.resid_std * math.sqrt(1.0 + 1.0 / self.n + (x0 - self.x_mean) ** 2 / self.sxx))

    def summary_line(self, unit: str) -> str:
        if self.single_point:
            return f"K = {self.slope:.8f}"
        s = f"m = {self.slope:.4f}, b = {self.intercept:.4f}, R² = {self.r_squared:.4f}, n = {self.n}"
        if self.rmse is not None:
            s += f", RMSE = {self.rmse:.2f} {unit}"
        return s


@dataclass
class EmissivitySettings:
    """Optional emissivity correction (thermal only). See README: usually leave it off."""
    enabled: bool = False
    target_emissivity: float = 0.95     # emissivity of the calibration panels
    scene_emissivity: float = 0.98      # emissivity of the surveyed surface (vegetation ~0.97-0.99)
    sky_temperature: float = -40.0      # reflected (sky) temperature, C

    def to_dict(self) -> dict:
        return {"enabled": self.enabled, "target_emissivity": self.target_emissivity,
                "scene_emissivity": self.scene_emissivity, "sky_temperature": self.sky_temperature}

    @classmethod
    def from_dict(cls, d: dict) -> "EmissivitySettings":
        return cls(bool(d.get("enabled", False)), float(d.get("target_emissivity", 0.95)),
                   float(d.get("scene_emissivity", 0.98)), float(d.get("sky_temperature", -40.0)))


@dataclass
class Timestamp:
    value: Optional[datetime]
    source: str = "none"        # 'XMP', 'EXIF', 'TIFF tag', 'file mtime', 'none'
    tz: str = ""                # '+02:00', 'Z', '' when the source carries no zone

    @property
    def reliable(self) -> bool:
        return self.value is not None and self.source not in ("file mtime", "none")


@dataclass
class CalibrationEvent:
    """One calibration image: its capture time, its targets and the fitted coefficients."""
    image_path: str
    timestamp: datetime
    timestamp_source: str = "none"
    timestamp_tz: str = ""
    targets: List[CalibrationTarget] = field(default_factory=list)
    fit: Optional[FitResult] = None

    def __post_init__(self):
        if not self.targets:
            self.targets = [CalibrationTarget('Target 1', COLOR_PALETTE[0]),
                            CalibrationTarget('Target 2', COLOR_PALETTE[1])]

    # --- compatibility with the first release -----------------------------
    @property
    def slope_m(self) -> float:
        return self.fit.slope if self.fit else 1.0

    @property
    def intercept_b(self) -> float:
        return self.fit.intercept if self.fit else 0.0

    @property
    def r_squared(self) -> float:
        return (self.fit.r_squared or 0.0) if self.fit else 0.0

    @property
    def is_fitted(self) -> bool:
        return self.fit is not None

    def add_target(self) -> CalibrationTarget:
        idx = len(self.targets)
        target = CalibrationTarget(f'Target {idx + 1}', COLOR_PALETTE[idx % len(COLOR_PALETTE)])
        self.targets.append(target)
        return target

    def remove_target(self, index: int, min_targets: int = 2) -> bool:
        if len(self.targets) <= min_targets or not (0 <= index < len(self.targets)):
            return False
        self.targets.pop(index)
        for i, t in enumerate(self.targets):       # keep names and colours in order
            t.name = f'Target {i + 1}'
            t.color = COLOR_PALETTE[i % len(COLOR_PALETTE)]
        return True

    def to_dict(self) -> dict:
        return {"path": self.image_path, "timestamp": self.timestamp.isoformat(),
                "timestamp_source": self.timestamp_source, "timestamp_tz": self.timestamp_tz,
                "targets": [t.to_dict() for t in self.targets]}


# =============================================================================
# TIMESTAMPS
# =============================================================================

_TZ_RE = re.compile(r'(Z|[+-]\d{2}:?\d{2})$')


def _parse_datetime_string(dt_str: str) -> Tuple[Optional[datetime], str]:
    """Parse a date string. Returns (naive datetime in the *camera's local wall time*, zone string).

    The zone is only recorded, never applied: EXIF times are naive local wall time, and XMP times
    from the same camera carry the local offset, so stripping the offset keeps both consistent.
    A trailing 'Z' (UTC) is kept as a flag so the pre-flight check can warn about mixed zones.
    """
    if not dt_str or not dt_str.strip():
        return None, ""
    dt_str = dt_str.strip()
    tz = ""
    m = _TZ_RE.search(dt_str)
    if m:
        tz = m.group(1)
        dt_str = dt_str[:m.start()]
    candidates = [dt_str]
    if dt_str[4:5] == ':' and dt_str[7:8] == ':':      # EXIF style 2026:06:01 10:00:00
        candidates.append(dt_str[:4] + '-' + dt_str[5:7] + '-' + dt_str[8:])
    for cand in candidates:
        try:
            return datetime.fromisoformat(cand.replace(' ', 'T', 1)), tz
        except ValueError:
            pass
    for fmt in ('%Y:%m:%d %H:%M:%S', '%Y:%m:%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M:%S.%f',
                '%Y-%m-%dT%H:%M:%S', '%Y-%m-%dT%H:%M:%S.%f'):
        try:
            return datetime.strptime(dt_str, fmt), tz
        except ValueError:
            continue
    return None, ""


def extract_timestamp(image_path: str) -> Timestamp:
    """Capture time of an image. Tries XMP, EXIF (piexif / Pillow), TIFF tags, then file mtime.

    The source is recorded so the processing log can show it and the pre-flight check can warn when
    the tool had to fall back to the file modification time (which changes on every copy).
    """
    # 1) XMP packet (DJI writes xmp:CreateDate with the local offset)
    try:
        with open(image_path, 'rb') as f:
            head = f.read(4 * 1024 * 1024)      # metadata sits at the start of the file
        xs, xe = head.find(b'<?xpacket begin'), head.find(b'<?xpacket end')
        xmp = head[xs:xe + 200] if 0 <= xs < xe else head[:262144]
        m = re.search(rb'xmp:CreateDate="([^"]+)"', xmp) or re.search(rb'CreateDate="([^"]+)"', xmp) \
            or re.search(rb'<xmp:CreateDate>([^<]+)</xmp:CreateDate>', xmp)
        if m:
            dt, tz = _parse_datetime_string(m.group(1).decode('utf-8', errors='ignore'))
            if dt:
                return Timestamp(dt, "XMP", tz)
    except OSError:
        pass
    # 2) EXIF via piexif
    if HAS_PIEXIF:
        try:
            exif_dict = piexif.load(str(image_path))
            raw = exif_dict.get('Exif', {}).get(piexif.ExifIFD.DateTimeOriginal)
            source = "EXIF"
            if not raw:          # tag 306 in the 0th IFD is the plain TIFF DateTime tag
                raw = exif_dict.get('0th', {}).get(piexif.ImageIFD.DateTime)
                source = "TIFF tag"
            if raw:
                dt, tz = _parse_datetime_string(raw.decode('utf-8', errors='ignore') if isinstance(raw, bytes) else str(raw))
                if dt:
                    return Timestamp(dt, source, tz)
        except Exception:
            pass
    # 3) GDAL / rasterio tags (TIFF DateTime, EXIF domain)
    try:
        with rasterio.open(image_path) as src:
            tags = src.tags()
            exif_tags = src.tags(ns='EXIF') if 'EXIF' in src.tag_namespaces() else {}
            for key in ('EXIF_DateTimeOriginal', 'EXIF_DateTime'):
                if exif_tags.get(key):
                    dt, tz = _parse_datetime_string(exif_tags[key])
                    if dt:
                        return Timestamp(dt, "EXIF", tz)
            for key in ('TIFFTAG_DATETIME', 'DateTime', 'datetime', 'TIFFTAG_DATETIMEORIGINAL', 'DateTimeOriginal'):
                if tags.get(key):
                    dt, tz = _parse_datetime_string(tags[key])
                    if dt:
                        return Timestamp(dt, "TIFF tag", tz)
    except Exception:
        pass
    # 4) Pillow EXIF
    try:
        with Image.open(image_path) as img:
            exif = img.getexif()
            for tag in (36867, 306):          # DateTimeOriginal, DateTime
                val = exif.get(tag) if exif else None
                if val:
                    dt, tz = _parse_datetime_string(str(val))
                    if dt:
                        return Timestamp(dt, "EXIF", tz)
    except Exception:
        pass
    # 5) Last resort
    try:
        return Timestamp(datetime.fromtimestamp(os.path.getmtime(image_path)), "file mtime", "")
    except OSError:
        return Timestamp(None, "none", "")


def extract_datetime_from_exif(image_path: str) -> Optional[datetime]:
    """Kept for compatibility: just the datetime."""
    return extract_timestamp(image_path).value


# =============================================================================
# IMAGES
# =============================================================================

@dataclass
class ImageInfo:
    path: str
    width: int = 0
    height: int = 0
    bands: int = 0
    dtype: str = ""
    driver: str = ""
    error: str = ""

    @property
    def is_single_band(self) -> bool:
        return self.bands == 1

    @property
    def is_float(self) -> bool:
        return self.dtype.startswith('float')

    @property
    def is_8bit(self) -> bool:
        return self.dtype in ('uint8', 'int8')

    def describe(self) -> str:
        if self.error:
            return f"unreadable ({self.error})"
        return f"{self.width}×{self.height}, {self.bands} band{'s' if self.bands != 1 else ''}, {self.dtype}"


def inspect_image(image_path: str) -> ImageInfo:
    """Band count, size and data type without reading the pixels."""
    info = ImageInfo(path=str(image_path))
    try:
        with rasterio.open(image_path) as src:
            info.width, info.height, info.bands = src.width, src.height, src.count
            info.dtype, info.driver = str(src.dtypes[0]), str(src.driver)
            return info
    except Exception as e:
        err = str(e)
    try:
        with Image.open(image_path) as img:
            info.width, info.height = img.size
            info.bands = len(img.getbands())
            info.dtype = {'F': 'float32', 'I': 'int32', 'I;16': 'uint16', 'L': 'uint8', 'RGB': 'uint8',
                          'RGBA': 'uint8'}.get(img.mode, img.mode)
            info.driver = img.format or ""
            return info
    except Exception as e2:
        info.error = f"{err}; {e2}"
    return info


def load_thermal_image(image_path: str) -> np.ndarray:
    """Load a single-band image as float32 (assumed to be in the final units: C or DN).

    Raises ValueError for multi-band images (colour previews, R-JPEGs) because their pixel values
    are palette colours, not temperatures.
    """
    info = inspect_image(image_path)
    if info.error:
        raise ValueError(f"cannot read {os.path.basename(image_path)}: {info.error}")
    if not info.is_single_band:
        raise ValueError(f"{os.path.basename(image_path)} has {info.bands} bands. Only single-band images hold "
                         "temperatures or DN; a colour R-JPEG preview must be converted first (see README).")
    try:
        with rasterio.open(image_path) as src:
            return src.read(1).astype(np.float32)
    except Exception:
        with Image.open(image_path) as img:
            return np.array(img, dtype=np.float32)


def apply_clahe(image: np.ndarray) -> np.ndarray:
    normalized = cv2.normalize(image, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)
    return cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(normalized)


def apply_inferno_colormap(image: np.ndarray) -> np.ndarray:
    normalized = cv2.normalize(image, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)
    return cv2.cvtColor(cv2.applyColorMap(normalized, cv2.COLORMAP_INFERNO), cv2.COLOR_BGR2RGB)


def make_display_image(image: np.ndarray, mode: str) -> np.ndarray:
    """RGB uint8 preview of a float image. mode: 'inferno', 'grey', 'clahe'."""
    if mode == "inferno":
        return apply_inferno_colormap(image)
    if mode == "clahe":
        return cv2.cvtColor(apply_clahe(image), cv2.COLOR_GRAY2RGB)
    g = cv2.normalize(image, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)
    return cv2.cvtColor(g, cv2.COLOR_GRAY2RGB)


# =============================================================================
# ROI STATISTICS
# =============================================================================

def polygon_mask(polygon: List[Tuple[int, int]], image_shape: Tuple[int, ...],
                 inset_px: int = DEFAULT_ROI_INSET_PX) -> Tuple[np.ndarray, bool]:
    """Mask of the polygon shrunk by `inset_px` pixels on every side (binary erosion).

    The microbolometer blurs edges over 2-3 px, so pixels near the target edge are mixed with the
    background. A fixed inset in pixels removes them regardless of target size (the old 'inner 80%'
    scaled the polygon, which on a 12 px target trimmed well under one pixel).
    Returns (mask, inset_applied). When the erosion would empty the mask, the full polygon is used
    and inset_applied is False.
    """
    mask = np.zeros(image_shape[:2], dtype=np.uint8)
    pts = np.array(polygon, dtype=np.int32).reshape(-1, 1, 2)
    cv2.fillPoly(mask, [pts], 255)
    if inset_px <= 0:
        return mask, True
    k = np.ones((2 * inset_px + 1, 2 * inset_px + 1), np.uint8)
    eroded = cv2.erode(mask, k)
    if eroded.any():
        return eroded, True
    return mask, False


def inner_roi_contour(polygon: List[Tuple[int, int]], image_shape: Tuple[int, ...],
                      inset_px: int = DEFAULT_ROI_INSET_PX) -> List[Tuple[int, int]]:
    """Outline of the eroded region, for drawing."""
    mask, applied = polygon_mask(polygon, image_shape, inset_px)
    if not applied:
        return []
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return []
    c = max(contours, key=cv2.contourArea)
    c = cv2.approxPolyDP(c, 0.8, True)
    return [(int(p[0][0]), int(p[0][1])) for p in c]


@dataclass
class RoiStats:
    mean: float
    std: float
    count: int
    inset_applied: bool


def extract_roi_statistics(image: np.ndarray, polygon: List[Tuple[int, int]],
                           inset_px: int = DEFAULT_ROI_INSET_PX) -> RoiStats:
    mask, applied = polygon_mask(polygon, image.shape, inset_px)
    values = image[mask > 0]
    values = values[np.isfinite(values)]
    if values.size == 0:
        return RoiStats(float('nan'), float('nan'), 0, applied)
    return RoiStats(float(np.mean(values)), float(np.std(values)), int(values.size), applied)


# =============================================================================
# PHYSICS
# =============================================================================

def apply_emissivity_correction(T_app: np.ndarray, emissivity: float, T_sky: float) -> np.ndarray:
    """Apparent -> surface temperature for a grey body, broadband Stefan-Boltzmann approximation:
        T_obj^4 = (T_app^4 - (1 - e) * T_sky^4) / e
    A band-limited (8-14 um) Planck inversion would be more exact; the difference is small for the
    emissivities used here (>= 0.9). Note that the DJI SDK already applies its own emissivity
    setting (0.95 by default) when converting R-JPEGs, so only enable this for images converted
    with emissivity 1.0.
    """
    T_app_K = np.asarray(T_app, dtype=np.float64) + 273.15
    T_sky_K = T_sky + 273.15
    T_obj_K4 = (T_app_K ** 4 - (1.0 - emissivity) * T_sky_K ** 4) / emissivity
    T_obj_K4 = np.maximum(T_obj_K4, 0.0)
    return (np.power(T_obj_K4, 0.25) - 273.15).astype(np.float32)


# =============================================================================
# FITTING
# =============================================================================

def compute_linear_regression(drone_values: List[float], ground_truths: List[float]) -> FitResult:
    """Ordinary least squares  truth = m * drone + b  with residual diagnostics.

    With two targets the line passes through both points, so R² is 1 by construction and no
    scatter can be estimated; RMSE and the standard errors are then None.
    """
    x = np.asarray(drone_values, dtype=np.float64)
    y = np.asarray(ground_truths, dtype=np.float64)
    n = int(x.size)
    if n < 2:
        raise ValueError("at least two targets are needed for a linear fit")
    if np.ptp(x) == 0:
        raise ValueError("all targets have the same sensor value; the slope cannot be determined")
    res = stats.linregress(x, y)
    slope, intercept = float(res.slope), float(res.intercept)
    pred = slope * x + intercept
    resid = y - pred
    fit = FitResult(slope=slope, intercept=intercept, n=n, r_squared=float(res.rvalue ** 2),
                    residuals=[float(r) for r in resid], x_values=[float(v) for v in x],
                    y_values=[float(v) for v in y], x_mean=float(x.mean()),
                    sxx=float(((x - x.mean()) ** 2).sum()), x_min=float(x.min()), x_max=float(x.max()))
    if n > 2:
        sse = float((resid ** 2).sum())
        fit.rmse = math.sqrt(sse / n)
        fit.resid_std = math.sqrt(sse / (n - 2))
        fit.se_slope = float(res.stderr)
        fit.se_intercept = float(res.intercept_stderr)
        loo = []
        for i in range(n):
            keep = np.arange(n) != i
            if np.ptp(x[keep]) == 0:
                continue
            r = stats.linregress(x[keep], y[keep])
            loo.append(y[i] - (r.slope * x[i] + r.intercept))
        if loo:
            fit.loo_rmse = float(np.sqrt(np.mean(np.square(loo))))
    return fit


def compute_single_point(drone_value: float, ground_truth: float) -> FitResult:
    """MSI single-point calibration: reflectance = K * DN, K from one panel."""
    if drone_value == 0:
        raise ValueError("panel DN is zero")
    k = float(ground_truth) / float(drone_value)
    return FitResult(slope=k, intercept=0.0, n=1, single_point=True, r_squared=None,
                     x_values=[float(drone_value)], y_values=[float(ground_truth)],
                     x_mean=float(drone_value), x_min=float(drone_value), x_max=float(drone_value))


def recompute_fit(event: CalibrationEvent, single_point: bool, thermal: bool,
                  emissivity: Optional[EmissivitySettings] = None) -> Optional[str]:
    """Fit the event's coefficients from its complete targets. Returns a warning text or None."""
    targets = event.targets[:1] if single_point else event.targets
    xs, ys = [], []
    for t in targets:
        if t.is_complete():
            x = t.drone_mean
            if thermal and emissivity and emissivity.enabled:
                # correct the panel reading with the panel's own emissivity so the fit and the
                # survey images (corrected with the scene emissivity) live in the same space
                x = float(apply_emissivity_correction(np.array([x]), emissivity.target_emissivity,
                                                      emissivity.sky_temperature)[0])
            xs.append(x)
            ys.append(t.ground_truth)
    event.fit = None
    try:
        if single_point:
            if xs:
                event.fit = compute_single_point(xs[0], ys[0])
        elif len(xs) >= 2:
            event.fit = compute_linear_regression(xs, ys)
    except ValueError as e:
        return str(e)
    return None


# =============================================================================
# TEMPORAL INTERPOLATION
# =============================================================================

def coefficients_at(timestamp: datetime, events: List[CalibrationEvent]
                    ) -> Tuple[float, float, str, Tuple[CalibrationEvent, CalibrationEvent]]:
    """Piecewise-linear interpolation of (m, b) through *all* fitted calibration events.

    Returns (m, b, position, bracketing pair). position is 'single', 'before', 'between' or
    'after'. Outside the calibration window the nearest event's coefficients are used unchanged
    (clamped), and the pair is the nearest two events so a drift estimate can still be given.
    """
    fitted = sorted([e for e in events if e.fit is not None], key=lambda e: e.timestamp)
    if not fitted:
        raise ValueError("no fitted calibration events")
    if len(fitted) == 1:
        e = fitted[0]
        return e.fit.slope, e.fit.intercept, 'single', (e, e)
    ts = [e.timestamp.timestamp() for e in fitted]
    t = timestamp.timestamp()
    if t <= ts[0]:
        return fitted[0].fit.slope, fitted[0].fit.intercept, 'before', (fitted[0], fitted[1])
    if t >= ts[-1]:
        return fitted[-1].fit.slope, fitted[-1].fit.intercept, 'after', (fitted[-2], fitted[-1])
    i = bisect.bisect_right(ts, t) - 1
    e0, e1 = fitted[i], fitted[i + 1]
    f = (t - ts[i]) / (ts[i + 1] - ts[i])
    m = e0.fit.slope + f * (e1.fit.slope - e0.fit.slope)
    b = e0.fit.intercept + f * (e1.fit.intercept - e0.fit.intercept)
    return float(m), float(b), 'between', (e0, e1)


def interpolate_coefficients(timestamp: datetime, event_start: CalibrationEvent,
                             event_end: Optional[CalibrationEvent] = None) -> Tuple[float, float]:
    """Kept for compatibility with the first release (two events)."""
    events = [event_start] + ([event_end] if event_end is not None else [])
    m, b, _, _ = coefficients_at(timestamp, events)
    return m, b


# =============================================================================
# OUTPUT
# =============================================================================

def save_calibrated_geotiff(image: np.ndarray, output_path: str, source_path: Optional[str] = None,
                            extra_tags: Optional[Dict[str, str]] = None, copy_metadata: bool = True
                            ) -> List[str]:
    """Write a 32-bit float GeoTIFF (LZW, floating-point predictor).

    Georeferencing, GDAL metadata (TIFFTAG_DATETIME etc.) and the EXIF domain are copied from the
    source. If exiftool is on the PATH, the full EXIF/XMP/GPS block is copied too, so photogrammetry
    software keeps camera positions and capture times. Returns notes for the log.
    """
    notes: List[str] = []
    height, width = image.shape[:2]
    transform, crs = None, None
    src_tags: Dict[str, str] = {}
    src_exif: Dict[str, str] = {}
    if source_path:
        try:
            with rasterio.open(source_path) as src:
                transform, crs = src.transform, src.crs
                if copy_metadata:
                    src_tags = dict(src.tags())
                    if 'EXIF' in src.tag_namespaces():
                        src_exif = dict(src.tags(ns='EXIF'))
        except Exception as e:
            notes.append(f"could not read georeferencing of source: {e}")
    if transform is None or transform.is_identity:
        transform = from_bounds(0, 0, width, height, width, height)
    with rasterio.open(output_path, 'w', driver='GTiff', height=height, width=width, count=1,
                       dtype=np.float32, crs=crs, transform=transform, compress='LZW', predictor=3) as dst:
        dst.write(image.astype(np.float32), 1)
        tags = {k: v for k, v in src_tags.items() if not k.startswith('TIFFTAG_SOFTWARE')}
        tags.update(src_exif)
        if extra_tags:
            tags.update(extra_tags)
        tags['TIFFTAG_SOFTWARE'] = f"{TOOL_NAME} {__version__}"
        if tags:
            dst.update_tags(**tags)
    if copy_metadata and source_path and shutil.which('exiftool'):
        try:
            # copy EXIF/XMP/GPS, but never the source's GDAL metadata, software tag or GeoTIFF keys:
            # those describe the *output* (calibration formula, our georeferencing) and were just written
            subprocess.run(['exiftool', '-overwrite_original', '-q', '-q', '-tagsFromFile', str(source_path),
                            '-all:all', '-xmp', '-gps:all', '-EXIF:DateTimeOriginal',
                            '--GDALMetadata', '--GDALNoData', '--Software', '--GeoTiffDirectory',
                            '--GeoTiffDoubleParams', '--GeoTiffAsciiParams', '--ModelTiePoint', '--PixelScale',
                            '--ModelTransform', str(output_path)],
                           check=True, capture_output=True, timeout=60)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            notes.append(f"exiftool metadata copy failed: {e}")
    return notes


def project_filename(project: Optional[dict]) -> str:
    """`<title>.calib.json`, the file name a project is saved under next to the outputs."""
    title = (project or {}).get("title") or "calibration_project"
    safe = re.sub(r'[^\w.-]+', '_', str(title)).strip('_') or "calibration_project"
    return safe + PROJECT_EXT


def output_filenames(files: List[Path]) -> Dict[Path, str]:
    """`stem_calibrated.tif`, with the extension added when two inputs share a stem."""
    stems: Dict[str, int] = {}
    for p in files:
        stems[p.stem] = stems.get(p.stem, 0) + 1
    out = {}
    for p in files:
        if stems[p.stem] > 1:
            out[p] = f"{p.stem}_{p.suffix.lstrip('.').lower()}_calibrated.tif"
        else:
            out[p] = f"{p.stem}_calibrated.tif"
    return out


# =============================================================================
# BATCH PROCESSING (no Tk)
# =============================================================================

@dataclass
class BatchJob:
    mode: str
    single_point: bool
    events: List[CalibrationEvent]
    survey_files: List[Path]
    output_folder: Path
    emissivity: EmissivitySettings = field(default_factory=EmissivitySettings)
    roi_inset_px: int = DEFAULT_ROI_INSET_PX
    copy_metadata: bool = True
    skip_existing: bool = False
    name_filter: str = ""
    project: Optional[dict] = None

    @property
    def thermal(self) -> bool:
        return self.mode == MODE_THERMAL

    @property
    def unit(self) -> str:
        return "°C" if self.thermal else ("reflectance" if self.single_point or not self.thermal else "DN")


@dataclass
class PreflightReport:
    lines: List[Tuple[str, str]] = field(default_factory=list)    # (level, text), level ok|warn|block
    files: List[Path] = field(default_factory=list)              # files that will be processed
    timestamps: Dict[Path, Timestamp] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return any(level == 'block' for level, _ in self.lines)

    def add(self, level: str, text: str):
        self.lines.append((level, text))

    def text(self) -> str:
        sym = {'ok': '✓', 'warn': '⚠', 'block': '✗'}
        return "\n".join(f"{sym.get(l, '-')} {t}" for l, t in self.lines)


def collect_survey_files(folder: Optional[str], explicit_files: Optional[List[str]], name_filter: str,
                         calibration_paths: List[str]) -> List[Path]:
    files: List[Path] = []
    if explicit_files:
        files = [Path(p) for p in explicit_files if Path(p).suffix.lower() in IMAGE_EXTENSIONS]
    elif folder and os.path.isdir(folder):
        files = [p for p in sorted(Path(folder).iterdir()) if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS]
    if name_filter:
        files = [p for p in files if name_filter in p.name]
    calib = {Path(p).resolve() for p in calibration_paths}
    files = [p for p in files if p.resolve() not in calib and not p.stem.endswith('_calibrated')]
    return sorted(files, key=lambda p: p.name)


def preflight(job: BatchJob, timestamps: Optional[Dict[Path, Timestamp]] = None) -> PreflightReport:
    """Everything that can go wrong, listed before a single pixel is written.
    `timestamps` may carry already extracted capture times (the GUI scans them for the timeline)."""
    rep = PreflightReport()
    fitted = sorted([e for e in job.events if e.fit is not None], key=lambda e: e.timestamp)
    if not fitted:
        rep.add('block', "No calibration image has a complete fit yet.")
        return rep
    unfitted = [e for e in job.events if e.fit is None]
    if unfitted:
        rep.add('warn', f"{len(unfitted)} calibration image(s) without a fit are ignored: "
                        + ", ".join(os.path.basename(e.image_path) for e in unfitted))
    rep.add('ok', f"{len(fitted)} calibration event(s): " + ", ".join(
        f"{e.timestamp:%H:%M:%S} ({e.timestamp_source})" for e in fitted))
    for e in fitted:
        if not e.fit.single_point and e.fit.n == 2:
            rep.add('warn', f"{os.path.basename(e.image_path)}: only 2 targets, R² is 1 by construction and the "
                            "fit uncertainty cannot be estimated. A third target would fix that.")
        if e.timestamp_source in ('file mtime', 'none'):
            rep.add('warn', f"{os.path.basename(e.image_path)}: capture time taken from the file modification "
                            "time, which changes on every copy.")
    if not job.survey_files:
        rep.add('block', "No survey images found (check folder, filter and that calibration images are excluded).")
        return rep

    usable: List[Path] = []
    multiband, eightbit, unreadable = [], [], []
    src_counts: Dict[str, int] = {}
    zones: Dict[str, int] = {}
    before = after = 0
    t0, t1 = fitted[0].timestamp, fitted[-1].timestamp
    for p in job.survey_files:
        info = inspect_image(str(p))
        if info.error:
            unreadable.append(p.name)
            continue
        if not info.is_single_band:
            multiband.append(p.name)
            continue
        if info.is_8bit and job.thermal:
            eightbit.append(p.name)
        ts = (timestamps or {}).get(p) or extract_timestamp(str(p))
        rep.timestamps[p] = ts
        src_counts[ts.source] = src_counts.get(ts.source, 0) + 1
        zones[ts.tz or 'none'] = zones.get(ts.tz or 'none', 0) + 1
        if ts.value is not None and len(fitted) > 1:
            if ts.value < t0:
                before += 1
            elif ts.value > t1:
                after += 1
        usable.append(p)
    rep.files = usable
    if multiband:
        rep.add('warn', f"{len(multiband)} colour image(s) skipped (pixel values are palette colours, not "
                        f"temperatures): {', '.join(multiband[:4])}{' …' if len(multiband) > 4 else ''}. "
                        "Convert R-JPEGs to single-band temperature TIFFs first (README).")
    if unreadable:
        rep.add('warn', f"{len(unreadable)} unreadable file(s) skipped: {', '.join(unreadable[:4])}")
    if eightbit:
        rep.add('warn', f"{len(eightbit)} image(s) are 8-bit integers; those are usually stretched previews, not "
                        f"temperatures: {', '.join(eightbit[:4])}")
    if not usable:
        rep.add('block', "None of the survey images can be processed.")
        return rep
    rep.add('ok', f"{len(usable)} image(s) will be processed.")
    src_txt = ", ".join(f"{n} from {s}" for s, n in sorted(src_counts.items(), key=lambda kv: -kv[1]))
    level = 'warn' if src_counts.get('file mtime') or src_counts.get('none') else 'ok'
    rep.add(level, f"Capture times: {src_txt}." + (" File modification times change on every copy; "
                                                   "interpolation between calibration shots is then meaningless."
                                                   if level == 'warn' else ""))
    cal_zones = {e.timestamp_tz or 'none' for e in fitted}
    if len(set(zones) | cal_zones) > 1 and ('Z' in zones or 'Z' in cal_zones):
        rep.add('warn', "Some timestamps are in UTC ('Z') and others in local time; check that calibration and "
                        "survey images were converted the same way, otherwise they are hours apart.")
    if len(fitted) > 1:
        if before:
            rep.add('warn', f"{before} image(s) taken before the first calibration shot ({t0:%H:%M:%S}): the first "
                            "coefficients are used unchanged.")
        if after:
            rep.add('warn', f"{after} image(s) taken after the last calibration shot ({t1:%H:%M:%S}): the last "
                            "coefficients are used unchanged.")
        if not before and not after:
            rep.add('ok', f"All images lie between the first and last calibration shot "
                          f"({t0:%H:%M:%S} to {t1:%H:%M:%S}); coefficients are interpolated.")
    else:
        rep.add('ok', "Single calibration: one set of coefficients is applied to every image "
                      "(drift during the flight is not corrected).")
    names = output_filenames(usable)
    existing = [p for p in usable if (job.output_folder / names[p]).exists()]
    if existing:
        rep.add('warn', f"{len(existing)} output file(s) already exist in the output folder and will be "
                        f"{'skipped' if job.skip_existing else 'overwritten'}.")
    try:
        if usable and job.output_folder.resolve() == usable[0].parent.resolve():
            rep.add('warn', "Output folder is the input folder. Outputs are written next to the inputs "
                            "(they are excluded from later runs by their _calibrated suffix).")
    except OSError:
        pass
    if job.thermal and job.emissivity.enabled:
        rep.add('warn', f"Emissivity correction is ON (panels ε={job.emissivity.target_emissivity}, scene "
                        f"ε={job.emissivity.scene_emissivity}). Only correct if the images were converted "
                        "with emissivity 1.0; the DJI SDK applies 0.95 by default.")
    return rep


def run_batch(job: BatchJob, log: Callable[[str], None], progress: Callable[[int, int, str], None],
              is_cancelled: Callable[[], bool], timestamps: Optional[Dict[Path, Timestamp]] = None,
              on_row: Optional[Callable[[dict], None]] = None) -> dict:
    """Calibrate every survey image. Pure function: safe to run in a worker thread.
    `on_row` is called with each finished row (for a live results table)."""
    fitted = sorted([e for e in job.events if e.fit is not None], key=lambda e: e.timestamp)
    if not fitted:
        raise ValueError("no fitted calibration events")
    out_dir = job.output_folder
    out_dir.mkdir(parents=True, exist_ok=True)
    names = output_filenames(job.survey_files)
    unit = 'celsius' if job.thermal else 'reflectance'
    em = job.emissivity if job.thermal else EmissivitySettings()
    log(f"Mode: {'Thermal' if job.thermal else 'MSI'}" + (" (single-point K)" if job.single_point else ""))
    log(f"Calibration events: {len(fitted)}  →  " + ("interpolated in time" if len(fitted) > 1 else "constant"))
    if em.enabled:
        log(f"Emissivity correction ON: panels ε={em.target_emissivity}, scene ε={em.scene_emissivity}, "
            f"T_sky={em.sky_temperature} °C")
    log(f"{len(job.survey_files)} image(s) → {out_dir}")

    rows: List[dict] = []
    n_ok = n_err = n_skip = 0
    total = len(job.survey_files)
    for i, img_path in enumerate(job.survey_files):
        if is_cancelled():
            log("Cancelled.")
            break
        name = img_path.name
        out_name = names[img_path]
        out_path = out_dir / out_name
        progress(i, total, name)
        row: Dict[str, Any] = {'input_filename': name, 'output_filename': out_name}
        try:
            if job.skip_existing and out_path.exists():
                n_skip += 1
                row['status'] = 'skipped (exists)'
                rows.append(row)
                if on_row:
                    on_row(row)
                continue
            ts = (timestamps or {}).get(img_path) or extract_timestamp(str(img_path))
            when = ts.value or datetime.now()
            m, b, position, pair = coefficients_at(when, fitted)
            img = load_thermal_image(str(img_path))
            finite = np.isfinite(img)
            o_mean, o_min, o_max = float(np.mean(img[finite])), float(np.min(img[finite])), float(np.max(img[finite]))
            if em.enabled:
                img = apply_emissivity_correction(img, em.scene_emissivity, em.sky_temperature)
            calibrated = (m * img + b).astype(np.float32)
            c_mean = float(np.mean(calibrated[finite]))
            # --- regression prediction uncertainty at the image's mean sensor value (needs >= 3 targets)
            x0 = float(np.mean(img[finite]))
            e0, e1 = pair
            u_fit_vals = [u for u in (e0.fit.predict_uncertainty(x0), e1.fit.predict_uncertainty(x0)) if u is not None]
            u_fit = max(u_fit_vals) if u_fit_vals else None
            xr_min = min(e.fit.x_min for e in (e0, e1))
            xr_max = max(e.fit.x_max for e in (e0, e1))
            outside = float(np.mean((img[finite] < xr_min) | (img[finite] > xr_max)) * 100.0)
            tags = {'CALIBRATION_FORMULA': f"out = {m:.6f} * in + {b:.6f}",
                    'CALIBRATION_SLOPE': f"{m:.8f}", 'CALIBRATION_INTERCEPT': f"{b:.8f}",
                    'CALIBRATION_EVENTS': str(len(fitted)), 'CALIBRATION_POSITION': position,
                    'CALIBRATION_FIT_UNCERTAINTY_1SIGMA': f"{u_fit:.3f}" if u_fit is not None else "n/a"}
            notes = save_calibrated_geotiff(calibrated, str(out_path), str(img_path), tags, job.copy_metadata)
            for n_ in notes:
                log(f"  {name}: {n_}")
            row.update({
                'timestamp': when.isoformat(), 'timestamp_source': ts.source, 'timestamp_tz': ts.tz or '',
                'position_vs_calibration': position,
                'slope_m': round(m, 8), 'intercept_b': round(b, 6),
                'formula_applied': (f"Refl = {m:.10f} * DN" if job.single_point else f"Out = {m:.6f} * In + {b:.6f}"),
                f'original_mean_{unit}': round(o_mean, 4), f'original_min_{unit}': round(o_min, 4),
                f'original_max_{unit}': round(o_max, 4), f'calibrated_mean_{unit}': round(c_mean, 4),
                f'calibrated_min_{unit}': round(float(np.min(calibrated[finite])), 4),
                f'calibrated_max_{unit}': round(float(np.max(calibrated[finite])), 4),
                f'mean_shift_{unit}': round(c_mean - o_mean, 4),
                'pct_pixels_outside_target_range': round(outside, 1),
                'u_fit_1sigma': None if u_fit is None else round(u_fit, 4),
                'emissivity_corrected': em.enabled,
                'scene_emissivity': em.scene_emissivity if em.enabled else None,
                'status': 'ok'})
            n_ok += 1
            log(f"{name}: m={m:.4f} b={b:.3f} ({position}, {ts.source})"
                + (f", ±{u_fit:.2f}" if u_fit is not None else ""))
        except Exception as e:
            n_err += 1
            row['status'] = f'error: {e}'
            log(f"ERROR {name}: {e}")
        rows.append(row)
        if on_row:
            on_row(row)
    progress(total, total, "")

    log_df = pd.DataFrame(rows)
    csv_path = out_dir / "processing_log.csv"
    log_df.to_csv(csv_path, index=False, encoding='utf-8')
    project_path = None
    if job.project is not None:            # the project is saved next to the outputs, so a run is reproducible
        project_path = out_dir / project_filename(job.project)
        with open(project_path, 'w', encoding='utf-8') as f:
            json.dump(job.project, f, indent=2)
    with open(out_dir / "processing_summary.txt", 'w', encoding='utf-8') as f:
        f.write(f"IMAGE CALIBRATION PROCESSING SUMMARY ({'Thermal' if job.thermal else 'MSI'})\n")
        f.write("=" * 60 + "\n\n")
        f.write(f"Tool: {TOOL_NAME} {__version__}\nProcessing date: {datetime.now().isoformat()}\n")
        f.write(f"Images processed: {n_ok}, errors: {n_err}, skipped: {n_skip}\n")
        if job.name_filter:
            f.write(f"Filename filter: '{job.name_filter}'\n")
        f.write("\nCALIBRATION EVENTS\n" + "-" * 40 + "\n")
        for k, e in enumerate(fitted):
            f.write(f"  Event {k + 1}: {e.timestamp} ({e.timestamp_source}) {os.path.basename(e.image_path)}\n")
            f.write(f"    {e.fit.summary_line(job.unit)}\n")
            if e.fit.resid_std is not None:
                f.write(f"    residual std {e.fit.resid_std:.3f}, SE(m) {e.fit.se_slope:.4f}, "
                        f"SE(b) {e.fit.se_intercept:.3f}, leave-one-out RMSE "
                        f"{e.fit.loo_rmse if e.fit.loo_rmse is None else round(e.fit.loo_rmse, 3)}\n")
        f.write("\nMETHOD\n" + "-" * 40 + "\n")
        if job.single_point:
            f.write("Single-point: reflectance = K × DN, K = ρ_panel / DN_panel\n")
        else:
            f.write("Empirical line: out = m × in + b, fitted per calibration image by least squares,\n")
            f.write("m and b interpolated linearly in time between consecutive calibration images\n"
                    if len(fitted) > 1 else "constant m and b (single calibration image)\n")
        if em.enabled:
            f.write(f"Emissivity correction: panels ε={em.target_emissivity} (applied before the fit), "
                    f"scene ε={em.scene_emissivity} (applied to survey pixels), T_sky={em.sky_temperature} °C, "
                    "broadband Stefan-Boltzmann approximation\n")
        else:
            f.write("Emissivity correction: off\n")
        f.write("\nUNCERTAINTY\n" + "-" * 40 + "\n")
        f.write("u_fit_1sigma: 1-sigma prediction uncertainty of the regression at the image mean sensor value,\n")
        f.write("              s * sqrt(1 + 1/n + (x - mean)^2 / Sxx); needs at least 3 targets.\n")
        f.write("Not included: reference instrument accuracy, drift between calibration events (see\n")
        f.write("position_vs_calibration), panel emissivity mismatch, atmospheric path, sensor non-uniformity.\n")
    log(f"\nDone: {n_ok} ok, {n_err} errors, {n_skip} skipped. Log: {csv_path}"
        + (f", project: {project_path.name}" if project_path else ""))
    return {'ok': n_ok, 'errors': n_err, 'skipped': n_skip, 'csv': str(csv_path),
            'summary': str(out_dir / 'processing_summary.txt'), 'project': str(project_path) if project_path else None,
            'rows': rows}


# =============================================================================
# REPORTS
# =============================================================================

def render_fit_png(event: CalibrationEvent, unit: str) -> Optional[bytes]:
    """Regression plot as PNG bytes (Agg backend, no window needed). None without matplotlib."""
    if not HAS_MATPLOTLIB or event.fit is None or event.fit.single_point:
        return None
    import io as _io
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    fit = event.fit
    fig = Figure(figsize=(6.4, 4.8), dpi=110)
    FigureCanvasAgg(fig)
    gs = fig.add_gridspec(2, 1, height_ratios=[3, 1], hspace=0.08)
    ax = fig.add_subplot(gs[0])
    axr = fig.add_subplot(gs[1], sharex=ax)
    complete = [t for t in event.targets if t.is_complete()]
    x, y = np.array(fit.x_values), np.array(fit.y_values)
    span = max(np.ptp(x), 1e-6)
    xr = np.linspace(x.min() - 0.1 * span - 0.5, x.max() + 0.1 * span + 0.5, 200)
    ax.plot(xr, fit.predict(xr), color='black', linestyle='--', linewidth=1.2, label='fit')
    if fit.resid_std is not None:
        band = np.array([fit.predict_uncertainty(v) for v in xr])
        ax.fill_between(xr, fit.predict(xr) - band, fit.predict(xr) + band, color='#999999', alpha=0.25, label='±1σ prediction')
    for t, xi, yi, ri in zip(complete, x, y, fit.residuals):
        ax.scatter(xi, yi, color=t.color, edgecolor='black', s=70, zorder=3)
        ax.annotate(t.name, (xi, yi), textcoords="offset points", xytext=(7, 5), fontsize=8)
        axr.bar(xi, ri, width=span * 0.03 + 0.05, color=t.color, edgecolor='black')
    axr.axhline(0, color='black', linewidth=0.8)
    ax.set_ylabel(f"Reference ({unit})")
    ax.tick_params(labelbottom=False)
    ax.grid(alpha=0.3)
    ax.legend(loc='lower right', fontsize=8)
    axr.set_ylabel("residual")
    axr.set_xlabel(f"Sensor ROI mean ({unit})")
    axr.grid(alpha=0.3)
    buf = _io.BytesIO()
    fig.savefig(buf, format='png', bbox_inches='tight')
    return buf.getvalue()


def fit_report_html(event: CalibrationEvent, unit: str, events: List[CalibrationEvent],
                    settings: Optional[Dict[str, Any]] = None, png: Optional[bytes] = None) -> str:
    """Self-contained HTML report of one calibration fit: targets, coefficients, diagnostics and method,
    with the plot embedded. Meant to be attached to a methods section."""
    import base64
    import html as _html
    fit = event.fit
    if fit is None:
        raise ValueError("event has no fit")
    esc = _html.escape

    def num(v, nd=4):
        return "n/a" if v is None else f"{v:.{nd}f}"
    rows = []
    complete = [t for t in event.targets if t.is_complete()]
    for t, r in zip(complete, fit.residuals if not fit.single_point else [0.0]):
        rows.append(f"<tr><td><span class='sw' style='background:{t.color}'></span>{esc(t.name)}</td>"
                    f"<td>{num(t.drone_mean, 3)}</td><td>{num(t.drone_std, 3)}</td><td>{t.pixel_count}</td>"
                    f"<td>{num(t.ground_truth, 2)}</td><td>{num(r, 3)}</td></tr>")
    if fit.single_point:
        coeff = f"<p class='big'>ρ = K × DN, K = {fit.slope:.8f}</p>"
        diag = "<p>Single-point calibration: no residual statistics are possible.</p>"
    else:
        coeff = (f"<p class='big'>out = {fit.slope:.5f} × in {'+' if fit.intercept >= 0 else '−'} {abs(fit.intercept):.4f}</p>")
        diag = ("<table><tr><th>n</th><th>R²</th><th>RMSE</th><th>s (resid. std)</th><th>SE(m)</th><th>SE(b)</th>"
                "<th>leave-one-out RMSE</th><th>±1σ prediction at x̄</th></tr>"
                f"<tr><td>{fit.n}</td><td>{num(fit.r_squared, 4)}</td><td>{num(fit.rmse, 3)}</td><td>{num(fit.resid_std, 3)}</td>"
                f"<td>{num(fit.se_slope, 4)}</td><td>{num(fit.se_intercept, 3)}</td><td>{num(fit.loo_rmse, 3)}</td>"
                f"<td>{num(fit.predict_uncertainty(fit.x_mean), 3)} at {num(fit.x_mean, 1)}</td></tr></table>")
        if fit.n == 2:
            diag += "<p class='warn'>Only two targets: R² is 1 by construction and no scatter can be estimated.</p>"
    fitted = sorted([e for e in events if e.fit is not None], key=lambda e: e.timestamp)
    method = ("Empirical line method: reference = m × sensor + b fitted by ordinary least squares to the mean of the inner "
              "region of each target polygon (edge inset in pixels). " +
              (f"{len(fitted)} calibration events; m and b are interpolated linearly in time between consecutive events "
               "and held constant outside that window." if len(fitted) > 1 else
               "One calibration event; m and b are applied unchanged to every image.") +
              " Prediction uncertainty: s·√(1 + 1/n + (x − x̄)²/Sxx), which covers the regression only; reference instrument "
              "accuracy, drift between calibration events, emissivity mismatch, atmospheric path and sensor non-uniformity "
              "are not included.")
    srows = "".join(f"<tr><td>{esc(str(k))}</td><td>{esc(str(v))}</td></tr>" for k, v in (settings or {}).items())
    img = f"<img src='data:image/png;base64,{base64.b64encode(png).decode()}' alt='regression plot'>" if png else ""
    return f"""<!doctype html><html lang='en'><head><meta charset='utf-8'><title>Calibration fit report</title>
<style>body{{font:14px/1.45 system-ui,sans-serif;color:#1d2320;max-width:860px;margin:32px auto;padding:0 16px}}
h1{{font-size:20px}}h2{{font-size:15px;margin-top:28px;border-bottom:1px solid #d9dcd6;padding-bottom:4px}}
table{{border-collapse:collapse;margin:8px 0}}td,th{{border:1px solid #d9dcd6;padding:4px 10px;text-align:right}}
th{{background:#f3f4f1;font-weight:600}}td:first-child,th:first-child{{text-align:left}}
.big{{font-size:18px;font-family:ui-monospace,monospace}}.warn{{color:#b45309}}.muted{{color:#7a827d}}
.sw{{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px;vertical-align:middle}}
img{{max-width:100%;border:1px solid #d9dcd6;margin-top:8px}}</style></head><body>
<h1>Calibration fit report</h1>
<p class='muted'>{esc(os.path.basename(event.image_path))} · captured {event.timestamp:%Y-%m-%d %H:%M:%S}{esc(event.timestamp_tz)}
({esc(event.timestamp_source)}) · generated {datetime.now():%Y-%m-%d %H:%M} by {TOOL_NAME} {__version__}</p>
<h2>Targets</h2>
<table><tr><th>Target</th><th>Sensor mean ({esc(unit)})</th><th>σ</th><th>pixels</th><th>Reference ({esc(unit)})</th><th>Residual</th></tr>{''.join(rows)}</table>
<h2>Coefficients</h2>{coeff}<h2>Diagnostics</h2>{diag}{img}
<h2>Method</h2><p>{esc(method)}</p>
{('<h2>Settings</h2><table>' + srows + '</table>') if srows else ''}
</body></html>"""


# =============================================================================
# PROJECT FILES
# =============================================================================

def events_to_project(events: List[CalibrationEvent], mode: str, single_point: bool,
                      emissivity: EmissivitySettings, roi_inset_px: int, survey: Optional[dict] = None,
                      title: str = "") -> dict:
    return {"tool": "thermal_calibration_tool", "version": __version__, "saved": datetime.now().isoformat(),
            "title": title, "mode": mode, "msi_single_point": single_point, "roi_inset_px": roi_inset_px,
            "emissivity": emissivity.to_dict(),
            "calibration_images": [e.to_dict() for e in events], "survey": survey or {}}


def events_from_project(project: dict, project_dir: Optional[str], roi_inset_px: int) -> Tuple[List[CalibrationEvent], List[str]]:
    """Rebuild events from a project dict: re-reads the images and recomputes ROI statistics."""
    events, problems = [], []
    for d in project.get("calibration_images", []):
        path = d.get("path", "")
        if not os.path.exists(path) and project_dir:
            alt = os.path.join(project_dir, os.path.basename(path))
            if os.path.exists(alt):
                path = alt
        if not os.path.exists(path):
            problems.append(f"missing calibration image: {path}")
            continue
        ts = extract_timestamp(path)
        ev = CalibrationEvent(path, ts.value or datetime.now(), ts.source, ts.tz,
                              targets=[CalibrationTarget.from_dict(t) for t in d.get("targets", [])] or None)
        try:
            img = load_thermal_image(path)
        except ValueError as e:
            problems.append(str(e))
            continue
        for t in ev.targets:
            if t.has_polygon():
                st = extract_roi_statistics(img, t.polygon, roi_inset_px)
                t.drone_mean, t.drone_std, t.pixel_count, t.inset_applied = st.mean, st.std, st.count, st.inset_applied
        events.append(ev)
    events.sort(key=lambda e: e.timestamp)
    return events, problems


def run_project_headless(project_path: str, survey_folder: Optional[str] = None, output_folder: Optional[str] = None,
                         name_filter: Optional[str] = None, log: Callable[[str], None] = print) -> dict:
    """CLI entry: load a project file and process its survey images without the GUI."""
    with open(project_path, encoding='utf-8') as f:
        project = json.load(f)
    mode = project.get("mode", MODE_THERMAL)
    single = bool(project.get("msi_single_point", False)) and mode == MODE_MSI
    em = EmissivitySettings.from_dict(project.get("emissivity", {}))
    inset = int(project.get("roi_inset_px", DEFAULT_ROI_INSET_PX))
    events, problems = events_from_project(project, os.path.dirname(os.path.abspath(project_path)), inset)
    for p in problems:
        log(f"WARNING: {p}")
    for e in events:
        warn = recompute_fit(e, single, mode == MODE_THERMAL, em)
        if warn:
            log(f"WARNING: {os.path.basename(e.image_path)}: {warn}")
    sv = project.get("survey", {})
    folder = survey_folder or sv.get("folder") or (os.path.dirname(sv["files"][0]) if sv.get("files") else None)
    files = collect_survey_files(folder, None,
                                 name_filter if name_filter is not None else sv.get("filter", ""),
                                 [e.image_path for e in events])
    out = Path(output_folder or sv.get("output_folder") or (Path(folder or ".") / "calibrated"))
    job = BatchJob(mode, single, events, files, out, em, inset, bool(sv.get("copy_metadata", True)),
                   bool(sv.get("skip_existing", False)), sv.get("filter", "") if name_filter is None else name_filter, project)
    rep = preflight(job)
    log(rep.text())
    if rep.blocking:
        raise SystemExit("pre-flight check failed")
    job.survey_files = rep.files
    return run_batch(job, log, lambda i, n, name: None, lambda: False, rep.timestamps)


# =============================================================================
# GUI HELPERS
# =============================================================================

UI = {
    'bg': '#f3f4f1', 'panel': '#ffffff', 'border': '#d9dcd6', 'divider': '#e6e8e3', 'text': '#1d2320',
    'muted': '#7a827d', 'accent': '#2f6f5e', 'accent_dark': '#275c4e', 'accent_soft': '#eef5f1',
    'ok': '#2e7d4f', 'warn': '#b45309', 'warn_bg': '#fdf3e4', 'warn_border': '#f1d2a3',
    'block': '#b3261e', 'block_bg': '#fbe9e7', 'canvas': '#202521', 'drift': '#c0652a', 'blue': '#1f5f8b',
    'tick': '#9aa39d', 'tick_out': '#d7a98a', 'plot_bg': '#fbfbf9',
}


def hex_to_rgb(hex_color: str) -> Tuple[int, int, int]:
    h = hex_color.lstrip('#')
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def lighten_color(hex_color: str, factor: float = 0.6) -> str:
    r, g, b = hex_to_rgb(hex_color)
    return '#{:02x}{:02x}{:02x}'.format(int(r + (255 - r) * factor), int(g + (255 - g) * factor),
                                        int(b + (255 - b) * factor))


def parse_number(text: str) -> Optional[float]:
    """'15,0' and '15.0' both parse; empty is None; anything else raises ValueError."""
    s = text.strip().replace(',', '.')
    if not s:
        return None
    return float(s)


def setup_theme(root: tk.Tk) -> Dict[str, Any]:
    """Light, flat ttk theme matching the design mockup. Returns the named fonts."""
    style = ttk.Style(root)
    style.theme_use('clam')
    families = set(tkfont.families(root))
    ui_family = next((f for f in ("IBM Plex Sans", "Inter", "Noto Sans", "Segoe UI", "DejaVu Sans", "Helvetica")
                      if f in families), None)
    mono_family = next((f for f in ("IBM Plex Mono", "DejaVu Sans Mono", "Consolas", "Menlo", "Courier New")
                        if f in families), None)
    base = tkfont.nametofont("TkDefaultFont")
    base.configure(size=10, **({'family': ui_family} if ui_family else {}))
    tkfont.nametofont("TkTextFont").configure(size=10, **({'family': ui_family} if ui_family else {}))
    tkfont.nametofont("TkHeadingFont").configure(size=10, **({'family': ui_family} if ui_family else {}))
    mono = tkfont.nametofont("TkFixedFont")
    mono.configure(size=9, **({'family': mono_family} if mono_family else {}))
    fonts = {'ui': base, 'mono': mono}
    fonts['bold'] = tkfont.Font(root, font=base); fonts['bold'].configure(weight='bold')
    fonts['small'] = tkfont.Font(root, font=base); fonts['small'].configure(size=9)
    fonts['small_bold'] = tkfont.Font(root, font=base); fonts['small_bold'].configure(size=9, weight='bold')
    fonts['tiny'] = tkfont.Font(root, font=base); fonts['tiny'].configure(size=8)
    fonts['title'] = tkfont.Font(root, font=base); fonts['title'].configure(size=12, weight='bold')
    fonts['mono_small'] = tkfont.Font(root, font=mono); fonts['mono_small'].configure(size=8)
    fonts['mono_big'] = tkfont.Font(root, font=mono); fonts['mono_big'].configure(size=11)

    style.configure('.', background=UI['bg'], foreground=UI['text'], bordercolor=UI['border'],
                    lightcolor=UI['panel'], darkcolor=UI['border'], troughcolor='#e9ece7', font=base)
    style.configure('TFrame', background=UI['bg'])
    style.configure('Panel.TFrame', background=UI['panel'])
    style.configure('TLabel', background=UI['bg'])
    style.configure('Panel.TLabel', background=UI['panel'])
    style.configure('TButton', padding=(10, 4), background=UI['panel'], relief='flat', borderwidth=1)
    style.map('TButton', background=[('active', '#e9ece7'), ('disabled', '#f3f4f1')],
              foreground=[('disabled', '#9aa39d')])
    style.configure('Small.TButton', padding=(6, 2), font=fonts['small'])
    style.configure('Accent.TButton', background=UI['accent'], foreground='#ffffff', bordercolor=UI['accent'],
                    font=fonts['bold'], padding=(14, 5))
    style.map('Accent.TButton', background=[('active', UI['accent_dark']), ('disabled', '#a7c4ba')],
              foreground=[('disabled', '#ffffff')])
    style.configure('Toolbutton', padding=(9, 3), background=UI['panel'], relief='flat', borderwidth=1)
    style.map('Toolbutton', background=[('selected', UI['text']), ('active', '#e9ece7')],
              foreground=[('selected', '#ffffff')])
    style.configure('TCheckbutton', background=UI['bg'])
    style.configure('TRadiobutton', background=UI['bg'])
    style.configure('TEntry', fieldbackground=UI['panel'], padding=3)
    style.configure('TSpinbox', fieldbackground=UI['panel'], arrowsize=11)
    style.configure('TProgressbar', background=UI['accent'], troughcolor='#e9ece7', bordercolor='#e9ece7',
                    lightcolor=UI['accent'], darkcolor=UI['accent'])
    style.configure('Treeview', background=UI['panel'], fieldbackground=UI['panel'], rowheight=22, borderwidth=0,
                    font=fonts['small'])
    style.configure('Treeview.Heading', background=UI['bg'], font=fonts['small_bold'], relief='flat')
    style.configure('TScrollbar', background='#e9ece7', troughcolor=UI['bg'], arrowsize=12)
    style.configure('TNotebook', background=UI['bg'])
    return fonts


class ToolTip:
    """Hover help."""

    def __init__(self, widget, text: str):
        self.widget, self.text, self.tipwindow = widget, text, None
        widget.bind("<Enter>", self.show)
        widget.bind("<Leave>", self.hide)

    def show(self, event=None):
        if self.tipwindow or not self.text:
            return
        x = self.widget.winfo_rootx() + 20
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 5
        self.tipwindow = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        tk.Label(tw, text=self.text, justify=tk.LEFT, background="#FFFFDD", relief=tk.SOLID, borderwidth=1,
                 font=("TkDefaultFont", 9), wraplength=340, padx=6, pady=4).pack()

    def hide(self, event=None):
        if self.tipwindow:
            self.tipwindow.destroy()
            self.tipwindow = None


def help_icon(parent, text: str, manage: bool = True, bg: Optional[str] = None, **pack) -> tk.Label:
    """A small '?' with a tooltip. Packed with **pack unless manage=False (caller grids it)."""
    lbl = tk.Label(parent, text="?", cursor="hand2", fg=UI['blue'], bg=bg or UI['bg'], font=('TkDefaultFont', 9, 'bold'))
    if manage:
        lbl.pack(**pack)
    ToolTip(lbl, text)
    return lbl


def open_in_file_manager(path: str):
    """Reveal a folder or file in the system file manager."""
    try:
        if platform.system() == "Windows":
            os.startfile(path)  # type: ignore[attr-defined]
        elif platform.system() == "Darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        pass


# =============================================================================
# ZOOMABLE IMAGE CANVAS
# =============================================================================

class ZoomCanvas:
    """Canvas showing an RGB image with wheel zoom, right/middle-drag pan, fit and 1:1.

    The view is (scale, ox, oy): canvas = (image - o) * scale. Only the visible part of the image is
    resized per frame, so large zoom factors stay cheap. Overlays are drawn by a callback after every
    redraw, in canvas coordinates obtained through to_canvas().
    """
    MIN_SCALE, MAX_SCALE = 0.05, 40.0
    LOUPE_SIZE, LOUPE_HALF = 128, 8      # 17 image px → 128 canvas px

    def __init__(self, parent, on_click=None, on_motion=None, on_overlay=None, on_view_change=None):
        self.canvas = tk.Canvas(parent, bg=UI['canvas'], highlightthickness=0)
        self.on_click, self.on_motion, self.on_overlay, self.on_view_change = on_click, on_motion, on_overlay, on_view_change
        self.rgb: Optional[np.ndarray] = None
        self.scale, self.ox, self.oy, self.fitted = 1.0, 0.0, 0.0, True
        self._photo = None
        self._loupe_photo = None
        self._pan_last = None
        self.loupe_enabled = False
        c = self.canvas
        c.bind('<Configure>', self._on_configure)
        c.bind('<Button-1>', self._on_click)
        c.bind('<Motion>', self._on_motion)
        c.bind('<Leave>', self._on_leave)
        for b in ('<ButtonPress-2>', '<ButtonPress-3>'):
            c.bind(b, self._pan_begin)
        for b in ('<B2-Motion>', '<B3-Motion>'):
            c.bind(b, self._pan_move)
        c.bind('<Button-4>', lambda e: self._wheel(e, 1))
        c.bind('<Button-5>', lambda e: self._wheel(e, -1))
        c.bind('<MouseWheel>', lambda e: self._wheel(e, 1 if e.delta > 0 else -1))

    # --- geometry -----------------------------------------------------------
    def size(self) -> Tuple[int, int]:
        w, h = self.canvas.winfo_width(), self.canvas.winfo_height()
        return (w if w > 50 else 900), (h if h > 50 else 700)

    def to_canvas(self, x: float, y: float) -> Tuple[float, float]:
        return (x - self.ox) * self.scale, (y - self.oy) * self.scale

    def to_image(self, cx: float, cy: float) -> Tuple[float, float]:
        return cx / self.scale + self.ox, cy / self.scale + self.oy

    def set_image(self, rgb: Optional[np.ndarray], keep_view: bool = False):
        same = self.rgb is not None and rgb is not None and rgb.shape[:2] == self.rgb.shape[:2]
        self.rgb = rgb
        if rgb is None:
            self.canvas.delete('all')
            return
        if not (keep_view and same and not self.fitted):
            self.fit()
        self.redraw()

    def fit(self):
        if self.rgb is None:
            return
        W, H = self.size()
        h, w = self.rgb.shape[:2]
        self.scale = min(W / w, H / h)
        self.ox = (w - W / self.scale) / 2
        self.oy = (h - H / self.scale) / 2
        self.fitted = True

    def fit_and_redraw(self):
        self.fit()
        self.redraw()

    def one_to_one(self):
        if self.rgb is None:
            return
        W, H = self.size()
        cx, cy = self.to_image(W / 2, H / 2)
        self.scale = 1.0
        self.ox, self.oy = cx - W / 2, cy - H / 2
        self.fitted = False
        self._clamp()
        self.redraw()

    def zoom_at(self, cx: float, cy: float, factor: float):
        if self.rgb is None:
            return
        ix, iy = self.to_image(cx, cy)
        self.scale = max(self.MIN_SCALE, min(self.MAX_SCALE, self.scale * factor))
        self.ox, self.oy = ix - cx / self.scale, iy - cy / self.scale
        self.fitted = False
        self._clamp()
        self.redraw()

    def _clamp(self):
        """Keep at least a fifth of the canvas covered by image."""
        W, H = self.size()
        h, w = self.rgb.shape[:2]
        vw, vh = W / self.scale, H / self.scale
        self.ox = min(max(self.ox, -vw * 0.8), w - vw * 0.2)
        self.oy = min(max(self.oy, -vh * 0.8), h - vh * 0.2)

    # --- events --------------------------------------------------------------
    def _on_configure(self, event):
        if self.rgb is None:
            return
        if self.fitted:
            self.fit()
        self.redraw()

    def _wheel(self, event, direction: int):
        self.zoom_at(event.x, event.y, 1.25 if direction > 0 else 0.8)
        return "break"

    def _pan_begin(self, event):
        self._pan_last = (event.x, event.y)

    def _pan_move(self, event):
        if self._pan_last is None or self.rgb is None:
            return
        dx, dy = event.x - self._pan_last[0], event.y - self._pan_last[1]
        self._pan_last = (event.x, event.y)
        self.ox -= dx / self.scale
        self.oy -= dy / self.scale
        self.fitted = False
        self._clamp()
        self.redraw()

    def _on_click(self, event):
        if self.rgb is None or not self.on_click:
            return
        ix, iy = self.to_image(event.x, event.y)
        h, w = self.rgb.shape[:2]
        if 0 <= ix < w and 0 <= iy < h:
            self.on_click(int(ix), int(iy))

    def _on_motion(self, event):
        if self.rgb is None or not self.on_motion:
            return
        ix, iy = self.to_image(event.x, event.y)
        h, w = self.rgb.shape[:2]
        if 0 <= ix < w and 0 <= iy < h:
            self.on_motion(int(ix), int(iy), event.x, event.y)
            if self.loupe_enabled:
                self._draw_loupe(int(ix), int(iy))
        else:
            self.on_motion(None, None, None, None)
            self.canvas.delete('loupe')

    def _on_leave(self, event=None):
        if self.on_motion:
            self.on_motion(None, None, None, None)
        self.canvas.delete('loupe')

    # --- drawing -------------------------------------------------------------
    def redraw(self):
        c = self.canvas
        c.delete('all')
        if self.rgb is None:
            return
        W, H = self.size()
        h, w = self.rgb.shape[:2]
        x0, y0 = max(0, int(math.floor(self.ox))), max(0, int(math.floor(self.oy)))
        x1 = min(w, int(math.ceil(self.ox + W / self.scale)) + 1)
        y1 = min(h, int(math.ceil(self.oy + H / self.scale)) + 1)
        if x1 > x0 and y1 > y0:
            crop = self.rgb[y0:y1, x0:x1]
            dw, dh = max(1, int(round((x1 - x0) * self.scale))), max(1, int(round((y1 - y0) * self.scale)))
            interp = cv2.INTER_NEAREST if self.scale >= 1 else cv2.INTER_AREA
            resized = cv2.resize(crop, (dw, dh), interpolation=interp)
            self._photo = ImageTk.PhotoImage(Image.fromarray(resized))
            px, py = self.to_canvas(x0, y0)
            c.create_image(int(round(px)), int(round(py)), anchor=tk.NW, image=self._photo, tags='base')
        if self.on_overlay:
            self.on_overlay()
        if self.on_view_change:
            self.on_view_change(self.scale)

    def _draw_loupe(self, ix: int, iy: int):
        c = self.canvas
        c.delete('loupe')
        hh = self.LOUPE_HALF
        pad = np.pad(self.rgb, ((hh, hh), (hh, hh), (0, 0)), mode='edge')
        crop = pad[iy:iy + 2 * hh + 1, ix:ix + 2 * hh + 1]
        big = cv2.resize(crop, (self.LOUPE_SIZE, self.LOUPE_SIZE), interpolation=cv2.INTER_NEAREST)
        self._loupe_photo = ImageTk.PhotoImage(Image.fromarray(big))
        W, _ = self.size()
        x, y = W - self.LOUPE_SIZE - 12, 12
        c.create_image(x, y, anchor=tk.NW, image=self._loupe_photo, tags='loupe')
        c.create_rectangle(x, y, x + self.LOUPE_SIZE, y + self.LOUPE_SIZE, outline='white', width=2, tags='loupe')
        mid = self.LOUPE_SIZE / 2
        c.create_line(x + mid, y, x + mid, y + self.LOUPE_SIZE, fill='white', tags='loupe')
        c.create_line(x, y + mid, x + self.LOUPE_SIZE, y + mid, fill='white', tags='loupe')
        c.create_text(x + 4, y + self.LOUPE_SIZE - 4, anchor=tk.SW, text=f"{self.LOUPE_SIZE // (2 * hh + 1)}×",
                      fill='white', font=('TkDefaultFont', 8, 'bold'), tags='loupe')


# =============================================================================
# STEP RAIL
# =============================================================================

class StepSection:
    """One step of the left rail: a clickable header with a status badge and a one-line summary,
    and a body that is shown only while the step is active (accordion)."""

    def __init__(self, parent, number: int, title: str, on_activate: Callable[[int], None], fonts: Dict[str, Any]):
        self.number, self.fonts, self.done, self.active = number, fonts, False, False
        self.outer = tk.Frame(parent, bg=UI['panel'])
        self.outer.pack(fill=tk.X)
        self.header = tk.Frame(self.outer, bg=UI['panel'], cursor='hand2')
        self.header.pack(fill=tk.X)
        self.badge = tk.Canvas(self.header, width=24, height=24, bg=UI['panel'], highlightthickness=0, cursor='hand2')
        self.badge.pack(side=tk.LEFT, padx=(14, 10), pady=10)
        txt = tk.Frame(self.header, bg=UI['panel'])
        txt.pack(side=tk.LEFT, fill=tk.X, expand=True, pady=6)
        self.title = tk.Label(txt, text=f"{number} · {title}", bg=UI['panel'], fg=UI['text'], font=fonts['bold'], anchor='w')
        self.title.pack(fill=tk.X)
        self.summary = tk.Label(txt, text="", bg=UI['panel'], fg=UI['muted'], font=fonts['small'], anchor='w',
                                justify='left', wraplength=230)
        self.summary.pack(fill=tk.X)
        self.chevron = tk.Label(self.header, text="›", bg=UI['panel'], fg=UI['muted'], font=fonts['title'])
        self.chevron.pack(side=tk.RIGHT, padx=12)
        for w in (self.header, self.badge, txt, self.title, self.summary, self.chevron):
            w.bind('<Button-1>', lambda e, n=number: on_activate(n))
        self.body = tk.Frame(self.outer, bg=UI['bg'], padx=12, pady=8)
        self.divider = tk.Frame(self.outer, bg=UI['divider'], height=1)
        self.divider.pack(fill=tk.X, side=tk.BOTTOM)
        self._draw_badge()

    def set_done(self, done: bool, summary: Optional[str] = None):
        self.done = done
        if summary is not None:
            self.summary.config(text=summary)
        self._draw_badge()

    def set_summary(self, text: str):
        self.summary.config(text=text)

    def set_active(self, active: bool):
        self.active = active
        bg = UI['accent_soft'] if active else UI['panel']
        for w in (self.header, self.badge, self.title, self.summary, self.chevron, self.title.master):
            w.config(bg=bg)
        self.chevron.config(text="⌄" if active else "›")
        if active:
            self.body.pack(fill=tk.X, before=self.divider)
        else:
            self.body.pack_forget()
        self._draw_badge()

    def _draw_badge(self):
        c = self.badge
        c.delete('all')
        if self.done:
            c.create_oval(1, 1, 23, 23, fill=UI['ok'], outline=UI['ok'])
            c.create_line(7, 12, 11, 16, 17, 8, fill='white', width=2.5, capstyle='round', joinstyle='round')
        elif self.active:
            c.create_oval(1, 1, 23, 23, fill=UI['accent'], outline=UI['accent'])
            c.create_text(12, 12, text=str(self.number), fill='white', font=self.fonts['small_bold'])
        else:
            c.create_oval(2, 2, 22, 22, fill=UI['panel'], outline=UI['tick'], width=1.5)
            c.create_text(12, 12, text=str(self.number), fill=UI['muted'], font=self.fonts['small_bold'])


# =============================================================================
# TARGET ROW WIDGET
# =============================================================================

class TargetRowWidget:
    """One row of the target table: colour, name, Draw/Clear, ground-truth entry, ROI statistics."""

    def __init__(self, parent, index: int, color: str, on_draw, on_clear, on_remove, on_gt_change, fonts=None):
        self.index, self.color, self.on_gt_change = index, color, on_gt_change
        fonts = fonts or {}
        self.frame = tk.Frame(parent, bg=UI['panel'])
        self.frame.pack(fill=tk.X)
        tk.Frame(self.frame, bg=UI['divider'], height=1).pack(fill=tk.X, side=tk.TOP)
        # line 1: swatch, name, Draw/Clear, reference entry, remove. Packed left to right, no weighted
        # column that could be squeezed to nothing in a narrow panel.
        row = tk.Frame(self.frame, bg=UI['panel'])
        row.pack(fill=tk.X, padx=6, pady=(4, 0))
        self.swatch = tk.Label(row, bg=color, width=1, height=1, font=fonts.get('tiny'))
        self.swatch.pack(side=tk.LEFT, padx=(2, 6))
        self.name_label = tk.Label(row, text=f"T{index + 1}", width=3, bg=UI['panel'], fg=UI['text'], font=fonts.get('bold'), anchor='w')
        self.name_label.pack(side=tk.LEFT)
        self.draw_btn = ttk.Button(row, text="Draw", width=5, style='Small.TButton', command=lambda: on_draw(self.index))
        self.draw_btn.pack(side=tk.LEFT, padx=1)
        self.clear_btn = ttk.Button(row, text="Clear", width=5, style='Small.TButton', command=lambda: on_clear(self.index))
        self.clear_btn.pack(side=tk.LEFT, padx=1)
        self.gt_var = tk.StringVar()
        self._light = lighten_color(color, 0.8)
        self.gt_entry = tk.Entry(row, width=7, bg=self._light, fg=UI['text'], textvariable=self.gt_var,
                                 font=fonts.get('mono'), relief='solid', bd=1, justify='right',
                                 highlightthickness=0, insertbackground=UI['text'])
        self.gt_entry.pack(side=tk.LEFT, padx=6)
        self.gt_var.trace_add('write', self._on_gt_modified)
        self.remove_btn = ttk.Button(row, text="×", width=2, style='Small.TButton', command=lambda: on_remove(self.index))
        self.remove_btn.pack(side=tk.RIGHT, padx=2)
        # line 2: ROI statistics, full width so they are never cut off
        self.stats_label = tk.Label(self.frame, text="–", bg=UI['panel'], fg=UI['muted'], font=fonts.get('mono_small'), anchor='w')
        self.stats_label.pack(fill=tk.X, padx=(34, 6), pady=(0, 4))
        self._suspend = False

    def _on_gt_modified(self, *args):
        valid = True
        try:
            parse_number(self.gt_var.get())
        except ValueError:
            valid = False
        self.gt_entry.config(bg=self._light if valid else '#f5b7b1')
        if not self._suspend:
            self.on_gt_change(self.index)

    def get_ground_truth(self) -> Optional[float]:
        try:
            return parse_number(self.gt_var.get())
        except ValueError:
            return None

    def set_ground_truth(self, value: Optional[float]):
        self._suspend = True
        self.gt_var.set("" if value is None else f"{value:g}")
        self._suspend = False

    def set_stats(self, target: CalibrationTarget, unit: str):
        if target.drone_mean is None or not np.isfinite(target.drone_mean):
            self.stats_label.config(text="no polygon on this shot", fg=UI['muted'])
            return
        txt = f"mean {target.drone_mean:.2f} {unit} · σ {target.drone_std:.2f} · n = {target.pixel_count}"
        warn = target.pixel_count < MIN_ROI_PIXELS or not target.inset_applied
        self.stats_label.config(text=txt + ("  ⚠ few pixels" if warn else ""), fg=UI['warn'] if warn else UI['text'])

    def destroy(self):
        self.frame.destroy()


# =============================================================================
# FIT PANEL (right side, always live)
# =============================================================================

class FitPanel:
    """Mini regression plot, residual bars, coefficient tiles and warnings."""
    W = 320

    def __init__(self, parent, fonts: Dict[str, Any], on_plot: Callable[[], None], on_report: Callable[[], None]):
        self.fonts = fonts
        self.frame = tk.Frame(parent, bg=UI['panel'], width=self.W)
        self.frame.pack_propagate(False)
        tk.Frame(self.frame, bg=UI['border'], width=1).pack(side=tk.LEFT, fill=tk.Y)
        inner = tk.Frame(self.frame, bg=UI['panel'], padx=14, pady=12)
        inner.pack(fill=tk.BOTH, expand=True)
        head = tk.Frame(inner, bg=UI['panel'])
        head.pack(fill=tk.X)
        self.title = tk.Label(head, text="Fit", bg=UI['panel'], fg=UI['text'], font=fonts['title'], anchor='w')
        self.title.pack(side=tk.LEFT)
        self.subtitle = tk.Label(head, text="", bg=UI['panel'], fg=UI['muted'], font=fonts['small'], anchor='w')
        self.subtitle.pack(side=tk.LEFT, padx=8, pady=(3, 0))
        self.plot = tk.Canvas(inner, height=190, bg=UI['plot_bg'], highlightthickness=1, highlightbackground=UI['border'])
        self.plot.pack(fill=tk.X, pady=(10, 8))
        self.plot.bind('<Configure>', lambda e: self._draw_plot())
        tk.Label(inner, text="Residuals", bg=UI['panel'], fg=UI['muted'], font=fonts['small'], anchor='w').pack(fill=tk.X)
        # Residual bars are canvas items, never widgets: creating widgets at refresh time costs hundreds of
        # milliseconds of layout in Tk, drawing items costs nothing noticeable.
        self.RESID_ROW = 20
        self.resid_canvas = tk.Canvas(inner, height=self.RESID_ROW * 3, bg=UI['panel'], highlightthickness=0)
        self.resid_canvas.pack(fill=tk.X, pady=(2, 8))
        self.resid_canvas.bind('<Configure>', lambda e: self._draw_resid())
        tiles = tk.Frame(inner, bg=UI['panel'])
        tiles.pack(fill=tk.X)
        tiles.columnconfigure(0, weight=1)
        tiles.columnconfigure(1, weight=1)
        self.tiles = {}
        for i, (key, caption) in enumerate((('m', 'slope m'), ('b', 'offset b'), ('r2', 'R² · leave-one-out'), ('u', '±1σ prediction at mean'))):
            f = tk.Frame(tiles, bg=UI['panel'], highlightthickness=1, highlightbackground=UI['divider'], padx=8, pady=5)
            f.grid(row=i // 2, column=i % 2, sticky='nsew', padx=(0, 6) if i % 2 == 0 else 0, pady=(0, 6))
            tk.Label(f, text=caption, bg=UI['panel'], fg=UI['muted'], font=fonts['tiny'], anchor='w').pack(fill=tk.X)
            v = tk.Label(f, text="–", bg=UI['panel'], fg=UI['text'], font=fonts['mono'], anchor='w')
            v.pack(fill=tk.X)
            self.tiles[key] = v
        # one label for all warnings (text is replaced, the widget is never recreated)
        self.warn_label = tk.Label(inner, text="", bg=UI['panel'], fg='#6b4a12', font=fonts['small'], anchor='w',
                                   justify='left', wraplength=self.W - 48, padx=8, pady=0,
                                   highlightthickness=0, highlightbackground=UI['warn_border'])
        self.warn_label.pack(fill=tk.X, pady=(4, 0))
        btns = tk.Frame(inner, bg=UI['panel'])
        btns.pack(side=tk.BOTTOM, fill=tk.X, pady=(8, 0))
        self.plot_btn = ttk.Button(btns, text="Detailed plot", command=on_plot)
        self.plot_btn.pack(fill=tk.X, pady=(0, 6))
        self.report_btn = ttk.Button(btns, text="Export fit report", style='Accent.TButton', command=on_report)
        self.report_btn.pack(fill=tk.X)
        self._fit: Optional[FitResult] = None
        self._targets: List[CalibrationTarget] = []
        self._unit = "°C"
        self._message = "No calibration image selected"

    # --- public ---------------------------------------------------------------
    def clear(self, message: str):
        self._fit, self._targets, self._message = None, [], message
        self.subtitle.config(text="")
        for k in self.tiles:
            self.tiles[k].config(text="–")
        self._set_warnings([])
        self._draw_resid()
        self._draw_plot()
        self.plot_btn.state(['disabled'])
        self.report_btn.state(['disabled'])

    def update(self, event: CalibrationEvent, targets: List[CalibrationTarget], unit: str,
               warnings: List[str], subtitle: str):
        self._fit, self._targets, self._unit = event.fit, [t for t in targets if t.is_complete()], unit
        self.subtitle.config(text=subtitle)
        fit = event.fit
        if fit is None:
            self.clear("Need at least 2 complete targets")
            self.subtitle.config(text=subtitle)
            self._set_warnings(warnings)
            return
        if fit.single_point:
            self.tiles['m'].config(text=f"K = {fit.slope:.6g}")
            self.tiles['b'].config(text="0 (by definition)")
            self.tiles['r2'].config(text="–")
        else:
            self.tiles['m'].config(text=f"{fit.slope:.4f}" + (f" ± {fit.se_slope:.4f}" if fit.se_slope is not None else ""))
            self.tiles['b'].config(text=f"{fit.intercept:+.3f}" + (f" ± {fit.se_intercept:.3f}" if fit.se_intercept is not None else ""))
            self.tiles['r2'].config(text=f"{fit.r_squared:.4f}" + (f" · {fit.loo_rmse:.2f}" if fit.loo_rmse is not None else " · n/a"))
        u = None if fit.single_point else fit.predict_uncertainty(fit.x_mean)
        self.tiles['u'].config(text=(f"± {u:.2f} {unit} at {fit.x_mean:.1f}" if u is not None else "– (needs 3 targets)"))
        self._set_warnings(warnings)
        self._draw_resid()
        self._draw_plot()
        self.plot_btn.state(['!disabled'] if not fit.single_point else ['disabled'])
        self.report_btn.state(['!disabled'])

    # --- drawing ----------------------------------------------------------------
    def _set_warnings(self, warnings: List[str]):
        if warnings:
            self.warn_label.config(text="\n\n".join("⚠ " + w for w in warnings), bg=UI['warn_bg'],
                                   highlightthickness=1, pady=5)
        else:
            self.warn_label.config(text="", bg=UI['panel'], highlightthickness=0, pady=0)

    def _draw_resid(self):
        c = self.resid_canvas
        c.delete('all')
        fit = self._fit
        W = max(c.winfo_width(), 200)
        if fit is None or fit.single_point or not self._targets:
            c.create_text(2, 10, anchor='w', text="–", fill=UI['muted'], font=self.fonts['small'])
            return
        n = len(self._targets)
        pitch = min(self.RESID_ROW, max(12, int(c.winfo_height() / max(n, 1))))
        scale = max(max(abs(r) for r in fit.residuals), 1e-9)
        x0, x1 = 34, W - 56
        mid = (x0 + x1) / 2
        for i, (t, r) in enumerate(zip(self._targets, fit.residuals)):
            y = i * pitch + pitch / 2
            c.create_text(0, y, anchor='w', text=t.name.replace('Target ', 'T'), fill=UI['text'], font=self.fonts['small_bold'])
            c.create_rectangle(x0, y - 5, x1, y + 5, fill='#f3f4f1', outline='')
            c.create_line(mid, y - 5, mid, y + 5, fill=UI['border'])
            xe = mid + (x1 - mid) * 0.96 * r / scale
            c.create_rectangle(min(mid, xe), y - 3, max(mid, xe), y + 3, fill=t.color, outline='', tags='bar')
            c.create_text(W, y, anchor='e', text=f"{r:+.2f}", fill=UI['muted'], font=self.fonts['mono_small'])

    def _draw_plot(self):
        c = self.plot
        c.delete('all')
        W = max(c.winfo_width(), 200)
        H = max(c.winfo_height(), 120)
        fit = self._fit
        if fit is None or fit.single_point or not self._targets:
            c.create_text(W / 2, H / 2, text=self._message if fit is None else "single point: K = ρ / DN",
                          fill=UI['muted'], font=self.fonts['small'], width=W - 30, justify='center')
            return
        L, R, T, B = 40, 12, 12, 28
        x = np.array(fit.x_values)
        y = np.array(fit.y_values)
        span = max(float(np.ptp(x)), 1e-6)
        xmin, xmax = x.min() - 0.12 * span - 0.3, x.max() + 0.12 * span + 0.3
        xs = np.linspace(xmin, xmax, 40)
        band = np.array([fit.predict_uncertainty(v) or 0.0 for v in xs])
        ys_all = np.concatenate([y, fit.predict(xs) - band, fit.predict(xs) + band])
        ymin, ymax = float(ys_all.min()), float(ys_all.max())
        yspan = max(ymax - ymin, 1e-6)
        ymin, ymax = ymin - 0.08 * yspan, ymax + 0.08 * yspan

        def px(v):
            return L + (v - xmin) / (xmax - xmin) * (W - L - R)

        def py(v):
            return H - B - (v - ymin) / (ymax - ymin) * (H - T - B)
        c.create_line(L, T, L, H - B, fill=UI['border'])
        c.create_line(L, H - B, W - R, H - B, fill=UI['border'])
        if fit.resid_std is not None:
            upper = [(px(v), py(fit.predict(v) + b)) for v, b in zip(xs, band)]
            lower = [(px(v), py(fit.predict(v) - b)) for v, b in zip(xs, band)][::-1]
            c.create_polygon([k for p in upper + lower for k in p], fill='#d9dcd6', outline='', stipple='gray50')
        c.create_line(px(xmin), py(fit.predict(xmin)), px(xmax), py(fit.predict(xmax)), fill=UI['text'], width=2)
        for t, xi, yi in zip(self._targets, x, y):
            cx, cy = px(xi), py(yi)
            c.create_oval(cx - 6, cy - 6, cx + 6, cy + 6, fill=t.color, outline=UI['text'], width=1.5)
        c.create_text(L + 6, T + 4, anchor='nw', fill=UI['text'], font=self.fonts['mono_small'],
                      text=f"y = {fit.slope:.4f} x {'+' if fit.intercept >= 0 else '−'} {abs(fit.intercept):.2f}"
                           + (f"\nRMSE {fit.rmse:.2f} {self._unit}" if fit.rmse is not None else "\n2 targets: R² = 1 by construction"))
        c.create_text(W - R, H - 4, anchor='se', text=f"measured {self._unit}", fill=UI['muted'], font=self.fonts['tiny'])
        try:
            c.create_text(10, T + 4, anchor='ne', angle=90, text=f"reference {self._unit}", fill=UI['muted'], font=self.fonts['tiny'])
        except tk.TclError:
            pass
        c.create_text(L + 2, H - B + 3, anchor='nw', text=f"{xmin:.0f}", fill=UI['muted'], font=self.fonts['tiny'])
        c.create_text(W - R, H - B + 3, anchor='ne', text=f"{xmax:.0f}", fill=UI['muted'], font=self.fonts['tiny'])

# =============================================================================
# FLIGHT TIMELINE
# =============================================================================

class TimelinePanel:
    """Calibration shots as diamonds, survey images as ticks, the offset b(t) drawn across."""
    H = 118

    def __init__(self, parent, fonts: Dict[str, Any]):
        self.fonts = fonts
        self.frame = tk.Frame(parent, bg=UI['panel'], height=self.H)
        self.frame.pack_propagate(False)
        tk.Frame(self.frame, bg=UI['border'], height=1).pack(fill=tk.X, side=tk.TOP)
        top = tk.Frame(self.frame, bg=UI['panel'])
        top.pack(fill=tk.X, padx=14, pady=(7, 2))
        tk.Label(top, text="Flight timeline", bg=UI['panel'], fg=UI['text'], font=fonts['small_bold']).pack(side=tk.LEFT)
        for text, kind in (("calibration shot", 'diamond'), ("survey image", 'tick'), ("offset b(t)", 'line')):
            lg = tk.Canvas(top, width=16, height=12, bg=UI['panel'], highlightthickness=0)
            lg.pack(side=tk.LEFT, padx=(14, 4))
            if kind == 'diamond':
                lg.create_polygon(8, 1, 15, 6, 8, 11, 1, 6, fill=UI['accent'], outline='')
            elif kind == 'tick':
                lg.create_rectangle(7, 1, 9, 11, fill=UI['tick'], outline='')
            else:
                lg.create_line(0, 6, 16, 6, fill=UI['drift'], width=2)
            tk.Label(top, text=text, bg=UI['panel'], fg=UI['muted'], font=fonts['tiny']).pack(side=tk.LEFT)
        self.summary = tk.Label(top, text="", bg=UI['panel'], fg=UI['muted'], font=fonts['tiny'], anchor='e')
        self.summary.pack(side=tk.RIGHT)
        self.canvas = tk.Canvas(self.frame, bg=UI['bg'], highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True, padx=14, pady=(2, 8))
        self.canvas.bind('<Configure>', lambda e: self.redraw())
        self.events: List[CalibrationEvent] = []
        self.survey: List[Tuple[datetime, bool]] = []

    def set_data(self, events: List[CalibrationEvent], survey: List[Tuple[datetime, bool]], summary: str = ""):
        self.events = sorted(events, key=lambda e: e.timestamp)
        self.survey = survey
        self.summary.config(text=summary)
        self.redraw()

    def redraw(self):
        c = self.canvas
        c.delete('all')
        W = max(c.winfo_width(), 200)
        H = max(c.winfo_height(), 40)
        times = [e.timestamp for e in self.events] + [t for t, _ in self.survey]
        if not times:
            c.create_text(W / 2, H / 2, text="Add calibration images; survey images appear here once chosen in step 4.",
                          fill=UI['muted'], font=self.fonts['tiny'])
            return
        t0, t1 = min(times), max(times)
        span = max((t1 - t0).total_seconds(), 60.0)
        pad = span * 0.05
        mid = H * 0.48

        def x_of(t: datetime) -> float:
            return 10 + (t - t0).total_seconds() / (span + 2 * pad) * (W - 20) + pad / (span + 2 * pad) * (W - 20)
        c.create_line(10, mid, W - 10, mid, fill=UI['border'])
        for t, outside in self.survey:
            x = x_of(t)
            c.create_rectangle(x - 1, mid - 11, x + 1, mid - 1, fill=UI['tick_out'] if outside else UI['tick'], outline='')
        fitted = [e for e in self.events if e.fit is not None]
        if len(fitted) >= 2:
            bs = [e.fit.intercept for e in fitted]
            bmin, bmax = min(bs), max(bs)
            yb_top, yb_bot = mid + 6, H - 16

            def y_of(b):
                return yb_bot - (b - bmin) / (bmax - bmin) * (yb_bot - yb_top) if bmax > bmin else (yb_top + yb_bot) / 2
            pts = [k for e in fitted for k in (x_of(e.timestamp), y_of(e.fit.intercept))]
            c.create_line(pts, fill=UI['drift'], width=2)
        for e in self.events:
            x = x_of(e.timestamp)
            fill = UI['accent'] if e.fit is not None else UI['panel']
            c.create_polygon(x, mid - 7, x + 7, mid, x, mid + 7, x - 7, mid, fill=fill, outline=UI['accent'], width=2)
            label = f"{e.timestamp:%H:%M:%S}" + (f" · b = {e.fit.intercept:+.2f}" if e.fit is not None and not e.fit.single_point
                                                 else (f" · K = {e.fit.slope:.4g}" if e.fit is not None else " · not fitted"))
            c.create_text(min(max(x, 60), W - 60), H - 2, anchor='s', text=label, fill=UI['text'], font=self.fonts['tiny'])
        if self.survey:
            c.create_text(10, 2, anchor='nw', text=f"{t0:%H:%M}", fill=UI['muted'], font=self.fonts['tiny'])
            c.create_text(W - 10, 2, anchor='ne', text=f"{t1:%H:%M}", fill=UI['muted'], font=self.fonts['tiny'])


# =============================================================================
# PROCESS VIEW (centre, step 4)
# =============================================================================

class ProcessView:
    """Pre-flight list, run bar and live results table."""
    COLUMNS = (('image', 'Image', 230, 'w'), ('ts', 'Time · source', 120, 'w'), ('m', 'm', 64, 'e'), ('b', 'b', 64, 'e'),
               ('min', 'mean in', 64, 'e'), ('mout', 'mean out', 68, 'e'), ('u', '± 1σ', 52, 'e'), ('status', 'Status', 90, 'w'))

    def __init__(self, parent, fonts: Dict[str, Any], on_check, on_run, on_cancel, on_open_folder):
        self.fonts = fonts
        self.frame = tk.Frame(parent, bg=UI['bg'])
        # pre-flight
        pf = tk.Frame(self.frame, bg=UI['panel'], highlightthickness=1, highlightbackground=UI['border'])
        pf.pack(fill=tk.X, padx=16, pady=(14, 10))
        head = tk.Frame(pf, bg=UI['panel'])
        head.pack(fill=tk.X, padx=12, pady=(8, 4))
        tk.Label(head, text="Pre-flight check", bg=UI['panel'], fg=UI['text'], font=fonts['title']).pack(side=tk.LEFT)
        self.pf_counts = tk.Label(head, text="not run yet", bg=UI['panel'], fg=UI['muted'], font=fonts['small'])
        self.pf_counts.pack(side=tk.RIGHT)
        self.preflight_text = tk.Text(pf, height=7, state=tk.DISABLED, font=fonts['small'], relief='flat', wrap='word',
                                      bg=UI['panel'], fg=UI['text'], padx=12, pady=4, highlightthickness=0)
        self.preflight_text.pack(fill=tk.X)
        self.preflight_text.tag_configure('ok', foreground=UI['ok'])
        self.preflight_text.tag_configure('warn', foreground=UI['warn'])
        self.preflight_text.tag_configure('block', foreground=UI['block'])
        # run bar
        run = tk.Frame(self.frame, bg=UI['panel'], highlightthickness=1, highlightbackground=UI['border'])
        run.pack(fill=tk.X, padx=16, pady=(0, 10))
        bar = tk.Frame(run, bg=UI['panel'])
        bar.pack(fill=tk.X, padx=12, pady=10)
        self.check_btn = ttk.Button(bar, text="Check", command=on_check)
        self.check_btn.pack(side=tk.LEFT)
        self.run_btn = ttk.Button(bar, text="Run", style='Accent.TButton', command=on_run)
        self.run_btn.pack(side=tk.LEFT, padx=8)
        self.cancel_btn = ttk.Button(bar, text="Cancel", command=on_cancel, state=tk.DISABLED)
        self.cancel_btn.pack(side=tk.LEFT)
        prog = tk.Frame(bar, bg=UI['panel'])
        prog.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(16, 0))
        self.progress_label = tk.Label(prog, text="Ready", bg=UI['panel'], fg=UI['muted'], font=fonts['small'], anchor='w')
        self.progress_label.pack(fill=tk.X)
        self.progress_var = tk.DoubleVar(value=0)
        self.progress_bar = ttk.Progressbar(prog, variable=self.progress_var, maximum=100, mode='determinate')
        self.progress_bar.pack(fill=tk.X, pady=(3, 0))
        # results
        res = tk.Frame(self.frame, bg=UI['panel'], highlightthickness=1, highlightbackground=UI['border'])
        res.pack(fill=tk.BOTH, expand=True, padx=16, pady=(0, 14))
        rh = tk.Frame(res, bg=UI['panel'])
        rh.pack(fill=tk.X, padx=12, pady=(8, 4))
        tk.Label(rh, text="Results", bg=UI['panel'], fg=UI['text'], font=fonts['title']).pack(side=tk.LEFT)
        self.results_hint = tk.Label(rh, text="one row per image; grey rows are not processed yet", bg=UI['panel'],
                                     fg=UI['muted'], font=fonts['small'])
        self.results_hint.pack(side=tk.LEFT, padx=10)
        self.open_btn = ttk.Button(rh, text="Open output folder", command=on_open_folder, state=tk.DISABLED)
        self.open_btn.pack(side=tk.RIGHT)
        tf = tk.Frame(res, bg=UI['panel'])
        tf.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 10))
        self.tree = ttk.Treeview(tf, columns=[c[0] for c in self.COLUMNS], show='headings', selectmode='browse')
        for key, title, width, anchor in self.COLUMNS:
            self.tree.heading(key, text=title, anchor=anchor)
            self.tree.column(key, width=width, anchor=anchor, stretch=(key in ('image', 'status')))
        sb = ttk.Scrollbar(tf, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.pack(fill=tk.BOTH, expand=True)
        self.tree.tag_configure('ok', foreground=UI['text'])
        self.tree.tag_configure('warn', foreground=UI['warn'])
        self.tree.tag_configure('err', foreground=UI['block'])
        self.tree.tag_configure('pending', foreground=UI['muted'])
        self.log_lines: List[str] = []
        self._items: Dict[str, str] = {}      # input filename -> tree item

    def set_preflight(self, rep: Optional[PreflightReport]):
        t = self.preflight_text
        t.config(state=tk.NORMAL)
        t.delete(1.0, tk.END)
        if rep is None:
            self.pf_counts.config(text="not run yet")
        else:
            sym = {'ok': '✓ ', 'warn': '⚠ ', 'block': '✗ '}
            for level, text in rep.lines:
                t.insert(tk.END, sym[level] + text + "\n", level)
            n = {k: sum(1 for l, _ in rep.lines if l == k) for k in ('ok', 'warn', 'block')}
            self.pf_counts.config(text=f"{n['ok']} passed · {n['warn']} warnings · {n['block']} blocking",
                                  fg=UI['block'] if n['block'] else (UI['warn'] if n['warn'] else UI['ok']))
        t.config(state=tk.DISABLED)

    def clear_rows(self):
        for i in self.tree.get_children():
            self.tree.delete(i)
        self._items = {}
        self.log_lines = []

    def set_pending(self, files: List[Path], timestamps: Dict[Path, Timestamp]):
        """List every image that will be processed, greyed out, before anything runs."""
        self.clear_rows()
        for p in files:
            ts = timestamps.get(p)
            when = f"{ts.value:%H:%M:%S} · {ts.source}" if ts and ts.value else ""
            self._items[p.name] = self.tree.insert('', tk.END, values=(p.name, when, "", "", "", "", "", "not processed"),
                                                   tags=('pending',))

    def add_row(self, row: dict, unit_key: str):
        """Fill in (or append) the row of a finished image."""
        def f(v, nd=3):
            return "" if v is None or (isinstance(v, float) and not np.isfinite(v)) else (f"{v:.{nd}f}" if isinstance(v, (int, float)) else str(v))
        status = str(row.get('status', ''))
        pos = row.get('position_vs_calibration', '')
        shown = status if status != 'ok' else (pos if pos in ('before', 'after') else 'ok')
        tag = 'err' if status.startswith('error') else ('warn' if shown in ('before', 'after') or status.startswith('skipped') else 'ok')
        ts = row.get('timestamp', '')
        ts = ts[11:19] if isinstance(ts, str) and len(ts) >= 19 else ts
        values = (row.get('input_filename', ''), f"{ts} · {row.get('timestamp_source', '')}",
                  f(row.get('slope_m'), 4), f(row.get('intercept_b')),
                  f(row.get(f'original_mean_{unit_key}'), 2), f(row.get(f'calibrated_mean_{unit_key}'), 2),
                  f(row.get('u_fit_1sigma'), 2), shown)
        name = str(row.get('input_filename', ''))
        if name in self._items and self.tree.exists(self._items[name]):
            self.tree.item(self._items[name], values=values, tags=(tag,))
            self.tree.see(self._items[name])
        else:
            self._items[name] = self.tree.insert('', tk.END, values=values, tags=(tag,))
            self.tree.yview_moveto(1.0)


# =============================================================================
# FIT PLOT DIALOG (detailed, matplotlib)
# =============================================================================

class RegressionPlotDialog:
    """Scatter + fitted line + prediction band, residuals below. Uses target colours."""

    def __init__(self, parent, event: CalibrationEvent, targets: List[CalibrationTarget], unit: str, title: str):
        if not HAS_MATPLOTLIB:
            messagebox.showerror("Matplotlib required", "Install matplotlib to view the regression plot.")
            return
        fit = event.fit
        self.win = tk.Toplevel(parent)
        self.win.title(title)
        self.win.geometry("760x640")
        self.win.transient(parent)
        self.fig = Figure(figsize=(7.4, 5.8), dpi=100)
        gs = self.fig.add_gridspec(3, 1, height_ratios=[3, 1, 0.01], hspace=0.08)
        ax = self.fig.add_subplot(gs[0])
        axr = self.fig.add_subplot(gs[1], sharex=ax)
        complete = [t for t in targets if t.is_complete()]
        x = np.array(fit.x_values)
        y = np.array(fit.y_values)
        span = max(float(np.ptp(x)), 1e-6)
        xr = np.linspace(x.min() - 0.1 * span - 0.5, x.max() + 0.1 * span + 0.5, 200)
        ax.plot(xr, fit.predict(xr), color='black', linestyle='--', linewidth=1.3, label='fit')
        if fit.resid_std is not None:
            band = np.array([fit.predict_uncertainty(v) for v in xr])
            ax.fill_between(xr, fit.predict(xr) - band, fit.predict(xr) + band, color='#999999', alpha=0.2,
                            label='±1σ prediction')
        for t, xi, yi, ri in zip(complete, x, y, fit.residuals):
            ax.scatter(xi, yi, color=t.color, edgecolor='black', s=80, zorder=3)
            ax.annotate(t.name, (xi, yi), textcoords="offset points", xytext=(7, 5), fontsize=9)
            axr.bar(xi, ri, width=span * 0.03 + 0.05, color=t.color, edgecolor='black')
        axr.axhline(0, color='black', linewidth=0.8)
        eq = f"y = {fit.slope:.4f} x + {fit.intercept:.4f}\nR² = {fit.r_squared:.4f}, n = {fit.n}"
        if fit.rmse is not None:
            eq += f"\nRMSE = {fit.rmse:.3f} {unit}, LOO = {fit.loo_rmse:.3f} {unit}" if fit.loo_rmse is not None \
                else f"\nRMSE = {fit.rmse:.3f} {unit}"
        else:
            eq += "\n2 targets: R² is 1 by construction"
        ax.text(0.02, 0.98, eq, transform=ax.transAxes, fontsize=10, verticalalignment='top',
                bbox=dict(facecolor='white', alpha=0.85, edgecolor='none'))
        ax.set_ylabel(f"Reference ({unit})")
        ax.grid(alpha=0.3)
        ax.legend(loc='lower right', fontsize=9)
        ax.tick_params(labelbottom=False)
        axr.set_ylabel("residual")
        axr.set_xlabel(f"Sensor ROI mean ({unit})")
        axr.grid(alpha=0.3)
        self.canvas = FigureCanvasTkAgg(self.fig, master=self.win)
        self.canvas.get_tk_widget().pack(fill='both', expand=True)
        try:
            NavigationToolbar2Tk(self.canvas, self.win).update()
        except Exception:
            pass
        btns = ttk.Frame(self.win)
        btns.pack(fill='x', pady=6)
        ttk.Button(btns, text="Save plot as PNG", command=self._save_png).pack(side='right', padx=6)
        ttk.Button(btns, text="Close", command=self.win.destroy).pack(side='right')
        self.canvas.draw()

    def _save_png(self):
        out = filedialog.asksaveasfilename(parent=self.win, defaultextension=".png", filetypes=[("PNG image", "*.png")],
                                           title="Save regression plot")
        if out:
            self.fig.savefig(out, dpi=200)


# =============================================================================
# MAIN APPLICATION
# =============================================================================

class ThermalCalibrationApp:
    """The GUI: a four-step rail (Images, Targets, Fit, Process), a zoomable image with a flight
    timeline below it, and a live fit panel. Science lives in the module functions above."""

    MODE_THERMAL = MODE_THERMAL
    MODE_MSI = MODE_MSI
    STEP_TITLES = {1: "Images", 2: "Targets", 3: "Fit", 4: "Process"}

    def __init__(self, root: tk.Tk):
        self.root = root
        self.fonts = setup_theme(root)
        self.root.configure(bg=UI['bg'])
        self.root.title(f"{TOOL_NAME}")
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        self.root.geometry(f"{min(1500, sw - 60)}x{min(950, sh - 100)}")
        self.root.minsize(1100, 700)

        self.sensor_mode = tk.StringVar(value=MODE_THERMAL)
        self.msi_single_point = tk.BooleanVar(value=True)
        self.viz_mode = tk.StringVar(value="inferno")
        self.roi_inset_var = tk.IntVar(value=DEFAULT_ROI_INSET_PX)
        self.emis_enabled = tk.BooleanVar(value=False)
        self.emis_target_var = tk.StringVar(value="0.95")
        self.emis_scene_var = tk.StringVar(value="0.98")
        self.tsky_var = tk.StringVar(value="-40")
        self.copy_meta_var = tk.BooleanVar(value=True)
        self.skip_existing_var = tk.BooleanVar(value=False)
        self.filter_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Add calibration images to begin")
        self.zoom_var = tk.StringVar(value="")
        self.project_var = tk.StringVar(value="untitled project")
        self.saved_var = tk.StringVar(value="")

        self.calibration_events: List[CalibrationEvent] = []
        self.current_event_index = 0
        self.current_image: Optional[np.ndarray] = None
        self._image_cache: Dict[str, np.ndarray] = {}            # path -> float image (bounded)
        self._display_cache: Dict[Tuple[str, str], np.ndarray] = {}   # (path, viz mode) -> RGB preview
        self._colorbar_photo = None
        self.target_rows: List[TargetRowWidget] = []
        self.drawing_polygon = False
        self.polygon_points: List[Tuple[int, int]] = []
        self.active_target_index = 0
        self.project_path: Optional[str] = None
        self.active_step = 1
        self._survey_timestamps: Dict[Path, Timestamp] = {}
        self._survey_scan_id = 0
        self._batch_thread: Optional[threading.Thread] = None
        self._q: "queue.Queue[tuple]" = queue.Queue()
        self._batch_cancel = threading.Event()
        self._last_preflight: Optional[PreflightReport] = None
        self._last_output: Optional[str] = None

        self._build_menu()
        self._build_ui()
        self.root.bind('<Escape>', lambda e: self._cancel_drawing())
        self.root.bind('<Control-s>', lambda e: self._save_project())
        self.root.bind('<Control-o>', lambda e: self._open_project())
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.show_step(1)
        self.root.after(150, self._poll_queue)

    # ------------------------------------------------------------------ build
    def _build_menu(self):
        menubar = tk.Menu(self.root)
        fm = tk.Menu(menubar, tearoff=0)
        fm.add_command(label="New project", command=self._new_project)
        fm.add_command(label="Open project…", accelerator="Ctrl+O", command=self._open_project)
        fm.add_command(label="Save project", accelerator="Ctrl+S", command=self._save_project)
        fm.add_command(label="Save project as…", command=lambda: self._save_project(save_as=True))
        fm.add_separator()
        fm.add_command(label="Exit", command=self._on_close)
        menubar.add_cascade(label="File", menu=fm)
        hm = tk.Menu(menubar, tearoff=0)
        hm.add_command(label="How to use", command=self._show_help)
        hm.add_command(label="About", command=lambda: messagebox.showinfo(
            "About", f"{TOOL_NAME} {__version__}\nEmpirical Line Method calibration for drone thermal and "
                     "single-band multispectral images.\nGPL-3.0 · Moritz Nelle"))
        menubar.add_cascade(label="Help", menu=hm)
        self.root.config(menu=menubar)

    def _build_ui(self):
        f = self.fonts
        # ---- top bar
        top = tk.Frame(self.root, bg=UI['panel'], height=52)
        top.pack(fill=tk.X)
        top.pack_propagate(False)
        tk.Label(top, text="Calibration Tool", bg=UI['panel'], fg=UI['text'], font=f['bold']).pack(side=tk.LEFT, padx=(18, 0))
        tk.Frame(top, bg=UI['border'], width=1).pack(side=tk.LEFT, fill=tk.Y, padx=14, pady=14)
        self.title_entry = tk.Entry(top, textvariable=self.project_var, bg=UI['panel'], fg=UI['text'], font=f['ui'],
                                    relief='flat', bd=0, width=30, highlightthickness=1, highlightbackground=UI['panel'],
                                    highlightcolor=UI['accent'], insertbackground=UI['text'], cursor='xterm')
        self.title_entry.pack(side=tk.LEFT, ipady=3)
        ToolTip(self.title_entry, "Project title: click to edit. It becomes the file name when you save.")
        self.title_entry.bind('<Return>', lambda e: self._on_title_edited(True))
        self.title_entry.bind('<FocusOut>', lambda e: self._on_title_edited(False))
        tk.Label(top, textvariable=self.saved_var, bg=UI['panel'], fg=UI['muted'], font=f['small']).pack(side=tk.LEFT, padx=8)
        self.msi_method_frame = tk.Frame(top, bg=UI['panel'])
        for text, val in (("Single point", True), ("Multi point", False)):
            ttk.Radiobutton(self.msi_method_frame, text=text, style='Toolbutton', variable=self.msi_single_point, value=val,
                            command=self._on_single_point_toggle).pack(side=tk.LEFT)
        mode = tk.Frame(top, bg=UI['panel'], highlightthickness=1, highlightbackground=UI['border'])
        mode.pack(side=tk.RIGHT, padx=18, pady=10)
        ttk.Radiobutton(mode, text="Thermal", style='Toolbutton', variable=self.sensor_mode, value=MODE_THERMAL,
                        command=self._on_mode_switch).pack(side=tk.LEFT)
        ttk.Radiobutton(mode, text="Multispectral", style='Toolbutton', variable=self.sensor_mode, value=MODE_MSI,
                        command=self._on_mode_switch).pack(side=tk.LEFT)
        tk.Frame(self.root, bg=UI['border'], height=1).pack(fill=tk.X)

        body = tk.Frame(self.root, bg=UI['bg'])
        body.pack(fill=tk.BOTH, expand=True)

        # ---- left rail
        rail_outer = tk.Frame(body, bg=UI['panel'], width=340)
        rail_outer.pack(side=tk.LEFT, fill=tk.Y)
        rail_outer.pack_propagate(False)
        self._rail_canvas = tk.Canvas(rail_outer, bg=UI['panel'], highlightthickness=0)
        rail_sb = ttk.Scrollbar(rail_outer, orient='vertical', command=self._rail_canvas.yview)
        self.rail = tk.Frame(self._rail_canvas, bg=UI['panel'])
        self.rail.bind('<Configure>', lambda e: self._rail_canvas.configure(scrollregion=self._rail_canvas.bbox('all')))
        self._rail_window = self._rail_canvas.create_window((0, 0), window=self.rail, anchor='nw')
        self._rail_canvas.bind('<Configure>', lambda e: self._rail_canvas.itemconfigure(self._rail_window, width=e.width))
        self._rail_canvas.configure(yscrollcommand=rail_sb.set)
        rail_sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._rail_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        rail_outer.bind('<Enter>', lambda e: self._bind_panel_wheel(True))
        rail_outer.bind('<Leave>', lambda e: self._bind_panel_wheel(False))
        self.steps: Dict[int, StepSection] = {n: StepSection(self.rail, n, t, self.show_step, f) for n, t in self.STEP_TITLES.items()}
        self._build_step_images(self.steps[1].body)
        self._build_step_targets(self.steps[2].body)
        self._build_step_fit(self.steps[3].body)
        self._build_step_process(self.steps[4].body)
        self.rail_hint = tk.Label(self.rail, text="Single shot? Fine: one set of coefficients is applied to all images. "
                                                  "Two shots also correct drift during the flight.",
                                  bg=UI['panel'], fg=UI['muted'], font=f['tiny'], justify='left', wraplength=280, padx=14, pady=10)
        self.rail_hint.pack(fill=tk.X, side=tk.BOTTOM)
        tk.Frame(body, bg=UI['border'], width=1).pack(side=tk.LEFT, fill=tk.Y)

        # ---- right fit panel
        self.fit_panel = FitPanel(body, f, self._show_regression_plot, self._export_fit_report)
        self.fit_panel.frame.pack(side=tk.RIGHT, fill=tk.Y)

        # ---- centre: image view or process view
        self.center = tk.Frame(body, bg=UI['bg'])
        self.center.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.image_view = tk.Frame(self.center, bg=UI['bg'])
        tb = tk.Frame(self.image_view, bg=UI['panel'], height=40)
        tb.pack(fill=tk.X)
        tb.pack_propagate(False)
        self.image_name_label = tk.Label(tb, text="No image", bg=UI['panel'], fg=UI['text'], font=f['small_bold'])
        self.image_info_label = tk.Label(tb, text="", bg=UI['panel'], fg=UI['muted'], font=f['tiny'], anchor='w')
        viz = tk.Frame(tb, bg=UI['panel'], highlightthickness=1, highlightbackground=UI['border'])
        viz.pack(side=tk.RIGHT, padx=12, pady=6)
        for text, val in (("Inferno", "inferno"), ("Grey", "grey"), ("CLAHE", "clahe")):
            ttk.Radiobutton(viz, text=text, style='Toolbutton', variable=self.viz_mode, value=val,
                            command=self._update_display).pack(side=tk.LEFT)
        zoom = tk.Frame(tb, bg=UI['panel'], highlightthickness=1, highlightbackground=UI['border'])
        zoom.pack(side=tk.RIGHT, padx=4, pady=6)
        ttk.Button(zoom, text="Fit", style='Small.TButton', width=4, command=lambda: self.view.fit_and_redraw()).pack(side=tk.LEFT)
        ttk.Button(zoom, text="1:1", style='Small.TButton', width=4, command=lambda: self.view.one_to_one()).pack(side=tk.LEFT)
        tk.Label(zoom, textvariable=self.zoom_var, bg=UI['panel'], fg=UI['muted'], font=f['small'], width=6).pack(side=tk.LEFT, padx=4)
        # labels are packed last so the controls on the right keep their space; the info text gets what is left
        self.image_name_label.pack(side=tk.LEFT, padx=(12, 8))
        self.image_info_label.pack(side=tk.LEFT, fill=tk.X, expand=True)
        tk.Frame(self.image_view, bg=UI['border'], height=1).pack(fill=tk.X)
        self.view = ZoomCanvas(self.image_view, on_click=self._on_image_click, on_motion=self._on_image_motion,
                               on_overlay=self._draw_overlay, on_view_change=self._on_view_change)
        self.view.canvas.pack(fill=tk.BOTH, expand=True)
        self.timeline = TimelinePanel(self.image_view, f)
        self.timeline.frame.pack(fill=tk.X, side=tk.BOTTOM)
        self.process_view = ProcessView(self.center, f, self._run_preflight, self._start_batch_processing, self._cancel_batch,
                                        self._open_output_folder)
        self.image_view.pack(fill=tk.BOTH, expand=True)

        # ---- status bar
        tk.Frame(self.root, bg=UI['border'], height=1).pack(fill=tk.X)
        status = tk.Frame(self.root, bg=UI['panel'], height=26)
        status.pack(fill=tk.X)
        status.pack_propagate(False)
        tk.Label(status, textvariable=self.status_var, bg=UI['panel'], fg=UI['blue'], font=f['small'], anchor='w').pack(side=tk.LEFT, padx=16)
        self.mode_status = tk.Label(status, text="Thermal", bg=UI['panel'], fg=UI['muted'], font=f['small'])
        self.mode_status.pack(side=tk.RIGHT, padx=16)

    def _section(self, parent, title: Optional[str] = None) -> tk.Frame:
        """White card inside a step body."""
        if title:
            tk.Label(parent, text=title, bg=UI['bg'], fg=UI['muted'], font=self.fonts['tiny'], anchor='w').pack(fill=tk.X, pady=(6, 2))
        card = tk.Frame(parent, bg=UI['panel'], highlightthickness=1, highlightbackground=UI['border'])
        card.pack(fill=tk.X)
        return card

    def _build_step_images(self, body):
        f = self.fonts
        row = tk.Frame(body, bg=UI['bg'])
        row.pack(fill=tk.X, pady=(2, 6))
        ttk.Button(row, text="Add images…", style='Accent.TButton', command=self._load_calibration_images).pack(side=tk.LEFT)
        ttk.Button(row, text="Remove", command=self._remove_calibration_image).pack(side=tk.LEFT, padx=6)
        help_icon(row, "Images that show the reference targets. One image is enough (its coefficients are used for the "
                       "whole flight). Two or more, e.g. before and after the flight, also correct drift during the flight: "
                       "coefficients are interpolated in time between them.\n\nImages must be single-band temperature (or DN) "
                       "files, not colour previews.", side=tk.LEFT)
        card = self._section(body)
        self.calib_listbox = tk.Listbox(card, height=5, exportselection=False, font=f['mono_small'], activestyle='none',
                                        relief='flat', bd=0, bg=UI['panel'], fg=UI['text'], selectbackground=UI['accent_soft'],
                                        selectforeground=UI['text'], highlightthickness=0)
        self.calib_listbox.pack(fill=tk.X, padx=6, pady=6)
        self.calib_listbox.bind('<<ListboxSelect>>', self._on_calib_select)
        self.current_image_label = tk.Label(body, text="No image selected", bg=UI['bg'], fg=UI['muted'], font=f['tiny'],
                                            justify='left', anchor='w', wraplength=280)
        self.current_image_label.pack(fill=tk.X, pady=(4, 0))

    def _build_step_targets(self, body):
        f = self.fonts
        nav = tk.Frame(body, bg=UI['bg'])
        nav.pack(fill=tk.X, pady=(2, 6))
        ttk.Button(nav, text="‹", style='Small.TButton', width=2, command=lambda: self._step_image(-1)).pack(side=tk.LEFT)
        self.shot_label = tk.Label(nav, text="no shot", bg=UI['bg'], fg=UI['text'], font=f['small_bold'])
        self.shot_label.pack(side=tk.LEFT, padx=8)
        ttk.Button(nav, text="›", style='Small.TButton', width=2, command=lambda: self._step_image(1)).pack(side=tk.LEFT)
        self.targets_help_btn = help_icon(nav, "", side=tk.RIGHT)
        self._update_targets_tooltip()
        card = self._section(body)
        header = tk.Frame(card, bg=UI['bg'])
        header.pack(fill=tk.X)
        hrow = tk.Frame(header, bg=UI['bg'])
        hrow.pack(fill=tk.X, padx=6, pady=3)
        hrow.columnconfigure(4, weight=1)
        tk.Label(hrow, text="", width=1, bg=UI['bg']).grid(row=0, column=0, padx=(2, 6))
        tk.Label(hrow, text="ID", width=3, bg=UI['bg'], fg=UI['muted'], font=f['tiny'], anchor='w').grid(row=0, column=1, sticky='w')
        tk.Label(hrow, text="Polygon", width=11, bg=UI['bg'], fg=UI['muted'], font=f['tiny'], anchor='w').grid(row=0, column=2, padx=2, sticky='w')
        self.gt_header_label = tk.Label(hrow, text=self._gt_label(), width=7, bg=UI['bg'], fg=UI['muted'], font=f['tiny'], anchor='e')
        self.gt_header_label.grid(row=0, column=3, padx=4)
        tk.Label(hrow, text="mean ± σ · n", bg=UI['bg'], fg=UI['muted'], font=f['tiny'], anchor='w').grid(row=0, column=4, sticky='w')
        self.targets_container = tk.Frame(card, bg=UI['panel'])
        self.targets_container.pack(fill=tk.X)
        tb = tk.Frame(body, bg=UI['bg'])
        tb.pack(fill=tk.X, pady=6)
        self.add_target_btn = ttk.Button(tb, text="+ Add target", command=self._add_target)
        self.add_target_btn.pack(side=tk.LEFT)
        self.copy_btn = ttk.Button(tb, text="Copy to other shots", command=self._copy_polygons)
        self.copy_btn.pack(side=tk.LEFT, padx=6)
        ir = tk.Frame(body, bg=UI['bg'])
        ir.pack(fill=tk.X)
        tk.Label(ir, text="ROI edge inset (px)", bg=UI['bg'], fg=UI['text'], font=f['small']).pack(side=tk.LEFT)
        sp = ttk.Spinbox(ir, from_=0, to=20, width=4, textvariable=self.roi_inset_var, command=self._on_inset_change)
        sp.pack(side=tk.LEFT, padx=6)
        sp.bind('<FocusOut>', lambda e: self._on_inset_change())
        sp.bind('<Return>', lambda e: self._on_inset_change())
        help_icon(ir, "Pixels removed from the polygon edge before the mean is taken. Thermal sensors blur edges over 2 to "
                      "3 px, so edge pixels are mixed with the background. 3 px is a good default; the dashed outline on the "
                      f"image shows the region actually used. A ⚠ means fewer than {MIN_ROI_PIXELS} pixels or no inset "
                      "possible: draw the polygon larger or zoom in.", side=tk.LEFT)
        tk.Label(body, text="Wheel: zoom · right-drag: pan · Esc: cancel drawing · comma or dot accepted",
                 bg=UI['bg'], fg=UI['muted'], font=f['tiny'], justify='left', anchor='w', wraplength=280).pack(fill=tk.X, pady=(6, 0))

    def _build_step_fit(self, body):
        f = self.fonts
        tk.Label(body, text="The fit panel on the right updates as you enter readings: the regression line, residuals, "
                            "standard errors, RMSE, leave-one-out error and the ±1σ prediction uncertainty of the fit.",
                 bg=UI['bg'], fg=UI['muted'], font=f['tiny'], justify='left', anchor='w', wraplength=280).pack(fill=tk.X, pady=(2, 6))
        self.emis_frame = tk.Frame(body, bg=UI['bg'])
        self.emis_frame.pack(fill=tk.X, pady=(2, 0))
        er = tk.Frame(self.emis_frame, bg=UI['bg'])
        er.pack(fill=tk.X)
        ttk.Checkbutton(er, text="Emissivity correction (advanced)", variable=self.emis_enabled, command=self._on_settings_change).pack(side=tk.LEFT)
        help_icon(er, "Usually OFF. The empirical line already absorbs the panels' emissivity, and the DJI SDK applies its "
                      "own emissivity setting (0.95 by default) when converting R-JPEGs.\n\nWhen ON: panel ROI values are "
                      "corrected with the panel emissivity before the fit, and survey pixels with the scene emissivity "
                      "before the coefficients are applied, so both sides of the fit are in the same space. Broadband "
                      "grey-body approximation:\nT_obj⁴ = (T_app⁴ − (1−ε)·T_sky⁴) / ε", side=tk.LEFT, padx=4)
        ev = tk.Frame(self.emis_frame, bg=UI['bg'])
        ev.pack(fill=tk.X, pady=(4, 0))
        for text, var in (("Panel ε", self.emis_target_var), ("Scene ε", self.emis_scene_var), ("T_sky °C", self.tsky_var)):
            tk.Label(ev, text=text, bg=UI['bg'], fg=UI['text'], font=f['small']).pack(side=tk.LEFT, padx=(0, 3))
            e_ = ttk.Entry(ev, width=6, textvariable=var, justify='right')
            e_.pack(side=tk.LEFT, padx=(0, 10))
            e_.bind('<FocusOut>', lambda e: self._on_settings_change())
            e_.bind('<Return>', lambda e: self._on_settings_change())
        tk.Label(body, text="With 3 or more targets the scatter can be estimated. With 2, R² is 1 by construction "
                            "and nothing can be checked.",
                 bg=UI['bg'], fg=UI['muted'], font=f['tiny'], justify='left', anchor='w', wraplength=280).pack(fill=tk.X, pady=(10, 0))

    def _build_step_process(self, body):
        f = self.fonts

        def field(label, var_entry: bool = True):
            tk.Label(body, text=label, bg=UI['bg'], fg=UI['muted'], font=f['tiny'], anchor='w').pack(fill=tk.X, pady=(6, 1))
            r = tk.Frame(body, bg=UI['bg'])
            r.pack(fill=tk.X)
            return r
        r = field("Survey folder (all images in it are processed; use the filter to narrow down)")
        self.survey_entry = ttk.Entry(r)
        self.survey_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.survey_entry.bind('<FocusOut>', lambda e: self._on_survey_changed())
        self.survey_entry.bind('<Return>', lambda e: self._on_survey_changed())
        ttk.Button(r, text="Browse", style='Small.TButton', command=self._browse_survey_folder).pack(side=tk.LEFT, padx=(4, 0))
        r = field("Output folder")
        self.output_entry = ttk.Entry(r)
        self.output_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(r, text="Browse", style='Small.TButton', command=self._browse_output_folder).pack(side=tk.LEFT, padx=(4, 0))
        r = field("Filename filter")
        fe = ttk.Entry(r, width=16, textvariable=self.filter_var, font=f['mono'])
        fe.pack(side=tk.LEFT)
        fe.bind('<FocusOut>', lambda e: self._on_survey_changed())
        fe.bind('<Return>', lambda e: self._on_survey_changed())
        help_icon(r, "Only files whose name contains this text are processed, e.g. '_T_deg' to skip the colour previews "
                     "next to the temperature TIFFs, or '_3' for one multispectral band.", side=tk.LEFT, padx=6)
        opts = tk.Frame(body, bg=UI['bg'])
        opts.pack(fill=tk.X, pady=(10, 0))
        ttk.Checkbutton(opts, text="Copy EXIF/XMP/GPS to outputs" + ("" if shutil.which('exiftool') else " (exiftool not found: GDAL tags only)"),
                        variable=self.copy_meta_var).pack(anchor='w')
        ttk.Checkbutton(opts, text="Skip images whose output already exists", variable=self.skip_existing_var).pack(anchor='w', pady=(4, 0))
        tk.Label(body, text="Choosing a folder runs the pre-flight check and lists the images; then press Run.",
                 bg=UI['bg'], fg=UI['muted'], font=f['tiny'], justify='left', anchor='w', wraplength=280).pack(fill=tk.X, pady=(10, 0))

    # --------------------------------------------------------------- steps
    def show_step(self, n: int):
        """Activate step n: expand its body in the rail and show the matching centre view."""
        self.active_step = n
        for k, s in self.steps.items():
            s.set_active(k == n)
        if n == 4:
            self.image_view.pack_forget()
            self.process_view.frame.pack(fill=tk.BOTH, expand=True)
            self._on_survey_changed()
        else:
            self.process_view.frame.pack_forget()
            self.image_view.pack(fill=tk.BOTH, expand=True)
        self._rail_canvas.yview_moveto(0)

    def _step_image(self, delta: int):
        if not self.calibration_events:
            return
        idx = (self.current_event_index + delta) % len(self.calibration_events)
        self.calib_listbox.selection_clear(0, tk.END)
        self.calib_listbox.selection_set(idx)
        self._cancel_drawing(silent=True)
        self._load_image(idx)

    def _update_step_summaries(self):
        evs = self.calibration_events
        n = len(evs)
        if n:
            times = ", ".join(f"{e.timestamp:%H:%M}" for e in evs[:3]) + (" …" if n > 3 else "")
            self.steps[1].set_done(True, f"{n} calibration shot{'s' if n != 1 else ''}, {times}")
        else:
            self.steps[1].set_done(False, "none yet")
        complete = [sum(1 for t in (e.targets[:1] if self._is_single_point() else e.targets) if t.is_complete()) for e in evs]
        need = 1 if self._is_single_point() else 2
        targets_done = bool(evs) and all(c >= need for c in complete)
        self.steps[2].set_done(targets_done, (f"{sum(complete)} complete target(s) on {n} shot(s)" if evs else "waiting for images"))
        fitted = [e for e in evs if e.fit is not None]
        if fitted:
            rm = [e.fit.rmse for e in fitted if e.fit.rmse is not None]
            txt = f"{len(fitted)}/{n} fitted" + (f" · RMSE {min(rm):.2f} to {max(rm):.2f} {self._unit()}" if rm else "")
            self.steps[3].set_done(len(fitted) == n, txt)
        else:
            self.steps[3].set_done(False, "no fit yet")
        out = self._last_output
        self.steps[4].set_done(out is not None, (f"written to {os.path.basename(out)}" if out else
                                                 (f"{len(self._survey_timestamps)} survey images, not processed yet"
                                                  if self._survey_timestamps else "choose the survey folder")))

    # --------------------------------------------------------------- helpers
    def _bind_panel_wheel(self, on: bool):
        if on:
            self.root.bind_all('<Button-4>', lambda e: self._rail_canvas.yview_scroll(-1, 'units'))
            self.root.bind_all('<Button-5>', lambda e: self._rail_canvas.yview_scroll(1, 'units'))
            self.root.bind_all('<MouseWheel>', lambda e: self._rail_canvas.yview_scroll(-1 if e.delta > 0 else 1, 'units'))
        else:
            for ev in ('<Button-4>', '<Button-5>', '<MouseWheel>'):
                self.root.unbind_all(ev)

    def _is_thermal(self) -> bool:
        return self.sensor_mode.get() == MODE_THERMAL

    def _is_single_point(self) -> bool:
        return (not self._is_thermal()) and self.msi_single_point.get()

    def _unit(self) -> str:
        return "°C" if self._is_thermal() else "DN"

    def _unit_key(self) -> str:
        return 'celsius' if self._is_thermal() else 'reflectance'

    def _gt_label(self) -> str:
        return "truth °C" if self._is_thermal() else "ρ (0–1)"

    def _current_event(self) -> Optional[CalibrationEvent]:
        if not self.calibration_events:
            return None
        return self.calibration_events[min(self.current_event_index, len(self.calibration_events) - 1)]

    def _visible_targets(self, ev: CalibrationEvent) -> List[CalibrationTarget]:
        return ev.targets[:1] if self._is_single_point() else ev.targets

    def _roi_inset(self) -> int:
        try:
            return max(0, int(self.roi_inset_var.get()))
        except (tk.TclError, ValueError):
            return DEFAULT_ROI_INSET_PX

    def _emissivity_settings(self) -> EmissivitySettings:
        def f(var, default):
            try:
                v = parse_number(var.get())
                return default if v is None else v
            except ValueError:
                return default
        return EmissivitySettings(self.emis_enabled.get() and self._is_thermal(), f(self.emis_target_var, 0.95),
                                  f(self.emis_scene_var, 0.98), f(self.tsky_var, -40.0))

    def _update_targets_tooltip(self):
        if self._is_thermal():
            text = ("For each target:\n1. Draw: click the 4 corners of the panel on the image (zoom in first)\n"
                    "2. Type the IR thermometer reading (°C) in the truth field; comma or dot\n\n"
                    "The fit updates as soon as 2 targets are complete; 3 or more give an uncertainty estimate.")
        else:
            text = ("For each reflectance panel:\n1. Draw: click the 4 corners of the panel\n"
                    "2. Type the known reflectance as a fraction (0–1, e.g. 50 % → 0.5)\n\n"
                    "Calibrate one band at a time (load single-band images).")
        self.targets_help_btn.unbind("<Enter>")
        self.targets_help_btn.unbind("<Leave>")
        ToolTip(self.targets_help_btn, text)

    # ------------------------------------------------------------ mode switch
    def _on_mode_switch(self):
        thermal = self._is_thermal()
        self.mode_status.config(text="Thermal" if thermal else ("Multispectral · single point" if self.msi_single_point.get() else "Multispectral · multi point"))
        if thermal:
            self.msi_method_frame.pack_forget()
            self.emis_frame.pack(fill=tk.X, pady=(8, 0))
        else:
            self.msi_method_frame.pack(side=tk.RIGHT, padx=(0, 4), pady=10)
            self.emis_frame.pack_forget()
        self.gt_header_label.config(text=self._gt_label())
        self._update_targets_tooltip()
        self._on_single_point_toggle()
        self.status_var.set(f"Switched to {'thermal' if thermal else 'multispectral'} mode.")

    def _on_single_point_toggle(self):
        single = self._is_single_point()
        self.add_target_btn.state(['disabled'] if single else ['!disabled'])
        self.mode_status.config(text="Thermal" if self._is_thermal() else ("Multispectral · single point" if single else "Multispectral · multi point"))
        self._rebuild_target_rows()        # extra targets are hidden in single-point mode, never deleted
        self._recompute_all_fits()
        self._update_display()

    def _on_settings_change(self):
        self._recompute_all_fits()

    def _on_inset_change(self):
        inset = self._roi_inset()
        for i, ev in enumerate(self.calibration_events):
            try:
                img = self._cached_image(ev.image_path)
            except ValueError:
                continue
            for t in ev.targets:
                if t.has_polygon():
                    st = extract_roi_statistics(img, t.polygon, inset)
                    t.drone_mean, t.drone_std, t.pixel_count, t.inset_applied = st.mean, st.std, st.count, st.inset_applied
        self._refresh_rows_stats()
        self._recompute_all_fits()
        self._update_display()

    # ------------------------------------------------------ calibration images
    def _load_calibration_images(self):
        filepaths = filedialog.askopenfilenames(title="Select calibration images", filetypes=[
            ("Image files", "*.tif *.tiff *.jpg *.jpeg *.png"), ("TIFF files", "*.tif *.tiff"), ("All files", "*.*")])
        if filepaths:
            self._add_calibration_images(list(filepaths))

    def _add_calibration_images(self, filepaths: List[str]):
        self._cancel_drawing(silent=True)
        rejected, added = [], 0
        existing = {os.path.abspath(e.image_path) for e in self.calibration_events}
        n_targets = max((len(e.targets) for e in self.calibration_events), default=2)
        for fp in filepaths:
            if os.path.abspath(fp) in existing:
                continue
            info = inspect_image(fp)
            if info.error or not info.is_single_band:
                rejected.append(f"{os.path.basename(fp)}: {info.describe()}")
                continue
            ts = extract_timestamp(fp)
            ev = CalibrationEvent(fp, ts.value or datetime.now(), ts.source, ts.tz)
            while len(ev.targets) < n_targets:      # targets are shared by all shots
                ev.add_target()
            self.calibration_events.append(ev)
            added += 1
        if rejected:
            messagebox.showwarning("Images skipped", "These files are not single-band temperature/DN images and were "
                                   "skipped. Colour R-JPEG previews must be converted first (see README):\n\n" + "\n".join(rejected))
        if not added:
            return
        self.calibration_events.sort(key=lambda e: e.timestamp)
        self._refresh_calib_listbox()
        self.calib_listbox.selection_clear(0, tk.END)
        self.calib_listbox.selection_set(0)
        self._load_image(0)
        self._update_timeline()
        self._update_step_summaries()
        mtime = [e for e in self.calibration_events if e.timestamp_source in ('file mtime', 'none')]
        if mtime:
            self.status_var.set(f"⚠ {len(mtime)} image(s) have no capture time in their metadata; the file date is used.")
        else:
            self.status_var.set(f"{len(self.calibration_events)} calibration image(s). Go to step 2 and draw the targets.")
        if self.active_step == 1 and added:
            self.show_step(2)

    def _remove_calibration_image(self):
        sel = self.calib_listbox.curselection()
        if not sel:
            return
        self._cancel_drawing(silent=True)
        del self.calibration_events[sel[0]]
        self._refresh_calib_listbox()
        if self.calibration_events:
            idx = min(sel[0], len(self.calibration_events) - 1)
            self.calib_listbox.selection_set(idx)
            self._load_image(idx)
        else:
            self.current_image = None
            self.view.set_image(None)
            self._rebuild_target_rows()
            self.current_image_label.config(text="No image selected")
            self.image_name_label.config(text="No image")
            self.image_info_label.config(text="")
            self.shot_label.config(text="no shot")
            self.fit_panel.clear("No calibration image selected")
        self._recompute_all_fits()
        self._update_timeline()

    def _refresh_calib_listbox(self):
        self.calib_listbox.delete(0, tk.END)
        n = len(self.calibration_events)
        for i, ev in enumerate(self.calibration_events):
            label = "Only" if n == 1 else ("Start" if i == 0 else ("End" if i == n - 1 else f"Mid{i}"))
            src = ev.timestamp_source if ev.timestamp_source not in ('none',) else '?'
            self.calib_listbox.insert(tk.END, f"[{label:5}] {ev.timestamp:%H:%M:%S} {src:9} {os.path.basename(ev.image_path)}")

    def _on_calib_select(self, event):
        sel = self.calib_listbox.curselection()
        if sel and sel[0] != self.current_event_index:
            self._cancel_drawing(silent=True)
            self._load_image(sel[0])

    def _load_image(self, index: int):
        self.current_event_index = index
        ev = self.calibration_events[index]
        try:
            self.current_image = self._cached_image(ev.image_path)
        except ValueError as e:
            messagebox.showerror("Cannot load image", str(e))
            return
        inset = self._roi_inset()
        for t in ev.targets:          # ROI statistics are recomputed on load (inset may have changed)
            if t.has_polygon():
                st = extract_roi_statistics(self.current_image, t.polygon, inset)
                t.drone_mean, t.drone_std, t.pixel_count, t.inset_applied = st.mean, st.std, st.count, st.inset_applied
        h, w = self.current_image.shape[:2]
        n = len(self.calibration_events)
        label = "only" if n == 1 else ("start" if index == 0 else ("end" if index == n - 1 else f"mid {index}"))
        self.current_image_label.config(text=f"{index + 1}/{n}  {os.path.basename(ev.image_path)}")
        self.image_name_label.config(text=os.path.basename(ev.image_path))
        self.image_info_label.config(text=f"{w} × {h} · float32 · {ev.timestamp:%Y-%m-%d %H:%M:%S}{ev.timestamp_tz} ({ev.timestamp_source})")
        self.shot_label.config(text=f"shot {index + 1} of {n} · {label}")
        self._rebuild_target_rows()
        self._recompute_fit(ev)
        self._update_display(keep_view=False)
        self.status_var.set(f"Shot {index + 1} selected. Draw polygons and enter reference values for this image.")

    # ------------------------------------------------------------- display
    def _cached_image(self, path: str) -> np.ndarray:
        """Loaded images are kept (a dozen at most) so switching between shots does not re-read the file."""
        img = self._image_cache.get(path)
        if img is None:
            img = load_thermal_image(path)
            if len(self._image_cache) >= 12:
                self._image_cache.pop(next(iter(self._image_cache)))
                self._display_cache = {k: v for k, v in self._display_cache.items() if k[0] in self._image_cache}
            self._image_cache[path] = img
        return img

    def _update_display(self, keep_view: bool = True):
        ev = self._current_event()
        if self.current_image is None or ev is None:
            return
        key = (ev.image_path, self.viz_mode.get())
        rgb = self._display_cache.get(key)
        if rgb is None:
            rgb = make_display_image(self.current_image, self.viz_mode.get())
            self._display_cache[key] = rgb
        self.view.set_image(rgb, keep_view=keep_view)

    def _on_view_change(self, scale: float):
        self.zoom_var.set(f"{scale * 100:.0f} %")

    def _draw_overlay(self):
        c = self.view.canvas
        ev = self._current_event()
        if ev is None or self.current_image is None:
            return
        targets = self._visible_targets(ev)
        inset = self._roi_inset()
        for i, t in enumerate(targets):
            if not t.has_polygon():
                continue
            pts = [self.view.to_canvas(x, y) for x, y in t.polygon]
            flat = [v for p in pts for v in p]
            closed = flat + flat[:2]
            c.create_line(closed, fill='black', width=5, tags='overlay')
            c.create_line(closed, fill=t.color, width=2, tags='overlay')
            inner = inner_roi_contour(t.polygon, self.current_image.shape, inset)
            if inner:
                ipts = [v for x, y in inner for v in self.view.to_canvas(x + 0.5, y + 0.5)]
                c.create_line(ipts + ipts[:2], fill='white', width=1, dash=(4, 3), tags='overlay')
            for x, y in pts:
                c.create_oval(x - 4, y - 4, x + 4, y + 4, fill=t.color, outline='white', width=1.5, tags='overlay')
            cx, cy = sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)
            label = f"T{i + 1}" + (f" {t.drone_mean:.2f}" if t.drone_mean is not None and np.isfinite(t.drone_mean) else "")
            tw = 7 * len(label) + 8
            c.create_rectangle(cx - tw / 2, cy - 10, cx + tw / 2, cy + 10, fill='white', outline=t.color, width=2, tags='overlay')
            c.create_text(cx, cy, text=label, fill=UI['text'], font=self.fonts['small_bold'], tags='overlay')
        if self.drawing_polygon and self.polygon_points:
            color = targets[self.active_target_index].color if self.active_target_index < len(targets) else 'white'
            pts = [self.view.to_canvas(x, y) for x, y in self.polygon_points]
            if len(pts) > 1:
                flat = [v for p in pts for v in p]
                c.create_line(flat, fill='black', width=4, tags='overlay')
                c.create_line(flat, fill=color, width=2, dash=(6, 3), tags='overlay')
            for x, y in pts:
                c.create_oval(x - 5, y - 5, x + 5, y + 5, fill=color, outline='white', width=2, tags='overlay')
        self._draw_colorbar()

    def _draw_colorbar(self):
        if self.current_image is None or self.viz_mode.get() == 'clahe':
            return
        c = self.view.canvas
        W, H = self.view.size()
        bar_h = max(H - 48, 40)
        ramp = np.linspace(1, 0, bar_h, dtype=np.float32).reshape(-1, 1)
        rgb = make_display_image(np.repeat(ramp, 14, axis=1), self.viz_mode.get())
        self._colorbar_photo = ImageTk.PhotoImage(Image.fromarray(rgb))
        x = W - 30
        c.create_image(x, 24, anchor=tk.NW, image=self._colorbar_photo, tags='overlay')
        c.create_rectangle(x, 24, x + 14, 24 + bar_h, outline='#ffffff', tags='overlay')
        finite = self.current_image[np.isfinite(self.current_image)]
        if finite.size:
            c.create_text(x - 4, 24, anchor='ne', text=f"{finite.max():.1f}", fill='white', font=self.fonts['mono_small'], tags='overlay')
            c.create_text(x - 4, 24 + bar_h, anchor='se', text=f"{finite.min():.1f}", fill='white', font=self.fonts['mono_small'], tags='overlay')

    def _on_image_motion(self, ix: Optional[int], iy: Optional[int], cx: Optional[int] = None, cy: Optional[int] = None):
        c = self.view.canvas
        c.delete('readout')
        if self.current_image is None or ix is None:
            return
        val = self.current_image[iy, ix]
        text = f"{val:.2f} {self._unit()} · {ix}, {iy}"
        if self.drawing_polygon:
            text += f" · T{self.active_target_index + 1}: {len(self.polygon_points)}/4"
        if cx is not None:
            W, _ = self.view.size()
            tx = cx + 14 if cx < W - 180 else cx - 14
            anchor = 'w' if cx < W - 180 else 'e'
            tid = c.create_text(tx, cy + 14, anchor=anchor, text=text, fill='white', font=self.fonts['mono_small'], tags='readout')
            x0, y0, x1, y1 = c.bbox(tid)
            c.create_rectangle(x0 - 4, y0 - 2, x1 + 4, y1 + 2, fill='#141815', outline='', tags='readout')
            c.tag_raise(tid)

    # ---------------------------------------------------------- polygons
    def _start_polygon_selection(self, target_index: int):
        ev = self._current_event()
        if ev is None or self.current_image is None:
            self.status_var.set("Load and select a calibration image first.")
            return
        self._cancel_drawing(silent=True)
        self.active_target_index = target_index
        self.drawing_polygon = True
        self.polygon_points = []
        self.view.loupe_enabled = True
        self.view.canvas.config(cursor='crosshair')
        self.status_var.set(f"Drawing T{target_index + 1}: click the 4 corners of the panel (Esc cancels). "
                            "The previous polygon is kept until the new one is complete.")
        self.view.redraw()

    def _cancel_drawing(self, silent: bool = False):
        if not self.drawing_polygon:
            return
        self.drawing_polygon = False
        self.polygon_points = []
        self.view.loupe_enabled = False
        self.view.canvas.config(cursor='')
        self.view.canvas.delete('loupe')
        self.view.redraw()
        if not silent:
            self.status_var.set("Drawing cancelled; the previous polygon is unchanged.")

    def _on_image_click(self, ix: int, iy: int):
        if not self.drawing_polygon or self.current_image is None:
            return
        self.polygon_points.append((ix, iy))
        if len(self.polygon_points) == 4:
            self._complete_polygon_drawing()
        else:
            self.view.redraw()

    def _complete_polygon_drawing(self):
        ev = self._current_event()
        targets = self._visible_targets(ev)
        target = targets[self.active_target_index]
        target.polygon = list(self.polygon_points)
        st = extract_roi_statistics(self.current_image, target.polygon, self._roi_inset())
        target.drone_mean, target.drone_std, target.pixel_count, target.inset_applied = st.mean, st.std, st.count, st.inset_applied
        self.drawing_polygon = False
        self.polygon_points = []
        self.view.loupe_enabled = False
        self.view.canvas.config(cursor='')
        self._refresh_rows_stats()
        self._recompute_fit(ev)
        self.view.redraw()
        warn = ""
        if st.count < MIN_ROI_PIXELS:
            warn = f" ⚠ only {st.count} pixels inside the inset; draw it larger or reduce the inset."
        elif not st.inset_applied:
            warn = " ⚠ polygon too small for the edge inset; the full polygon was used."
        self.status_var.set(f"T{self.active_target_index + 1}: mean {st.mean:.2f} {self._unit()} over {st.count} px. "
                            f"Now enter its reference value.{warn}")

    def _clear_polygon(self, target_index: int):
        ev = self._current_event()
        if ev is None:
            return
        self._cancel_drawing(silent=True)
        targets = self._visible_targets(ev)
        if target_index < len(targets):
            t = targets[target_index]
            t.polygon, t.drone_mean, t.drone_std, t.pixel_count = [], None, None, 0
            self._refresh_rows_stats()
            self._recompute_fit(ev)
            self.view.redraw()
            self.status_var.set(f"Cleared T{target_index + 1}.")

    def _copy_polygons(self):
        ev = self._current_event()
        if ev is None or len(self.calibration_events) < 2:
            self.status_var.set("Nothing to copy to: load a second calibration image first.")
            return
        inset = self._roi_inset()
        n = 0
        for other in self.calibration_events:
            if other is ev:
                continue
            try:
                img = self._cached_image(other.image_path)
            except ValueError:
                continue
            while len(other.targets) < len(ev.targets):
                other.add_target()
            for src, dst in zip(ev.targets, other.targets):
                if src.has_polygon() and not dst.has_polygon() and img.shape == self.current_image.shape:
                    dst.polygon = list(src.polygon)
                    st = extract_roi_statistics(img, dst.polygon, inset)
                    dst.drone_mean, dst.drone_std, dst.pixel_count, dst.inset_applied = st.mean, st.std, st.count, st.inset_applied
                    n += 1
        self._recompute_all_fits()
        self.status_var.set(f"Copied {n} polygon(s) to the other shots (only where none was drawn yet). "
                            "Check them: targets move between shots unless the camera is fixed.")

    # ------------------------------------------------------------ targets
    def _rebuild_target_rows(self):
        ev = self._current_event()
        targets = self._visible_targets(ev) if ev is not None else []
        unit = self._unit()
        if len(self.target_rows) != len(targets) or any(r.color != t.color for r, t in zip(self.target_rows, targets)):
            for row in self.target_rows:        # count or colours changed: build the table afresh
                row.destroy()
            self.target_rows.clear()
            for i, t in enumerate(targets):
                self.target_rows.append(TargetRowWidget(self.targets_container, i, t.color, self._start_polygon_selection,
                                                        self._clear_polygon, self._remove_target, self._on_gt_change, self.fonts))
        for row, t in zip(self.target_rows, targets):   # same table: just refresh the values (no flash)
            row.set_ground_truth(t.ground_truth)
            row.set_stats(t, unit)

    def _refresh_rows_stats(self):
        ev = self._current_event()
        if ev is None:
            return
        for row, t in zip(self.target_rows, self._visible_targets(ev)):
            row.set_stats(t, self._unit())

    def _add_target(self):
        """Targets are shared: a new target appears on every calibration shot (polygons are drawn per shot)."""
        ev = self._current_event()
        if ev is None:
            self.status_var.set("Load calibration images first.")
            return
        if len(ev.targets) >= len(COLOR_PALETTE):
            self.status_var.set(f"Maximum of {len(COLOR_PALETTE)} targets.")
            return
        n = len(ev.targets) + 1
        for e in self.calibration_events:
            while len(e.targets) < n:
                e.add_target()
        self._rebuild_target_rows()
        self._update_step_summaries()
        self.status_var.set(f"Added T{n} on all {len(self.calibration_events)} shot(s). Draw its polygon and enter the "
                            "reference value on each shot.")

    def _remove_target(self, index: int):
        """Removes the target from every calibration shot."""
        ev = self._current_event()
        if ev is None:
            return
        self._cancel_drawing(silent=True)
        if len(ev.targets) <= 2:
            self.status_var.set("At least 2 targets are needed.")
            return
        drawn = sum(1 for e in self.calibration_events if index < len(e.targets) and e.targets[index].has_polygon())
        if drawn and not messagebox.askyesno("Remove target",
                                             f"T{index + 1} has a polygon on {drawn} shot(s). Remove it from all shots?"):
            return
        for e in self.calibration_events:
            e.remove_target(index, min_targets=2)
        self._rebuild_target_rows()
        self._recompute_all_fits()
        self.view.redraw()

    def _on_gt_change(self, index: int):
        ev = self._current_event()
        if ev is None or index >= len(self.target_rows):
            return
        targets = self._visible_targets(ev)
        if index < len(targets):
            targets[index].ground_truth = self.target_rows[index].get_ground_truth()
            self._recompute_fit(ev)

    # ---------------------------------------------------------------- fits
    def _recompute_fit(self, ev: CalibrationEvent):
        warn = recompute_fit(ev, self._is_single_point(), self._is_thermal(), self._emissivity_settings())
        if ev is self._current_event():
            self._refresh_fit_panel(warn)
        self._update_timeline()
        self._update_step_summaries()

    def _recompute_all_fits(self):
        for ev in self.calibration_events:
            recompute_fit(ev, self._is_single_point(), self._is_thermal(), self._emissivity_settings())
        self._refresh_fit_panel(None)
        self._update_timeline()
        self._update_step_summaries()

    def _fit_warnings(self, ev: CalibrationEvent, extra: Optional[str]) -> List[str]:
        warnings = [extra] if extra else []
        fit = ev.fit
        if fit is None or fit.single_point:
            return warnings
        if fit.resid_std is None:
            warnings.append("2 targets: R² = 1 by construction and no fit uncertainty can be estimated. "
                            "A third target makes the calibration checkable.")
        # with 3 targets every leave-one-out fit is a 2-point line, so LOO >> RMSE is normal there
        if fit.n >= 4 and fit.rmse is not None and fit.loo_rmse and fit.loo_rmse > 3 * max(fit.rmse, 1e-9):
            warnings.append("Leave-one-out error is much larger than the RMSE: one target dominates the fit.")
        if fit.slope <= 0:
            warnings.append("Negative slope: a reference value is probably assigned to the wrong target.")
        if self.current_image is not None:
            finite = self.current_image[np.isfinite(self.current_image)]
            if finite.size:
                lo, hi = float(np.percentile(finite, 2)), float(np.percentile(finite, 98))
                if lo < fit.x_min - 0.5 or hi > fit.x_max + 0.5:
                    warnings.append(f"Targets span {fit.x_min:.1f} to {fit.x_max:.1f} {self._unit()}; this scene reaches "
                                    f"{lo:.1f} to {hi:.1f}. Pixels outside the span are extrapolated.")
        return warnings

    def _refresh_fit_panel(self, warn: Optional[str] = None):
        ev = self._current_event()
        if ev is None:
            self.fit_panel.clear("No calibration image selected")
            return
        n = len(self.calibration_events)
        idx = self.current_event_index
        label = "only" if n == 1 else ("start" if idx == 0 else ("end" if idx == n - 1 else f"mid {idx}"))
        self.fit_panel.update(ev, self._visible_targets(ev), self._unit(), self._fit_warnings(ev, warn),
                              f"shot {idx + 1}/{n} · {label} · {sum(1 for t in self._visible_targets(ev) if t.is_complete())} targets")

    def _show_regression_plot(self):
        ev = self._current_event()
        if ev is None or ev.fit is None or ev.fit.single_point:
            self.status_var.set("Need a fit with at least 2 complete targets to plot.")
            return
        RegressionPlotDialog(self.root, ev, ev.targets, self._unit(), f"Fit for {Path(ev.image_path).name}")

    def _export_fit_report(self):
        ev = self._current_event()
        if ev is None or ev.fit is None:
            self.status_var.set("Nothing to report yet.")
            return
        default = Path(ev.image_path).stem + "_fit_report.html"
        path = filedialog.asksaveasfilename(title="Export fit report", defaultextension=".html", initialfile=default,
                                            filetypes=[("HTML report", "*.html")])
        if not path:
            return
        self._write_fit_report(path, ev)
        self.status_var.set(f"Fit report written: {path}")

    def _write_fit_report(self, path: str, ev: CalibrationEvent) -> str:
        em = self._emissivity_settings()
        settings = {"project": self.project_var.get(), "mode": "thermal" if self._is_thermal() else "multispectral",
                    "ROI edge inset (px)": self._roi_inset(), "emissivity correction": "on" if em.enabled else "off"}
        if em.enabled:
            settings.update({"panel ε": em.target_emissivity, "scene ε": em.scene_emissivity, "T_sky (°C)": em.sky_temperature})
        html = fit_report_html(ev, self._unit(), self.calibration_events, settings, render_fit_png(ev, self._unit()))
        with open(path, 'w', encoding='utf-8') as f:
            f.write(html)
        return html

    # ------------------------------------------------------------ timeline
    def _update_timeline(self):
        fitted = sorted([e for e in self.calibration_events if e.fit is not None], key=lambda e: e.timestamp)
        survey = []
        before = after = 0
        for p, ts in self._survey_timestamps.items():
            if ts.value is None:
                continue
            outside = bool(fitted) and len(fitted) > 1 and (ts.value < fitted[0].timestamp or ts.value > fitted[-1].timestamp)
            before += int(bool(fitted) and ts.value < fitted[0].timestamp)
            after += int(bool(fitted) and ts.value > fitted[-1].timestamp)
            survey.append((ts.value, outside))
        summary = ""
        if survey:
            t0, t1 = min(t for t, _ in survey), max(t for t, _ in survey)
            summary = f"{len(survey)} images · {t0:%H:%M} to {t1:%H:%M}"
            if len(fitted) > 1 and (before or after):
                summary += f" · {before} before / {after} after the calibration window"
        self.timeline.set_data(self.calibration_events, survey, summary)

    def _on_survey_changed(self):
        """Scan the survey folder's capture times in a background thread, then run the pre-flight check and
        list the images in the results table (for the timeline and so the user sees what Run will do)."""
        folder = self.survey_entry.get().strip()
        files = collect_survey_files(folder, None, self.filter_var.get().strip(),
                                     [e.image_path for e in self.calibration_events])
        self._survey_scan_id += 1
        scan_id = self._survey_scan_id
        if not files:
            self._survey_timestamps = {}
            self._update_timeline()
            self._update_step_summaries()
            self.process_view.set_preflight(None)
            self.process_view.clear_rows()
            if folder and not os.path.isdir(folder):
                self.status_var.set("The survey input must be a folder.")
            return
        known = {p: self._survey_timestamps[p] for p in files if p in self._survey_timestamps}
        todo = [p for p in files if p not in known]
        if not todo:
            self._survey_timestamps = known
            self._after_survey_scan()
            return

        def worker():
            result = dict(known)
            for p in todo:
                if scan_id != self._survey_scan_id:
                    return
                result[p] = extract_timestamp(str(p))
            self._q.put(('survey_scan', scan_id, result))
        threading.Thread(target=worker, daemon=True).start()
        self.status_var.set(f"Reading capture times of {len(todo)} survey image(s)…")

    def _after_survey_scan(self):
        """Capture times are known: update the timeline, run the pre-flight check and list the images."""
        self._update_timeline()
        if self._batch_thread and self._batch_thread.is_alive():
            return
        rep = self._run_preflight()
        n = len(rep.files) if rep else 0
        self.status_var.set(f"{len(self._survey_timestamps)} survey image(s) found; {n} ready to process."
                            + (" Fix the blocking items first." if rep and rep.blocking else ""))

    # ------------------------------------------------------------ project
    def _on_title_edited(self, commit: bool):
        title = self.project_var.get().strip()
        if not title:
            self.project_var.set("untitled project")
        if commit:
            self.root.focus_set()

    def _project_dict(self) -> dict:
        survey = {"folder": self.survey_entry.get(), "filter": self.filter_var.get(),
                  "output_folder": self.output_entry.get(), "copy_metadata": self.copy_meta_var.get(),
                  "skip_existing": self.skip_existing_var.get()}
        return events_to_project(self.calibration_events, self.sensor_mode.get(), self.msi_single_point.get(),
                                 self._emissivity_settings(), self._roi_inset(), survey, self.project_var.get().strip())

    def _new_project(self):
        if self.calibration_events and not messagebox.askyesno("New project", "Discard the current targets and fits?"):
            return
        self._cancel_drawing(silent=True)
        self.calibration_events = []
        self.current_image = None
        self._image_cache, self._display_cache = {}, {}
        self.project_path = None
        self._last_output = None
        self._survey_timestamps = {}
        self.view.set_image(None)
        self._refresh_calib_listbox()
        self._rebuild_target_rows()
        self.fit_panel.clear("No calibration image selected")
        self.current_image_label.config(text="No image selected")
        self.image_name_label.config(text="No image")
        self.image_info_label.config(text="")
        self.shot_label.config(text="no shot")
        self.project_var.set("untitled project")
        self.saved_var.set("")
        self.survey_entry.delete(0, tk.END)
        self.process_view.clear_rows()
        self.process_view.set_preflight(None)
        self._update_timeline()
        self._update_step_summaries()
        self.show_step(1)

    def _save_project(self, save_as: bool = False):
        path = self.project_path
        if save_as or not path:
            title = self.project_var.get().strip() or "untitled project"
            safe = re.sub(r'[^\w.-]+', '_', title).strip('_') or "project"
            path = filedialog.asksaveasfilename(title="Save project", defaultextension=PROJECT_EXT, initialfile=safe + PROJECT_EXT,
                                                filetypes=[("Calibration project", f"*{PROJECT_EXT}"), ("JSON", "*.json")])
            if not path:
                return
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(self._project_dict(), f, indent=2)
        self.project_path = path
        self.saved_var.set(f"saved {datetime.now():%H:%M} · {os.path.basename(path)}")
        self.status_var.set(f"Project saved: {path}")

    def _open_project(self, path: Optional[str] = None):
        if not path:
            path = filedialog.askopenfilename(title="Open project", filetypes=[("Calibration project", f"*{PROJECT_EXT}"),
                                                                                ("JSON", "*.json"), ("All files", "*.*")])
        if not path:
            return
        try:
            with open(path, encoding='utf-8') as f:
                project = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            messagebox.showerror("Cannot open project", str(e))
            return
        self._cancel_drawing(silent=True)
        self.sensor_mode.set(project.get("mode", MODE_THERMAL))
        self.msi_single_point.set(bool(project.get("msi_single_point", True)))
        self.roi_inset_var.set(int(project.get("roi_inset_px", DEFAULT_ROI_INSET_PX)))
        em = EmissivitySettings.from_dict(project.get("emissivity", {}))
        self.emis_enabled.set(em.enabled)
        self.emis_target_var.set(f"{em.target_emissivity:g}")
        self.emis_scene_var.set(f"{em.scene_emissivity:g}")
        self.tsky_var.set(f"{em.sky_temperature:g}")
        sv = project.get("survey", {})
        folder = sv.get("folder") or (os.path.dirname(sv["files"][0]) if sv.get("files") else "")
        self.survey_entry.delete(0, tk.END)
        self.survey_entry.insert(0, folder)
        self.filter_var.set(sv.get("filter", ""))
        self.output_entry.delete(0, tk.END)
        self.output_entry.insert(0, sv.get("output_folder", ""))
        self.copy_meta_var.set(bool(sv.get("copy_metadata", True)))
        self.skip_existing_var.set(bool(sv.get("skip_existing", False)))
        events, problems = events_from_project(project, os.path.dirname(os.path.abspath(path)), self._roi_inset())
        n_targets = max((len(e.targets) for e in events), default=2)
        for e in events:                       # targets are shared by all shots
            while len(e.targets) < n_targets:
                e.add_target()
        self.calibration_events = events
        self.project_path = path
        self._last_output = None
        self._survey_timestamps = {}
        self._on_mode_switch()
        self._refresh_calib_listbox()
        if events:
            self.calib_listbox.selection_set(0)
            self._load_image(0)
        self._recompute_all_fits()
        self.project_var.set(project.get("title") or os.path.basename(path).replace(PROJECT_EXT, "").replace(".json", ""))
        self.saved_var.set(f"opened {datetime.now():%H:%M} · {os.path.basename(path)}")
        self._on_survey_changed()
        if problems:
            messagebox.showwarning("Project opened with problems", "\n".join(problems))
        self.status_var.set(f"Project opened: {len(events)} calibration image(s).")
        self.show_step(2 if events else 1)

    def _show_help(self):
        messagebox.showinfo("How to use",
                            "1 · Images: add the calibration shot(s) that show your reference panels (single-band temperature TIFFs).\n"
                            "2 · Targets: zoom in (wheel), press Draw for a target and click its 4 corners. Type the reference reading.\n"
                            "3 · Fit: watch the panel on the right. With 3 or more targets you get an uncertainty estimate.\n"
                            "4 · Process: choose the survey images and the output folder, press Check, then Run.\n\n"
                            "Save the project (Ctrl+S) to keep polygons and readings; it can be re-run from the command "
                            "line with --project FILE --run.")

    # --------------------------------------------------------------- batch
    def _browse_survey_folder(self):
        folder = filedialog.askdirectory(title="Select the survey folder (every image in it is processed)")
        if folder:
            self.survey_entry.delete(0, tk.END)
            self.survey_entry.insert(0, folder)
            if not self.output_entry.get():
                self.output_entry.insert(0, os.path.join(folder, "calibrated"))
            self._on_survey_changed()

    def _browse_output_folder(self):
        folder = filedialog.askdirectory(title="Select output folder")
        if folder:
            self.output_entry.delete(0, tk.END)
            self.output_entry.insert(0, folder)

    def _open_output_folder(self):
        if self._last_output:
            open_in_file_manager(self._last_output)

    def _build_job(self) -> BatchJob:
        folder = self.survey_entry.get().strip()
        files = collect_survey_files(folder, None, self.filter_var.get().strip(),
                                     [e.image_path for e in self.calibration_events])
        out = self.output_entry.get().strip()
        if not out:
            out = str(Path(folder or ".") / "calibrated")
            self.output_entry.insert(0, out)
        return BatchJob(self.sensor_mode.get(), self._is_single_point(), list(self.calibration_events), files, Path(out),
                        self._emissivity_settings(), self._roi_inset(), self.copy_meta_var.get(),
                        self.skip_existing_var.get(), self.filter_var.get().strip(), self._project_dict())

    def _run_preflight(self) -> Optional[PreflightReport]:
        job = self._build_job()
        self.status_var.set("Checking survey images…")
        self.root.update_idletasks()
        rep = preflight(job, self._survey_timestamps)
        self._last_preflight = rep
        self._survey_timestamps.update(rep.timestamps)
        self.process_view.set_preflight(rep)
        if not (self._batch_thread and self._batch_thread.is_alive()):
            self.process_view.set_pending(rep.files, rep.timestamps)
        self._update_timeline()
        self._update_step_summaries()
        self.status_var.set("Pre-flight check: " + ("problems found, see the list." if rep.blocking else
                                                   f"{len(rep.files)} image(s) ready."))
        return rep

    def _start_batch_processing(self):
        if self._batch_thread and self._batch_thread.is_alive():
            return
        self._cancel_drawing(silent=True)
        rep = self._run_preflight()
        if rep is None or rep.blocking:
            messagebox.showerror("Cannot start", rep.text() if rep else "No job.")
            return
        warns = [t for l, t in rep.lines if l == 'warn']
        if warns and not messagebox.askokcancel("Pre-flight warnings", "\n\n".join(warns) + "\n\nStart anyway?"):
            return
        job = self._build_job()
        job.survey_files = rep.files
        pv = self.process_view
        pv.set_pending(rep.files, rep.timestamps)
        pv.progress_var.set(0)
        self._batch_cancel.clear()
        pv.run_btn.state(['disabled'])
        pv.cancel_btn.state(['!disabled'])
        pv.check_btn.state(['disabled'])
        pv.open_btn.state(['disabled'])
        q = self._q
        unit_key = self._unit_key()
        t_start = datetime.now()

        def worker():
            try:
                result = run_batch(job, lambda m: q.put(('log', m)),
                                   lambda i, n, name: q.put(('progress', i, n, name, t_start)),
                                   self._batch_cancel.is_set, rep.timestamps, lambda row: q.put(('row', row, unit_key)))
                q.put(('done', result, str(job.output_folder)))
            except Exception:
                q.put(('error', traceback.format_exc()))

        self._batch_thread = threading.Thread(target=worker, daemon=True)
        self._batch_thread.start()

    def _cancel_batch(self):
        self._batch_cancel.set()
        self.process_view.progress_label.config(text="Cancelling…")

    def _poll_queue(self):
        pv = self.process_view
        try:
            while True:
                msg = self._q.get_nowait()
                kind = msg[0]
                if kind == 'log':
                    pv.log_lines.append(msg[1])
                elif kind == 'progress':
                    i, n, name, t0 = msg[1], msg[2], msg[3], msg[4]
                    pv.progress_var.set(100.0 * i / max(n, 1))
                    eta = ""
                    if i > 0 and i < n:
                        per = (datetime.now() - t0).total_seconds() / i
                        eta = f" · about {int(per * (n - i)) + 1} s left"
                    pv.progress_label.config(text=(f"Processing {i} of {n} · {name}" if name else f"{i} of {n}") + eta)
                elif kind == 'row':
                    pv.add_row(msg[1], msg[2])
                elif kind == 'done':
                    r, out = msg[1], msg[2]
                    self._last_output = out
                    pv.progress_label.config(text=f"Done: {r['ok']} ok, {r['errors']} errors, {r['skipped']} skipped")
                    pv.progress_var.set(100)
                    pv.open_btn.state(['!disabled'])
                    self.status_var.set(f"Batch complete: {r['ok']} image(s), the log and the project file written to {out}")
                    self._finish_batch()
                elif kind == 'error':
                    pv.log_lines.append("FAILED:\n" + msg[1])
                    pv.progress_label.config(text="Failed: " + msg[1].strip().splitlines()[-1][:80])
                    self.status_var.set("Batch processing failed.")
                    self._finish_batch()
                elif kind == 'survey_scan':
                    if msg[1] == self._survey_scan_id:
                        self._survey_timestamps = msg[2]
                        self._after_survey_scan()
        except queue.Empty:
            pass
        self.root.after(150, self._poll_queue)

    def _finish_batch(self):
        pv = self.process_view
        pv.run_btn.state(['!disabled'])
        pv.cancel_btn.state(['disabled'])
        pv.check_btn.state(['!disabled'])
        self._update_step_summaries()

    # compatibility names used by tests and the screenshot driver
    @property
    def start_btn(self):
        return self.process_view.run_btn

    @property
    def progress_label(self):
        return self.process_view.progress_label

    @property
    def log_text(self):
        return self.process_view.log_lines

    def _on_close(self):
        if self._batch_thread and self._batch_thread.is_alive():
            if not messagebox.askyesno("Quit", "Batch processing is running. Quit anyway?"):
                return
            self._batch_cancel.set()
        self.root.destroy()

# =============================================================================
# MAIN ENTRY POINT
# =============================================================================

def main(argv: Optional[List[str]] = None):
    ap = argparse.ArgumentParser(description=f"{TOOL_NAME} {__version__}")
    ap.add_argument("--project", help=f"project file (*{PROJECT_EXT}) to open")
    ap.add_argument("--run", action="store_true", help="process the project's survey images without the GUI")
    ap.add_argument("--survey", help="survey folder (overrides the project's)")
    ap.add_argument("--output", help="output folder (overrides the project's)")
    ap.add_argument("--filter", help="filename filter (overrides the project's)")
    args = ap.parse_args(argv)
    if args.run:
        if not args.project:
            ap.error("--run needs --project")
        run_project_headless(args.project, args.survey, args.output, args.filter)
        return
    if platform.system() == "Windows":
        try:  # crisp text on HiDPI screens
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = tk.Tk()
    app = ThermalCalibrationApp(root)
    if args.project:
        root.after(200, lambda: app._open_project(args.project))
    root.mainloop()


if __name__ == "__main__":
    main()
