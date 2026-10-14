#!/usr/bin/env python3
"""Build the deterministic golden flight with the Python tool and save it as a project file, so the
Rust engine can be checked against tests/golden/expected.json (`cargo test` in rust/).

Writes tests/data/golden_flight/ (gitignored): three calibration shots with three panels, six survey
images with their true arrays, ground_truth.json and flight.calib.json.
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import helpers  # noqa: E402
import thermal_calibration_tool as tct  # noqa: E402

out = ROOT / "tests" / "data" / "golden_flight"
truth = helpers.build_golden_flight(out)
events = [helpers.make_event(str(out / "calibration" / rec["file"]), gt=rec["gun"],
                             polygons={k: [tuple(p) for p in v] for k, v in rec["polygons"].items()})
          for rec in truth["calibration"]]
survey = {"folder": str(out / "survey"), "output_folder": str(out / "calibrated_py"), "filter": "",
          "copy_metadata": True, "skip_existing": False}
project = tct.events_to_project(events, tct.MODE_THERMAL, False, tct.EmissivitySettings(), 3, survey=survey,
                                title="golden flight", project_dir=str(out))
(out / "flight.calib.json").write_text(json.dumps(project, indent=2), encoding="utf-8")
print("golden flight written to", out)
for e in events:
    print(f"  {Path(e.image_path).name}: {e.fit.summary_line('°C')}")
