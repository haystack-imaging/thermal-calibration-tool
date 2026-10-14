//! Reading single-band images and writing calibrated GeoTIFFs, in pure Rust.
//!
//! TIFF (including GeoTIFF written by GDAL / the DJI conversion script) is read with the `tiff` crate,
//! PNG and JPEG with `image`. Outputs are 32-bit float GeoTIFFs with LZW compression; the GeoTIFF
//! tags, the TIFF date and the GDAL metadata block are copied from the source, NaN is declared as
//! nodata. With exiftool on the PATH the full EXIF/XMP/GPS block is copied as well.

use crate::{file_name, Result, TOOL_NAME, VERSION};
use std::fs::File;
use std::io::{BufReader, BufWriter, Write};
use std::path::{Path, PathBuf};
use std::process::Command;
use tiff::decoder::{Decoder, DecodingResult};
use tiff::encoder::{colortype, compression::Lzw, TiffEncoder};
use tiff::tags::Tag;
use tiff::ColorType;

/// GDAL's metadata tag (42112); the `tiff` crate names only the nodata one.
const GDAL_METADATA: Tag = Tag::Unknown(42112);

/// Band count, size and data type of an image, without reading the pixels where possible.
#[derive(Clone, Debug, Default)]
pub struct ImageInfo {
    pub path: String,
    pub width: u32,
    pub height: u32,
    pub bands: u32,
    /// numpy-style: `float32`, `uint16`, `uint8` ...
    pub dtype: String,
    pub driver: String,
    pub error: String,
}

impl ImageInfo {
    pub fn is_single_band(&self) -> bool {
        self.bands == 1
    }

    pub fn is_float(&self) -> bool {
        self.dtype.starts_with("float")
    }

    pub fn is_8bit(&self) -> bool {
        self.dtype == "uint8" || self.dtype == "int8"
    }

    pub fn describe(&self) -> String {
        if !self.error.is_empty() {
            return format!("unreadable ({})", self.error);
        }
        format!(
            "{}×{}, {} band{}, {}",
            self.width,
            self.height,
            self.bands,
            if self.bands != 1 { "s" } else { "" },
            self.dtype
        )
    }
}

fn tiff_dtype(sample_format: u32, bits: u8) -> String {
    match (sample_format, bits) {
        (3, 32) => "float32".into(),
        (3, 64) => "float64".into(),
        (2, 8) => "int8".into(),
        (2, 16) => "int16".into(),
        (2, 32) => "int32".into(),
        (2, 64) => "int64".into(),
        (_, 8) => "uint8".into(),
        (_, 16) => "uint16".into(),
        (_, 32) => "uint32".into(),
        (_, 64) => "uint64".into(),
        (_, b) => format!("{b}bit"),
    }
}

fn inspect_tiff(path: &Path) -> Result<ImageInfo> {
    let mut dec = Decoder::new(BufReader::new(File::open(path)?))?;
    let (width, height) = dec.dimensions()?;
    let (bands, bits) = match dec.colortype()? {
        ColorType::Gray(b) => (1, b),
        ColorType::RGB(b) => (3, b),
        ColorType::RGBA(b) => (4, b),
        ColorType::CMYK(b) => (4, b),
        ColorType::YCbCr(b) => (3, b),
        _ => (1, 8),
    };
    let fmt = dec
        .get_tag_u32_vec(Tag::SampleFormat)
        .ok()
        .and_then(|v| v.first().copied())
        .unwrap_or(1);
    Ok(ImageInfo {
        path: path.display().to_string(),
        width,
        height,
        bands,
        dtype: tiff_dtype(fmt, bits),
        driver: "GTiff".into(),
        error: String::new(),
    })
}

fn inspect_other(path: &Path) -> Result<ImageInfo> {
    let reader = image::ImageReader::open(path)?.with_guessed_format()?;
    let driver = reader
        .format()
        .map(|f| format!("{f:?}"))
        .unwrap_or_default();
    let img = reader.decode()?;
    let color = img.color();
    let dtype = match color {
        image::ColorType::L16
        | image::ColorType::La16
        | image::ColorType::Rgb16
        | image::ColorType::Rgba16 => "uint16",
        image::ColorType::Rgb32F | image::ColorType::Rgba32F => "float32",
        _ => "uint8",
    };
    Ok(ImageInfo {
        path: path.display().to_string(),
        width: img.width(),
        height: img.height(),
        bands: color.channel_count() as u32,
        dtype: dtype.into(),
        driver,
        error: String::new(),
    })
}

/// Band count, size and data type. A file neither `tiff` nor `image` can open gets an `error`.
pub fn inspect_image(path: &Path) -> ImageInfo {
    let e1 = match inspect_tiff(path) {
        Ok(info) => return info,
        Err(e) => e,
    };
    match inspect_other(path) {
        Ok(info) => info,
        Err(e2) => ImageInfo {
            path: path.display().to_string(),
            error: format!("{e1}; {e2}"),
            ..Default::default()
        },
    }
}

/// A single-band float image, row-major; NaN marks nodata.
#[derive(Clone, Debug, PartialEq)]
pub struct Raster {
    pub width: usize,
    pub height: usize,
    pub data: Vec<f32>,
}

impl Raster {
    pub fn new(width: usize, height: usize, data: Vec<f32>) -> Self {
        assert_eq!(data.len(), width * height, "raster size mismatch");
        Raster {
            width,
            height,
            data,
        }
    }

    pub fn get(&self, x: usize, y: usize) -> f32 {
        self.data[y * self.width + x]
    }

    /// Mean, min and max of the finite pixels; None when there are none.
    pub fn finite_stats(&self) -> Option<(f64, f64, f64)> {
        let mut n = 0usize;
        let (mut sum, mut lo, mut hi) = (0.0f64, f64::INFINITY, f64::NEG_INFINITY);
        for &v in &self.data {
            if v.is_finite() {
                let v = v as f64;
                n += 1;
                sum += v;
                lo = lo.min(v);
                hi = hi.max(v);
            }
        }
        (n > 0).then(|| (sum / n as f64, lo, hi))
    }
}

fn load_tiff(path: &Path) -> Result<Raster> {
    let mut dec = Decoder::new(BufReader::new(File::open(path)?))?;
    let (w, h) = dec.dimensions()?;
    let nodata = dec
        .get_tag_ascii_string(Tag::GdalNodata)
        .ok()
        .and_then(|s| s.trim().parse::<f64>().ok());
    let data: Vec<f32> = match dec.read_image()? {
        DecodingResult::U8(v) => v.into_iter().map(f32::from).collect(),
        DecodingResult::U16(v) => v.into_iter().map(f32::from).collect(),
        DecodingResult::U32(v) => v.into_iter().map(|x| x as f32).collect(),
        DecodingResult::U64(v) => v.into_iter().map(|x| x as f32).collect(),
        DecodingResult::I8(v) => v.into_iter().map(f32::from).collect(),
        DecodingResult::I16(v) => v.into_iter().map(f32::from).collect(),
        DecodingResult::I32(v) => v.into_iter().map(|x| x as f32).collect(),
        DecodingResult::I64(v) => v.into_iter().map(|x| x as f32).collect(),
        DecodingResult::F32(v) => v,
        DecodingResult::F64(v) => v.into_iter().map(|x| x as f32).collect(),
    };
    if data.len() != (w as usize) * (h as usize) {
        return Err(format!(
            "{}: unexpected pixel count {} for {}×{}",
            file_name(path),
            data.len(),
            w,
            h
        )
        .into());
    }
    let mut r = Raster::new(w as usize, h as usize, data);
    if let Some(nd) = nodata {
        if !nd.is_nan() {
            let ndf = nd as f32;
            for p in &mut r.data {
                if *p == ndf {
                    *p = f32::NAN; // nodata is not a temperature
                }
            }
        }
    }
    Ok(r)
}

fn load_other(path: &Path) -> Result<Raster> {
    let img = image::open(path)?;
    let (w, h) = (img.width() as usize, img.height() as usize);
    let data: Vec<f32> = match img {
        image::DynamicImage::ImageLuma8(b) => b.into_raw().into_iter().map(f32::from).collect(),
        image::DynamicImage::ImageLuma16(b) => b.into_raw().into_iter().map(f32::from).collect(),
        other => other
            .into_luma16()
            .into_raw()
            .into_iter()
            .map(f32::from)
            .collect(),
    };
    Ok(Raster::new(w, h, data))
}

/// Load a single-band image as float32 (assumed to be in the final units: °C or DN). Multi-band
/// images (colour previews, R-JPEGs) are refused: their pixel values are palette colours.
pub fn load_image(path: &Path) -> Result<Raster> {
    let info = inspect_image(path);
    if !info.error.is_empty() {
        return Err(format!("cannot read {}: {}", file_name(path), info.error).into());
    }
    if !info.is_single_band() {
        return Err(format!(
            "{} has {} bands. Only single-band images hold temperatures or DN; a colour R-JPEG preview must be \
             converted first (see README).",
            file_name(path),
            info.bands
        )
        .into());
    }
    if info.driver == "GTiff" {
        load_tiff(path)
    } else {
        load_other(path)
    }
}

/// What is carried over from a source TIFF to its calibrated output.
#[derive(Clone, Debug, Default)]
pub struct SourceTags {
    pub pixel_scale: Option<Vec<f64>>,
    pub tiepoint: Option<Vec<f64>>,
    pub transformation: Option<Vec<f64>>,
    pub geo_key_directory: Option<Vec<u16>>,
    pub geo_double_params: Option<Vec<f64>>,
    pub geo_ascii_params: Option<String>,
    pub datetime: Option<String>,
    pub image_description: Option<String>,
    pub artist: Option<String>,
    pub copyright: Option<String>,
    pub xmp: Option<Vec<u8>>,
    /// GDAL metadata items as (name, extra attributes, escaped value), in file order.
    pub gdal_items: Vec<(String, String, String)>,
}

impl SourceTags {
    pub fn is_georeferenced(&self) -> bool {
        self.pixel_scale.is_some()
            || self.tiepoint.is_some()
            || self.transformation.is_some()
            || self.geo_key_directory.is_some()
    }
}

/// Parse a `<GDALMetadata>` block into (name, other attributes, raw value) items.
pub fn parse_gdal_metadata(xml: &str) -> Vec<(String, String, String)> {
    let re =
        regex::Regex::new(r#"<Item\s+name="([^"]*)"([^>]*)>([^<]*)</Item>"#).expect("static regex");
    re.captures_iter(xml)
        .map(|c| (c[1].to_string(), c[2].trim().to_string(), c[3].to_string()))
        .collect()
}

fn xml_escape(s: &str) -> String {
    s.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
}

/// The `<GDALMetadata>` block GDAL and rasterio read as tags.
pub fn gdal_metadata_xml(items: &[(String, String, String)]) -> String {
    let mut s = String::from("<GDALMetadata>\n");
    for (name, attrs, value) in items {
        let attrs = if attrs.is_empty() {
            String::new()
        } else {
            format!(" {attrs}")
        };
        s.push_str(&format!(
            "  <Item name=\"{}\"{}>{}</Item>\n",
            xml_escape(name),
            attrs,
            value
        ));
    }
    s.push_str("</GDALMetadata>\n");
    s
}

/// Read the tags of a source TIFF that the output should carry.
pub fn read_source_tags(path: &Path, with_metadata: bool) -> Result<SourceTags> {
    let mut dec = Decoder::new(BufReader::new(File::open(path)?))?;
    let mut t = SourceTags {
        pixel_scale: dec.get_tag_f64_vec(Tag::ModelPixelScaleTag).ok(),
        tiepoint: dec.get_tag_f64_vec(Tag::ModelTiepointTag).ok(),
        transformation: dec.get_tag_f64_vec(Tag::ModelTransformationTag).ok(),
        geo_key_directory: dec
            .get_tag_u32_vec(Tag::GeoKeyDirectoryTag)
            .ok()
            .map(|v| v.into_iter().map(|x| x as u16).collect()),
        geo_double_params: dec.get_tag_f64_vec(Tag::GeoDoubleParamsTag).ok(),
        geo_ascii_params: dec.get_tag_ascii_string(Tag::GeoAsciiParamsTag).ok(),
        ..Default::default()
    };
    if with_metadata {
        t.datetime = dec.get_tag_ascii_string(Tag::DateTime).ok();
        t.image_description = dec.get_tag_ascii_string(Tag::ImageDescription).ok();
        t.artist = dec.get_tag_ascii_string(Tag::Artist).ok();
        t.copyright = dec.get_tag_ascii_string(Tag::Copyright).ok();
        t.xmp = dec.get_tag_u8_vec(Tag::Unknown(700)).ok();
        if let Ok(xml) = dec.get_tag_ascii_string(GDAL_METADATA) {
            t.gdal_items = parse_gdal_metadata(&xml)
                .into_iter()
                .filter(|(name, _, _)| !name.starts_with("TIFFTAG_SOFTWARE"))
                .collect();
        }
    }
    Ok(t)
}

/// Where exiftool is, if it is on the PATH.
pub fn exiftool_path() -> Option<PathBuf> {
    let path = std::env::var_os("PATH")?;
    for dir in std::env::split_paths(&path) {
        for name in ["exiftool", "exiftool.exe"] {
            let p = dir.join(name);
            if p.is_file() {
                return Some(p);
            }
        }
    }
    None
}

/// Arguments that copy EXIF/XMP/GPS but never the source's GDAL metadata, software tag or GeoTIFF keys:
/// those describe the *output* and were just written.
pub const EXIFTOOL_COPY_ARGS: [&str; 14] = [
    "-all:all",
    "-xmp",
    "-gps:all",
    "-EXIF:DateTimeOriginal",
    "--GDALMetadata",
    "--GDALNoData",
    "--Software",
    "--GeoTiffDirectory",
    "--GeoTiffDoubleParams",
    "--GeoTiffAsciiParams",
    "--ModelTiePoint",
    "--PixelScale",
    "--ModelTransform",
    "-overwrite_original",
];

/// Copy EXIF/XMP/GPS from each source to its output in ONE exiftool process (an argument file with
/// `-execute` separators). Returns notes for the log; empty when everything went well or exiftool is
/// not installed.
pub fn copy_metadata_with_exiftool(pairs: &[(PathBuf, PathBuf)]) -> Vec<String> {
    let mut notes = Vec::new();
    let Some(exiftool) = exiftool_path() else {
        return notes;
    };
    if pairs.is_empty() {
        return notes;
    }
    let argfile = std::env::temp_dir().join(format!(
        "thermal_calibration_{}_{}.args",
        std::process::id(),
        pairs.len()
    ));
    let write = (|| -> std::io::Result<()> {
        let mut f = BufWriter::new(File::create(&argfile)?);
        for (src, dst) in pairs {
            writeln!(f, "-q\n-q\n-tagsFromFile\n{}", src.display())?;
            for a in EXIFTOOL_COPY_ARGS {
                writeln!(f, "{a}")?;
            }
            writeln!(f, "{}\n-execute", dst.display())?;
        }
        f.flush()
    })();
    if let Err(e) = write {
        notes.push(format!("exiftool metadata copy failed: {e}"));
        return notes;
    }
    match Command::new(exiftool).arg("-@").arg(&argfile).output() {
        Ok(out) => {
            let err = String::from_utf8_lossy(&out.stderr).trim().to_string();
            if !out.status.success() || !err.is_empty() {
                let msg = if err.is_empty() {
                    String::from_utf8_lossy(&out.stdout).trim().to_string()
                } else {
                    err
                };
                notes.push(format!(
                    "exiftool metadata copy reported: {}",
                    msg.chars().take(300).collect::<String>()
                ));
            }
        }
        Err(e) => notes.push(format!("exiftool metadata copy failed: {e}")),
    }
    let _ = std::fs::remove_file(&argfile);
    notes
}

/// Write a 32-bit float GeoTIFF (LZW). Georeferencing, the TIFF date, XMP and the GDAL metadata are
/// copied from the source; a source without georeferencing gets none (no invented transform). NaN
/// pixels are declared as nodata. With `defer_exiftool` the EXIF/XMP/GPS copy is left to the caller,
/// who batches it. Returns notes for the log.
pub fn save_calibrated_geotiff(
    img: &Raster,
    output_path: &Path,
    source_path: Option<&Path>,
    extra_tags: &[(String, String)],
    copy_metadata: bool,
    defer_exiftool: bool,
) -> Result<Vec<String>> {
    let mut notes = Vec::new();
    let mut src = SourceTags::default();
    if let Some(sp) = source_path {
        match read_source_tags(sp, copy_metadata) {
            Ok(t) => src = t,
            Err(e) => notes.push(format!("could not read georeferencing of source: {e}")),
        }
    }
    let mut items = src.gdal_items.clone();
    for (k, v) in extra_tags {
        items.retain(|(name, _, _)| name != k);
        items.push((k.clone(), String::new(), xml_escape(v)));
    }
    {
        let file = BufWriter::new(File::create(output_path)?);
        let mut enc = TiffEncoder::new(file)?;
        let mut image = enc.new_image_with_compression::<colortype::Gray32Float, Lzw>(
            img.width as u32,
            img.height as u32,
            Lzw,
        )?;
        {
            let e = image.encoder();
            if let Some(v) = &src.pixel_scale {
                e.write_tag(Tag::ModelPixelScaleTag, v.as_slice())?;
            }
            if let Some(v) = &src.tiepoint {
                e.write_tag(Tag::ModelTiepointTag, v.as_slice())?;
            }
            if let Some(v) = &src.transformation {
                e.write_tag(Tag::ModelTransformationTag, v.as_slice())?;
            }
            if let Some(v) = &src.geo_key_directory {
                e.write_tag(Tag::GeoKeyDirectoryTag, v.as_slice())?;
            }
            if let Some(v) = &src.geo_double_params {
                e.write_tag(Tag::GeoDoubleParamsTag, v.as_slice())?;
            }
            if let Some(v) = &src.geo_ascii_params {
                e.write_tag(Tag::GeoAsciiParamsTag, v.as_str())?;
            }
            if let Some(v) = &src.datetime {
                e.write_tag(Tag::DateTime, v.as_str())?;
            }
            if let Some(v) = &src.image_description {
                e.write_tag(Tag::ImageDescription, v.as_str())?;
            }
            if let Some(v) = &src.artist {
                e.write_tag(Tag::Artist, v.as_str())?;
            }
            if let Some(v) = &src.copyright {
                e.write_tag(Tag::Copyright, v.as_str())?;
            }
            if let Some(v) = &src.xmp {
                e.write_tag(Tag::Unknown(700), v.as_slice())?;
            }
            e.write_tag(Tag::Software, format!("{TOOL_NAME} {VERSION}").as_str())?;
            e.write_tag(Tag::GdalNodata, "nan")?;
            if !items.is_empty() {
                e.write_tag(GDAL_METADATA, gdal_metadata_xml(&items).as_str())?;
            }
        }
        image.write_data(&img.data)?;
    }
    if copy_metadata && !defer_exiftool {
        if let Some(sp) = source_path {
            notes.extend(copy_metadata_with_exiftool(&[(
                sp.to_path_buf(),
                output_path.to_path_buf(),
            )]));
        }
    }
    Ok(notes)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn gdal_metadata_round_trip() {
        let items = vec![
            ("A".to_string(), String::new(), "1 &amp; 2".to_string()),
            ("B".to_string(), "sample=\"0\"".to_string(), "x".to_string()),
        ];
        let xml = gdal_metadata_xml(&items);
        assert_eq!(parse_gdal_metadata(&xml), items);
    }

    #[test]
    fn write_then_read_keeps_pixels_and_nan() {
        let dir = std::env::temp_dir().join(format!("thermal_core_rt_{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let p = dir.join("t.tif");
        let mut data: Vec<f32> = (0..200).map(|i| i as f32 * 0.5 - 10.0).collect();
        data[7] = f32::NAN;
        let r = Raster::new(20, 10, data);
        let tags = vec![(
            "CALIBRATION_FORMULA".to_string(),
            "out = 1 * in + 0".to_string(),
        )];
        save_calibrated_geotiff(&r, &p, None, &tags, false, true).unwrap();
        let info = inspect_image(&p);
        assert_eq!(
            (info.width, info.height, info.bands, info.dtype.as_str()),
            (20, 10, 1, "float32")
        );
        let back = load_image(&p).unwrap();
        assert_eq!(back.width, 20);
        assert!(back.data[7].is_nan() && back.data[8] == r.data[8]);
        let tags = read_source_tags(&p, true).unwrap();
        assert_eq!(tags.gdal_items[0].0, "CALIBRATION_FORMULA");
        assert!(!tags.is_georeferenced());
        let _ = std::fs::remove_dir_all(&dir);
    }
}
