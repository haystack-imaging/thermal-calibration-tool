//! Runs the desktop app with `tests/e2e/flow.js` inside its web view on the golden flight and checks
//! the report. Needs a display (`DISPLAY` or `WAYLAND_DISPLAY`) and the golden flight
//! (`python3 rust/tools/make_golden_flight.py`); skipped otherwise. `THERMAL_E2E_LAUNCH` may hold a
//! launcher prefix, e.g. `systemd-run --user --wait --collect -q --setenv=DISPLAY=:0`, for environments
//! where the app must start outside the test's own process tree.

use serde_json::{json, Value};
use std::path::{Path, PathBuf};
use std::process::Command;

fn repo_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../..")
        .canonicalize()
        .expect("repo root")
}

fn port_open() -> bool {
    std::net::TcpStream::connect_timeout(
        &"127.0.0.1:5173".parse().unwrap(),
        std::time::Duration::from_millis(300),
    )
    .is_ok()
}

/// A debug build of the app loads the front-end from the Vite dev server. Reuse one that is running
/// (e.g. the developer's `tauri dev`), otherwise start one for the test and stop it afterwards.
fn ensure_dev_server() -> Option<std::process::Child> {
    if port_open() {
        eprintln!("dev server on 127.0.0.1:5173 already running: reusing it");
        return None;
    }
    let app_dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("..");
    let vite = app_dir.join("node_modules/vite/bin/vite.js");
    assert!(
        vite.exists(),
        "run `npm install` in rust/app first ({} missing)",
        vite.display()
    );
    let mut child = Command::new("node")
        .arg(vite)
        .args(["--host", "127.0.0.1", "--port", "5173", "--strictPort"])
        .current_dir(&app_dir)
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .spawn()
        .expect("start the Vite dev server");
    for _ in 0..100 {
        if port_open() {
            return Some(child);
        }
        std::thread::sleep(std::time::Duration::from_millis(300));
    }
    let _ = child.kill();
    let _ = child.wait();
    panic!("the Vite dev server did not come up on 127.0.0.1:5173");
}

#[test]
fn full_flow_on_the_golden_flight() {
    let root = repo_root();
    let flight = root.join("tests/data/golden_flight");
    if std::env::var_os("DISPLAY").is_none() && std::env::var_os("WAYLAND_DISPLAY").is_none() {
        eprintln!("SKIPPED: no display");
        return;
    }
    if !flight.join("ground_truth.json").exists() {
        eprintln!("SKIPPED: build the flight first with  python3 rust/tools/make_golden_flight.py");
        return;
    }
    let truth: Value =
        serde_json::from_str(&std::fs::read_to_string(flight.join("ground_truth.json")).unwrap())
            .unwrap();
    let expected: Value = serde_json::from_str(
        &std::fs::read_to_string(root.join("tests/golden/expected.json")).unwrap(),
    )
    .unwrap();
    let work = std::env::temp_dir().join(format!("thermal_e2e_{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&work);
    std::fs::create_dir_all(&work).unwrap();
    let shots: Vec<Value> = truth["calibration"]
        .as_array()
        .unwrap()
        .iter()
        .map(|rec| {
            let names: Vec<&str> = rec["gun"]
                .as_object()
                .unwrap()
                .keys()
                .map(String::as_str)
                .collect();
            json!({
                "file": rec["file"],
                "polygons": names.iter().map(|n| rec["polygons"][n].clone()).collect::<Vec<_>>(),
                "gun": names.iter().map(|n| rec["gun"][n].clone()).collect::<Vec<_>>(),
            })
        })
        .collect();
    let env = json!({
        "calibration": truth["calibration"].as_array().unwrap().iter().map(|r| flight.join("calibration").join(r["file"].as_str().unwrap()).to_string_lossy().to_string()).collect::<Vec<_>>(),
        "shots": shots,
        "expected": expected["events"],
        "survey": flight.join("survey").to_string_lossy(),
        "output": work.join("calibrated").to_string_lossy(),
        "project": work.join("flight-e2e.calib.json").to_string_lossy(),
        "report": work.join("fit_report.html").to_string_lossy(),
        "csv": work.join("inspect_table.csv").to_string_lossy(),
    });
    let env_file = work.join("env.json");
    std::fs::write(&env_file, serde_json::to_string_pretty(&env).unwrap()).unwrap();
    let report = work.join("report.json");
    let script = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../tests/e2e/flow.js")
        .canonicalize()
        .unwrap();
    let exe = env!("CARGO_BIN_EXE_thermal-calibration-app");

    let mut dev_server = ensure_dev_server();
    let launcher: Vec<String> = std::env::var("THERMAL_E2E_LAUNCH")
        .map(|s| s.split_whitespace().map(str::to_string).collect())
        .unwrap_or_default();
    let mut cmd = if launcher.is_empty() {
        Command::new(exe)
    } else {
        let mut c = Command::new(&launcher[0]);
        c.args(&launcher[1..]).arg(exe);
        c
    };
    cmd.args([
        "--e2e-script",
        &script.to_string_lossy(),
        "--e2e-env-file",
        &env_file.to_string_lossy(),
        "--e2e-report",
        &report.to_string_lossy(),
        "--e2e-timeout",
        "150",
    ]);
    let status = cmd.status().expect("start the app");
    if let Some(child) = dev_server.as_mut() {
        let _ = child.kill();
        let _ = child.wait();
    }
    let text = std::fs::read_to_string(&report)
        .unwrap_or_else(|e| panic!("no report written ({e}); app exit {status}"));
    let rep: Value = serde_json::from_str(&text).unwrap();
    for e in rep["entries"].as_array().unwrap() {
        match e["kind"].as_str().unwrap_or("") {
            "step" => eprintln!(
                "  [{}] {}{}",
                if e["ok"] == true { "ok" } else { "FAIL" },
                e["name"],
                if e["ok"] == true {
                    String::new()
                } else {
                    format!("  {}", e["details"])
                }
            ),
            kind => eprintln!("  [{kind}] {}", e["text"]),
        }
    }
    assert!(
        rep["ok"] == true,
        "e2e failed: {} steps, {} failed, {} errors (report: {})",
        rep["steps"],
        rep["failed"],
        rep["errors"],
        report.display()
    );
    assert!(status.success(), "app exit status {status}");
    // what the run wrote
    let csv = std::fs::read_to_string(work.join("calibrated/processing_log.csv"))
        .expect("processing_log.csv");
    assert_eq!(csv.lines().count(), 7, "header + 6 rows");
    assert!(work.join("calibrated/survey_0_calibrated.tif").exists());
    assert!(work.join("flight-e2e.calib.json").exists() && work.join("fit_report.html").exists());
    let table = std::fs::read_to_string(work.join("inspect_table.csv")).expect("inspect_table.csv");
    assert_eq!(table.lines().count(), 2, "header + P1: {table}");
    assert!(
        table
            .lines()
            .nth(1)
            .unwrap()
            .starts_with("survey_0_calibrated.tif,P1,"),
        "{table}"
    );
    let saved: Value =
        serde_json::from_str(&std::fs::read_to_string(work.join("flight-e2e.calib.json")).unwrap())
            .unwrap();
    assert_eq!(saved["calibration_images"].as_array().unwrap().len(), 3);
    assert_eq!(
        saved["calibration_images"][0]["targets"][0]["name"],
        "black"
    );
    let _ = std::fs::remove_dir_all(&work);
}
