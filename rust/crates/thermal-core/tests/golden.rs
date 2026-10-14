//! The Rust engine against the Python tool's golden flight (`tests/golden/expected.json`).
//!
//! The flight itself is built by the Python tool: `python3 rust/tools/make_golden_flight.py` writes
//! `tests/data/golden_flight/` (gitignored). Without it this test is skipped, not failed.
//!
//! Tolerances: the Python tool sums float32 arrays with numpy (pairwise, in float32), this crate in
//! f64, so ROI means and the fits agree to about 1e-6 relative; the limits below are 10 to 100 times
//! wider than that and still far tighter than anything a thermometer can resolve.

use serde_json::Value;
use std::path::{Path, PathBuf};
use thermal_core::batch::{preflight, run_batch, Callbacks, Cell};
use thermal_core::project::load_project_job;
use thermal_core::raster::load_image;

fn repo_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../..")
        .canonicalize()
        .expect("repo root")
}

/// A minimal `.npy` reader for the little-endian float32 C-order arrays the flight stores.
fn read_npy_f32(path: &Path) -> (usize, usize, Vec<f32>) {
    let b = std::fs::read(path).expect("npy");
    assert_eq!(&b[..6], b"\x93NUMPY");
    let (hlen, start) = if b[6] == 1 {
        (u16::from_le_bytes([b[8], b[9]]) as usize, 10)
    } else {
        (u32::from_le_bytes([b[8], b[9], b[10], b[11]]) as usize, 12)
    };
    let header = std::str::from_utf8(&b[start..start + hlen]).expect("ascii header");
    assert!(
        header.contains("'descr': '<f4'") && header.contains("'fortran_order': False"),
        "{header}"
    );
    let shape = header
        .split("'shape': (")
        .nth(1)
        .expect("shape")
        .split(')')
        .next()
        .expect("shape");
    let dims: Vec<usize> = shape
        .split(',')
        .filter_map(|s| s.trim().parse().ok())
        .collect();
    let data: Vec<f32> = b[start + hlen..]
        .as_chunks::<4>()
        .0
        .iter()
        .map(|c| f32::from_le_bytes(*c))
        .collect();
    assert_eq!(data.len(), dims[0] * dims[1]);
    (dims[0], dims[1], data)
}

fn rel(a: f64, b: f64) -> f64 {
    (a - b).abs() / b.abs().max(1e-12)
}

#[test]
fn golden_flight_matches_the_python_reference() {
    let root = repo_root();
    let flight = root.join("tests/data/golden_flight");
    let expected_path = root.join("tests/golden/expected.json");
    if !flight.join("flight.calib.json").exists() || !expected_path.exists() {
        eprintln!("SKIPPED: build the flight first with  python3 rust/tools/make_golden_flight.py");
        return;
    }
    let expected: Value =
        serde_json::from_str(&std::fs::read_to_string(&expected_path).unwrap()).unwrap();
    let out = std::env::temp_dir().join(format!("thermal_core_golden_{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&out);

    let mut loaded =
        load_project_job(&flight.join("flight.calib.json"), None, Some(&out), None).unwrap();
    assert!(loaded.problems.is_empty(), "{:?}", loaded.problems);
    assert!(loaded.fit_warnings.is_empty(), "{:?}", loaded.fit_warnings);

    // --- the per-shot fits
    let exp_events = expected["events"].as_array().unwrap();
    assert_eq!(exp_events.len(), loaded.job.events.len());
    for (e, ev) in exp_events.iter().zip(&loaded.job.events) {
        assert_eq!(e["file"].as_str().unwrap(), ev.file_name());
        let f = ev.fit.as_ref().expect("fitted");
        let g = |k: &str| e[k].as_f64().unwrap();
        assert!(
            rel(f.slope, g("slope")) < 1e-4,
            "{}: slope {} vs {}",
            ev.file_name(),
            f.slope,
            g("slope")
        );
        assert!(
            (f.intercept - g("intercept")).abs() < 2e-3,
            "{}: intercept {} vs {}",
            ev.file_name(),
            f.intercept,
            g("intercept")
        );
        assert!(
            (f.r_squared.unwrap() - g("r2")).abs() < 1e-4,
            "{}: r2",
            ev.file_name()
        );
        assert!(
            rel(f.rmse.unwrap(), g("rmse")) < 2e-3,
            "{}: rmse {} vs {}",
            ev.file_name(),
            f.rmse.unwrap(),
            g("rmse")
        );
        assert!(
            rel(f.se_slope.unwrap(), g("se_slope")) < 2e-3,
            "{}: se_slope",
            ev.file_name()
        );
        assert!(
            rel(f.se_intercept.unwrap(), g("se_intercept")) < 2e-3,
            "{}: se_intercept",
            ev.file_name()
        );
        assert!(
            rel(f.loo_rmse.unwrap(), g("loo")) < 2e-3,
            "{}: loo",
            ev.file_name()
        );
    }

    // --- the run
    let rep = preflight(&loaded.job, None);
    assert!(!rep.blocking(), "{}", rep.text());
    loaded.job.survey_files = rep.files.clone();
    let mut log = |_: &str| {};
    let mut progress = |_: usize, _: usize, _: &str| {};
    let cancelled = || false;
    let mut cb = Callbacks {
        log: &mut log,
        progress: &mut progress,
        is_cancelled: &cancelled,
        on_row: None,
    };
    let res = run_batch(&loaded.job, &mut cb, Some(&rep.timestamps)).unwrap();
    assert_eq!((res.ok, res.errors, res.skipped), (6, 0, 0));

    let exp_rows = expected["rows"].as_array().unwrap();
    assert_eq!(exp_rows.len(), res.rows.len());
    for (e, row) in exp_rows.iter().zip(&res.rows) {
        let name = e["input_filename"].as_str().unwrap();
        assert_eq!(row.get("input_filename").unwrap().as_str().unwrap(), name);
        let num = |k: &str| row.get(k).and_then(Cell::as_f64);
        assert!(
            rel(num("slope_m").unwrap(), e["slope_m"].as_f64().unwrap()) < 1e-4,
            "{name}: slope_m"
        );
        assert!(
            (num("intercept_b").unwrap() - e["intercept_b"].as_f64().unwrap()).abs() < 2e-3,
            "{name}: intercept_b"
        );
        let (got, want) = (
            num("calibrated_mean_celsius").unwrap(),
            e["calibrated_mean_celsius"].as_f64().unwrap(),
        );
        assert!(
            (got - want).abs() < 0.01,
            "{name}: calibrated mean {got} vs {want}"
        );
        match e["u_fit_1sigma"].as_f64() {
            Some(u) => assert!(rel(num("u_fit_1sigma").unwrap(), u) < 2e-3, "{name}: u_fit"),
            None => assert!(
                num("u_fit_1sigma").is_none(),
                "{name}: u_fit should be empty"
            ),
        }
        let (got, want) = (
            num("pct_pixels_outside_target_range").unwrap(),
            e["pct_pixels_outside_target_range"].as_f64().unwrap(),
        );
        assert!(
            (got - want).abs() <= 0.5,
            "{name}: pct outside {got} vs {want}"
        );
        assert_eq!(
            row.get("position_vs_calibration")
                .unwrap()
                .as_str()
                .unwrap(),
            e["position_vs_calibration"].as_str().unwrap(),
            "{name}"
        );
    }

    // --- the written images against the true scene (the number that matters in the end)
    let truth: Value =
        serde_json::from_str(&std::fs::read_to_string(flight.join("ground_truth.json")).unwrap())
            .unwrap();
    let exp_errs = expected["survey_mean_errors"].as_array().unwrap();
    for (rec, exp) in truth["survey"].as_array().unwrap().iter().zip(exp_errs) {
        let (h, w, true_data) =
            read_npy_f32(&flight.join("survey").join(rec["true"].as_str().unwrap()));
        let cal = load_image(
            &out.join(
                rec["file"]
                    .as_str()
                    .unwrap()
                    .replace(".tif", "_calibrated.tif"),
            ),
        )
        .unwrap();
        assert_eq!((cal.height, cal.width), (h, w));
        let inner_mean = |data: &[f32]| {
            let (mut s, mut n) = (0.0f64, 0usize);
            for y in 20..h - 20 {
                for x in 20..w - 20 {
                    s += data[y * w + x] as f64;
                    n += 1;
                }
            }
            s / n as f64
        };
        let err = inner_mean(&cal.data) - inner_mean(&true_data);
        assert!(
            (err - exp.as_f64().unwrap()).abs() < 0.01,
            "{}: mean error {} vs {}",
            rec["file"],
            err,
            exp
        );
    }

    // --- the log keeps the pinned column order, the summary and the project copy exist
    let csv = std::fs::read_to_string(&res.csv).unwrap();
    let header = csv.lines().next().unwrap();
    assert!(
        header.starts_with(
            "input_filename,output_filename,timestamp,timestamp_source,timestamp_tz,position_vs_calibration,slope_m,intercept_b,\
             formula_applied,original_mean_celsius,original_min_celsius,original_max_celsius,calibrated_mean_celsius"
        ),
        "{header}"
    );
    assert!(header.ends_with("pct_pixels_outside_target_range,u_fit_1sigma,emissivity_corrected,scene_emissivity,status"), "{header}");
    assert!(res.summary.exists() && res.project.as_ref().is_some_and(|p| p.exists()));
    let _ = std::fs::remove_dir_all(&out);
}
