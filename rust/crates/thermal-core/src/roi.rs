//! Polygon targets: the filled polygon, the eroded region of interest and its statistics.
//!
//! The fill includes the boundary pixels (as OpenCV's `fillPoly` does, which the Python tool used), so
//! an axis-aligned rectangle (x0, y0)–(x1, y1) covers x0..=x1 and y0..=y1. The inset erodes with a
//! square (2·inset + 1) structuring element; neighbours outside the image do not erode (OpenCV's
//! border rule), so a target touching the image edge keeps its edge pixels.

use crate::raster::Raster;
use crate::DEFAULT_ROI_INSET_PX;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Mask {
    pub width: usize,
    pub height: usize,
    pub data: Vec<u8>,
}

impl Mask {
    pub fn empty(width: usize, height: usize) -> Self {
        Mask {
            width,
            height,
            data: vec![0; width * height],
        }
    }

    pub fn get(&self, x: usize, y: usize) -> bool {
        self.data[y * self.width + x] != 0
    }

    pub fn set(&mut self, x: usize, y: usize) {
        self.data[y * self.width + x] = 1;
    }

    pub fn count(&self) -> usize {
        self.data.iter().filter(|&&v| v != 0).count()
    }

    pub fn any(&self) -> bool {
        self.data.iter().any(|&v| v != 0)
    }

    /// Bounding box of the set pixels: (x0, y0, x1, y1) inclusive.
    pub fn bbox(&self) -> Option<(usize, usize, usize, usize)> {
        let (mut x0, mut y0, mut x1, mut y1) = (usize::MAX, usize::MAX, 0, 0);
        for y in 0..self.height {
            for x in 0..self.width {
                if self.get(x, y) {
                    x0 = x0.min(x);
                    y0 = y0.min(y);
                    x1 = x1.max(x);
                    y1 = y1.max(y);
                }
            }
        }
        (x0 != usize::MAX).then_some((x0, y0, x1, y1))
    }
}

fn draw_line(mask: &mut Mask, (x0, y0): (i32, i32), (x1, y1): (i32, i32)) {
    // 8-connected Bresenham, clipped to the image
    let (dx, dy) = ((x1 - x0).abs(), -(y1 - y0).abs());
    let (sx, sy) = (if x0 < x1 { 1 } else { -1 }, if y0 < y1 { 1 } else { -1 });
    let (mut x, mut y, mut err) = (x0, y0, dx + dy);
    loop {
        if x >= 0 && y >= 0 && (x as usize) < mask.width && (y as usize) < mask.height {
            mask.set(x as usize, y as usize);
        }
        if x == x1 && y == y1 {
            break;
        }
        let e2 = 2 * err;
        if e2 >= dy {
            err += dy;
            x += sx;
        }
        if e2 <= dx {
            err += dx;
            y += sy;
        }
    }
}

/// The filled polygon, boundary included, clipped to the image.
pub fn fill_polygon(polygon: &[(i32, i32)], width: usize, height: usize) -> Mask {
    let mut mask = Mask::empty(width, height);
    let n = polygon.len();
    if n < 3 || width == 0 || height == 0 {
        return mask;
    }
    let y_lo = polygon.iter().map(|p| p.1).min().unwrap_or(0).max(0);
    let y_hi = polygon
        .iter()
        .map(|p| p.1)
        .max()
        .unwrap_or(-1)
        .min(height as i32 - 1);
    let mut xs: Vec<f64> = Vec::with_capacity(n);
    for y in y_lo..=y_hi {
        xs.clear();
        for i in 0..n {
            let (x0, y0) = polygon[i];
            let (x1, y1) = polygon[(i + 1) % n];
            if y0 == y1 {
                continue;
            }
            let (ya, yb, xa, xb) = if y0 < y1 {
                (y0, y1, x0, x1)
            } else {
                (y1, y0, x1, x0)
            };
            if y >= ya && y < yb {
                // half-open rule: each crossing counted once
                let t = (y - ya) as f64 / (yb - ya) as f64;
                xs.push(xa as f64 + t * (xb - xa) as f64);
            }
        }
        xs.sort_by(|a, b| a.partial_cmp(b).expect("finite"));
        for pair in xs.chunks(2) {
            if pair.len() == 2 {
                let a = (pair[0].ceil() as i64).max(0);
                let b = (pair[1].floor() as i64).min(width as i64 - 1);
                for x in a..=b {
                    mask.set(x as usize, y as usize);
                }
            }
        }
    }
    for i in 0..n {
        draw_line(&mut mask, polygon[i], polygon[(i + 1) % n]);
    }
    mask
}

/// Binary erosion with a (2r+1)×(2r+1) square; neighbours outside the image count as set.
pub fn erode(mask: &Mask, r: u32) -> Mask {
    let mut out = Mask::empty(mask.width, mask.height);
    if r == 0 {
        return mask.clone();
    }
    let r = r as i64;
    let Some((x0, y0, x1, y1)) = mask.bbox() else {
        return out;
    };
    for y in y0..=y1 {
        'px: for x in x0..=x1 {
            if !mask.get(x, y) {
                continue;
            }
            for dy in -r..=r {
                let yy = y as i64 + dy;
                if yy < 0 || yy >= mask.height as i64 {
                    continue;
                }
                for dx in -r..=r {
                    let xx = x as i64 + dx;
                    if xx < 0 || xx >= mask.width as i64 {
                        continue;
                    }
                    if !mask.get(xx as usize, yy as usize) {
                        continue 'px;
                    }
                }
            }
            out.set(x, y);
        }
    }
    out
}

/// Mask of the polygon shrunk by `inset_px` pixels on every side. Returns (mask, inset_applied): when
/// the erosion would empty the mask the full polygon is used and `inset_applied` is false.
pub fn polygon_mask(
    polygon: &[(i32, i32)],
    width: usize,
    height: usize,
    inset_px: u32,
) -> (Mask, bool) {
    let mask = fill_polygon(polygon, width, height);
    if inset_px == 0 {
        return (mask, true);
    }
    let eroded = erode(&mask, inset_px);
    if eroded.any() {
        (eroded, true)
    } else {
        (mask, false)
    }
}

/// Order clicked corners around their centroid so four clicks always give a simple quadrilateral,
/// never a bow-tie.
pub fn order_corners(points: &[(i32, i32)]) -> Vec<(i32, i32)> {
    if points.len() < 3 {
        return points.to_vec();
    }
    let n = points.len() as f64;
    let cx = points.iter().map(|p| p.0 as f64).sum::<f64>() / n;
    let cy = points.iter().map(|p| p.1 as f64).sum::<f64>() / n;
    let mut v = points.to_vec();
    v.sort_by(|a, b| {
        let ka = (a.1 as f64 - cy).atan2(a.0 as f64 - cx);
        let kb = (b.1 as f64 - cy).atan2(b.0 as f64 - cx);
        ka.partial_cmp(&kb).expect("finite")
    });
    v
}

pub fn polygon_is_convex(points: &[(i32, i32)]) -> bool {
    let n = points.len();
    if n < 3 {
        return false;
    }
    let mut sign = 0i64;
    for i in 0..n {
        let (ax, ay) = points[i];
        let (bx, by) = points[(i + 1) % n];
        let (cx, cy) = points[(i + 2) % n];
        let cross = (bx - ax) as i64 * (cy - by) as i64 - (by - ay) as i64 * (cx - bx) as i64;
        if cross != 0 {
            if sign == 0 {
                sign = cross.signum();
            } else if cross.signum() != sign {
                return false;
            }
        }
    }
    true
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct RoiStats {
    pub mean: f64,
    /// Population standard deviation (numpy's default), like the Python tool.
    pub std: f64,
    pub count: usize,
    pub inset_applied: bool,
}

/// Mean and spread of the finite pixels inside the eroded polygon.
pub fn extract_roi_statistics(img: &Raster, polygon: &[(i32, i32)], inset_px: u32) -> RoiStats {
    let (mask, applied) = polygon_mask(polygon, img.width, img.height, inset_px);
    let values: Vec<f64> = mask
        .data
        .iter()
        .zip(img.data.iter())
        .filter(|(m, v)| **m != 0 && v.is_finite())
        .map(|(_, v)| *v as f64)
        .collect();
    if values.is_empty() {
        return RoiStats {
            mean: f64::NAN,
            std: f64::NAN,
            count: 0,
            inset_applied: applied,
        };
    }
    let n = values.len() as f64;
    let mean = values.iter().sum::<f64>() / n;
    let var = values.iter().map(|v| (v - mean).powi(2)).sum::<f64>() / n;
    RoiStats {
        mean,
        std: var.sqrt(),
        count: values.len(),
        inset_applied: applied,
    }
}

/// Convenience with the default inset.
pub fn roi_statistics(img: &Raster, polygon: &[(i32, i32)]) -> RoiStats {
    extract_roi_statistics(img, polygon, DEFAULT_ROI_INSET_PX)
}

/// Outline(s) of the eroded region as pixel-edge paths, for drawing: each piece is a closed polygon in
/// image coordinates (pixel corners). Pieces are the 4-connected components of the mask boundary.
pub fn mask_outlines(mask: &Mask) -> Vec<Vec<(i32, i32)>> {
    // Collect the boundary edges of set pixels (edges facing an unset neighbour or the image edge),
    // then chain them into closed loops.
    use std::collections::HashMap;
    let mut edges: HashMap<(i32, i32), Vec<(i32, i32)>> = HashMap::new();
    let mut add = |a: (i32, i32), b: (i32, i32)| edges.entry(a).or_default().push(b);
    let set = |x: i64, y: i64| {
        x >= 0
            && y >= 0
            && (x as usize) < mask.width
            && (y as usize) < mask.height
            && mask.get(x as usize, y as usize)
    };
    for y in 0..mask.height as i64 {
        for x in 0..mask.width as i64 {
            if !set(x, y) {
                continue;
            }
            let (xi, yi) = (x as i32, y as i32);
            if !set(x, y - 1) {
                add((xi, yi), (xi + 1, yi)); // top edge, left to right
            }
            if !set(x + 1, y) {
                add((xi + 1, yi), (xi + 1, yi + 1)); // right edge, top to bottom
            }
            if !set(x, y + 1) {
                add((xi + 1, yi + 1), (xi, yi + 1)); // bottom edge, right to left
            }
            if !set(x - 1, y) {
                add((xi, yi + 1), (xi, yi)); // left edge, bottom to top
            }
        }
    }
    let mut loops = Vec::new();
    while let Some((&start, _)) = edges.iter().next() {
        let mut path = vec![start];
        let mut cur = start;
        while let Some(nexts) = edges.get_mut(&cur) {
            let next = nexts.pop().expect("non-empty");
            if nexts.is_empty() {
                edges.remove(&cur);
            }
            if next == start {
                break;
            }
            path.push(next);
            cur = next;
        }
        // drop collinear points so a rectangle is four corners
        let mut simple: Vec<(i32, i32)> = Vec::with_capacity(path.len());
        let m = path.len();
        for i in 0..m {
            let (p, c, n) = (path[(i + m - 1) % m], path[i], path[(i + 1) % m]);
            let collinear = (c.0 - p.0) * (n.1 - c.1) == (c.1 - p.1) * (n.0 - c.0);
            if !collinear {
                simple.push(c);
            }
        }
        if simple.len() >= 3 {
            loops.push(simple);
        }
    }
    loops.sort_by_key(|l| std::cmp::Reverse(l.len()));
    loops
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rect(x0: i32, y0: i32, x1: i32, y1: i32) -> Vec<(i32, i32)> {
        vec![(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    }

    #[test]
    fn rectangle_fill_is_inclusive_like_opencv() {
        let m = fill_polygon(&rect(2, 3, 6, 8), 20, 20);
        assert_eq!(m.count(), 5 * 6);
        assert!(m.get(2, 3) && m.get(6, 8) && !m.get(7, 8) && !m.get(2, 9));
    }

    #[test]
    fn erosion_removes_a_band_and_keeps_image_edges() {
        let m = fill_polygon(&rect(10, 10, 30, 30), 50, 50);
        let (e, applied) = polygon_mask(&rect(10, 10, 30, 30), 50, 50, 3);
        assert!(applied && e.count() == 15 * 15 && m.count() == 21 * 21);
        let (small, applied) = polygon_mask(&rect(10, 10, 14, 14), 50, 50, 3);
        assert!(
            !applied && small.count() == 25,
            "too small to erode: the full polygon is used"
        );
        let (edge, applied) = polygon_mask(&rect(0, 0, 10, 10), 50, 50, 3);
        assert!(
            applied && edge.get(0, 0),
            "neighbours outside the image do not erode"
        );
    }

    #[test]
    fn stats_ignore_nan_and_use_population_std() {
        let mut data = vec![f32::NAN; 100];
        for y in 0..10 {
            for x in 0..10 {
                if (2..=7).contains(&x) && (2..=7).contains(&y) {
                    data[y * 10 + x] = if (x + y) % 2 == 0 { 10.0 } else { 12.0 };
                }
            }
        }
        let img = Raster::new(10, 10, data);
        let st = extract_roi_statistics(&img, &rect(0, 0, 9, 9), 0);
        assert_eq!(st.count, 36);
        assert!((st.mean - 11.0).abs() < 1e-9 && (st.std - 1.0).abs() < 1e-9);
        let st = extract_roi_statistics(&img, &rect(0, 0, 1, 1), 0);
        assert!(st.mean.is_nan() && st.count == 0);
    }

    #[test]
    fn corners_are_ordered_and_convexity_detected() {
        let bowtie = vec![(0, 0), (10, 10), (10, 0), (0, 10)];
        let ordered = order_corners(&bowtie);
        assert!(polygon_is_convex(&ordered) && !polygon_is_convex(&bowtie));
    }

    #[test]
    fn outlines_of_a_rectangle_are_four_corners() {
        let m = fill_polygon(&rect(2, 2, 5, 4), 10, 10);
        let out = mask_outlines(&m);
        assert_eq!(out.len(), 1);
        assert_eq!(out[0].len(), 4);
        let xs: Vec<i32> = out[0].iter().map(|p| p.0).collect();
        assert_eq!((xs.iter().min(), xs.iter().max()), (Some(&2), Some(&6)));
    }
}
