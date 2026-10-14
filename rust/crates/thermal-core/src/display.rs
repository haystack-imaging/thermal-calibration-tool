//! Display previews of a raster for the user interface: inferno, grey, and a percentile-stretched
//! grey. RGB8, NaN pixels black.

use crate::raster::Raster;
use crate::Result;
use std::sync::LazyLock;

/// Matplotlib's inferno colour map by a degree-6 polynomial fit (Matt Zucker's coefficients); the
/// difference to the tabulated map is below 1/255 in most of the range.
fn inferno(t: f64) -> [u8; 3] {
    const C: [[f64; 3]; 7] = [
        [
            0.0002189403691192265,
            0.001651004631001012,
            -0.01948089843709184,
        ],
        [0.1065134194856116, 0.5639564367884091, 3.932712388889277],
        [11.60249308247187, -3.972853965665698, -15.9423941062914],
        [-41.70399613139459, 17.43639888205313, 44.35414519872813],
        [77.162935699427, -33.40235894210092, -81.80730925738993],
        [-71.31942824499214, 32.62606426397723, 73.20951985803202],
        [25.13112622477341, -12.24266895238567, -23.07032500287172],
    ];
    let t = t.clamp(0.0, 1.0);
    let mut out = [0u8; 3];
    for (i, o) in out.iter_mut().enumerate() {
        let v = C[0][i]
            + t * (C[1][i]
                + t * (C[2][i] + t * (C[3][i] + t * (C[4][i] + t * (C[5][i] + t * C[6][i])))));
        *o = (v.clamp(0.0, 1.0) * 255.0).round() as u8;
    }
    out
}

static INFERNO_LUT: LazyLock<Vec<[u8; 3]>> =
    LazyLock::new(|| (0..256).map(|i| inferno(i as f64 / 255.0)).collect());

#[derive(Clone, Debug)]
pub struct Display {
    pub width: usize,
    pub height: usize,
    pub rgb: Vec<u8>,
    /// The value range mapped to black..white (or the colour map ends).
    pub lo: f32,
    pub hi: f32,
}

/// Value range of a stretch: the full finite range, or the 1st..99th percentile for "clahe".
fn stretch_range(img: &Raster, percentile: bool) -> (f32, f32) {
    let mut v: Vec<f32> = img.data.iter().copied().filter(|x| x.is_finite()).collect();
    if v.is_empty() {
        return (0.0, 1.0);
    }
    if percentile {
        v.sort_by(|a, b| a.partial_cmp(b).expect("finite"));
        let at = |p: f64| v[((v.len() - 1) as f64 * p).round() as usize];
        (at(0.01), at(0.99))
    } else {
        let lo = v.iter().copied().fold(f32::INFINITY, f32::min);
        let hi = v.iter().copied().fold(f32::NEG_INFINITY, f32::max);
        (lo, hi)
    }
}

/// RGB preview. `mode`: "inferno", "grey" or "clahe" (percentile-stretched grey for now; a real
/// contrast-limited equalisation is planned). `range` overrides the automatic stretch.
pub fn make_display(img: &Raster, mode: &str, range: Option<(f32, f32)>) -> Display {
    let (lo, hi) = range.unwrap_or_else(|| stretch_range(img, mode == "clahe"));
    let span = if hi > lo { hi - lo } else { 1.0 };
    let mut rgb = Vec::with_capacity(img.data.len() * 3);
    for &v in &img.data {
        if !v.is_finite() {
            rgb.extend_from_slice(&[0, 0, 0]);
            continue;
        }
        let g = (((v - lo) / span).clamp(0.0, 1.0) * 255.0).round() as u8;
        match mode {
            "inferno" => rgb.extend_from_slice(&INFERNO_LUT[g as usize]),
            _ => rgb.extend_from_slice(&[g, g, g]),
        }
    }
    Display {
        width: img.width,
        height: img.height,
        rgb,
        lo,
        hi,
    }
}

/// The preview as PNG bytes (for a web view).
pub fn encode_png(d: &Display) -> Result<Vec<u8>> {
    let img = image::RgbImage::from_raw(d.width as u32, d.height as u32, d.rgb.clone())
        .ok_or_else(|| crate::Error::Msg("display buffer size mismatch".into()))?;
    let mut buf = std::io::Cursor::new(Vec::new());
    img.write_to(&mut buf, image::ImageFormat::Png)?;
    Ok(buf.into_inner())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn inferno_ends_dark_and_bright() {
        let (lo, hi) = (INFERNO_LUT[0], INFERNO_LUT[255]);
        assert!(lo.iter().all(|&c| c < 20), "{lo:?}");
        assert!(hi[0] > 240 && hi[1] > 240 && hi[2] > 100, "{hi:?}");
    }

    #[test]
    fn stretch_and_png() {
        let r = Raster::new(4, 1, vec![10.0, 20.0, f32::NAN, 30.0]);
        let d = make_display(&r, "grey", None);
        assert_eq!((d.lo, d.hi), (10.0, 30.0));
        assert_eq!(&d.rgb[..3], &[0, 0, 0]);
        assert_eq!(&d.rgb[6..9], &[0, 0, 0], "NaN is black");
        assert_eq!(&d.rgb[9..12], &[255, 255, 255]);
        let png = encode_png(&d).unwrap();
        assert_eq!(&png[1..4], b"PNG");
    }
}
