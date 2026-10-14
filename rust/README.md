# Rust / Tauri rewrite (work in progress)

A port of the Thermal Calibration Tool to Rust, with a Tauri desktop shell. The Python tool in the
repository root stays the reference release until this one is complete.

```
rust/
├── crates/thermal-core/   the engine: timestamps, raster I/O, ROI, fits, interpolation, batch, project files
├── crates/thermal-cli/    `thermal-calibration --project FILE` – headless runs, same outputs as the Python CLI
├── app/                   the desktop app: Vite + TypeScript front-end, Tauri shell in app/src-tauri
└── tools/make_golden_flight.py   builds the reference flight with the Python tool for the engine test
```

## Status

| Part | State |
|------|-------|
| Engine (`thermal-core`) | Complete port of the science core. Pure Rust: no GDAL, no OpenCV. Reads single-band TIFF (GeoTIFF, LZW/deflate), PNG, JPEG; writes float32 LZW GeoTIFFs with georeferencing, TIFF date, XMP and GDAL metadata copied; exiftool used when on the PATH, as before. |
| Validation | 26 Rust tests, among them the golden flight: the Python tool's `tests/golden/expected.json` reproduced to 2·10⁻⁷ (slope), 5·10⁻⁶ (intercept), 4·10⁻⁶ °C (output pixels); identical CSV columns, georeferencing and nodata. |
| CLI (`thermal-calibration`) | Done. Same arguments as `python thermal_calibration_tool.py --project … --run`. |
| Desktop app | Front-end written and tested in a browser against an in-browser mock engine (steps 1–3, drawing, readings, fit panel with the other shots dashed, timeline, settings, pre-flight, run with live rows). The Tauri shell (`app/src-tauri`) is written but **not yet compiled**: it needs WebKitGTK on this machine (see below). |
| Not ported yet | HTML fit report with plot, the matplotlib "Detailed plot" window, a real CLAHE display (a percentile stretch stands in), the Windows/macOS launchers (Tauri builds installers instead). |

## Building

The engine and the CLI need only Rust (https://rustup.rs):

```bash
cd rust
cargo build --release          # target/release/thermal-calibration
cargo test                     # unit tests; the golden test is skipped until the flight exists
python3 tools/make_golden_flight.py   # once, from the repo root's Python environment
cargo test                     # now including the comparison with the Python reference
```

The desktop app needs Node 18+ and, on Linux, the WebKitGTK development packages:

```bash
# Fedora
sudo dnf install webkit2gtk4.1-devel openssl-devel librsvg2-devel libappindicator-gtk3-devel gtk3-devel file curl wget
# Debian / Ubuntu
sudo apt install libwebkit2gtk-4.1-dev build-essential curl wget file libxdo-dev libssl-dev libayatana-appindicator3-dev librsvg2-dev
```

Then:

```bash
cd rust/app
npm install
npm run dev          # front-end alone in a browser, with the mock engine (no Rust needed)
npm run tauri dev    # the real app
npm run tauri build  # installers in app/src-tauri/target/release/bundle
```

## Design notes

- The front-end receives each calibration image once as float32 pixels and does zoom, pan, colour
  mapping, the loupe and the readout itself; the Rust side measures ROIs, fits and runs the batch.
- Commands and their JSON shapes are in `app/src-tauri/src/lib.rs` (Rust) and `app/src/types.ts`
  (TypeScript). `app/src/mock.ts` imitates them for browser-only development and is never loaded in
  the desktop app.
- The polygon fill includes boundary pixels, as OpenCV's `fillPoly` did in the Python tool, so the ROI
  statistics match for the usual axis-aligned targets; the inset erodes with a square element.
- Timestamps follow the Python rules exactly (camera wall time, zone recorded but not applied).
