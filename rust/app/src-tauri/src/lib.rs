//! The Tauri shell: application state and the commands the web front-end calls. All numbers come
//! from `thermal-core`; this file only holds state, converts it to JSON and runs the batch in a thread.

use base64::Engine;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use tauri::{AppHandle, Emitter, Manager, State};
use thermal_core::batch::{collect_survey_files, preflight, run_batch, BatchJob, Callbacks, Row};
use thermal_core::fit::recompute_fit;
use thermal_core::model::*;
use thermal_core::project::{events_from_project, events_to_project, project_filename};
use thermal_core::raster::{inspect_image, load_image, Raster};
use thermal_core::roi::{extract_roi_statistics, mask_outlines, order_corners, polygon_mask};
use thermal_core::timestamp::extract_timestamp;
use thermal_core::{DEFAULT_ROI_INSET_PX, MIN_ROI_PIXELS, TOOL_NAME, VERSION};

// ----------------------------------------------------------------------------- state

#[derive(Clone, Serialize, Deserialize)]
pub struct SurveySettings {
    pub folder: String,
    pub output_folder: String,
    pub filter: String,
    pub copy_metadata: bool,
    pub skip_existing: bool,
}

impl Default for SurveySettings {
    fn default() -> Self {
        SurveySettings {
            folder: String::new(),
            output_folder: String::new(),
            filter: String::new(),
            copy_metadata: true,
            skip_existing: false,
        }
    }
}

#[derive(Clone, Serialize, Deserialize)]
pub struct Settings {
    pub mode: Mode,
    pub single_point: bool,
    pub emissivity: EmissivitySettings,
    pub roi_inset_px: u32,
}

impl Default for Settings {
    fn default() -> Self {
        Settings {
            mode: Mode::Thermal,
            single_point: true,
            emissivity: EmissivitySettings::default(),
            roi_inset_px: DEFAULT_ROI_INSET_PX,
        }
    }
}

#[derive(Default)]
pub struct Inner {
    events: Vec<CalibrationEvent>,
    cache: HashMap<PathBuf, Arc<Raster>>,
    settings: Settings,
    survey: SurveySettings,
    title: String,
    project_path: Option<PathBuf>,
    survey_timestamps: HashMap<PathBuf, Timestamp>,
    last_output: Option<PathBuf>,
    cancel: Arc<AtomicBool>,
    running: bool,
    /// Undo history: the targets and fits of every shot before each change (newest last).
    undo: Vec<Vec<CalibrationEvent>>,
    redo: Vec<Vec<CalibrationEvent>>,
}

pub struct AppState(pub Mutex<Inner>);

impl Inner {
    fn single_point(&self) -> bool {
        self.settings.mode == Mode::Msi && self.settings.single_point
    }

    fn raster(&mut self, path: &Path) -> Result<Arc<Raster>, String> {
        if let Some(r) = self.cache.get(path) {
            return Ok(r.clone());
        }
        let r = Arc::new(load_image(path).map_err(|e| e.to_string())?);
        self.cache.insert(path.to_path_buf(), r.clone());
        Ok(r)
    }

    /// ROI statistics of every drawn target of one event (after the inset or the image changed).
    fn remeasure(&mut self, index: usize) -> Result<(), String> {
        let path = self.events[index].image_path.clone();
        let img = self.raster(&path)?;
        let inset = self.settings.roi_inset_px;
        for t in &mut self.events[index].targets {
            if t.has_polygon() {
                let st = extract_roi_statistics(&img, &t.polygon, inset);
                t.drone_mean = Some(st.mean);
                t.drone_std = Some(st.std);
                t.pixel_count = st.count;
                t.inset_applied = st.inset_applied;
            } else {
                t.drone_mean = None;
                t.drone_std = None;
                t.pixel_count = 0;
            }
        }
        Ok(())
    }

    fn refit(&mut self, index: usize) {
        let (single, thermal, em) = (
            self.single_point(),
            self.settings.mode.is_thermal(),
            self.settings.emissivity.clone(),
        );
        recompute_fit(&mut self.events[index], single, thermal, Some(&em));
    }

    fn refit_all(&mut self) {
        for i in 0..self.events.len() {
            self.refit(i);
        }
    }

    fn remeasure_all(&mut self) -> Result<(), String> {
        for i in 0..self.events.len() {
            self.remeasure(i)?;
        }
        self.refit_all();
        Ok(())
    }

    /// Remember the shots as they are, before a change that the user may want to take back.
    fn snapshot(&mut self) {
        self.undo.push(self.events.clone());
        if self.undo.len() > 50 {
            self.undo.remove(0);
        }
        self.redo.clear();
    }

    fn sort_events(&mut self) {
        self.events.sort_by_key(|e| e.timestamp);
    }

    fn job(&self) -> BatchJob {
        let folder = (!self.survey.folder.is_empty()).then(|| PathBuf::from(&self.survey.folder));
        let cal: Vec<PathBuf> = self.events.iter().map(|e| e.image_path.clone()).collect();
        let files = collect_survey_files(folder.as_deref(), &self.survey.filter, &cal);
        let out = if self.survey.output_folder.is_empty() {
            folder
                .clone()
                .unwrap_or_else(|| PathBuf::from("."))
                .join("calibrated")
        } else {
            PathBuf::from(&self.survey.output_folder)
        };
        let mut job = BatchJob::new(
            self.settings.mode,
            self.single_point(),
            self.events.clone(),
            files,
            out,
        );
        job.emissivity = self.settings.emissivity.clone();
        job.roi_inset_px = self.settings.roi_inset_px;
        job.copy_metadata = self.survey.copy_metadata;
        job.skip_existing = self.survey.skip_existing;
        job.name_filter = self.survey.filter.clone();
        job.project = Some(self.project_json(None));
        job
    }

    fn project_json(&self, project_dir: Option<&Path>) -> serde_json::Value {
        let survey = serde_json::json!({
            "folder": self.survey.folder, "output_folder": self.survey.output_folder, "filter": self.survey.filter,
            "copy_metadata": self.survey.copy_metadata, "skip_existing": self.survey.skip_existing,
        });
        events_to_project(
            &self.events,
            self.settings.mode,
            self.settings.single_point,
            &self.settings.emissivity,
            self.settings.roi_inset_px,
            Some(survey),
            &self.title,
            project_dir,
        )
    }

    fn scan_survey_timestamps(&mut self) {
        let job = self.job();
        let mut fresh: HashMap<PathBuf, Timestamp> = HashMap::new();
        for p in &job.survey_files {
            let ts = self
                .survey_timestamps
                .remove(p)
                .unwrap_or_else(|| extract_timestamp(p));
            fresh.insert(p.clone(), ts);
        }
        self.survey_timestamps = fresh;
    }
}

// ----------------------------------------------------------------------------- DTOs

#[derive(Serialize)]
pub struct TargetDto {
    pub name: String,
    pub label: String,
    pub color: String,
    pub polygon: Vec<[i32; 2]>,
    /// Outline(s) of the eroded region actually used, for drawing; empty when no inset was possible.
    pub inner: Vec<Vec<[i32; 2]>>,
    pub mean: Option<f64>,
    pub std: Option<f64>,
    pub pixels: usize,
    pub ground_truth: Option<f64>,
    pub inset_applied: bool,
    pub complete: bool,
    pub warn_small: bool,
}

#[derive(Serialize)]
pub struct EventDto {
    pub index: usize,
    pub path: String,
    pub file_name: String,
    pub timestamp: String,
    pub timestamp_source: String,
    pub timestamp_tz: String,
    pub targets: Vec<TargetDto>,
    pub fit: Option<FitResult>,
    pub fit_warnings: Vec<String>,
    /// Finite range of the image, for the extrapolation warning and the display stretch.
    pub range: Option<[f64; 2]>,
}

#[derive(Serialize)]
pub struct TimelinePoint {
    pub file: String,
    pub t: String,
    pub source: String,
    pub outside: bool,
}

#[derive(Serialize)]
pub struct StateDto {
    pub tool_name: String,
    pub version: String,
    pub title: String,
    pub project_path: Option<String>,
    pub settings: Settings,
    pub survey: SurveySettings,
    pub events: Vec<EventDto>,
    pub survey_timeline: Vec<TimelinePoint>,
    pub last_output: Option<String>,
    pub running: bool,
    pub can_undo: bool,
    pub can_redo: bool,
}

#[derive(Serialize)]
pub struct AddResult {
    pub added: usize,
    pub rejected: Vec<String>,
    pub duplicates: Vec<String>,
    pub state: StateDto,
}

#[derive(Serialize)]
pub struct ImageData {
    pub width: usize,
    pub height: usize,
    /// Little-endian float32 pixels, base64; NaN = nodata.
    pub data_base64: String,
    pub lo: f64,
    pub hi: f64,
}

#[derive(Serialize)]
pub struct PreflightDto {
    pub lines: Vec<(String, String)>,
    pub files: Vec<String>,
    pub blocking: bool,
    pub state: StateDto,
}

#[derive(Serialize, Clone)]
pub struct BatchDone {
    pub ok: usize,
    pub errors: usize,
    pub skipped: usize,
    pub total: usize,
    pub cancelled: bool,
    pub output: String,
    pub error: Option<String>,
}

fn fit_warnings(ev: &CalibrationEvent, thermal: bool, range: Option<[f64; 2]>) -> Vec<String> {
    let mut w = Vec::new();
    let Some(f) = &ev.fit else { return w };
    if f.single_point {
        return w;
    }
    if f.n == 2 {
        w.push("2 targets: R² = 1 by construction and no fit uncertainty can be estimated. A third target would fix that.".into());
    }
    if f.slope <= 0.0 {
        w.push(
            "The slope is zero or negative: a reading or a polygon is probably on the wrong panel."
                .into(),
        );
    } else if thermal && (f.slope < 0.5 || f.slope > 2.0) {
        w.push(format!(
            "Slope {:.2} is far from 1: check the readings and the panels.",
            f.slope
        ));
    }
    if let Some([lo, hi]) = range {
        let span = (f.x_max - f.x_min).max(1e-6);
        if lo < f.x_min - 0.1 * span || hi > f.x_max + 0.1 * span {
            w.push(format!(
                "Targets span {:.1} to {:.1}; this scene reaches {:.1} to {:.1}. Pixels outside the span are extrapolated.",
                f.x_min, f.x_max, lo, hi
            ));
        }
    }
    w
}

fn event_dto(inner: &mut Inner, index: usize) -> EventDto {
    let ev = inner.events[index].clone();
    let (w, h, range) = match inner.raster(&ev.image_path) {
        Ok(r) => (
            r.width,
            r.height,
            r.finite_stats().map(|(_, lo, hi)| [lo, hi]),
        ),
        Err(_) => (0, 0, None),
    };
    let inset = inner.settings.roi_inset_px;
    let targets = ev
        .targets
        .iter()
        .enumerate()
        .map(|(i, t)| {
            let inner_paths = if t.has_polygon() && w > 0 {
                let (mask, applied) = polygon_mask(&t.polygon, w, h, inset);
                if applied {
                    mask_outlines(&mask)
                        .into_iter()
                        .map(|p| p.into_iter().map(|(x, y)| [x, y]).collect())
                        .collect()
                } else {
                    Vec::new()
                }
            } else {
                Vec::new()
            };
            TargetDto {
                name: t.name.clone(),
                label: target_label(i, &t.name),
                color: t.color.clone(),
                polygon: t.polygon.iter().map(|&(x, y)| [x, y]).collect(),
                inner: inner_paths,
                mean: t.drone_mean,
                std: t.drone_std,
                pixels: t.pixel_count,
                ground_truth: t.ground_truth,
                inset_applied: t.inset_applied,
                complete: t.is_complete(),
                warn_small: t.has_polygon() && (t.pixel_count < MIN_ROI_PIXELS || !t.inset_applied),
            }
        })
        .collect();
    EventDto {
        index,
        path: ev.image_path.to_string_lossy().to_string(),
        file_name: ev.file_name(),
        timestamp: isoformat(ev.timestamp),
        timestamp_source: ev.timestamp_source.clone(),
        timestamp_tz: ev.timestamp_tz.clone(),
        fit_warnings: fit_warnings(&ev, inner.settings.mode.is_thermal(), range),
        fit: ev.fit.clone(),
        targets,
        range,
    }
}

fn state_dto(inner: &mut Inner) -> StateDto {
    let events: Vec<EventDto> = (0..inner.events.len())
        .map(|i| event_dto(inner, i))
        .collect();
    let fitted: Vec<&CalibrationEvent> = thermal_core::interp::fitted_sorted(&inner.events);
    let (t0, t1) = (
        fitted.first().map(|e| e.timestamp),
        fitted.last().map(|e| e.timestamp),
    );
    let mut timeline: Vec<TimelinePoint> = inner
        .survey_timestamps
        .iter()
        .filter_map(|(p, ts)| {
            let v = ts.value?;
            let outside = fitted.len() > 1 && (Some(v) < t0 || Some(v) > t1);
            Some(TimelinePoint {
                file: thermal_core::file_name(p),
                t: isoformat(v),
                source: ts.source.clone(),
                outside,
            })
        })
        .collect();
    timeline.sort_by(|a, b| a.t.cmp(&b.t));
    StateDto {
        tool_name: TOOL_NAME.into(),
        version: VERSION.into(),
        title: inner.title.clone(),
        project_path: inner
            .project_path
            .as_ref()
            .map(|p| p.to_string_lossy().to_string()),
        settings: inner.settings.clone(),
        survey: inner.survey.clone(),
        events,
        survey_timeline: timeline,
        last_output: inner
            .last_output
            .as_ref()
            .map(|p| p.to_string_lossy().to_string()),
        running: inner.running,
        can_undo: !inner.undo.is_empty(),
        can_redo: !inner.redo.is_empty(),
    }
}

type Shared<'a> = State<'a, AppState>;

fn lock(state: &AppState) -> std::sync::MutexGuard<'_, Inner> {
    state.0.lock().unwrap_or_else(|e| e.into_inner())
}

// ----------------------------------------------------------------------------- commands

#[tauri::command]
fn get_state(state: Shared) -> StateDto {
    state_dto(&mut lock(&state))
}

#[tauri::command]
fn add_calibration_images(paths: Vec<String>, state: Shared) -> Result<AddResult, String> {
    let mut inner = lock(&state);
    let mut existing: Vec<PathBuf> = inner
        .events
        .iter()
        .map(|e| std::path::absolute(&e.image_path).unwrap_or_else(|_| e.image_path.clone()))
        .collect();
    let n_targets = inner
        .events
        .iter()
        .map(|e| e.targets.len())
        .max()
        .unwrap_or(2);
    let (mut rejected, mut duplicates, mut added) = (Vec::new(), Vec::new(), 0usize);
    let mut fresh: Vec<CalibrationEvent> = Vec::new();
    for p in paths {
        let path = PathBuf::from(&p);
        let abs = std::path::absolute(&path).unwrap_or_else(|_| path.clone());
        if existing.contains(&abs) {
            duplicates.push(thermal_core::file_name(&path));
            continue;
        }
        let info = inspect_image(&path);
        if !info.error.is_empty() || !info.is_single_band() {
            rejected.push(format!(
                "{}: {}",
                thermal_core::file_name(&path),
                info.describe()
            ));
            continue;
        }
        if inner.settings.mode.is_thermal() && info.is_8bit() {
            rejected.push(format!(
                "{}: 8-bit integers are a stretched preview, not temperatures",
                thermal_core::file_name(&path)
            ));
            continue;
        }
        let ts = extract_timestamp(&path);
        let when = ts.value.unwrap_or_else(chrono_now);
        let mut ev = CalibrationEvent::new(path.clone(), when, &ts.source, &ts.tz);
        while ev.targets.len() < n_targets {
            ev.add_target();
        }
        existing.push(abs);
        fresh.push(ev);
        added += 1;
    }
    if !fresh.is_empty() {
        inner.snapshot();
        inner.events.extend(fresh);
        inner.sort_events();
    }
    Ok(AddResult {
        added,
        rejected,
        duplicates,
        state: state_dto(&mut inner),
    })
}

fn chrono_now() -> chrono::NaiveDateTime {
    chrono::Local::now().naive_local()
}

#[tauri::command]
fn remove_calibration_image(index: usize, state: Shared) -> Result<StateDto, String> {
    let mut inner = lock(&state);
    if index >= inner.events.len() {
        return Err("no such image".into());
    }
    inner.snapshot();
    let ev = inner.events.remove(index);
    inner.cache.remove(&ev.image_path);
    Ok(state_dto(&mut inner))
}

#[tauri::command]
fn load_image_data(index: usize, state: Shared) -> Result<ImageData, String> {
    let mut inner = lock(&state);
    let path = inner
        .events
        .get(index)
        .ok_or("no such image")?
        .image_path
        .clone();
    let r = inner.raster(&path)?;
    let (lo, hi) = r
        .finite_stats()
        .map(|(_, lo, hi)| (lo, hi))
        .unwrap_or((0.0, 1.0));
    let mut bytes = Vec::with_capacity(r.data.len() * 4);
    for v in &r.data {
        bytes.extend_from_slice(&v.to_le_bytes());
    }
    Ok(ImageData {
        width: r.width,
        height: r.height,
        data_base64: base64::engine::general_purpose::STANDARD.encode(bytes),
        lo,
        hi,
    })
}

#[tauri::command]
fn set_polygon(
    event: usize,
    target: usize,
    polygon: Vec<[i32; 2]>,
    state: Shared,
) -> Result<StateDto, String> {
    let mut inner = lock(&state);
    inner.snapshot();
    let t = inner
        .events
        .get_mut(event)
        .and_then(|e| e.targets.get_mut(target))
        .ok_or("no such target")?;
    let pts: Vec<(i32, i32)> = polygon.iter().map(|p| (p[0], p[1])).collect();
    // four clicks in any order make a rectangle (never a bow-tie); more corners keep the drawn order
    t.polygon = if pts.len() == 4 {
        order_corners(&pts)
    } else {
        pts
    };
    inner.remeasure(event)?;
    inner.refit(event);
    Ok(state_dto(&mut inner))
}

#[tauri::command]
fn clear_polygon(event: usize, target: usize, state: Shared) -> Result<StateDto, String> {
    let mut inner = lock(&state);
    inner.snapshot();
    let t = inner
        .events
        .get_mut(event)
        .and_then(|e| e.targets.get_mut(target))
        .ok_or("no such target")?;
    t.polygon.clear();
    inner.remeasure(event)?;
    inner.refit(event);
    Ok(state_dto(&mut inner))
}

#[tauri::command]
fn set_ground_truth(
    event: usize,
    target: usize,
    value: Option<f64>,
    state: Shared,
) -> Result<StateDto, String> {
    let mut inner = lock(&state);
    let wanted = value.filter(|v| v.is_finite());
    if inner
        .events
        .get(event)
        .and_then(|e| e.targets.get(target))
        .is_some_and(|t| t.ground_truth != wanted)
    {
        inner.snapshot();
    }
    let t = inner
        .events
        .get_mut(event)
        .and_then(|e| e.targets.get_mut(target))
        .ok_or("no such target")?;
    t.ground_truth = value.filter(|v| v.is_finite());
    inner.refit(event);
    Ok(state_dto(&mut inner))
}

#[tauri::command]
fn add_target(state: Shared) -> Result<StateDto, String> {
    let mut inner = lock(&state);
    if inner.events.is_empty() {
        return Err("Load calibration images first.".into());
    }
    let n = inner
        .events
        .iter()
        .map(|e| e.targets.len())
        .max()
        .unwrap_or(0)
        + 1;
    if n > thermal_core::COLOR_PALETTE.len() {
        return Err(format!(
            "Maximum of {} targets.",
            thermal_core::COLOR_PALETTE.len()
        ));
    }
    inner.snapshot();
    for e in &mut inner.events {
        while e.targets.len() < n {
            e.add_target();
        }
    }
    Ok(state_dto(&mut inner))
}

#[tauri::command]
fn remove_target(index: usize, state: Shared) -> Result<StateDto, String> {
    let mut inner = lock(&state);
    if inner
        .events
        .iter()
        .any(|e| e.targets.len() > 1 && index < e.targets.len())
    {
        inner.snapshot();
    }
    for e in &mut inner.events {
        e.remove_target(index, 1);
    }
    inner.refit_all();
    Ok(state_dto(&mut inner))
}

#[tauri::command]
fn rename_target(index: usize, name: String, state: Shared) -> Result<StateDto, String> {
    let mut inner = lock(&state);
    inner.snapshot();
    let name: String = name.trim().chars().take(16).collect();
    for (i, e) in inner.events.iter_mut().enumerate() {
        if let Some(t) = e.targets.get_mut(index) {
            t.name = if name.is_empty() {
                format!("Target {}", index + 1)
            } else {
                name.clone()
            };
        } else if i == 0 {
            return Err("no such target".into());
        }
    }
    Ok(state_dto(&mut inner))
}

/// Copy the polygons of one shot to every other shot that has none (the camera did not move).
#[tauri::command]
fn copy_polygons(source: usize, state: Shared) -> Result<StateDto, String> {
    let mut inner = lock(&state);
    inner.snapshot();
    let src = inner
        .events
        .get(source)
        .ok_or("no such image")?
        .targets
        .clone();
    for i in 0..inner.events.len() {
        if i == source {
            continue;
        }
        for (t, s) in inner.events[i].targets.iter_mut().zip(&src) {
            if !t.has_polygon() && s.has_polygon() {
                t.polygon = s.polygon.clone();
            }
        }
        inner.remeasure(i)?;
        inner.refit(i);
    }
    Ok(state_dto(&mut inner))
}

#[tauri::command]
fn set_settings(settings: Settings, state: Shared) -> Result<StateDto, String> {
    let mut inner = lock(&state);
    if settings.mode != inner.settings.mode && !inner.events.is_empty() {
        return Err(
            "The sensor type is locked while images are loaded. New project to change it.".into(),
        );
    }
    let remeasure = settings.roi_inset_px != inner.settings.roi_inset_px;
    inner.settings = Settings {
        emissivity: settings.emissivity.bounded(),
        ..settings
    };
    if remeasure {
        inner.remeasure_all()?;
    } else {
        inner.refit_all();
    }
    Ok(state_dto(&mut inner))
}

#[tauri::command]
fn set_title(title: String, state: Shared) -> StateDto {
    let mut inner = lock(&state);
    inner.title = title.trim().to_string();
    state_dto(&mut inner)
}

fn preflight_dto(inner: &mut Inner) -> PreflightDto {
    let job = inner.job();
    let rep = preflight(&job, Some(&inner.survey_timestamps));
    PreflightDto {
        lines: rep
            .lines
            .iter()
            .map(|(l, t)| (l.as_str().to_string(), t.clone()))
            .collect(),
        files: rep
            .files
            .iter()
            .map(|p| thermal_core::file_name(p))
            .collect(),
        blocking: rep.blocking(),
        state: state_dto(inner),
    }
}

/// Survey folder, output folder and filter; scans the capture times and runs the pre-flight check.
#[tauri::command]
fn set_survey(survey: SurveySettings, state: Shared) -> Result<PreflightDto, String> {
    let mut inner = lock(&state);
    let changed = survey.folder != inner.survey.folder || survey.filter != inner.survey.filter;
    inner.survey = survey;
    if changed {
        inner.last_output = None;
    }
    if inner.survey.output_folder.is_empty() && !inner.survey.folder.is_empty() {
        inner.survey.output_folder = Path::new(&inner.survey.folder)
            .join("calibrated")
            .to_string_lossy()
            .to_string();
    }
    inner.scan_survey_timestamps();
    Ok(preflight_dto(&mut inner))
}

#[tauri::command]
fn run_preflight(state: Shared) -> Result<PreflightDto, String> {
    let mut inner = lock(&state);
    inner.scan_survey_timestamps();
    Ok(preflight_dto(&mut inner))
}

fn row_json(row: &Row) -> serde_json::Value {
    let mut m = serde_json::Map::new();
    for (k, c) in &row.0 {
        let v = match c {
            thermal_core::batch::Cell::Str(s) => serde_json::Value::String(s.clone()),
            thermal_core::batch::Cell::Num(x) => serde_json::json!(x),
            thermal_core::batch::Cell::Int(i) => serde_json::json!(i),
            thermal_core::batch::Cell::Bool(b) => serde_json::json!(b),
            thermal_core::batch::Cell::Null => serde_json::Value::Null,
        };
        m.insert(k.clone(), v);
    }
    serde_json::Value::Object(m)
}

/// Start the batch in a worker thread. Progress arrives as window events: `batch-log` (string),
/// `batch-progress` ({done, total, name}), `batch-row` (the row), `batch-done` (BatchDone).
#[tauri::command]
fn start_batch(app: AppHandle, state: Shared) -> Result<bool, String> {
    let (job, timestamps, cancel) = {
        let mut inner = lock(&state);
        if inner.running {
            return Err("a run is in progress".into());
        }
        inner.scan_survey_timestamps();
        let job = inner.job();
        let rep = preflight(&job, Some(&inner.survey_timestamps));
        if rep.blocking() {
            return Err(rep.text());
        }
        let mut job = job;
        job.survey_files = rep.files;
        inner.cancel.store(false, Ordering::SeqCst);
        inner.running = true;
        (job, rep.timestamps, inner.cancel.clone())
    };
    std::thread::spawn(move || {
        let output = job.output_folder.clone();
        let app2 = app.clone();
        let app3 = app.clone();
        let app4 = app.clone();
        let mut log = move |s: &str| {
            let _ = app2.emit("batch-log", s.to_string());
        };
        let mut progress = move |done: usize, total: usize, name: &str| {
            let _ = app3.emit(
                "batch-progress",
                serde_json::json!({"done": done, "total": total, "name": name}),
            );
        };
        let mut on_row = move |row: &Row| {
            let _ = app4.emit("batch-row", row_json(row));
        };
        let is_cancelled = move || cancel.load(Ordering::SeqCst);
        let mut cb = Callbacks {
            log: &mut log,
            progress: &mut progress,
            is_cancelled: &is_cancelled,
            on_row: Some(&mut on_row),
        };
        let result = run_batch(&job, &mut cb, Some(&timestamps));
        let done = match &result {
            Ok(r) => BatchDone {
                ok: r.ok,
                errors: r.errors,
                skipped: r.skipped,
                total: r.total,
                cancelled: r.cancelled,
                output: output.to_string_lossy().to_string(),
                error: None,
            },
            Err(e) => BatchDone {
                ok: 0,
                errors: 0,
                skipped: 0,
                total: 0,
                cancelled: false,
                output: output.to_string_lossy().to_string(),
                error: Some(e.to_string()),
            },
        };
        {
            let st = app.state::<AppState>();
            let mut inner = lock(&st);
            inner.running = false;
            inner.last_output = match &result {
                Ok(r) if !r.cancelled => Some(output.clone()),
                _ => None,
            };
        }
        let _ = app.emit("batch-done", done);
    });
    Ok(true)
}

#[tauri::command]
fn cancel_batch(state: Shared) {
    lock(&state).cancel.store(true, Ordering::SeqCst);
}

#[tauri::command]
fn save_project(path: Option<String>, state: Shared) -> Result<String, String> {
    let mut inner = lock(&state);
    let path = match path
        .map(PathBuf::from)
        .or_else(|| inner.project_path.clone())
    {
        Some(p) => p,
        None => return Err("no path".into()),
    };
    let dir = std::path::absolute(&path)
        .ok()
        .and_then(|p| p.parent().map(Path::to_path_buf));
    let json = inner.project_json(dir.as_deref());
    std::fs::write(
        &path,
        serde_json::to_string_pretty(&json).map_err(|e| e.to_string())?,
    )
    .map_err(|e| e.to_string())?;
    inner.project_path = Some(path.clone());
    Ok(path.to_string_lossy().to_string())
}

#[tauri::command]
fn suggested_project_filename(state: Shared) -> String {
    let inner = lock(&state);
    project_filename(Some(&serde_json::json!({"title": inner.title})))
}

/// Load a project file into the state (settings, survey, images re-read, targets measured, fits
/// recomputed). Returns the problems found, e.g. missing images; the rest of the project still loads.
fn open_project_into(inner: &mut Inner, path: &str) -> Result<Vec<String>, String> {
    let text = std::fs::read_to_string(path).map_err(|e| e.to_string())?;
    let project: serde_json::Value = serde_json::from_str(&text).map_err(|e| e.to_string())?;
    let mode = Mode::parse(
        project
            .get("mode")
            .and_then(|v| v.as_str())
            .unwrap_or("thermal"),
    );
    let inset = project
        .get("roi_inset_px")
        .and_then(|v| v.as_u64())
        .map_or(DEFAULT_ROI_INSET_PX, |v| v as u32);
    inner.settings = Settings {
        mode,
        single_point: project
            .get("msi_single_point")
            .and_then(|v| v.as_bool())
            .unwrap_or(false),
        emissivity: EmissivitySettings::from_json(
            project
                .get("emissivity")
                .unwrap_or(&serde_json::Value::Null),
        ),
        roi_inset_px: inset,
    };
    let sv = project.get("survey").cloned().unwrap_or_default();
    let field = |k: &str| sv.get(k).and_then(|v| v.as_str()).unwrap_or("").to_string();
    inner.survey = SurveySettings {
        folder: field("folder"),
        output_folder: field("output_folder"),
        filter: field("filter"),
        copy_metadata: sv
            .get("copy_metadata")
            .and_then(|v| v.as_bool())
            .unwrap_or(true),
        skip_existing: sv
            .get("skip_existing")
            .and_then(|v| v.as_bool())
            .unwrap_or(false),
    };
    let dir = std::path::absolute(path)
        .ok()
        .and_then(|p| p.parent().map(Path::to_path_buf));
    let (events, problems) = events_from_project(&project, dir.as_deref(), inset);
    inner.events = events;
    inner.cache.clear();
    inner.survey_timestamps.clear();
    inner.last_output = None;
    inner.undo.clear();
    inner.redo.clear();
    inner.refit_all();
    inner.title = project
        .get("title")
        .and_then(|v| v.as_str())
        .filter(|s| !s.is_empty())
        .map(str::to_string)
        .unwrap_or_else(|| {
            thermal_core::file_name(Path::new(path))
                .replace(".calib.json", "")
                .replace(".json", "")
        });
    inner.project_path = Some(PathBuf::from(path));
    if !inner.survey.folder.is_empty() {
        inner.scan_survey_timestamps();
    }
    Ok(problems)
}

#[derive(Serialize)]
pub struct OpenResult {
    pub problems: Vec<String>,
    pub state: StateDto,
}

#[tauri::command]
fn open_project(path: String, state: Shared) -> Result<OpenResult, String> {
    let mut inner = lock(&state);
    let problems = open_project_into(&mut inner, &path)?;
    Ok(OpenResult {
        problems,
        state: state_dto(&mut inner),
    })
}

#[tauri::command]
fn new_project(state: Shared) -> StateDto {
    let mut inner = lock(&state);
    let cancel = inner.cancel.clone();
    *inner = Inner {
        cancel,
        ..Default::default()
    };
    state_dto(&mut inner)
}

/// The HTML fit report of one calibration shot (the front-end asks where to save it).
#[tauri::command]
fn fit_report_html(index: usize, state: Shared) -> Result<String, String> {
    let inner = lock(&state);
    let ev = inner.events.get(index).ok_or("no such image")?;
    let em = &inner.settings.emissivity;
    let mut settings = vec![
        ("project".to_string(), inner.title.clone()),
        (
            "mode".to_string(),
            if inner.settings.mode.is_thermal() {
                "thermal"
            } else {
                "multispectral"
            }
            .to_string(),
        ),
        (
            "ROI edge inset (px)".to_string(),
            inner.settings.roi_inset_px.to_string(),
        ),
        (
            "emissivity correction".to_string(),
            if em.enabled { "on" } else { "off" }.to_string(),
        ),
    ];
    if em.enabled {
        settings.push(("panel ε".to_string(), em.target_emissivity.to_string()));
        settings.push(("scene ε".to_string(), em.scene_emissivity.to_string()));
        settings.push(("T_sky (°C)".to_string(), em.sky_temperature.to_string()));
    }
    let unit = if inner.settings.mode.is_thermal() {
        "°C"
    } else {
        "ρ"
    };
    thermal_core::report::fit_report_html(ev, unit, &inner.events, &settings)
        .map_err(|e| e.to_string())
}

#[tauri::command]
fn write_text_file(path: String, text: String) -> Result<String, String> {
    std::fs::write(&path, text).map_err(|e| e.to_string())?;
    Ok(path)
}

#[tauri::command]
fn open_in_file_manager(path: String) -> Result<(), String> {
    #[cfg(target_os = "linux")]
    let cmd = ("xdg-open", vec![path]);
    #[cfg(target_os = "macos")]
    let cmd = ("open", vec![path]);
    #[cfg(target_os = "windows")]
    let cmd = ("explorer", vec![path]);
    std::process::Command::new(cmd.0)
        .args(cmd.1)
        .spawn()
        .map(|_| ())
        .map_err(|e| e.to_string())
}

pub fn run() {
    // `thermal-calibration-app --project flight.calib.json` opens that project at start;
    // `--screenshot out.png [--screenshot-delay SECONDS]` writes a snapshot of the window and exits
    let args: Vec<String> = std::env::args().collect();
    let arg = |name: &str| {
        args.iter()
            .position(|a| a == name)
            .and_then(|i| args.get(i + 1).cloned())
    };
    let project_arg = arg("--project");
    let screenshot_arg = arg("--screenshot");
    let screenshot_delay: u64 = arg("--screenshot-delay")
        .and_then(|v| v.parse().ok())
        .unwrap_or(4);
    // `--e2e-script FILE [--e2e-env-file JSON] [--e2e-report FILE] [--e2e-timeout SECONDS]` runs a test
    // script inside the web view once the page has loaded (see tests/e2e/README.md)
    let e2e_script = arg("--e2e-script")
        .map(|f| std::fs::read_to_string(&f).unwrap_or_else(|e| panic!("cannot read {f}: {e}")));
    let e2e_env = arg("--e2e-env-file")
        .map(|f| std::fs::read_to_string(&f).unwrap_or_else(|e| panic!("cannot read {f}: {e}")))
        .unwrap_or_else(|| "{}".into());
    let e2e_report_path = arg("--e2e-report").map(PathBuf::from);
    let e2e_timeout: u64 = arg("--e2e-timeout")
        .and_then(|v| v.parse().ok())
        .unwrap_or(180);
    let e2e_script_given = e2e_script.is_some();
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .manage(AppState(Mutex::new(Inner::default())))
        .manage(E2eState { report_path: e2e_report_path.clone(), entries: Mutex::new(Vec::new()) })
        .on_page_load(move |webview, payload| {
            if payload.event() == tauri::webview::PageLoadEvent::Finished {
                if let Some(script) = &e2e_script {
                    let js = format!(
                        "{E2E_BOOTSTRAP}\nwindow.__e2e.env = {e2e_env};\n(async () => {{ try {{\n{script}\n}} catch (e) {{ \
                         await window.__e2e.report('script', false, (e && e.message ? e.message + ' | ' : '') + String(e && e.stack || e)); await window.__e2e.done(false); }} }})();"
                    );
                    if let Err(e) = webview.eval(&js) {
                        eprintln!("e2e: could not run the script: {e}");
                    }
                }
            }
        })
        .setup(move |app| {
            if e2e_script_given {
                let handle = app.handle().clone();
                std::thread::spawn(move || {
                    std::thread::sleep(std::time::Duration::from_secs(e2e_timeout));
                    let st = handle.state::<E2eState>();
                    st.add(serde_json::json!({"kind": "timeout", "text": format!("no e2e_done after {e2e_timeout} s")}));
                    st.write(false);
                    handle.exit(2);
                });
            }
            if let Some(p) = project_arg {
                let st = app.state::<AppState>();
                let mut inner = lock(&st);
                match open_project_into(&mut inner, &p) {
                    Ok(problems) => problems.iter().for_each(|w| eprintln!("WARNING: {w}")),
                    Err(e) => eprintln!("could not open {p}: {e}"),
                }
            }
            if let Some(path) = screenshot_arg {
                schedule_screenshot(app.handle().clone(), path, screenshot_delay);
            }
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            get_state,
            add_calibration_images,
            remove_calibration_image,
            load_image_data,
            set_polygon,
            clear_polygon,
            set_ground_truth,
            add_target,
            remove_target,
            rename_target,
            copy_polygons,
            set_settings,
            set_title,
            set_survey,
            run_preflight,
            start_batch,
            cancel_batch,
            save_project,
            suggested_project_filename,
            open_project,
            new_project,
            open_in_file_manager,
            fit_report_html,
            write_text_file,
            undo,
            redo,
            e2e_report,
            e2e_done,
        ])
        .run(tauri::generate_context!())
        .expect("error while running the application");
}

#[cfg(test)]
mod tests {
    //! The state logic without a window, on the golden flight (skipped when it is not built).
    use super::*;

    fn golden() -> Option<PathBuf> {
        let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../..");
        let p = root.join("tests/data/golden_flight/flight.calib.json");
        p.exists().then(|| p.canonicalize().expect("canonical"))
    }

    #[test]
    fn open_measure_fit_preflight_and_run() {
        let Some(project) = golden() else {
            eprintln!("SKIPPED: python3 rust/tools/make_golden_flight.py first");
            return;
        };
        let mut inner = Inner::default();
        let problems = open_project_into(&mut inner, &project.to_string_lossy()).unwrap();
        assert!(problems.is_empty(), "{problems:?}");
        assert_eq!(inner.events.len(), 3);
        assert!(inner.events.iter().all(|e| e.fit.is_some()));
        assert_eq!(inner.title, "golden flight");
        let dto = state_dto(&mut inner);
        assert_eq!(dto.events[0].targets.len(), 3);
        assert!(dto.events[0]
            .targets
            .iter()
            .all(|t| t.complete && !t.inner.is_empty()));
        assert!(
            dto.survey_timeline.len() == 6,
            "the project's survey folder was scanned"
        );
        // redraw one target smaller, reading unchanged: the fit follows
        let before = inner.events[0].fit.as_ref().unwrap().slope;
        inner.events[0].targets[0].polygon =
            order_corners(&[(44, 44), (76, 44), (76, 76), (44, 76)]);
        inner.remeasure(0).unwrap();
        inner.refit(0);
        let after = inner.events[0].fit.as_ref().unwrap().slope;
        assert!((before - after).abs() < 0.05 && before != after);
        // a changed inset re-measures everything
        inner.settings.roi_inset_px = 5;
        inner.remeasure_all().unwrap();
        assert!(inner
            .events
            .iter()
            .all(|e| e.targets.iter().all(|t| t.pixel_count < 36 * 36)));
        // the run, into a scratch folder
        let out = std::env::temp_dir().join(format!("thermal_app_test_{}", std::process::id()));
        inner.survey.output_folder = out.to_string_lossy().to_string();
        let pf = preflight_dto(&mut inner);
        assert!(!pf.blocking, "{:?}", pf.lines);
        assert_eq!(pf.files.len(), 6);
        let mut job = inner.job();
        assert!(job.project.is_some());
        job.survey_files = inner.job().survey_files;
        let (mut log, mut progress, cancelled) =
            (|_: &str| {}, |_: usize, _: usize, _: &str| {}, || false);
        let mut rows = Vec::new();
        let mut on_row = |r: &Row| rows.push(row_json(r));
        let mut cb = Callbacks {
            log: &mut log,
            progress: &mut progress,
            is_cancelled: &cancelled,
            on_row: Some(&mut on_row),
        };
        let res = run_batch(&job, &mut cb, Some(&inner.survey_timestamps)).unwrap();
        assert_eq!((res.ok, res.errors), (6, 0));
        assert_eq!(rows.len(), 6);
        assert_eq!(rows[0]["status"], "ok");
        assert!(rows[0]["slope_m"].is_number() && rows[0]["u_fit_1sigma"].is_number());
        let _ = std::fs::remove_dir_all(&out);
    }

    #[test]
    fn add_images_rejects_duplicates_and_previews() {
        let Some(project) = golden() else { return };
        let dir = project.parent().unwrap().join("calibration");
        let mut inner = Inner::default();
        let first = dir.join("cal_0.tif");
        let ts = extract_timestamp(&first);
        inner.events.push(CalibrationEvent::new(
            first.clone(),
            ts.value.unwrap(),
            &ts.source,
            &ts.tz,
        ));
        assert_eq!(inner.events[0].targets.len(), 2, "two default targets");
        let st = inner.raster(&first).unwrap();
        assert_eq!((st.width, st.height), (320, 256));
        assert!(inner.cache.contains_key(&first));
        let json = inner.project_json(Some(dir.parent().unwrap()));
        assert_eq!(
            json["calibration_images"][0]["path_relative"],
            "calibration/cal_0.tif"
        );
    }
}

/// After `delay` seconds, snapshot the main web view into a PNG and exit (Linux: WebKitGTK).
#[cfg(target_os = "linux")]
fn schedule_screenshot(handle: AppHandle, path: String, delay: u64) {
    std::thread::spawn(move || {
        std::thread::sleep(std::time::Duration::from_secs(delay));
        eprintln!("screenshot: requesting the snapshot");
        let on_main = handle.clone();
        let _ = handle.run_on_main_thread(move || {
            eprintln!("screenshot: on the main thread");
            let Some(window) = on_main.get_webview_window("main") else {
                eprintln!("screenshot: no main window");
                on_main.exit(2);
                return;
            };
            let after = on_main.clone();
            let _ = window.with_webview(move |platform| {
                use webkit2gtk::WebViewExt;
                let view = platform.inner();
                eprintln!(
                    "screenshot: web view at hand, uri={:?}, loading={}",
                    view.uri(),
                    view.is_loading()
                );
                view.snapshot(
                    webkit2gtk::SnapshotRegion::Visible,
                    webkit2gtk::SnapshotOptions::NONE,
                    None::<&gio::Cancellable>,
                    move |result| {
                        let code = match result.map(cairo::ImageSurface::try_from) {
                            Ok(Ok(image)) => match std::fs::File::create(&path).and_then(|mut f| {
                                image.write_to_png(&mut f).map_err(std::io::Error::other)
                            }) {
                                Ok(()) => {
                                    eprintln!("screenshot written: {path}");
                                    0
                                }
                                Err(e) => {
                                    eprintln!("screenshot: could not write {path}: {e}");
                                    1
                                }
                            },
                            Ok(Err(_)) => {
                                eprintln!("screenshot: the snapshot is not an image surface");
                                1
                            }
                            Err(e) => {
                                eprintln!("screenshot failed: {e}");
                                1
                            }
                        };
                        after.exit(code);
                    },
                );
            });
        });
    });
}

#[cfg(not(target_os = "linux"))]
fn schedule_screenshot(handle: AppHandle, _path: String, delay: u64) {
    std::thread::spawn(move || {
        std::thread::sleep(std::time::Duration::from_secs(delay));
        eprintln!("--screenshot is only implemented on Linux");
        handle.exit(2);
    });
}

#[tauri::command]
fn undo(state: Shared) -> StateDto {
    let mut inner = lock(&state);
    if let Some(prev) = inner.undo.pop() {
        let now = std::mem::replace(&mut inner.events, prev);
        inner.redo.push(now);
    }
    state_dto(&mut inner)
}

#[tauri::command]
fn redo(state: Shared) -> StateDto {
    let mut inner = lock(&state);
    if let Some(next) = inner.redo.pop() {
        let now = std::mem::replace(&mut inner.events, next);
        inner.undo.push(now);
    }
    state_dto(&mut inner)
}

// ----------------------------------------------------------------------------- end-to-end harness

/// Collected by the test script running inside the web view (`--e2e-script`).
pub struct E2eState {
    pub report_path: Option<PathBuf>,
    pub entries: Mutex<Vec<serde_json::Value>>,
}

impl E2eState {
    fn add(&self, entry: serde_json::Value) {
        self.entries
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .push(entry);
    }

    fn write(&self, ok: bool) {
        let entries = self
            .entries
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .clone();
        let steps = entries.iter().filter(|e| e["kind"] == "step").count();
        let failed = entries
            .iter()
            .filter(|e| e["kind"] == "step" && e["ok"] == false)
            .count();
        let errors = entries
            .iter()
            .filter(|e| {
                matches!(
                    e["kind"].as_str(),
                    Some("error" | "unhandledrejection" | "console.error" | "timeout")
                )
            })
            .count();
        let report = serde_json::json!({"ok": ok && failed == 0 && errors == 0, "steps": steps, "failed": failed, "errors": errors, "entries": entries});
        let text = serde_json::to_string_pretty(&report).unwrap_or_default();
        match &self.report_path {
            Some(p) => {
                if let Err(e) = std::fs::write(p, &text) {
                    eprintln!("e2e: could not write {}: {e}", p.display());
                }
            }
            None => println!("{text}"),
        }
        eprintln!(
            "e2e: {} steps, {} failed, {} errors → {}",
            steps,
            failed,
            errors,
            if ok && failed == 0 && errors == 0 {
                "OK"
            } else {
                "FAILED"
            }
        );
    }
}

#[tauri::command]
fn e2e_report(entry: serde_json::Value, e2e: State<E2eState>) {
    if let Some(kind) = entry.get("kind").and_then(|k| k.as_str()) {
        let text = entry
            .get("name")
            .or_else(|| entry.get("text"))
            .and_then(|v| v.as_str())
            .unwrap_or("");
        let ok = entry.get("ok").and_then(|v| v.as_bool());
        eprintln!(
            "e2e: [{kind}] {text}{}",
            ok.map_or(String::new(), |o| if o {
                " ✓".into()
            } else {
                " ✗".into()
            })
        );
    }
    e2e.add(entry);
}

#[tauri::command]
fn e2e_done(ok: bool, app: AppHandle, e2e: State<E2eState>) {
    e2e.write(ok);
    let failed = e2e
        .entries
        .lock()
        .unwrap_or_else(|e| e.into_inner())
        .iter()
        .any(|e| {
            (e["kind"] == "step" && e["ok"] == false)
                || matches!(
                    e["kind"].as_str(),
                    Some("error" | "unhandledrejection" | "console.error")
                )
        });
    app.exit(if ok && !failed { 0 } else { 1 });
}

/// Helpers the test scripts use: report/log/done, wait/until, click/type/key, error capture.
const E2E_BOOTSTRAP: &str = r#"(function () {
  const inv = (cmd, args) => window.__TAURI_INTERNALS__.invoke(cmd, args);
  const e2e = (window.__e2e = window.__e2e || {});
  e2e.report = (name, ok, details) => inv('e2e_report', { entry: { kind: 'step', name, ok: !!ok, details: details === undefined ? null : details, t: Date.now() } });
  e2e.log = (text) => inv('e2e_report', { entry: { kind: 'log', text: String(text), t: Date.now() } });
  e2e.done = (ok) => inv('e2e_done', { ok: !!ok });
  e2e.wait = (ms) => new Promise((r) => setTimeout(r, ms));
  e2e.until = async (fn, timeout = 15000, every = 50) => {
    const t0 = Date.now();
    let last;
    while (Date.now() - t0 < timeout) { try { const v = fn(); if (v) return v; } catch (e) { last = e; } await e2e.wait(every); }
    throw new Error('timeout waiting for ' + fn.toString().slice(0, 100) + (last ? ' (' + last + ')' : ''));
  };
  e2e.$ = (sel) => { const el = document.querySelector(sel); if (!el) throw new Error('no element ' + sel); return el; };
  e2e.click = (sel) => { const el = e2e.$(sel); el.click(); return el; };
  e2e.type = (sel, text) => { const el = e2e.$(sel); el.focus(); el.value = text; el.dispatchEvent(new Event('change', { bubbles: true })); return el; };
  e2e.key = (sel, key) => e2e.$(sel).dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true }));
  e2e.mouse = (el, type, x, y, button = 0) => el.dispatchEvent(new MouseEvent(type, { bubbles: true, clientX: x, clientY: y, button }));
  window.addEventListener('error', (ev) => inv('e2e_report', { entry: { kind: 'error', text: String(ev.message), t: Date.now() } }));
  window.addEventListener('unhandledrejection', (ev) => inv('e2e_report', { entry: { kind: 'unhandledrejection', text: String(ev.reason && (ev.reason.stack || ev.reason.message || ev.reason)), t: Date.now() } }));
  const ce = console.error.bind(console);
  console.error = (...a) => { ce(...a); inv('e2e_report', { entry: { kind: 'console.error', text: a.map((x) => String((x && x.stack) || x)).join(' '), t: Date.now() } }); };
})();"#;
