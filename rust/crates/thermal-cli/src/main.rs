//! `thermal-calibration --project flight.calib.json [--survey DIR] [--output DIR] [--filter TEXT]`
//!
//! Processes the survey images of a project file without a window, exactly like the GUI's Run:
//! pre-flight check, calibrated GeoTIFFs, processing_log.csv, processing_summary.txt and a copy of
//! the project next to the outputs. Exit code 0 when every image was written, 1 otherwise.

use std::path::PathBuf;
use std::process::ExitCode;
use thermal_core::{TOOL_NAME, VERSION};

const USAGE: &str = "\
usage: thermal-calibration --project FILE [--survey DIR] [--output DIR] [--filter TEXT]

  --project FILE   the *.calib.json project to run (saved by the GUI or next to earlier outputs)
  --survey DIR     survey folder; default: the one stored in the project
  --output DIR     output folder; default: the one stored in the project, else <survey>/calibrated
  --filter TEXT    only files whose name contains TEXT; default: the project's filter
  --version        print the version";

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let (mut project, mut survey, mut output, mut filter): (
        Option<PathBuf>,
        Option<PathBuf>,
        Option<PathBuf>,
        Option<String>,
    ) = (None, None, None, None);
    let mut i = 0;
    while i < args.len() {
        let value = args.get(i + 1).cloned();
        match args[i].as_str() {
            "--project" | "-p" => {
                project = value.map(PathBuf::from);
                i += 2;
            }
            "--survey" => {
                survey = value.map(PathBuf::from);
                i += 2;
            }
            "--output" => {
                output = value.map(PathBuf::from);
                i += 2;
            }
            "--filter" => {
                filter = value;
                i += 2;
            }
            "--run" => i += 1, // accepted for parity with the Python command line
            "--version" | "-V" => {
                println!("{TOOL_NAME} {VERSION}");
                return ExitCode::SUCCESS;
            }
            "-h" | "--help" => {
                println!("{USAGE}");
                return ExitCode::SUCCESS;
            }
            other => {
                eprintln!("unknown argument: {other}\n{USAGE}");
                return ExitCode::from(2);
            }
        }
    }
    let Some(project) = project else {
        eprintln!("{USAGE}");
        return ExitCode::from(2);
    };
    let mut log = |s: &str| println!("{s}");
    match thermal_core::project::run_project_headless(
        &project,
        survey.as_deref(),
        output.as_deref(),
        filter.as_deref(),
        &mut log,
    ) {
        Ok(r) if r.errors == 0 && !r.cancelled => ExitCode::SUCCESS,
        Ok(_) => ExitCode::from(1),
        Err(e) => {
            eprintln!("{e}");
            ExitCode::from(1)
        }
    }
}
