# Contributing

Bug reports, feature requests and pull requests are welcome.

## Reporting a problem

Open an issue using the bug report template. The two things that make a report actionable are the
`processing_log.csv` of the run and, if possible, one calibration image with its reference readings (a
single-band TIFF is small). Say which camera and which R-JPEG converter (and SDK version) produced the
images.

## Development setup

```bash
git clone https://github.com/haystack-imaging/thermal-calibration-tool
cd thermal-calibration-tool
pip install -e ".[exif,test]"
python -m pytest tests -q
```

GUI tests need a display (`xvfb-run -a python -m pytest tests -q` on a headless Linux box). See
`tests/README.md` for the test data generators.

## Rules for changes

- The science functions (`compute_linear_regression`, `coefficients_at`, `extract_roi_statistics`,
  `apply_emissivity_correction`, `run_batch`, …) have no Tk dependency. Keep it that way; it is what makes
  them testable and scriptable.
- A change of the method must come with: updated golden data (`python tests/make_golden.py`, commit the
  new `tests/golden/expected.json`), a CHANGELOG entry that says what moved and by how much, and a version
  bump.
- The columns of `processing_log.csv` are an interface that people cite. Adding a column is fine; renaming
  or removing one needs a major version bump. `tests/test_io.py::test_processing_log_schema_is_stable`
  pins the list.
- Every bug fix gets a regression test named after the problem.
- No new runtime dependencies without a reason in the pull request; the install must stay a plain `pip`.
