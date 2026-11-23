"""The demo flight generator (demo/make_demo_flight.py): its calibration shots must be readable by the
tool, carry their capture times, and give back the camera drift the generator put in."""
import json
import sys
from pathlib import Path

import numpy as np
import pytest

import thermal_calibration_tool as tct

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "demo"))
import make_demo_flight as demo  # noqa: E402


@pytest.fixture(scope="module")
def flight(tmp_path_factory):
    out = tmp_path_factory.mktemp("demo") / "flight"
    demo.main([str(out), "--only", "calibration", "--supersample", "1", "--jobs", "1"])
    return out


def test_files_and_metadata(flight):
    shots = sorted((flight / "calibration").glob("*.tif"))
    assert len(shots) == 2
    truth = json.loads((flight / "ground_truth.json").read_text())
    for path, rec in zip(shots, [r for r in truth["images"] if r["kind"] == "calibration"]):
        img = tct.load_thermal_image(str(path))
        assert img.shape == (demo.H, demo.W) and img.dtype == np.float32
        assert 15 < float(np.median(img)) < 45                    # a summer field, in degrees C
        ts = tct.extract_timestamp(str(path))
        assert ts.value is not None and ts.value.isoformat() == rec["time"][:19]
    sheet = (flight / "field_sheet.csv").read_text().splitlines()
    assert sheet[0] == "time,image,panel,reading_C" and len(sheet) == 7


def test_fit_recovers_the_drift(flight):
    """Polygons from the generator, readings from the 'thermometer': the fitted correction must match the
    known gain and offset to within what the readings' rounding and noise allow."""
    cfg = json.loads((flight / "screenshot_config.json").read_text())
    truth = {Path(r["file"]).name: r for r in json.loads((flight / "ground_truth.json").read_text())["images"]}
    for shot in cfg["calibration"]:
        img = tct.load_thermal_image(shot["file"])
        means, refs = [], []
        for name, poly in shot["panels"].items():
            st = tct.extract_roi_statistics(img, [tuple(p) for p in poly], 3)
            assert st.count > 1500 and st.std < 0.4                # the polygon sits on the panel
            means.append(st.mean)
            refs.append(shot["gun"][name])
        fit = tct.compute_linear_regression(means, refs)
        rec = truth[Path(shot["file"]).name]
        for t_true in (30.0, 40.0, 50.0):
            t_drone = rec["gain"] * t_true + rec["offset_C"]
            assert abs(fit.slope * t_drone + fit.intercept - t_true) < 0.6
        assert fit.r_squared > 0.995


def test_project_file_opens(flight):
    project = json.loads((flight / "demo.calib.json").read_text())
    events, problems = tct.events_from_project(project, str(flight), 3)
    assert not problems and len(events) == 2
    assert all(len(e.targets) == 3 and all(t.has_polygon() and t.ground_truth for t in e.targets) for e in events)
