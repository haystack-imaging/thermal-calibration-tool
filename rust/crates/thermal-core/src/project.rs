//! `*.calib.json` project files (the same schema as the Python tool) and the headless run.

use crate::batch::{collect_survey_files, preflight, run_batch, BatchJob, BatchResult, Callbacks};
use crate::fit::recompute_fit;
use crate::model::{
    isoformat, relative_path, CalibrationEvent, CalibrationTarget, EmissivitySettings, Mode,
};
use crate::raster::load_image;
use crate::roi::extract_roi_statistics;
use crate::timestamp::extract_timestamp;
use crate::{Result, DEFAULT_ROI_INSET_PX, PROJECT_EXT, VERSION};
use serde_json::{json, Value};
use std::path::{Path, PathBuf};

/// The project as JSON. Image paths are stored absolute and, when `project_dir` is given, also
/// relative to it, so a folder that moves as a whole still opens.
#[allow(clippy::too_many_arguments)]
pub fn events_to_project(
    events: &[CalibrationEvent],
    mode: Mode,
    single_point: bool,
    emissivity: &EmissivitySettings,
    roi_inset_px: u32,
    survey: Option<Value>,
    title: &str,
    project_dir: Option<&Path>,
) -> Value {
    json!({
        "tool": "thermal_calibration_tool",
        "version": VERSION,
        "saved": isoformat(chrono::Local::now().naive_local()),
        "title": title,
        "mode": mode.as_str(),
        "msi_single_point": single_point,
        "roi_inset_px": roi_inset_px,
        "emissivity": emissivity.to_json(),
        "calibration_images": events.iter().map(|e| e.to_json(project_dir)).collect::<Vec<_>>(),
        "survey": survey_with_relative_folders(survey.unwrap_or_else(|| json!({})), project_dir),
    })
}

/// The survey settings with `folder_relative` and `output_folder_relative` (relative to the project
/// folder, with "/"), so a folder that moves as a whole with its project still opens.
pub fn survey_with_relative_folders(mut survey: Value, project_dir: Option<&Path>) -> Value {
    let (Some(dir), Some(obj)) = (project_dir, survey.as_object_mut()) else {
        return survey;
    };
    for key in ["folder", "output_folder"] {
        let rel_key = format!("{key}_relative");
        let rel = obj
            .get(key)
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
            .and_then(|s| relative_path(Path::new(s), dir));
        match rel {
            Some(r) => {
                obj.insert(rel_key, Value::String(r));
            }
            None => {
                obj.remove(&rel_key);
            }
        }
    }
    survey
}

/// `rel` ("/" or "\\" separated) below `dir`, with "." and ".." resolved.
fn join_relative(dir: &Path, rel: &str) -> PathBuf {
    let mut p = dir.to_path_buf();
    for part in rel
        .split(['/', '\\'])
        .filter(|s| !s.is_empty() && *s != ".")
    {
        if part == ".." {
            p.pop();
        } else {
            p.push(part);
        }
    }
    p
}

/// The survey settings after the project moved: when the stored survey folder does not exist but the
/// one relative to the project folder does, both folders are taken relative to the project folder.
pub fn resolve_survey_folders(survey: &Value, project_dir: Option<&Path>) -> Value {
    let mut sv = survey.clone();
    let get = |k: &str| {
        survey
            .get(k)
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string()
    };
    let (folder, rel) = (get("folder"), get("folder_relative"));
    if let Some(dir) = project_dir {
        if !rel.is_empty() && (folder.is_empty() || !Path::new(&folder).is_dir()) {
            let alt = join_relative(dir, &rel);
            if alt.is_dir() {
                sv["folder"] = Value::String(alt.to_string_lossy().to_string());
                let out_rel = get("output_folder_relative");
                if !out_rel.is_empty() {
                    sv["output_folder"] =
                        Value::String(join_relative(dir, &out_rel).to_string_lossy().to_string());
                }
            }
        }
    }
    sv
}

/// `<title>.calib.json`, the file name a project is saved under next to the outputs.
pub fn project_filename(project: Option<&Value>) -> String {
    let title = project
        .and_then(|p| p.get("title"))
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .unwrap_or("calibration_project");
    let re = regex::Regex::new(r"[^\w.-]+").expect("static regex");
    let safe = re.replace_all(title, "_").to_string();
    let safe: String = safe.trim_matches('_').chars().take(80).collect();
    let safe = safe.trim_end_matches('_');
    let safe = if safe.is_empty() {
        "calibration_project"
    } else {
        safe
    };
    format!("{safe}{PROJECT_EXT}")
}

/// Rebuild events from a project: re-reads the images and recomputes the ROI statistics. Missing
/// images are looked for relative to the project folder.
pub fn events_from_project(
    project: &Value,
    project_dir: Option<&Path>,
    roi_inset_px: u32,
) -> (Vec<CalibrationEvent>, Vec<String>) {
    let mut events = Vec::new();
    let mut problems = Vec::new();
    for d in project
        .get("calibration_images")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default()
    {
        let mut path = PathBuf::from(d.get("path").and_then(Value::as_str).unwrap_or(""));
        if !path.exists() {
            if let Some(dir) = project_dir {
                let mut alts = Vec::new();
                if let Some(rel) = d.get("path_relative").and_then(Value::as_str) {
                    // "/" from any system, "\\" from older projects saved on Windows
                    let mut alt = dir.to_path_buf();
                    for part in rel
                        .split(['/', '\\'])
                        .filter(|p| !p.is_empty() && *p != ".")
                    {
                        alt.push(part);
                    }
                    alts.push(alt);
                }
                if let Some(name) = path.file_name() {
                    alts.push(dir.join(name));
                }
                if let Some(alt) = alts.into_iter().find(|a| a.exists()) {
                    path = alt;
                }
            }
        }
        if !path.exists() {
            problems.push(format!("missing calibration image: {}", path.display()));
            continue;
        }
        let ts = extract_timestamp(&path);
        let targets: Vec<CalibrationTarget> = d
            .get("targets")
            .and_then(Value::as_array)
            .map(|a| a.iter().map(CalibrationTarget::from_json).collect())
            .unwrap_or_default();
        let when = ts
            .value
            .unwrap_or_else(|| chrono::Local::now().naive_local());
        let mut ev =
            CalibrationEvent::with_targets(path.clone(), when, &ts.source, &ts.tz, targets);
        let img = match load_image(&path) {
            Ok(i) => i,
            Err(e) => {
                problems.push(e.to_string());
                continue;
            }
        };
        for t in &mut ev.targets {
            if t.has_polygon() {
                let st = extract_roi_statistics(&img, &t.polygon, roi_inset_px);
                t.drone_mean = Some(st.mean);
                t.drone_std = Some(st.std);
                t.pixel_count = st.count;
                t.inset_applied = st.inset_applied;
            }
        }
        events.push(ev);
    }
    events.sort_by_key(|e| e.timestamp);
    (events, problems)
}

/// A project file turned into a job: images re-read, targets measured, fits recomputed.
pub struct LoadedProject {
    pub project: Value,
    pub job: BatchJob,
    pub problems: Vec<String>,
    /// (file name, warning) for calibration images whose fit failed.
    pub fit_warnings: Vec<(String, String)>,
}

pub fn load_project_job(
    project_path: &Path,
    survey_folder: Option<&Path>,
    output_folder: Option<&Path>,
    name_filter: Option<&str>,
) -> Result<LoadedProject> {
    let text = std::fs::read_to_string(project_path)?;
    let project: Value = serde_json::from_str(&text)?;
    let mode = Mode::parse(
        project
            .get("mode")
            .and_then(Value::as_str)
            .unwrap_or("thermal"),
    );
    let single = project
        .get("msi_single_point")
        .and_then(Value::as_bool)
        .unwrap_or(false)
        && mode == Mode::Msi;
    let em = EmissivitySettings::from_json(project.get("emissivity").unwrap_or(&Value::Null));
    let inset = project
        .get("roi_inset_px")
        .and_then(Value::as_u64)
        .map_or(DEFAULT_ROI_INSET_PX, |v| v as u32);
    let project_dir = std::path::absolute(project_path)
        .ok()
        .and_then(|p| p.parent().map(Path::to_path_buf));
    let (mut events, problems) = events_from_project(&project, project_dir.as_deref(), inset);
    let mut fit_warnings = Vec::new();
    for e in &mut events {
        if let Some(w) = recompute_fit(e, single, mode.is_thermal(), Some(&em)) {
            fit_warnings.push((e.file_name(), w));
        }
    }
    let sv = resolve_survey_folders(
        &project.get("survey").cloned().unwrap_or_else(|| json!({})),
        project_dir.as_deref(),
    );
    let folder: Option<PathBuf> = survey_folder
        .map(Path::to_path_buf)
        .or_else(|| {
            sv.get("folder")
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())
                .map(PathBuf::from)
        })
        .or_else(|| {
            sv.get("files")
                .and_then(Value::as_array)
                .and_then(|a| a.first())
                .and_then(Value::as_str)
                .and_then(|f| Path::new(f).parent().map(Path::to_path_buf))
        });
    let filter = name_filter.map(str::to_string).unwrap_or_else(|| {
        sv.get("filter")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string()
    });
    let cal_paths: Vec<PathBuf> = events.iter().map(|e| e.image_path.clone()).collect();
    let files = collect_survey_files(folder.as_deref(), &filter, &cal_paths);
    let out = output_folder
        .map(Path::to_path_buf)
        .or_else(|| {
            sv.get("output_folder")
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())
                .map(PathBuf::from)
        })
        .unwrap_or_else(|| {
            folder
                .clone()
                .unwrap_or_else(|| PathBuf::from("."))
                .join("calibrated")
        });
    let mut job = BatchJob::new(mode, single, events, files, out);
    job.emissivity = em;
    job.roi_inset_px = inset;
    job.copy_metadata = sv
        .get("copy_metadata")
        .and_then(Value::as_bool)
        .unwrap_or(true);
    job.skip_existing = sv
        .get("skip_existing")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    job.name_filter = filter;
    job.project = Some(project.clone());
    Ok(LoadedProject {
        project,
        job,
        problems,
        fit_warnings,
    })
}

/// CLI entry: load a project file and process its survey images without a window.
pub fn run_project_headless(
    project_path: &Path,
    survey_folder: Option<&Path>,
    output_folder: Option<&Path>,
    name_filter: Option<&str>,
    log: &mut dyn FnMut(&str),
) -> Result<BatchResult> {
    let mut loaded = load_project_job(project_path, survey_folder, output_folder, name_filter)?;
    for p in &loaded.problems {
        log(&format!("WARNING: {p}"));
    }
    for (f, w) in &loaded.fit_warnings {
        log(&format!("WARNING: {f}: {w}"));
    }
    let rep = preflight(&loaded.job, None);
    log(&rep.text());
    if rep.blocking() {
        return Err("pre-flight check failed".into());
    }
    loaded.job.survey_files = rep.files.clone();
    let mut progress = |_: usize, _: usize, _: &str| {};
    let cancelled = || false;
    let mut cb = Callbacks {
        log,
        progress: &mut progress,
        is_cancelled: &cancelled,
        on_row: None,
    };
    run_batch(&loaded.job, &mut cb, Some(&rep.timestamps))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn survey_folders_move_with_the_project() {
        let dir = std::env::temp_dir().join(format!("thermal_core_moved_{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join("survey")).unwrap();
        let saved = survey_with_relative_folders(
            json!({"folder": dir.join("survey").to_string_lossy(), "output_folder": dir.join("calibrated").to_string_lossy()}),
            Some(&dir),
        );
        assert_eq!(saved["folder_relative"], "survey");
        assert_eq!(saved["output_folder_relative"], "calibrated");
        // the folder was made on another computer: its absolute paths do not exist here
        let moved = json!({"folder": "/nonexistent/flight/survey", "folder_relative": "survey",
                           "output_folder": "/nonexistent/flight/calibrated", "output_folder_relative": "calibrated"});
        let sv = resolve_survey_folders(&moved, Some(&dir));
        assert_eq!(
            sv["folder"].as_str().unwrap(),
            dir.join("survey").to_string_lossy()
        );
        assert_eq!(
            sv["output_folder"].as_str().unwrap(),
            dir.join("calibrated").to_string_lossy()
        );
        // an existing absolute folder wins, and nothing changes without a project folder
        let here = json!({"folder": dir.to_string_lossy(), "folder_relative": "survey", "output_folder": "/x", "output_folder_relative": "calibrated"});
        assert_eq!(resolve_survey_folders(&here, Some(&dir)), here);
        assert_eq!(resolve_survey_folders(&moved, None), moved);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn project_file_names_are_safe() {
        assert_eq!(project_filename(None), "calibration_project.calib.json");
        assert_eq!(
            project_filename(Some(&json!({"title": "Wheat June / 2026!"}))),
            "Wheat_June_2026.calib.json"
        );
        assert_eq!(
            project_filename(Some(&json!({"title": "___"}))),
            "calibration_project.calib.json"
        );
    }

    #[test]
    fn project_round_trip_keeps_targets() {
        let t0 = chrono::NaiveDateTime::parse_from_str("2026-06-01 10:00:00", "%Y-%m-%d %H:%M:%S")
            .unwrap();
        let mut ev = CalibrationEvent::new("/nonexistent/a.tif", t0, "XMP", "+02:00");
        ev.targets[0].polygon = vec![(1, 1), (5, 1), (5, 5), (1, 5)];
        ev.targets[0].ground_truth = Some(15.5);
        let p = events_to_project(
            &[ev],
            Mode::Thermal,
            false,
            &EmissivitySettings::default(),
            3,
            None,
            "t",
            Some(Path::new("/nonexistent")),
        );
        assert_eq!(p["calibration_images"][0]["path_relative"], "a.tif");
        assert_eq!(
            p["calibration_images"][0]["targets"][0]["polygon"][2],
            json!([5, 5])
        );
        assert_eq!(
            p["calibration_images"][0]["timestamp"],
            "2026-06-01T10:00:00"
        );
        let t = CalibrationTarget::from_json(&p["calibration_images"][0]["targets"][0]);
        assert_eq!(t.polygon.len(), 4);
        assert_eq!(t.ground_truth, Some(15.5));
        let (events, problems) = events_from_project(&p, None, 3);
        assert!(events.is_empty() && problems[0].starts_with("missing calibration image"));
    }
}
