# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the project uses semantic versioning.

## [0.2.0] - 2026-10-12

A review of the March release found several defects that produced wrong numbers silently. All of them
are fixed and each has a regression test (`tests/test_core.py`). Anyone who processed data with 0.1.0
should re-run it; the cases below say when the old output was affected.

### Fixed (results)
- Calibration images between the first and the last were listed but never used; coefficients are now
  interpolated piecewise-linearly through every fitted calibration image. *(affected: flights with three or
  more calibration shots)*
- The optional emissivity correction was applied to survey pixels but not to the target readings the fit
  was based on, shifting all outputs by about +2 to +3 °C. The correction is now applied consistently
  (panel emissivity before the fit, scene emissivity on survey pixels) and is off by default. *(affected:
  runs with emissivity correction enabled)*
- Colour JPEG previews (DJI `*_T.JPG`) were accepted and their palette values treated as temperatures. Multi-
  band images are now refused; 8-bit images produce a warning. *(affected: batches that contained the
  previews next to the temperature TIFFs)*
- A reference value of `0.0` or one typed with a comma (`15,0`) was silently ignored. *(affected: ice-bath
  references, comma-decimal locales)*
- Switching the multispectral method recomputed only the current calibration image and deleted extra
  targets without asking.
- The "inner 80 %" region of interest shrank the polygon by well under a pixel on small targets, so the
  blurred edge entered the mean (about 1 to 3 °C bias at typical target sizes). The ROI is now eroded by a
  fixed number of pixels (default 3) and the region used is drawn on the image.
- The processing summary was written in the locale encoding and crashed on Windows when the emissivity
  correction was enabled.
- Starting a polygon on one image and finishing it on another produced a polygon from both.
- Output GeoTIFFs lost all EXIF/XMP/GPS metadata; with exiftool on the PATH the full block is now copied,
  and GDAL tags are always carried over.
- Switching between calibration shots could take seconds and left the target table blank meanwhile: the
  table and the fit panel rebuilt their widgets on every switch, which Tk lays out slowly. Widgets are now
  reused, residual bars are drawn on one canvas and loaded images are cached (about 0.1 s per switch).
- From the pre-release review: a target whose polygon fell outside the image (or over NaN pixels) counted
  as complete and produced an all-NaN fit and all-NaN outputs marked "ok"; such targets are now incomplete
  and non-finite fits are blocked. A source nodata value was calibrated as a temperature; it is now NaN, and
  outputs declare NaN as nodata. The per-image prediction uncertainty and the extrapolation percentage used
  a calibration shot that was not applied before the first / after the last shot. An image without any
  capture time was treated as taken "now"; with several shots it is now an error. Date-only timestamps and
  two-digit UTC offsets were mis-parsed. Four corners clicked in Z order made a bow-tie polygon; corners
  are now ordered. Non-georeferenced inputs got an invented geotransform. Cancelling a batch reported
  success. Emissivity correction now works in apparent-temperature space (fit, apply, then convert).

### Added
- Fit diagnostics: residuals, RMSE, standard errors of m and b, leave-one-out error, ±1σ prediction band,
  extrapolation warning; HTML fit report export.
- Per-image prediction uncertainty of the fit (`u_fit_1sigma`) and the image's position relative to the
  calibration shots in the processing log.
- Capture-time source (XMP/EXIF/TIFF tag/file date) recorded per image and checked before processing.
- Pre-flight check listing file types, time sources, images outside the calibration window and overwrites;
  it runs by itself when the survey folder is chosen, and every image is listed before processing.
- Zoom, pan, loupe, cursor readout and colour bar on the image view.
- Four-step layout (Images, Targets, Fit, Process) with a flight timeline, a live fit panel and an editable
  project title. Targets are shared by all calibration shots.
- Step 2 guidance card: states the next action (which target to draw on which shot, which reading to type,
  when to switch shots) and highlights the row concerned; the reading field gets the focus after the fourth
  corner and Enter moves to the next target. Targets can be named (double-click the ID); the name appears on
  the image and in the processing summary.
- The sensor type (thermal / multispectral) is chosen in step 1 and locked while images are loaded, so a
  project cannot change mode halfway through. Redrawing a target hides its old outline at once (Esc restores
  it); an empty image view says what to do first; the window title shows the project name.
- Step 1 has its own guidance card and no longer jumps to step 2 after the first image: it asks for every
  calibration shot of the flight and offers an "Open 2 · Targets" button. A calibration image chosen a second
  time is reported instead of silently ignored. The "all done" guidance asks whether the images show more
  panels (+ Add target) and offers "Open 4 · Process". × removes a target down to the last one (the last row's
  × is greyed out) instead of silently refusing at two; Draw, Clear and × have tooltips.
- Step 4 has a guidance card too (calibration incomplete → survey folder → ready, with a Run button → done,
  with an Open-folder button), and the empty results table says what to do instead of staying blank.
- Double-click launchers `Launch.bat` (Windows), `Launch.command` (macOS) and `Launch.sh` (Linux), all running
  `launch.py`: they create a private `.venv`, install or update the packages when `requirements.txt` changes,
  pull new commits on a clean checkout and start the tool.
- Batch processing in a worker thread with Cancel and a live results table.
- Project files (`*.calib.json`) and a headless command line (`--project FILE --run`); every run saves the
  project next to its outputs.
- Test suite (159 tests), golden regression data, continuous integration, GPL-3.0 license, citation file.

### Changed
- Minimum supported Python is 3.10; `piexif` is optional.
- The processing log gained columns (`timestamp_source`, `timestamp_tz`, `position_vs_calibration`,
  `pct_pixels_outside_target_range`, `u_fit_1sigma`). The column list is pinned by a test.
- Survey images are given as a folder (plus an optional filename filter), no longer as a file list.

## [0.1.0] - 2026-03-06

First public release: thermal and single-band multispectral calibration with up to two calibration
images, Tk GUI, CSV processing log.
