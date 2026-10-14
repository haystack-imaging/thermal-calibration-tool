//! The data model: targets, fits, events and settings. Mirrors the Python dataclasses one to one.

use crate::COLOR_PALETTE;
use chrono::NaiveDateTime;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::path::{Path, PathBuf};

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Mode {
    Thermal,
    Msi,
}

impl Mode {
    pub fn as_str(self) -> &'static str {
        match self {
            Mode::Thermal => "thermal",
            Mode::Msi => "msi",
        }
    }

    pub fn parse(s: &str) -> Mode {
        if s == "msi" {
            Mode::Msi
        } else {
            Mode::Thermal
        }
    }

    pub fn is_thermal(self) -> bool {
        matches!(self, Mode::Thermal)
    }
}

/// `Target 3` (what `add_target` assigns) or empty; anything else was typed by the user.
pub fn is_default_target_name(name: &str) -> bool {
    let n = name.trim();
    n.is_empty()
        || n.strip_prefix("Target ")
            .is_some_and(|digits| !digits.is_empty() && digits.chars().all(|c| c.is_ascii_digit()))
}

/// `T1` for a default name, `T1 black` for a user-named target.
pub fn target_label(index: usize, name: &str) -> String {
    if is_default_target_name(name) {
        format!("T{}", index + 1)
    } else {
        format!("T{} {}", index + 1, name.trim())
    }
}

/// One reference target on one calibration image.
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct CalibrationTarget {
    pub name: String,
    pub color: String,
    /// Image pixel coordinates of the corners.
    pub polygon: Vec<(i32, i32)>,
    /// Mean of the inner ROI, raw sensor units.
    pub drone_mean: Option<f64>,
    pub drone_std: Option<f64>,
    pub pixel_count: usize,
    /// None = not entered (0.0 is a valid value).
    pub ground_truth: Option<f64>,
    /// False when the ROI was too small to erode.
    pub inset_applied: bool,
}

impl CalibrationTarget {
    pub fn new(name: &str, color: &str) -> Self {
        CalibrationTarget {
            name: name.to_string(),
            color: color.to_string(),
            polygon: Vec::new(),
            drone_mean: None,
            drone_std: None,
            pixel_count: 0,
            ground_truth: None,
            inset_applied: true,
        }
    }

    pub fn has_polygon(&self) -> bool {
        self.polygon.len() >= 3
    }

    /// Usable in a fit: polygon drawn, a finite ROI mean (a polygon outside the image or over NaN
    /// pixels has none) and a reference value entered.
    pub fn is_complete(&self) -> bool {
        self.has_polygon()
            && self.drone_mean.is_some_and(f64::is_finite)
            && self.ground_truth.is_some_and(f64::is_finite)
    }

    pub fn to_json(&self) -> Value {
        json!({
            "name": self.name,
            "color": self.color,
            "polygon": self.polygon.iter().map(|(x, y)| json!([x, y])).collect::<Vec<_>>(),
            "ground_truth": self.ground_truth,
        })
    }

    pub fn from_json(v: &Value) -> Self {
        let mut t = CalibrationTarget::new(
            v.get("name").and_then(Value::as_str).unwrap_or("Target"),
            v.get("color")
                .and_then(Value::as_str)
                .unwrap_or(COLOR_PALETTE[0]),
        );
        if let Some(poly) = v.get("polygon").and_then(Value::as_array) {
            t.polygon = poly
                .iter()
                .filter_map(|p| {
                    let a = p.as_array()?;
                    Some((a.first()?.as_f64()? as i32, a.get(1)?.as_f64()? as i32))
                })
                .collect();
        }
        t.ground_truth = v.get("ground_truth").and_then(Value::as_f64);
        t
    }
}

/// Result of a calibration fit, with the numbers needed for an uncertainty statement.
#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct FitResult {
    pub slope: f64,
    pub intercept: f64,
    pub n: usize,
    pub single_point: bool,
    pub r_squared: Option<f64>,
    /// Root mean square of the residuals, sqrt(SSE / n), descriptive.
    pub rmse: Option<f64>,
    /// Residual standard error, s = sqrt(SSE / (n - 2)), n > 2.
    pub resid_std: Option<f64>,
    pub se_slope: Option<f64>,
    pub se_intercept: Option<f64>,
    /// Leave-one-out RMSE, n >= 3.
    pub loo_rmse: Option<f64>,
    pub residuals: Vec<f64>,
    pub x_values: Vec<f64>,
    pub y_values: Vec<f64>,
    pub x_mean: f64,
    pub sxx: f64,
    pub x_min: f64,
    pub x_max: f64,
}

impl FitResult {
    pub fn predict(&self, x: f64) -> f64 {
        self.slope * x + self.intercept
    }

    /// 1-sigma uncertainty of a calibrated value at sensor value `x0` (prediction interval width).
    /// Needs at least 3 targets; with 2 the line passes through both points and nothing is left to
    /// estimate the scatter from.
    pub fn predict_uncertainty(&self, x0: f64) -> Option<f64> {
        if self.single_point || self.n < 3 || self.sxx <= 0.0 {
            return None;
        }
        let s = self.resid_std?;
        Some(s * (1.0 + 1.0 / self.n as f64 + (x0 - self.x_mean).powi(2) / self.sxx).sqrt())
    }

    pub fn summary_line(&self, unit: &str) -> String {
        if self.single_point {
            return format!("K = {:.8}", self.slope);
        }
        let r2 = match self.r_squared {
            Some(r) if r.is_finite() => format!("{r:.4}"),
            _ => "n/a".to_string(),
        };
        let mut s = format!(
            "m = {:.4}, b = {:.4}, R² = {}, n = {}",
            self.slope, self.intercept, r2, self.n
        );
        if let Some(rmse) = self.rmse {
            s.push_str(&format!(", RMSE = {rmse:.2} {unit}"));
        }
        s
    }

    pub fn to_json(&self) -> Value {
        json!({
            "slope": self.slope, "intercept": self.intercept, "n": self.n, "r_squared": self.r_squared,
            "rmse": self.rmse, "resid_std": self.resid_std, "se_slope": self.se_slope,
            "se_intercept": self.se_intercept, "loo_rmse": self.loo_rmse, "single_point": self.single_point,
        })
    }
}

/// Optional emissivity correction (thermal only). See the README: usually leave it off.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct EmissivitySettings {
    pub enabled: bool,
    /// Emissivity of the calibration panels.
    pub target_emissivity: f64,
    /// Emissivity of the surveyed surface (vegetation about 0.97 to 0.99).
    pub scene_emissivity: f64,
    /// Reflected (sky) temperature, °C.
    pub sky_temperature: f64,
}

impl Default for EmissivitySettings {
    fn default() -> Self {
        EmissivitySettings {
            enabled: false,
            target_emissivity: 0.95,
            scene_emissivity: 0.98,
            sky_temperature: -40.0,
        }
    }
}

impl EmissivitySettings {
    /// Emissivities outside (0.5, 1] and sky temperatures outside [-100, 60] °C are typing errors.
    pub fn bounded(mut self) -> Self {
        self.target_emissivity = self.target_emissivity.clamp(0.5, 1.0);
        self.scene_emissivity = self.scene_emissivity.clamp(0.5, 1.0);
        self.sky_temperature = self.sky_temperature.clamp(-100.0, 60.0);
        self
    }

    pub fn to_json(&self) -> Value {
        json!({"enabled": self.enabled, "target_emissivity": self.target_emissivity,
               "scene_emissivity": self.scene_emissivity, "sky_temperature": self.sky_temperature})
    }

    pub fn from_json(v: &Value) -> Self {
        let d = EmissivitySettings::default();
        EmissivitySettings {
            enabled: v.get("enabled").and_then(Value::as_bool).unwrap_or(false),
            target_emissivity: v
                .get("target_emissivity")
                .and_then(Value::as_f64)
                .unwrap_or(d.target_emissivity),
            scene_emissivity: v
                .get("scene_emissivity")
                .and_then(Value::as_f64)
                .unwrap_or(d.scene_emissivity),
            sky_temperature: v
                .get("sky_temperature")
                .and_then(Value::as_f64)
                .unwrap_or(d.sky_temperature),
        }
        .bounded()
    }
}

/// Capture time of an image and where it came from.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Timestamp {
    pub value: Option<NaiveDateTime>,
    /// 'XMP', 'EXIF', 'TIFF tag', 'file mtime', 'none'.
    pub source: String,
    /// '+02:00', 'Z', or '' when the source carries no zone.
    pub tz: String,
}

impl Timestamp {
    pub fn none() -> Self {
        Timestamp {
            value: None,
            source: "none".to_string(),
            tz: String::new(),
        }
    }

    pub fn new(value: NaiveDateTime, source: &str, tz: &str) -> Self {
        Timestamp {
            value: Some(value),
            source: source.to_string(),
            tz: tz.to_string(),
        }
    }

    pub fn reliable(&self) -> bool {
        self.value.is_some() && self.source != "file mtime" && self.source != "none"
    }
}

/// One calibration image: its capture time, its targets and the fitted coefficients.
#[derive(Clone, Debug)]
pub struct CalibrationEvent {
    pub image_path: PathBuf,
    pub timestamp: NaiveDateTime,
    pub timestamp_source: String,
    pub timestamp_tz: String,
    pub targets: Vec<CalibrationTarget>,
    pub fit: Option<FitResult>,
}

impl CalibrationEvent {
    /// An event with the two default targets.
    pub fn new(
        image_path: impl Into<PathBuf>,
        timestamp: NaiveDateTime,
        source: &str,
        tz: &str,
    ) -> Self {
        Self::with_targets(image_path, timestamp, source, tz, Vec::new())
    }

    /// An event with the given targets (the two defaults when the list is empty).
    pub fn with_targets(
        image_path: impl Into<PathBuf>,
        timestamp: NaiveDateTime,
        source: &str,
        tz: &str,
        mut targets: Vec<CalibrationTarget>,
    ) -> Self {
        if targets.is_empty() {
            targets = vec![
                CalibrationTarget::new("Target 1", COLOR_PALETTE[0]),
                CalibrationTarget::new("Target 2", COLOR_PALETTE[1]),
            ];
        }
        CalibrationEvent {
            image_path: image_path.into(),
            timestamp,
            timestamp_source: source.to_string(),
            timestamp_tz: tz.to_string(),
            targets,
            fit: None,
        }
    }

    pub fn file_name(&self) -> String {
        crate::file_name(&self.image_path)
    }

    pub fn is_fitted(&self) -> bool {
        self.fit.is_some()
    }

    pub fn add_target(&mut self) -> &mut CalibrationTarget {
        let idx = self.targets.len();
        self.targets.push(CalibrationTarget::new(
            &format!("Target {}", idx + 1),
            COLOR_PALETTE[idx % COLOR_PALETTE.len()],
        ));
        self.targets.last_mut().expect("just pushed")
    }

    /// Removes a target; default names and colours are renumbered, custom names stay.
    pub fn remove_target(&mut self, index: usize, min_targets: usize) -> bool {
        if self.targets.len() <= min_targets || index >= self.targets.len() {
            return false;
        }
        self.targets.remove(index);
        for (i, t) in self.targets.iter_mut().enumerate() {
            if is_default_target_name(&t.name) {
                t.name = format!("Target {}", i + 1);
            }
            t.color = COLOR_PALETTE[i % COLOR_PALETTE.len()].to_string();
        }
        true
    }

    /// The event as stored in a project file. Paths are absolute and, with `project_dir`, also
    /// relative to it, so a folder that moves as a whole still opens.
    pub fn to_json(&self, project_dir: Option<&Path>) -> Value {
        let abs = std::path::absolute(&self.image_path).unwrap_or_else(|_| self.image_path.clone());
        let mut d = json!({
            "path": abs.to_string_lossy(),
            "timestamp": isoformat(self.timestamp),
            "timestamp_source": self.timestamp_source,
            "timestamp_tz": self.timestamp_tz,
            "targets": self.targets.iter().map(CalibrationTarget::to_json).collect::<Vec<_>>(),
        });
        if let Some(dir) = project_dir {
            if let Some(rel) = relative_path(&abs, dir) {
                d["path_relative"] = Value::String(rel);
            }
        }
        if let Some(f) = &self.fit {
            d["fit"] = f.to_json();
            d["targets_measured"] = Value::Array(
                self.targets
                    .iter()
                    .filter(|t| t.is_complete())
                    .map(|t| json!({"name": t.name, "mean": t.drone_mean, "std": t.drone_std, "pixels": t.pixel_count}))
                    .collect(),
            );
        }
        d
    }
}

/// Python's `datetime.isoformat()`: seconds always, microseconds only when they are not zero.
pub fn isoformat(dt: NaiveDateTime) -> String {
    use chrono::Timelike;
    if dt.nanosecond() == 0 {
        dt.format("%Y-%m-%dT%H:%M:%S").to_string()
    } else {
        dt.format("%Y-%m-%dT%H:%M:%S%.6f").to_string()
    }
}

/// `os.path.relpath(path, base)` without touching the file system; None when the two have no common
/// root (another drive on Windows).
pub fn relative_path(path: &Path, base: &Path) -> Option<String> {
    use std::path::Component;
    let path = std::path::absolute(path).ok()?;
    let base = std::path::absolute(base).ok()?;
    let p: Vec<Component> = path.components().collect();
    let b: Vec<Component> = base.components().collect();
    if p.first() != b.first() {
        return None; // different prefix / drive
    }
    let common = p.iter().zip(b.iter()).take_while(|(a, c)| a == c).count();
    let mut out = PathBuf::new();
    for _ in common..b.len() {
        out.push("..");
    }
    for c in &p[common..] {
        out.push(c.as_os_str());
    }
    let s = out.to_string_lossy().to_string();
    Some(if s.is_empty() { ".".to_string() } else { s })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn names_and_labels() {
        assert!(is_default_target_name("Target 3") && is_default_target_name("  "));
        assert!(!is_default_target_name("black") && !is_default_target_name("Target x"));
        assert_eq!(target_label(0, "Target 1"), "T1");
        assert_eq!(target_label(2, " white "), "T3 white");
    }

    #[test]
    fn remove_target_renumbers_defaults_only() {
        let mut ev = CalibrationEvent::new("a.tif", NaiveDateTime::default(), "none", "");
        ev.targets[0].name = "black".into();
        ev.add_target();
        assert!(ev.remove_target(1, 1));
        assert_eq!(
            ev.targets
                .iter()
                .map(|t| t.name.as_str())
                .collect::<Vec<_>>(),
            ["black", "Target 2"]
        );
        assert_eq!(ev.targets[1].color, COLOR_PALETTE[1]);
        assert!(!ev.remove_target(0, 2));
    }

    #[test]
    fn uncertainty_needs_three_targets() {
        let f = FitResult {
            n: 2,
            sxx: 1.0,
            resid_std: Some(1.0),
            ..Default::default()
        };
        assert!(f.predict_uncertainty(0.0).is_none());
        let f = FitResult {
            n: 3,
            sxx: 2.0,
            x_mean: 1.0,
            resid_std: Some(0.5),
            ..Default::default()
        };
        let u = f.predict_uncertainty(2.0).unwrap();
        assert!((u - 0.5 * (1.0 + 1.0 / 3.0 + 0.5f64).sqrt()).abs() < 1e-12);
    }

    #[test]
    fn isoformat_matches_python() {
        let dt = NaiveDateTime::parse_from_str("2026-06-01 10:00:00", "%Y-%m-%d %H:%M:%S").unwrap();
        assert_eq!(isoformat(dt), "2026-06-01T10:00:00");
        let dt =
            NaiveDateTime::parse_from_str("2026-06-01 10:00:00.5", "%Y-%m-%d %H:%M:%S%.f").unwrap();
        assert_eq!(isoformat(dt), "2026-06-01T10:00:00.500000");
    }

    #[test]
    fn relative_paths() {
        assert_eq!(
            relative_path(Path::new("/a/b/c.tif"), Path::new("/a/b")).as_deref(),
            Some("c.tif")
        );
        assert_eq!(
            relative_path(Path::new("/a/x/c.tif"), Path::new("/a/b")).as_deref(),
            Some("../x/c.tif")
        );
    }
}
