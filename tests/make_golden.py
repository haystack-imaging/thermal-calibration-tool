#!/usr/bin/env python3
"""Regenerate tests/golden/expected.json from the deterministic golden flight.

Run this only after an intended change of the method, and commit the new file together with the
code change and a note in the changelog, so reviewers can see what moved and by how much.
"""
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from helpers import build_golden_flight, run_golden_pipeline  # noqa: E402
import thermal_calibration_tool as tct  # noqa: E402

with tempfile.TemporaryDirectory() as tmp:
    build_golden_flight(Path(tmp) / "flight")
    summary = run_golden_pipeline(Path(tmp) / "flight", Path(tmp) / "out")
summary["tool_version"] = tct.__version__
out = HERE / "golden" / "expected.json"
out.parent.mkdir(exist_ok=True)
old = json.loads(out.read_text(encoding="utf-8")) if out.exists() else None
out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
print("wrote", out)
if old:
    for e_old, e_new in zip(old["events"], summary["events"]):
        print(f"  {e_new['file']}: m {e_old['slope']:.5f} -> {e_new['slope']:.5f}, b {e_old['intercept']:.4f} -> {e_new['intercept']:.4f}")
    print("  survey errors:", [round(v, 4) for v in old["survey_mean_errors"]], "->", [round(v, 4) for v in summary["survey_mean_errors"]])
