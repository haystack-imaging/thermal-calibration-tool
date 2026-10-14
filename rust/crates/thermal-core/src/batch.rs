//! The pre-flight check and the processing run: every survey image calibrated, written as a GeoTIFF,
//! and logged in `processing_log.csv` (the file people cite, with a stable column order).

use crate::interp::{coefficients_at, fitted_sorted, Position};
use crate::model::{CalibrationEvent, EmissivitySettings, Mode, Timestamp};
use crate::physics::apply_emissivity_correction;
use crate::raster::{
    copy_metadata_with_exiftool, inspect_image, load_image, save_calibrated_geotiff, Raster,
};
use crate::timestamp::extract_timestamp;
use crate::{file_name, Result, DEFAULT_ROI_INSET_PX, IMAGE_EXTENSIONS, TOOL_NAME, VERSION};
use serde_json::Value;
use std::collections::{BTreeMap, HashMap};
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};

#[derive(Clone, Debug)]
pub struct BatchJob {
    pub mode: Mode,
    pub single_point: bool,
    pub events: Vec<CalibrationEvent>,
    pub survey_files: Vec<PathBuf>,
    pub output_folder: PathBuf,
    pub emissivity: EmissivitySettings,
    pub roi_inset_px: u32,
    pub copy_metadata: bool,
    pub skip_existing: bool,
    pub name_filter: String,
    /// The project dict, saved next to the outputs so a run is reproducible.
    pub project: Option<Value>,
}

impl BatchJob {
    pub fn new(
        mode: Mode,
        single_point: bool,
        events: Vec<CalibrationEvent>,
        survey_files: Vec<PathBuf>,
        output_folder: PathBuf,
    ) -> Self {
        BatchJob {
            mode,
            single_point,
            events,
            survey_files,
            output_folder,
            emissivity: EmissivitySettings::default(),
            roi_inset_px: DEFAULT_ROI_INSET_PX,
            copy_metadata: true,
            skip_existing: false,
            name_filter: String::new(),
            project: None,
        }
    }

    pub fn thermal(&self) -> bool {
        self.mode.is_thermal()
    }

    pub fn unit(&self) -> &'static str {
        if self.thermal() {
            "°C"
        } else {
            "reflectance"
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Level {
    Ok,
    Warn,
    Block,
}

impl Level {
    pub fn as_str(self) -> &'static str {
        match self {
            Level::Ok => "ok",
            Level::Warn => "warn",
            Level::Block => "block",
        }
    }
}

/// Everything that can go wrong, listed before a single pixel is written.
#[derive(Clone, Debug, Default)]
pub struct PreflightReport {
    pub lines: Vec<(Level, String)>,
    /// Files that will be processed.
    pub files: Vec<PathBuf>,
    pub timestamps: HashMap<PathBuf, Timestamp>,
}

impl PreflightReport {
    pub fn blocking(&self) -> bool {
        self.lines.iter().any(|(l, _)| *l == Level::Block)
    }

    pub fn add(&mut self, level: Level, text: impl Into<String>) {
        self.lines.push((level, text.into()));
    }

    pub fn text(&self) -> String {
        self.lines
            .iter()
            .map(|(l, t)| {
                let sym = match l {
                    Level::Ok => "✓",
                    Level::Warn => "⚠",
                    Level::Block => "✗",
                };
                format!("{sym} {t}")
            })
            .collect::<Vec<_>>()
            .join("\n")
    }
}

fn has_image_extension(p: &Path) -> bool {
    p.extension()
        .and_then(|e| e.to_str())
        .is_some_and(|e| IMAGE_EXTENSIONS.contains(&e.to_ascii_lowercase().as_str()))
}

fn stem(p: &Path) -> String {
    p.file_stem()
        .map(|s| s.to_string_lossy().to_string())
        .unwrap_or_default()
}

/// The images of a survey folder, minus the calibration shots and earlier outputs, filtered by name.
pub fn collect_survey_files(
    folder: Option<&Path>,
    name_filter: &str,
    calibration_paths: &[PathBuf],
) -> Vec<PathBuf> {
    let mut files: Vec<PathBuf> = Vec::new();
    if let Some(dir) = folder {
        if dir.is_dir() {
            if let Ok(rd) = fs::read_dir(dir) {
                files = rd
                    .filter_map(|e| e.ok().map(|e| e.path()))
                    .filter(|p| p.is_file() && has_image_extension(p))
                    .collect();
            }
        }
    }
    if !name_filter.is_empty() {
        files.retain(|p| file_name(p).contains(name_filter));
    }
    let calib: Vec<PathBuf> = calibration_paths
        .iter()
        .map(|p| fs::canonicalize(p).unwrap_or_else(|_| p.clone()))
        .collect();
    files.retain(|p| {
        let canon = fs::canonicalize(p).unwrap_or_else(|_| p.clone());
        !calib.contains(&canon) && !stem(p).ends_with("_calibrated")
    });
    files.sort_by_key(|p| file_name(p));
    files
}

/// `stem_calibrated.tif`, with the extension added when two inputs share a stem.
pub fn output_filenames(files: &[PathBuf]) -> HashMap<PathBuf, String> {
    let mut stems: HashMap<String, usize> = HashMap::new();
    for p in files {
        *stems.entry(stem(p)).or_default() += 1;
    }
    files
        .iter()
        .map(|p| {
            let s = stem(p);
            let name = if stems[&s] > 1 {
                let ext = p
                    .extension()
                    .map(|e| e.to_string_lossy().to_ascii_lowercase())
                    .unwrap_or_default();
                format!("{s}_{ext}_calibrated.tif")
            } else {
                format!("{s}_calibrated.tif")
            };
            (p.clone(), name)
        })
        .collect()
}

fn fmt_time(t: chrono::NaiveDateTime) -> String {
    t.format("%H:%M:%S").to_string()
}

fn list_some(names: &[String]) -> String {
    let shown: Vec<&str> = names.iter().take(4).map(String::as_str).collect();
    format!(
        "{}{}",
        shown.join(", "),
        if names.len() > 4 { " …" } else { "" }
    )
}

/// The pre-flight check. `timestamps` may carry already extracted capture times.
pub fn preflight(
    job: &BatchJob,
    timestamps: Option<&HashMap<PathBuf, Timestamp>>,
) -> PreflightReport {
    let mut rep = PreflightReport::default();
    let fitted = fitted_sorted(&job.events);
    if fitted.is_empty() {
        rep.add(Level::Block, "No calibration image has a complete fit yet.");
        return rep;
    }
    let unfitted: Vec<String> = job
        .events
        .iter()
        .filter(|e| e.fit.is_none())
        .map(|e| e.file_name())
        .collect();
    if !unfitted.is_empty() {
        rep.add(
            Level::Warn,
            format!(
                "{} calibration image(s) without a fit are ignored: {}",
                unfitted.len(),
                unfitted.join(", ")
            ),
        );
    }
    rep.add(
        Level::Ok,
        format!(
            "{} calibration event(s): {}",
            fitted.len(),
            fitted
                .iter()
                .map(|e| format!("{} ({})", fmt_time(e.timestamp), e.timestamp_source))
                .collect::<Vec<_>>()
                .join(", ")
        ),
    );
    for e in &fitted {
        let f = e.fit.as_ref().expect("fitted");
        if !(f.slope.is_finite() && f.intercept.is_finite()) {
            rep.add(
                Level::Block,
                format!("{}: the fitted coefficients are not finite.", e.file_name()),
            );
        }
        if !f.single_point && f.n == 2 {
            rep.add(
                Level::Warn,
                format!(
                    "{}: only 2 targets, R² is 1 by construction and the fit uncertainty cannot be estimated. A third \
                     target would fix that.",
                    e.file_name()
                ),
            );
        }
        if e.timestamp_source == "file mtime" || e.timestamp_source == "none" {
            rep.add(
                Level::Warn,
                format!("{}: capture time taken from the file modification time, which changes on every copy.", e.file_name()),
            );
        }
    }
    if job.survey_files.is_empty() {
        rep.add(Level::Block, "No survey images found (check folder, filter and that calibration images are excluded).");
        return rep;
    }
    let mut usable: Vec<PathBuf> = Vec::new();
    let (mut multiband, mut eightbit, mut unreadable) = (Vec::new(), Vec::new(), Vec::new());
    let mut src_counts: BTreeMap<String, usize> = BTreeMap::new();
    let mut zones: BTreeMap<String, usize> = BTreeMap::new();
    let (mut before, mut after) = (0usize, 0usize);
    let (t0, t1) = (fitted[0].timestamp, fitted[fitted.len() - 1].timestamp);
    for p in &job.survey_files {
        let info = inspect_image(p);
        if !info.error.is_empty() {
            unreadable.push(file_name(p));
            continue;
        }
        if !info.is_single_band() {
            multiband.push(file_name(p));
            continue;
        }
        if info.is_8bit() && job.thermal() {
            eightbit.push(file_name(p));
        }
        let ts = timestamps
            .and_then(|m| m.get(p).cloned())
            .unwrap_or_else(|| extract_timestamp(p));
        *src_counts.entry(ts.source.clone()).or_default() += 1;
        *zones
            .entry(if ts.tz.is_empty() {
                "none".to_string()
            } else {
                ts.tz.clone()
            })
            .or_default() += 1;
        if let Some(v) = ts.value {
            if fitted.len() > 1 {
                if v < t0 {
                    before += 1;
                } else if v > t1 {
                    after += 1;
                }
            }
        }
        rep.timestamps.insert(p.clone(), ts);
        usable.push(p.clone());
    }
    rep.files = usable.clone();
    if !multiband.is_empty() {
        rep.add(
            Level::Warn,
            format!(
                "{} colour image(s) skipped (pixel values are palette colours, not temperatures): {}. Convert R-JPEGs to \
                 single-band temperature TIFFs first (README).",
                multiband.len(),
                list_some(&multiband)
            ),
        );
    }
    if !unreadable.is_empty() {
        rep.add(
            Level::Warn,
            format!(
                "{} unreadable file(s) skipped: {}",
                unreadable.len(),
                list_some(&unreadable)
            ),
        );
    }
    if !eightbit.is_empty() {
        rep.add(
            Level::Warn,
            format!(
                "{} image(s) are 8-bit integers; those are usually stretched previews, not temperatures: {}",
                eightbit.len(),
                list_some(&eightbit)
            ),
        );
    }
    if usable.is_empty() {
        rep.add(Level::Block, "None of the survey images can be processed.");
        return rep;
    }
    rep.add(
        Level::Ok,
        format!("{} image(s) will be processed.", usable.len()),
    );
    let mut by_count: Vec<(&String, &usize)> = src_counts.iter().collect();
    by_count.sort_by(|a, b| b.1.cmp(a.1).then(a.0.cmp(b.0)));
    let src_txt = by_count
        .iter()
        .map(|(s, n)| format!("{n} from {s}"))
        .collect::<Vec<_>>()
        .join(", ");
    let unreliable = src_counts.get("file mtime").copied().unwrap_or(0)
        + src_counts.get("none").copied().unwrap_or(0);
    let level = if unreliable > 0 {
        Level::Warn
    } else {
        Level::Ok
    };
    rep.add(
        level,
        format!(
            "Capture times: {src_txt}.{}",
            if level == Level::Warn {
                " File modification times change on every copy; interpolation between calibration shots is then meaningless."
            } else {
                ""
            }
        ),
    );
    if let Some(&n) = src_counts.get("none") {
        if fitted.len() > 1 {
            rep.add(
                Level::Block,
                format!(
                    "{n} image(s) have no capture time at all; with several calibration shots they cannot be placed in time. \
                     Fix the metadata or use one calibration shot."
                ),
            );
        }
    }
    let mut all_zones: Vec<String> = zones.keys().cloned().collect();
    for e in &fitted {
        let z = if e.timestamp_tz.is_empty() {
            "none".to_string()
        } else {
            e.timestamp_tz.clone()
        };
        if !all_zones.contains(&z) {
            all_zones.push(z);
        }
    }
    let real_zones: Vec<&String> = all_zones
        .iter()
        .filter(|z| z.as_str() != "" && z.as_str() != "none")
        .collect();
    if all_zones.len() > 1 && all_zones.iter().any(|z| z == "Z") {
        rep.add(
            Level::Warn,
            "Some timestamps are in UTC ('Z') and others in local time; check that calibration and survey images were \
             converted the same way, otherwise they are hours apart.",
        );
    } else if real_zones.len() > 1 {
        let mut sorted: Vec<&str> = real_zones.iter().map(|s| s.as_str()).collect();
        sorted.sort();
        rep.add(
            Level::Warn,
            format!(
                "Timestamps carry different UTC offsets ({}); all times are compared as local wall time, so an offset change \
                 shifts images by that difference.",
                sorted.join(", ")
            ),
        );
    }
    if fitted.len() > 1 {
        if before > 0 {
            rep.add(
                Level::Warn,
                format!("{before} image(s) taken before the first calibration shot ({}): the first coefficients are used unchanged.", fmt_time(t0)),
            );
        }
        if after > 0 {
            rep.add(
                Level::Warn,
                format!("{after} image(s) taken after the last calibration shot ({}): the last coefficients are used unchanged.", fmt_time(t1)),
            );
        }
        if before == 0 && after == 0 {
            rep.add(
                Level::Ok,
                format!(
                    "All images lie between the first and last calibration shot ({} to {}); coefficients are interpolated.",
                    fmt_time(t0),
                    fmt_time(t1)
                ),
            );
        }
    } else {
        rep.add(
            Level::Ok,
            "Single calibration: one set of coefficients is applied to every image (drift during the flight is not corrected).",
        );
    }
    let names = output_filenames(&usable);
    let existing = usable
        .iter()
        .filter(|p| job.output_folder.join(&names[*p]).exists())
        .count();
    if existing > 0 {
        rep.add(
            Level::Warn,
            format!(
                "{existing} output file(s) already exist in the output folder and will be {}.",
                if job.skip_existing {
                    "skipped"
                } else {
                    "overwritten"
                }
            ),
        );
    }
    if let (Ok(out), Some(Ok(inp))) = (
        fs::canonicalize(&job.output_folder),
        usable[0].parent().map(fs::canonicalize),
    ) {
        if out == inp {
            rep.add(
                Level::Warn,
                "Output folder is the input folder. Outputs are written next to the inputs (they are excluded from later runs \
                 by their _calibrated suffix).",
            );
        }
    }
    let writable = fs::create_dir_all(&job.output_folder).and_then(|_| {
        let probe = job.output_folder.join(".write_test");
        fs::write(&probe, b"")?;
        fs::remove_file(&probe)
    });
    if let Err(e) = writable {
        rep.add(
            Level::Block,
            format!("Output folder cannot be written: {e}"),
        );
    }
    if job.thermal() && job.emissivity.enabled {
        rep.add(
            Level::Warn,
            format!(
                "Emissivity correction is ON (panels ε={}, scene ε={}). Only correct if the images were converted with \
                 emissivity 1.0; the DJI SDK applies 0.95 by default.",
                job.emissivity.target_emissivity, job.emissivity.scene_emissivity
            ),
        );
    }
    rep
}

/// One cell of the processing log. Floats print like pandas does (`1.0`, `0.25`), None is empty.
#[derive(Clone, Debug, PartialEq)]
pub enum Cell {
    Str(String),
    Num(f64),
    Int(i64),
    Bool(bool),
    Null,
}

impl Cell {
    pub fn text(&self) -> String {
        match self {
            Cell::Str(s) => s.clone(),
            Cell::Num(v) => fmt_float(*v),
            Cell::Int(i) => i.to_string(),
            Cell::Bool(b) => if *b { "True" } else { "False" }.to_string(),
            Cell::Null => String::new(),
        }
    }

    pub fn as_f64(&self) -> Option<f64> {
        match self {
            Cell::Num(v) => Some(*v),
            Cell::Int(i) => Some(*i as f64),
            _ => None,
        }
    }

    pub fn as_str(&self) -> Option<&str> {
        match self {
            Cell::Str(s) => Some(s),
            _ => None,
        }
    }
}

/// `1.0` for integral values, the shortest round-trip repr otherwise (what pandas writes).
pub fn fmt_float(v: f64) -> String {
    if !v.is_finite() {
        return String::new();
    }
    if v.fract() == 0.0 && v.abs() < 1e15 {
        format!("{v:.1}")
    } else {
        format!("{v}")
    }
}

/// Python's `round(x, n)`.
pub fn round_to(v: f64, digits: i32) -> f64 {
    let f = 10f64.powi(digits);
    (v * f).round() / f
}

/// One row of the processing log: an ordered list of (column, cell), so the CSV keeps the column
/// order the Python tool established.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Row(pub Vec<(String, Cell)>);

impl Row {
    pub fn set(&mut self, key: &str, cell: Cell) {
        if let Some(slot) = self.0.iter_mut().find(|(k, _)| k == key) {
            slot.1 = cell;
        } else {
            self.0.push((key.to_string(), cell));
        }
    }

    pub fn get(&self, key: &str) -> Option<&Cell> {
        self.0.iter().find(|(k, _)| k == key).map(|(_, c)| c)
    }

    pub fn status(&self) -> &str {
        self.get("status").and_then(Cell::as_str).unwrap_or("")
    }
}

#[derive(Clone, Debug)]
pub struct BatchResult {
    pub ok: usize,
    pub errors: usize,
    pub skipped: usize,
    pub total: usize,
    pub cancelled: bool,
    pub csv: PathBuf,
    pub summary: PathBuf,
    pub project: Option<PathBuf>,
    pub rows: Vec<Row>,
}

/// Columns in order of first appearance across the rows (what `pandas.DataFrame(rows)` does).
pub fn csv_columns(rows: &[Row]) -> Vec<String> {
    let mut cols: Vec<String> = Vec::new();
    for r in rows {
        for (k, _) in &r.0 {
            if !cols.contains(k) {
                cols.push(k.clone());
            }
        }
    }
    cols
}

pub fn write_csv(path: &Path, rows: &[Row]) -> Result<()> {
    let cols = csv_columns(rows);
    let mut w = csv::Writer::from_path(path)?;
    w.write_record(&cols)?;
    for r in rows {
        let rec: Vec<String> = cols
            .iter()
            .map(|c| r.get(c).map(Cell::text).unwrap_or_default())
            .collect();
        w.write_record(&rec)?;
    }
    w.flush()?;
    Ok(())
}

/// Callbacks of a run: log lines, progress (done, total, current name) and the cancel flag.
pub struct Callbacks<'a> {
    pub log: &'a mut dyn FnMut(&str),
    pub progress: &'a mut dyn FnMut(usize, usize, &str),
    pub is_cancelled: &'a dyn Fn() -> bool,
    pub on_row: Option<&'a mut dyn FnMut(&Row)>,
}

/// (m, b, position, u_fit, notes) of one processed image.
type Processed = (f64, f64, Position, Option<f64>, Vec<String>);

#[allow(clippy::too_many_arguments)]
fn process_one(
    job: &BatchJob,
    fitted: &[&CalibrationEvent],
    img_path: &Path,
    out_path: &Path,
    ts: &Timestamp,
    em: &EmissivitySettings,
    unit: &str,
    row: &mut Row,
) -> Result<Processed> {
    if ts.value.is_none() && fitted.len() > 1 {
        return Err(
            "no capture time in the metadata; cannot place the image between the calibration shots"
                .into(),
        );
    }
    let when = ts.value.unwrap_or(fitted[0].timestamp);
    let (m, b, position, pair) = coefficients_at(when, fitted)?;
    if !(m.is_finite() && b.is_finite()) {
        return Err("calibration coefficients are not finite".into());
    }
    let img = load_image(img_path)?;
    let Some((o_mean, o_min, o_max)) = img.finite_stats() else {
        return Err("the image has no valid pixels".into());
    };
    let mut calibrated = Raster::new(
        img.width,
        img.height,
        img.data
            .iter()
            .map(|&v| {
                if v.is_finite() {
                    (m * v as f64 + b) as f32
                } else {
                    f32::NAN
                }
            })
            .collect(),
    );
    if em.enabled {
        apply_emissivity_correction(
            &mut calibrated.data,
            em.scene_emissivity,
            em.sky_temperature,
        )?;
    }
    let (c_mean, c_min, c_max) = calibrated.finite_stats().expect("same finite pixels");
    // regression prediction uncertainty at the image's mean sensor value (needs >= 3 targets): only
    // the events whose coefficients are applied count; between two shots the larger one is reported
    let x0 = o_mean;
    let applied: Vec<&CalibrationEvent> = match position {
        Position::Between => vec![fitted[pair.0], fitted[pair.1]],
        Position::Before | Position::Single => vec![fitted[pair.0]],
        Position::After => vec![fitted[pair.1]],
    };
    let u_fit = applied
        .iter()
        .filter_map(|e| e.fit.as_ref().and_then(|f| f.predict_uncertainty(x0)))
        .fold(None, |acc: Option<f64>, u| {
            Some(acc.map_or(u, |a| a.max(u)))
        });
    let xr_min = applied
        .iter()
        .map(|e| e.fit.as_ref().expect("fitted").x_min)
        .fold(f64::INFINITY, f64::min);
    let xr_max = applied
        .iter()
        .map(|e| e.fit.as_ref().expect("fitted").x_max)
        .fold(f64::NEG_INFINITY, f64::max);
    let (mut n_fin, mut n_out) = (0usize, 0usize);
    for &v in &img.data {
        if v.is_finite() {
            n_fin += 1;
            let v = v as f64;
            if v < xr_min || v > xr_max {
                n_out += 1;
            }
        }
    }
    let outside = 100.0 * n_out as f64 / n_fin.max(1) as f64;
    let tags = vec![
        (
            "CALIBRATION_FORMULA".to_string(),
            format!("out = {m:.6} * in + {b:.6}"),
        ),
        ("CALIBRATION_SLOPE".to_string(), format!("{m:.8}")),
        ("CALIBRATION_INTERCEPT".to_string(), format!("{b:.8}")),
        ("CALIBRATION_EVENTS".to_string(), fitted.len().to_string()),
        (
            "CALIBRATION_POSITION".to_string(),
            position.as_str().to_string(),
        ),
        (
            "CALIBRATION_FIT_UNCERTAINTY_1SIGMA".to_string(),
            u_fit.map_or("n/a".to_string(), |u| format!("{u:.3}")),
        ),
    ];
    let notes = save_calibrated_geotiff(
        &calibrated,
        out_path,
        Some(img_path),
        &tags,
        job.copy_metadata,
        true,
    )?;
    row.set(
        "timestamp",
        Cell::Str(if ts.value.is_some() {
            crate::model::isoformat(when)
        } else {
            String::new()
        }),
    );
    row.set("timestamp_source", Cell::Str(ts.source.clone()));
    row.set("timestamp_tz", Cell::Str(ts.tz.clone()));
    row.set(
        "position_vs_calibration",
        Cell::Str(position.as_str().to_string()),
    );
    row.set("slope_m", Cell::Num(round_to(m, 8)));
    row.set("intercept_b", Cell::Num(round_to(b, 6)));
    row.set(
        "formula_applied",
        Cell::Str(if job.single_point {
            format!("Refl = {m:.10} * DN")
        } else {
            format!("Out = {m:.6} * In + {b:.6}")
        }),
    );
    row.set(
        &format!("original_mean_{unit}"),
        Cell::Num(round_to(o_mean, 4)),
    );
    row.set(
        &format!("original_min_{unit}"),
        Cell::Num(round_to(o_min, 4)),
    );
    row.set(
        &format!("original_max_{unit}"),
        Cell::Num(round_to(o_max, 4)),
    );
    row.set(
        &format!("calibrated_mean_{unit}"),
        Cell::Num(round_to(c_mean, 4)),
    );
    row.set(
        &format!("calibrated_min_{unit}"),
        Cell::Num(round_to(c_min, 4)),
    );
    row.set(
        &format!("calibrated_max_{unit}"),
        Cell::Num(round_to(c_max, 4)),
    );
    row.set(
        &format!("mean_shift_{unit}"),
        Cell::Num(round_to(c_mean - o_mean, 4)),
    );
    row.set(
        "pct_pixels_outside_target_range",
        Cell::Num(round_to(outside, 1)),
    );
    row.set(
        "u_fit_1sigma",
        u_fit.map_or(Cell::Null, |u| Cell::Num(round_to(u, 4))),
    );
    row.set("emissivity_corrected", Cell::Bool(em.enabled));
    row.set(
        "scene_emissivity",
        if em.enabled {
            Cell::Num(em.scene_emissivity)
        } else {
            Cell::Null
        },
    );
    row.set("status", Cell::Str("ok".into()));
    Ok((m, b, position, u_fit, notes))
}

/// Calibrate every survey image. Pure function apart from the files it writes: safe in a worker
/// thread. `on_row` is called with each finished row (for a live results table).
pub fn run_batch(
    job: &BatchJob,
    cb: &mut Callbacks,
    timestamps: Option<&HashMap<PathBuf, Timestamp>>,
) -> Result<BatchResult> {
    let fitted = fitted_sorted(&job.events);
    if fitted.is_empty() {
        return Err("no fitted calibration events".into());
    }
    let out_dir = &job.output_folder;
    fs::create_dir_all(out_dir)?;
    let names = output_filenames(&job.survey_files);
    let unit = if job.thermal() {
        "celsius"
    } else {
        "reflectance"
    };
    let em = if job.thermal() {
        job.emissivity.clone()
    } else {
        EmissivitySettings::default()
    };
    (cb.log)(&format!(
        "Mode: {}{}",
        if job.thermal() { "Thermal" } else { "MSI" },
        if job.single_point {
            " (single-point K)"
        } else {
            ""
        }
    ));
    (cb.log)(&format!(
        "Calibration events: {}  →  {}",
        fitted.len(),
        if fitted.len() > 1 {
            "interpolated in time"
        } else {
            "constant"
        }
    ));
    if em.enabled {
        (cb.log)(&format!(
            "Emissivity correction ON: panels ε={}, scene ε={}, T_sky={} °C",
            em.target_emissivity, em.scene_emissivity, em.sky_temperature
        ));
    }
    (cb.log)(&format!(
        "{} image(s) → {}",
        job.survey_files.len(),
        out_dir.display()
    ));

    let mut rows: Vec<Row> = Vec::new();
    let (mut n_ok, mut n_err, mut n_skip) = (0usize, 0usize, 0usize);
    let total = job.survey_files.len();
    let mut cancelled = false;
    let mut exif_pairs: Vec<(PathBuf, PathBuf)> = Vec::new();
    for (i, img_path) in job.survey_files.iter().enumerate() {
        if (cb.is_cancelled)() {
            (cb.log)("Cancelled.");
            cancelled = true;
            break;
        }
        let name = file_name(img_path);
        let out_name = names[img_path].clone();
        let out_path = out_dir.join(&out_name);
        (cb.progress)(i, total, &name);
        let mut row = Row::default();
        row.set("input_filename", Cell::Str(name.clone()));
        row.set("output_filename", Cell::Str(out_name));
        if job.skip_existing && out_path.exists() {
            n_skip += 1;
            row.set("status", Cell::Str("skipped (exists)".into()));
        } else {
            let ts = timestamps
                .and_then(|m| m.get(img_path).cloned())
                .unwrap_or_else(|| extract_timestamp(img_path));
            match process_one(job, &fitted, img_path, &out_path, &ts, &em, unit, &mut row) {
                Ok((m, b, position, u_fit, notes)) => {
                    if job.copy_metadata {
                        exif_pairs.push((img_path.clone(), out_path.clone()));
                    }
                    for n in notes {
                        (cb.log)(&format!("  {name}: {n}"));
                    }
                    n_ok += 1;
                    (cb.log)(&format!(
                        "{name}: m={m:.4} b={b:.3} ({}, {}){}",
                        position.as_str(),
                        ts.source,
                        u_fit.map_or(String::new(), |u| format!(", ±{u:.2}"))
                    ));
                }
                Err(e) => {
                    n_err += 1;
                    row.set("status", Cell::Str(format!("error: {e}")));
                    (cb.log)(&format!("ERROR {name}: {e}"));
                }
            }
        }
        if let Some(on_row) = cb.on_row.as_mut() {
            on_row(&row);
        }
        rows.push(row);
    }
    if !exif_pairs.is_empty() {
        (cb.progress)(total, total, "copying EXIF/XMP/GPS metadata");
        for n in copy_metadata_with_exiftool(&exif_pairs) {
            (cb.log)(&n);
        }
    }
    (cb.progress)(total, total, "");

    let csv_path = out_dir.join("processing_log.csv");
    write_csv(&csv_path, &rows)?;
    let mut project_path = None;
    if let Some(project) = &job.project {
        // the project is saved next to the outputs, so a run is reproducible
        let path = out_dir.join(crate::project::project_filename(Some(project)));
        let mut copy = project.clone();
        let out_abs = fs::canonicalize(out_dir).unwrap_or_else(|_| out_dir.clone());
        copy["calibration_images"] =
            Value::Array(fitted.iter().map(|e| e.to_json(Some(&out_abs))).collect());
        let mut survey = copy
            .get("survey")
            .cloned()
            .unwrap_or_else(|| Value::Object(Default::default()));
        if let Some(first) = job.survey_files.first().and_then(|p| p.parent()) {
            survey["folder"] = Value::String(
                fs::canonicalize(first)
                    .unwrap_or_else(|_| first.to_path_buf())
                    .to_string_lossy()
                    .to_string(),
            );
        }
        survey["output_folder"] = Value::String(out_abs.to_string_lossy().to_string());
        copy["survey"] = survey;
        match fs::write(&path, serde_json::to_string_pretty(&copy)?) {
            Ok(()) => project_path = Some(path),
            Err(e) => (cb.log)(&format!(
                "could not write the project file next to the outputs: {e}"
            )),
        }
    }
    write_summary(job, &fitted, &em, n_ok, n_err, n_skip)?;
    (cb.log)(&format!(
        "\n{}: {n_ok} ok, {n_err} errors, {n_skip} skipped{}. Log: {}{}",
        if cancelled { "Cancelled" } else { "Done" },
        if cancelled {
            format!(" of {total}")
        } else {
            String::new()
        },
        csv_path.display(),
        project_path
            .as_ref()
            .map_or(String::new(), |p| format!(", project: {}", file_name(p)))
    ));
    Ok(BatchResult {
        ok: n_ok,
        errors: n_err,
        skipped: n_skip,
        total,
        cancelled,
        csv: csv_path,
        summary: out_dir.join("processing_summary.txt"),
        project: project_path,
        rows,
    })
}

fn write_summary(
    job: &BatchJob,
    fitted: &[&CalibrationEvent],
    em: &EmissivitySettings,
    n_ok: usize,
    n_err: usize,
    n_skip: usize,
) -> Result<()> {
    let mut f = std::io::BufWriter::new(fs::File::create(
        job.output_folder.join("processing_summary.txt"),
    )?);
    writeln!(
        f,
        "IMAGE CALIBRATION PROCESSING SUMMARY ({})",
        if job.thermal() { "Thermal" } else { "MSI" }
    )?;
    writeln!(f, "{}\n", "=".repeat(60))?;
    writeln!(
        f,
        "Tool: {TOOL_NAME} {VERSION}\nProcessing date: {}",
        crate::model::isoformat(chrono::Local::now().naive_local())
    )?;
    writeln!(
        f,
        "Images processed: {n_ok}, errors: {n_err}, skipped: {n_skip}"
    )?;
    if !job.name_filter.is_empty() {
        writeln!(f, "Filename filter: '{}'", job.name_filter)?;
    }
    writeln!(f, "\nCALIBRATION EVENTS\n{}", "-".repeat(40))?;
    for (k, e) in fitted.iter().enumerate() {
        let fit = e.fit.as_ref().expect("fitted");
        writeln!(
            f,
            "  Event {}: {} ({}) {}",
            k + 1,
            e.timestamp.format("%Y-%m-%d %H:%M:%S"),
            e.timestamp_source,
            e.file_name()
        )?;
        writeln!(f, "    {}", fit.summary_line(job.unit()))?;
        if let (Some(s), Some(sm), Some(sb)) = (fit.resid_std, fit.se_slope, fit.se_intercept) {
            writeln!(
                f,
                "    residual std {s:.3}, SE(m) {sm:.4}, SE(b) {sb:.3}, leave-one-out RMSE {}",
                fit.loo_rmse
                    .map_or("None".to_string(), |l| format!("{}", round_to(l, 3)))
            )?;
        }
    }
    writeln!(f, "\nMETHOD\n{}", "-".repeat(40))?;
    if job.single_point {
        writeln!(
            f,
            "Single-point: reflectance = K × DN, K = ρ_panel / DN_panel"
        )?;
    } else {
        writeln!(
            f,
            "Empirical line: out = m × in + b, fitted per calibration image by least squares,"
        )?;
        writeln!(
            f,
            "{}",
            if fitted.len() > 1 {
                "m and b interpolated linearly in time between consecutive calibration images"
            } else {
                "constant m and b (single calibration image)"
            }
        )?;
    }
    if em.enabled {
        writeln!(
            f,
            "Emissivity correction: the reference readings were converted to apparent temperature with the\npanel emissivity \
             ε={} before the fit; m and b were applied to the raw pixels;\nthe result was converted to surface temperature \
             with the scene emissivity ε={}\n(T_sky={} °C, broadband grey-body approximation).",
            em.target_emissivity, em.scene_emissivity, em.sky_temperature
        )?;
    } else {
        writeln!(f, "Emissivity correction: off")?;
    }
    writeln!(f, "\nUNCERTAINTY\n{}", "-".repeat(40))?;
    writeln!(f, "u_fit_1sigma: 1-sigma prediction uncertainty of the regression at the image mean sensor value,")?;
    writeln!(f, "              s * sqrt(1 + 1/n + (x - mean)^2 / Sxx) with s = sqrt(SSE / (n - 2)); needs at least")?;
    writeln!(f, "              3 targets. Between two calibration shots the larger of the two is reported. With few")?;
    writeln!(f, "              targets this 1-sigma band is optimistic (t quantile: 1.84x at n = 3, 1.32x at n = 4).")?;
    writeln!(f, "pct_pixels_outside_target_range: pixels colder or warmer than the targets of the applied shot(s).")?;
    writeln!(
        f,
        "Not included: reference instrument accuracy, drift between calibration events (see"
    )?;
    writeln!(f, "position_vs_calibration), panel emissivity mismatch, atmospheric path, sensor non-uniformity.")?;
    f.flush()?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn floats_print_like_pandas() {
        assert_eq!(fmt_float(1.0), "1.0");
        assert_eq!(fmt_float(0.25), "0.25");
        assert_eq!(fmt_float(-3.0), "-3.0");
        assert_eq!(fmt_float(f64::NAN), "");
        assert_eq!(round_to(1.23456789, 4), 1.2346);
    }

    #[test]
    fn output_names_disambiguate_shared_stems() {
        let files = vec![
            PathBuf::from("a/x.tif"),
            PathBuf::from("a/x.jpg"),
            PathBuf::from("a/y.tif"),
        ];
        let n = output_filenames(&files);
        assert_eq!(n[&files[0]], "x_tif_calibrated.tif");
        assert_eq!(n[&files[1]], "x_jpg_calibrated.tif");
        assert_eq!(n[&files[2]], "y_calibrated.tif");
    }

    #[test]
    fn csv_columns_follow_first_appearance() {
        let mut a = Row::default();
        a.set("input_filename", Cell::Str("a".into()));
        a.set("status", Cell::Str("error: x".into()));
        let mut b = Row::default();
        b.set("input_filename", Cell::Str("b".into()));
        b.set("slope_m", Cell::Num(1.0));
        b.set("status", Cell::Str("ok".into()));
        assert_eq!(
            csv_columns(&[a, b]),
            ["input_filename", "status", "slope_m"]
        );
    }
}
