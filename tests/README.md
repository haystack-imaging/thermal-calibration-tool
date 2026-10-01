# Tests

```bash
python -m pytest tests -q
```

The tool is meant to produce numbers that end up in papers, so the suite is written to the standard of a
scientific instrument: every reported statistic is checked against an independent closed-form computation,
the calibration chain is checked for exact recovery of known sensor models, results are pinned by golden
data, and the output files are treated as an interface.

## What the suite guarantees

| File | Guarantees |
|------|------------|
| `test_science.py` | Regression slope, intercept, R², residuals, RMSE, residual standard error, SE(m), SE(b), leave-one-out RMSE and the prediction uncertainty equal the textbook formulas (15 random data sets). Two targets give an exact line and no diagnostics. A noise-free linear sensor `a·T + c` is recovered to float32 precision and the scene restored pixel for pixel (10 random models). NaN pixels stay NaN and never poison statistics. ROI means and pixel counts on constant patches are exact and follow the erosion geometry (10 random rectangles); the edge inset reduces blur bias monotonically. Interpolation is exact at every event, linear in between, clamped outside, independent of list order, and ignores unfitted events. The emissivity correction inverts the forward grey-body model (12 random ε, T_sky). Two runs on the same data give byte-identical outputs and logs. The golden flight reproduces the recorded coefficients and per-image results. |
| `test_io.py` | Timestamp parsing matrix (13 formats, zones, garbage); source priority XMP > EXIF > TIFF tag > mtime and the `reliable` flag. Image inspection and refusal of multi-band and unreadable files. Output GeoTIFF profile (float32, LZW, CRS and transform copied, tags merged, software tag set); EXIF/XMP/GPS survive the exiftool copy and the copy never overwrites the output's own metadata or georeferencing. Survey collection rules (extensions, filter, calibration images and earlier outputs excluded, unicode paths). Pre-flight blocking and warning conditions, mixed time zones. **The processing log's column list is pinned** (change it on purpose only). MSI single-point batch. Cancel stops after the current image; the row callback sees every row. Project files round-trip every setting and resolve moved images. The headless run equals the GUI path; the real CLI is exercised in a subprocess, including its failure modes. The fit report contains the numbers and the embedded plot. |
| `test_gui.py` | The four-step rail and centre views, step status badges and summaries, the fit panel (tiles, residual bars, warnings), fit report export, the timeline's background scan of survey capture times with the automatic pre-flight check and the pending rows, a batch run from the GUI filling the results table in place, targets shared across shots, the editable project title and the single menu, project save/open through the GUI, mode switching, the drawing flow with cursor readout and overlays, image removal and the empty state. |
| `test_core.py` | One test per defect fixed in 0.2.0 (see `CHANGELOG.md`), kept as regression guards. |

GUI tests use a hidden Tk root and skip without a display; `xvfb-run` provides one in CI. Tests that need
exiftool skip when it is absent.

## Golden data

`test_science.py::test_golden_flight_matches_recorded_results` rebuilds a deterministic flight (seeded, no
external tools), runs the full pipeline and compares with `golden/expected.json`. If a change of the
method is intended, regenerate the file and commit it with the change:

```bash
python tests/make_golden.py        # prints old → new coefficients and survey errors
```

## Synthetic flight (no download)

```bash
python tests/make_synthetic_flight.py        # → tests/data/synthetic_flight/
```

A 640×512 M4T-like flight: three calibration shots (start, mid, end) with three panels, 24 survey images of
crop rows, a drifting sensor model (`T_drone = a(t)·T_true + c(t)`), blur, fixed-pattern noise and
vignetting. EXIF/XMP capture times and GPS are written with exiftool. `ground_truth.json` holds the sensor
model, panel temperatures, simulated IR-gun readings and the expected coefficients; survey images come with
`*_TRUE.npy` arrays. Edge cases: a colour "R-JPEG preview", an 8-bit TIFF, a TIFF without capture time and
one with a UTC (`Z`) timestamp. `test_core.py` uses it when present.

## Real DJI M4T images (download, about 12 MB)

The [thermal_parser](https://github.com/SanNianYiSi/thermal_parser) repository ships sample R-JPEGs of
several DJI cameras and the DJI Thermal SDK libraries. Place these under `tests/vendor/thermal_parser/`
(same layout as the repository):

```
tests/vendor/thermal_parser/images/M4T/DJI_000x_R.JPG
tests/vendor/thermal_parser/plugins/dji_thermal_sdk_v1.7_20241205/linux/release_x64/*.so
```

Then convert them to temperature TIFFs:

```bash
python tests/convert_rjpeg.py tests/vendor/thermal_parser/images/M4T tests/data/real_m4t
```

`dji_sdk.py` is a minimal ctypes wrapper around `libdirp.so` (SDK 1.7). It declares the five-field
parameter struct (`distance, humidity, emissivity, reflection, ambient_temp`); the four-field struct in
thermal_parser lets the SDK write past the buffer and crash the interpreter. The SDK loads its
sub-libraries by bare name, so the wrapper re-executes Python with the SDK directory on `LD_LIBRARY_PATH`.

## Screenshots

```bash
python tests/screenshot_gui.py               # → GUI_screenshots/
```

Opens the real window on the current display, loads the synthetic flight, draws the panels through the click
handler (the first one through real Tk mouse events), zooms with wheel events, scans the survey images for
the timeline, runs the pre-flight check and the batch, and captures each state with ImageMagick's `import`.
Needs Linux/X11 or XWayland.

## Continuous integration

`.github/workflows/tests.yml` runs the science and I/O tests on Ubuntu (Python 3.10 and 3.12, with exiftool)
and the GUI tests under `xvfb-run`, plus the whole suite on Windows (exiftool tests skipped there).

`tests/data/` and `tests/vendor/` are ignored by git.
