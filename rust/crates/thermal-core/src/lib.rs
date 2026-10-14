//! Engine of the Thermal / MSI Calibration Tool: the empirical line method on drone images.
//!
//! A port of the science core of `thermal_calibration_tool.py` (the Python 0.2.0 release). The Python
//! tool remains the reference: `tests/golden/expected.json` in the repository pins its numbers, and the
//! integration test in `tests/golden.rs` checks this crate against them.
//!
//! Modules, in the order a flight goes through them:
//! - [`timestamp`]: capture time of an image (XMP, EXIF, TIFF tag, file date), kept as camera wall time;
//! - [`raster`]: reading single-band TIFF / PNG / JPEG images, writing calibrated GeoTIFFs;
//! - [`roi`]: polygon targets, the eroded region of interest and its statistics;
//! - [`fit`]: ordinary least squares per calibration shot with the regression diagnostics;
//! - [`interp`]: coefficients interpolated in time between the shots;
//! - [`physics`]: the optional emissivity correction;
//! - [`batch`]: pre-flight check and the processing run that writes outputs and the log;
//! - [`project`]: `*.calib.json` project files and the headless run.

pub mod batch;
pub mod display;
pub mod fit;
pub mod interp;
pub mod model;
pub mod physics;
pub mod project;
pub mod raster;
pub mod roi;
pub mod timestamp;

pub use model::*;

pub const TOOL_NAME: &str = "Thermal / MSI Calibration Tool";
pub const VERSION: &str = env!("CARGO_PKG_VERSION");
/// Pixels eroded from the polygon edge before statistics are taken (the microbolometer blurs 2 to 3 px).
pub const DEFAULT_ROI_INSET_PX: u32 = 3;
/// Fewer ROI pixels than this gives a warning in the user interface.
pub const MIN_ROI_PIXELS: usize = 25;
pub const PROJECT_EXT: &str = ".calib.json";
pub const COLOR_PALETTE: [&str; 10] = [
    "#E63946", "#2A9D8F", "#E9C46A", "#4C7BD9", "#F4A261", "#9B5DE5", "#00F5D4", "#F15BB5",
    "#00BBF9", "#B8E986",
];
pub const IMAGE_EXTENSIONS: [&str; 5] = ["tif", "tiff", "jpg", "jpeg", "png"];

#[derive(Debug, thiserror::Error)]
pub enum Error {
    #[error("{0}")]
    Msg(String),
    #[error("I/O error: {0}")]
    Io(#[from] std::io::Error),
    #[error("TIFF error: {0}")]
    Tiff(#[from] tiff::TiffError),
    #[error("image error: {0}")]
    Image(#[from] image::ImageError),
    #[error("JSON error: {0}")]
    Json(#[from] serde_json::Error),
    #[error("CSV error: {0}")]
    Csv(#[from] csv::Error),
}

impl From<String> for Error {
    fn from(s: String) -> Self {
        Error::Msg(s)
    }
}

impl From<&str> for Error {
    fn from(s: &str) -> Self {
        Error::Msg(s.to_string())
    }
}

pub type Result<T> = std::result::Result<T, Error>;

/// File name of a path for messages (never the full path).
pub fn file_name(path: &std::path::Path) -> String {
    path.file_name()
        .map(|n| n.to_string_lossy().to_string())
        .unwrap_or_else(|| path.display().to_string())
}
