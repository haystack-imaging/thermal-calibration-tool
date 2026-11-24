"""Input/output contract: timestamps, image inspection, metadata survival, file naming, pre-flight,
the processing log schema, project files, the headless CLI and the fit report."""
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest
import rasterio

import thermal_calibration_tool as tct
from helpers import T0, make_event, poly, read_arr, run_job, scene, write_tif

HERE = Path(__file__).resolve().parent
TOOL = HERE.parent / "thermal_calibration_tool.py"
HAS_EXIFTOOL = shutil.which("exiftool") is not None

# ----------------------------------------------------------------------------- timestamps
PARSE_CASES = [
    ("2026:06:01 10:00:00", T0, ""),
    ("2026-06-01 10:00:00", T0, ""),
    ("2026-06-01T10:00:00", T0, ""),
    ("2026-06-01T10:00:00.250", T0 + dt.timedelta(milliseconds=250), ""),
    ("2026-06-01T10:00:00+02:00", T0, "+02:00"),
    ("2026-06-01T10:00:00+0200", T0, "+0200"),
    ("2026-06-01T10:00:00-05:30", T0, "-05:30"),
    ("2026-06-01T08:00:00Z", T0 - dt.timedelta(hours=2), "Z"),
    ("2026:06:01 10:00:00.5", T0 + dt.timedelta(milliseconds=500), ""),
    ("  2026:06:01 10:00:00  ", T0, ""),
    ("", None, ""),
    ("not a date", None, ""),
    ("0000:00:00 00:00:00", None, ""),
]


@pytest.mark.parametrize("text,value,tz", PARSE_CASES)
def test_parse_datetime_matrix(text, value, tz):
    assert tct._parse_datetime_string(text) == (value, tz)


def test_timestamp_sources_and_priority(tmp_path):
    no_meta = write_tif(tmp_path / "none.tif", scene())
    os.utime(no_meta, (1_000_000_000, 1_000_000_000))
    ts = tct.extract_timestamp(no_meta)
    assert ts.source == "file mtime" and ts.value == dt.datetime.fromtimestamp(1_000_000_000) and not ts.reliable
    tagged = write_tif(tmp_path / "tag.tif", scene(), T0)
    ts = tct.extract_timestamp(tagged)
    assert (ts.value, ts.source, ts.tz) == (T0, "TIFF tag", "") and ts.reliable
    assert tct.extract_datetime_from_exif(tagged) == T0
    assert tct.extract_timestamp(str(tmp_path / "missing.tif")).source == "none"


@pytest.mark.skipif(not HAS_EXIFTOOL, reason="exiftool")
def test_xmp_beats_exif_beats_tiff_tag(tmp_path):
    p = write_tif(tmp_path / "x.tif", scene(), T0)
    subprocess.run(["exiftool", "-overwrite_original", "-q", "-EXIF:DateTimeOriginal=2026:06:01 11:00:00", p], check=True)
    assert tct.extract_timestamp(p).source == "EXIF" and tct.extract_timestamp(p).value == T0 + dt.timedelta(hours=1)
    subprocess.run(["exiftool", "-overwrite_original", "-q", "-XMP:CreateDate=2026:06:01 12:00:00+02:00", p], check=True)
    ts = tct.extract_timestamp(p)
    assert (ts.source, ts.value, ts.tz) == ("XMP", T0 + dt.timedelta(hours=2), "+02:00")


# ----------------------------------------------------------------------------- images
def test_inspect_and_load(tmp_path):
    f32 = write_tif(tmp_path / "f.tif", scene(), T0)
    info = tct.inspect_image(f32)
    assert (info.width, info.height, info.bands, info.dtype) == (200, 160, 1, "float32") and info.is_float and not info.is_8bit
    assert "1 band" in info.describe()
    cv2.imwrite(str(tmp_path / "u8.tif"), np.full((10, 12), 7, np.uint8))
    info8 = tct.inspect_image(str(tmp_path / "u8.tif"))
    assert info8.is_8bit and info8.is_single_band
    assert tct.load_thermal_image(str(tmp_path / "u8.tif")).dtype == np.float32
    cv2.imwrite(str(tmp_path / "rgb.png"), np.zeros((10, 12, 3), np.uint8))
    assert tct.inspect_image(str(tmp_path / "rgb.png")).bands == 3
    with pytest.raises(ValueError, match="bands"):
        tct.load_thermal_image(str(tmp_path / "rgb.png"))
    bad = tmp_path / "bad.tif"
    bad.write_bytes(b"not an image")
    assert tct.inspect_image(str(bad)).error
    with pytest.raises(ValueError, match="cannot read"):
        tct.load_thermal_image(str(bad))


def test_display_image_modes():
    img = scene()
    for mode in ("inferno", "grey", "clahe"):
        rgb = tct.make_display_image(img, mode)
        assert rgb.shape == (160, 200, 3) and rgb.dtype == np.uint8
    grey = tct.make_display_image(img, "grey")
    assert grey[25, 25, 0] < grey[25, 85, 0] < grey[25, 145, 0], "grey ramp follows temperature"


# ----------------------------------------------------------------------------- output files
def test_output_filename_collisions():
    files = [Path("a/X.tif"), Path("a/X.png"), Path("a/Y.tif"), Path("a/Z.TIFF")]
    names = tct.output_filenames(files)
    assert names[files[0]] == "X_tif_calibrated.tif" and names[files[1]] == "X_png_calibrated.tif"
    assert names[files[2]] == "Y_calibrated.tif" and names[files[3]] == "Z_calibrated.tif"
    assert len(set(names.values())) == 4


def test_geotiff_output_profile_and_tags(tmp_path):
    src = write_tif(tmp_path / "src.tif", scene(), T0, crs="EPSG:32631", tags={"CUSTOM": "kept"})
    out = tmp_path / "out.tif"
    notes = tct.save_calibrated_geotiff(scene() * 2, str(out), src, {"CALIBRATION_SLOPE": "2"}, copy_metadata=True)
    assert notes == []
    with rasterio.open(out) as s:
        assert s.count == 1 and s.dtypes[0] == "float32" and s.crs.to_epsg() == 32631
        assert s.profile["compress"].lower() == "lzw"
        with rasterio.open(src) as o:
            assert s.transform == o.transform
        t = s.tags()
        assert t["TIFFTAG_DATETIME"] == "2026:06:01 10:00:00" and t["CUSTOM"] == "kept" and t["CALIBRATION_SLOPE"] == "2"
        assert t["TIFFTAG_SOFTWARE"].startswith(tct.TOOL_NAME)
    assert np.allclose(read_arr(out), scene() * 2)
    out2 = tmp_path / "out2.tif"
    tct.save_calibrated_geotiff(scene(), str(out2), src, copy_metadata=False)
    with rasterio.open(out2) as s:
        assert "CUSTOM" not in s.tags() and s.crs.to_epsg() == 32631, "georeferencing is always kept"


def test_geotiff_output_without_source_gets_pixel_transform(tmp_path):
    out = tmp_path / "o.tif"
    tct.save_calibrated_geotiff(scene(), str(out))
    with rasterio.open(out) as s:
        assert s.crs is None and s.width == 200 and s.height == 160


@pytest.mark.skipif(not HAS_EXIFTOOL, reason="exiftool")
def test_exif_xmp_gps_survive_with_exiftool(tmp_path):
    src = write_tif(tmp_path / "src.tif", scene(), T0, crs="EPSG:32631")
    subprocess.run(["exiftool", "-overwrite_original", "-q", "-EXIF:DateTimeOriginal=2026:06:01 10:00:00", "-EXIF:Model=M4T",
                    "-GPSLatitude=51.987", "-GPSLatitudeRef=N", "-GPSLongitude=5.665", "-GPSLongitudeRef=E",
                    "-XMP:CreateDate=2026:06:01 10:00:00+02:00", "-XMP-drone-dji:RelativeAltitude=+30.0", src], check=True)
    out = tmp_path / "out.tif"
    tct.save_calibrated_geotiff(scene(), str(out), src)
    r = subprocess.run(["exiftool", "-j", "-G1", str(out)], capture_output=True, text=True, check=True).stdout
    meta = json.loads(r)[0]
    assert meta.get("ExifIFD:DateTimeOriginal") == "2026:06:01 10:00:00"
    assert meta.get("IFD0:Model") == "M4T"
    assert "GPS:GPSLatitude" in meta and "GPS:GPSLongitude" in meta
    assert meta.get("XMP-xmp:CreateDate") == "2026:06:01 10:00:00+02:00"
    assert meta.get("XMP-drone-dji:RelativeAltitude") in ("+30.0", 30.0, "30.0")
    with rasterio.open(out) as s:         # GeoTIFF keys survive exiftool's rewrite
        assert s.crs.to_epsg() == 32631 and s.read(1).dtype == np.float32


@pytest.mark.skipif(not HAS_EXIFTOOL, reason="exiftool")
def test_exiftool_copy_never_overwrites_output_metadata(tmp_path):
    """Regression: `-all:all` used to copy the source's GDAL metadata block over the output's, deleting the
    calibration tags and the software tag, and could replace our georeferencing with the source's."""
    src = write_tif(tmp_path / "src.tif", scene(), T0, crs="EPSG:32631", tags={"CUSTOM": "kept", "TIFFTAG_SOFTWARE": "theirs"})
    out = tmp_path / "out.tif"
    tct.save_calibrated_geotiff(scene(), str(out), src, {"CALIBRATION_FORMULA": "out = 1 * in + 0"})
    with rasterio.open(out) as s:
        t = s.tags()
        assert t["CALIBRATION_FORMULA"] == "out = 1 * in + 0" and t["CUSTOM"] == "kept"
        assert t["TIFFTAG_SOFTWARE"].startswith(tct.TOOL_NAME)
        assert s.crs.to_epsg() == 32631


# ----------------------------------------------------------------------------- survey collection and pre-flight
def test_collect_survey_files_rules(tmp_path):
    d = tmp_path / "flüght é"
    d.mkdir()
    for name in ("b.tif", "a.TIF", "c.jpg", "d.txt", "a_calibrated.tif", "cal.tif", "x_T_deg.tif", "x_T.JPG"):
        write_tif(d / name, scene()) if not name.endswith(".txt") else (d / name).write_text("x")
    files = tct.collect_survey_files(str(d), None, "", [str(d / "cal.tif")])
    assert [f.name for f in files] == ["a.TIF", "b.tif", "c.jpg", "x_T.JPG", "x_T_deg.tif"]
    assert [f.name for f in tct.collect_survey_files(str(d), None, "_T_deg", [])] == ["x_T_deg.tif"]
    explicit = tct.collect_survey_files(None, [str(d / "b.tif"), str(d / "d.txt")], "", [])
    assert [f.name for f in explicit] == ["b.tif"]
    assert tct.collect_survey_files(str(tmp_path / "nope"), None, "", []) == []


def test_preflight_blocks_and_warns(tmp_path):
    cal = write_tif(tmp_path / "cal.tif", scene(), T0)
    unfitted = tct.CalibrationEvent(cal, T0, targets=[])
    sur = tmp_path / "s"
    sur.mkdir()
    write_tif(sur / "a.tif", scene(), T0)
    rep = tct.preflight(tct.BatchJob(tct.MODE_THERMAL, False, [unfitted], [sur / "a.tif"], tmp_path / "out"))
    assert rep.blocking and "No calibration image has a complete fit" in rep.text()
    ev = make_event(cal)
    rep = tct.preflight(tct.BatchJob(tct.MODE_THERMAL, False, [ev, unfitted], [], tmp_path / "out"))
    assert rep.blocking and any("without a fit" in t for _, t in rep.lines)
    two = tct.CalibrationEvent(cal, T0, targets=[]); two.fit = tct.compute_linear_regression([1, 2], [1, 2])
    rep = tct.preflight(tct.BatchJob(tct.MODE_THERMAL, False, [two], [sur / "a.tif"], sur))
    txt = rep.text()
    assert "only 2 targets" in txt and "Output folder is the input folder" in txt and not rep.blocking
    em = tct.EmissivitySettings(True)
    rep = tct.preflight(tct.BatchJob(tct.MODE_THERMAL, False, [ev], [sur / "a.tif"], tmp_path / "out", em))
    assert any("Emissivity correction is ON" in t for l, t in rep.lines if l == "warn")
    assert rep.timestamps[sur / "a.tif"].source == "TIFF tag"


@pytest.mark.skipif(not HAS_EXIFTOOL, reason="exiftool")
def test_preflight_mixed_time_zones(tmp_path):
    cal = write_tif(tmp_path / "cal.tif", scene())
    subprocess.run(["exiftool", "-overwrite_original", "-q", "-XMP:CreateDate=2026:06:01 10:00:00+02:00", cal], check=True)
    sur = tmp_path / "s"
    sur.mkdir()
    z = write_tif(sur / "z.tif", scene())
    subprocess.run(["exiftool", "-overwrite_original", "-q", "-XMP:CreateDate=2026:06:01 08:00:00Z", z], check=True)
    rep = tct.preflight(tct.BatchJob(tct.MODE_THERMAL, False, [make_event(cal)], [Path(z)], tmp_path / "out"))
    assert any("UTC" in t for l, t in rep.lines if l == "warn")


# ----------------------------------------------------------------------------- processing log contract
EXPECTED_COLUMNS = [
    "input_filename", "output_filename", "timestamp", "timestamp_source", "timestamp_tz", "position_vs_calibration",
    "slope_m", "intercept_b", "formula_applied",
    "original_mean_celsius", "original_min_celsius", "original_max_celsius",
    "calibrated_mean_celsius", "calibrated_min_celsius", "calibrated_max_celsius", "mean_shift_celsius",
    "pct_pixels_outside_target_range",
    "u_fit_1sigma",
    "emissivity_corrected", "scene_emissivity", "status",
]


def test_processing_log_schema_is_stable(tmp_path):
    """The CSV is what people cite. Its columns are part of the interface: change this list on purpose only."""
    import pandas as pd
    cal = write_tif(tmp_path / "cal.tif", scene(), T0)
    sur = tmp_path / "s"
    sur.mkdir()
    write_tif(sur / "a.tif", scene(), T0 + dt.timedelta(minutes=1))
    res, logs = run_job(tct.BatchJob(tct.MODE_THERMAL, False, [make_event(cal)], [sur / "a.tif"], tmp_path / "out"))
    df = pd.read_csv(tmp_path / "out" / "processing_log.csv")
    assert list(df.columns) == EXPECTED_COLUMNS
    row = df.iloc[0]
    assert row["status"] == "ok" and row["position_vs_calibration"] == "single"
    assert row["timestamp"] == (T0 + dt.timedelta(minutes=1)).isoformat()
    assert row["u_fit_1sigma"] == 0.0, "exact synthetic targets: zero residual scatter"
    assert row["emissivity_corrected"] == False  # noqa: E712
    summary = (tmp_path / "out" / "processing_summary.txt").read_text(encoding="utf-8")
    for needle in ("CALIBRATION EVENTS", "METHOD", "UNCERTAINTY", "u_fit_1sigma", "Images processed: 1"):
        assert needle in summary
    assert not list((tmp_path / "out").glob("*.calib.json")), "no project copy without a project dict"
    assert any("Done: 1 ok" in l for l in logs)


def test_msi_single_point_batch(tmp_path):
    dn = scene() * 100.0
    cal = write_tif(tmp_path / "cal.tif", dn, T0)
    ev = tct.CalibrationEvent(cal, T0, targets=[])
    t = tct.CalibrationTarget("panel", polygon=poly("T2"))
    st = tct.extract_roi_statistics(dn, t.polygon, 3)
    t.drone_mean, t.drone_std, t.pixel_count, t.ground_truth = st.mean, st.std, st.count, 0.5
    ev.targets = [t]
    assert tct.recompute_fit(ev, True, False, None) is None and ev.fit.single_point
    k = 0.5 / 3000.0
    assert ev.fit.slope == pytest.approx(k)
    sur = tmp_path / "s"
    sur.mkdir()
    write_tif(sur / "band.tif", dn, T0)
    res, _ = run_job(tct.BatchJob(tct.MODE_MSI, True, [ev], [sur / "band.tif"], tmp_path / "out"))
    out = read_arr(tmp_path / "out" / "band_calibrated.tif")
    assert np.allclose(out, dn * k, rtol=1e-6)
    row = res["rows"][0]
    assert row["formula_applied"].startswith("Refl = ") and "calibrated_mean_reflectance" in row


def test_cancel_stops_after_current_image(tmp_path):
    cal = write_tif(tmp_path / "cal.tif", scene(), T0)
    sur = tmp_path / "s"
    sur.mkdir()
    files = [Path(write_tif(sur / f"{i}.tif", scene(), T0)) for i in range(5)]
    calls = {"n": 0}

    def cancelled():
        calls["n"] += 1
        return calls["n"] > 2
    res = tct.run_batch(tct.BatchJob(tct.MODE_THERMAL, False, [make_event(cal)], files, tmp_path / "out"),
                        lambda m: None, lambda i, n, name: None, cancelled)
    assert res["ok"] == 2 and len(list((tmp_path / "out").glob("*_calibrated.tif"))) == 2


def test_on_row_callback_receives_every_row(tmp_path):
    cal = write_tif(tmp_path / "cal.tif", scene(), T0)
    sur = tmp_path / "s"
    sur.mkdir()
    files = [Path(write_tif(sur / f"{i}.tif", scene(), T0)) for i in range(3)]
    seen = []
    tct.run_batch(tct.BatchJob(tct.MODE_THERMAL, False, [make_event(cal)], files, tmp_path / "out"),
                  lambda m: None, lambda i, n, name: None, lambda: False, on_row=seen.append)
    assert [r["input_filename"] for r in seen] == ["0.tif", "1.tif", "2.tif"]


# ----------------------------------------------------------------------------- project files and CLI
def test_project_roundtrip_preserves_everything(tmp_path):
    cal = write_tif(tmp_path / "cal.tif", scene(blur=0.5), T0)
    ev = make_event(cal)
    ev.targets[1].ground_truth = 0.0
    em = tct.EmissivitySettings(True, 0.9, 0.97, -30)
    project = tct.events_to_project([ev], tct.MODE_THERMAL, False, em, 2, {"folder": "x", "filter": "_T"}, "Wheat June")
    text = json.dumps(project)
    back = json.loads(text)
    assert back["version"] == tct.__version__ and back["roi_inset_px"] == 2 and back["title"] == "Wheat June"
    assert tct.EmissivitySettings.from_dict(back["emissivity"]) == em
    events, problems = tct.events_from_project(back, str(tmp_path), 2)
    assert problems == [] and len(events) == 1
    t_back = events[0].targets[1]
    assert t_back.ground_truth == 0.0 and t_back.polygon == ev.targets[1].polygon and t_back.pixel_count > 0
    moved = dict(back)
    moved["calibration_images"] = [dict(back["calibration_images"][0], path="/elsewhere/cal.tif")]
    events, problems = tct.events_from_project(moved, str(tmp_path), 2)
    assert problems == [] and events[0].image_path == str(tmp_path / "cal.tif"), "resolved next to the project file"
    gone = dict(back)
    gone["calibration_images"] = [dict(back["calibration_images"][0], path="/nowhere/cal.tif")]
    events, problems = tct.events_from_project(gone, str(tmp_path / "other"), 2)
    assert events == [] and problems and "missing" in problems[0]


def write_project(tmp_path, drift=0.0):
    cal = write_tif(tmp_path / "cal.tif", scene(drift), T0)
    sur = tmp_path / "survey"
    sur.mkdir(exist_ok=True)
    write_tif(sur / "a.tif", scene(drift), T0 + dt.timedelta(minutes=1))
    ev = make_event(cal)
    project = tct.events_to_project([ev], tct.MODE_THERMAL, False, tct.EmissivitySettings(), 3,
                                    {"folder": str(sur), "output_folder": str(tmp_path / "out")})
    pp = tmp_path / "flight.calib.json"
    pp.write_text(json.dumps(project, indent=2), encoding="utf-8")
    return pp


def test_headless_run_matches_gui_path(tmp_path):
    pp = write_project(tmp_path, drift=1.5)
    logs = []
    res = tct.run_project_headless(str(pp), log=logs.append)
    assert res["ok"] == 1 and any("✓" in l for l in logs)
    assert abs(float(read_arr(tmp_path / "out" / "a_calibrated.tif")[125, 100]) - 30.0) < 0.01
    assert res["project"] == str(tmp_path / "out" / "calibration_project.calib.json") and Path(res["project"]).exists()
    assert tct.project_filename({"title": "Wheat / June 2026"}) == "Wheat_June_2026.calib.json"
    assert tct.project_filename({}) == "calibration_project.calib.json"


def test_cli_subprocess(tmp_path):
    pp = write_project(tmp_path)
    out = tmp_path / "cli_out"
    r = subprocess.run([sys.executable, str(TOOL), "--project", str(pp), "--run", "--output", str(out)],
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr[-2000:]
    assert (out / "processing_log.csv").exists() and (out / "a_calibrated.tif").exists()
    bad = subprocess.run([sys.executable, str(TOOL), "--run"], capture_output=True, text=True, timeout=120)
    assert bad.returncode != 0 and "--project" in bad.stderr
    missing = tmp_path / "survey_empty"
    missing.mkdir()
    r2 = subprocess.run([sys.executable, str(TOOL), "--project", str(pp), "--run", "--survey", str(missing)],
                        capture_output=True, text=True, timeout=300)
    assert r2.returncode != 0 and "pre-flight" in (r2.stderr + r2.stdout).lower()


# ----------------------------------------------------------------------------- report
def test_fit_report_html(tmp_path):
    cal = write_tif(tmp_path / "cal.tif", scene(blur=0.5), T0)
    ev = make_event(cal)
    png = tct.render_fit_png(ev, "°C")
    html = tct.fit_report_html(ev, "°C", [ev], {"mode": "thermal"}, png)
    assert html.startswith("<!doctype html>") and "Calibration fit report" in html
    assert f"{ev.fit.slope:.5f}" in html and "prediction at x̄" in html and "mode" in html
    assert "<img src='data:image/png;base64," in html if png is not None else "<img" not in html
    for t in ev.targets:
        assert t.name in html
    (tmp_path / "r.html").write_text(html, encoding="utf-8")
    assert (tmp_path / "r.html").stat().st_size > 1000
    with pytest.raises(ValueError):
        tct.fit_report_html(tct.CalibrationEvent(cal, T0, targets=[]), "°C", [])


def test_survey_folders_move_with_the_project(tmp_path):
    """A project folder that moves as a whole (the demo flight, a copied flight) still finds its survey and
    output folders: they are stored relative to the project file as well."""
    (tmp_path / "survey").mkdir()
    saved = tct.survey_with_relative_folders(
        {"folder": str(tmp_path / "survey"), "output_folder": str(tmp_path / "calibrated")}, str(tmp_path))
    assert saved["folder_relative"] == "survey" and saved["output_folder_relative"] == "calibrated"
    moved = {"folder": "/nonexistent/flight/survey", "folder_relative": "survey",
             "output_folder": "/nonexistent/flight/calibrated", "output_folder_relative": "calibrated"}
    sv = tct.resolve_survey_folders(moved, str(tmp_path))
    assert sv["folder"] == str(tmp_path / "survey") and sv["output_folder"] == str(tmp_path / "calibrated")
    here = dict(moved, folder=str(tmp_path))           # an existing absolute folder wins
    assert tct.resolve_survey_folders(here, str(tmp_path)) == here
    assert tct.resolve_survey_folders(moved, None) == moved
