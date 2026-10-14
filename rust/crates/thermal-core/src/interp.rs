//! Coefficients interpolated in time between the calibration shots.

use crate::model::CalibrationEvent;
use crate::Result;
use chrono::NaiveDateTime;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Position {
    Single,
    Before,
    Between,
    After,
}

impl Position {
    pub fn as_str(self) -> &'static str {
        match self {
            Position::Single => "single",
            Position::Before => "before",
            Position::Between => "between",
            Position::After => "after",
        }
    }
}

/// The fitted events, earliest first.
pub fn fitted_sorted(events: &[CalibrationEvent]) -> Vec<&CalibrationEvent> {
    let mut v: Vec<&CalibrationEvent> = events.iter().filter(|e| e.fit.is_some()).collect();
    v.sort_by_key(|e| e.timestamp);
    v
}

fn seconds(t: NaiveDateTime) -> f64 {
    t.and_utc().timestamp_micros() as f64 / 1e6
}

/// Interpolated (m, b) at `timestamp`, piecewise-linear through *all* fitted shots.
///
/// Returns (m, b, position, bracketing pair as indices into `fitted`). An image at exactly a shot's
/// time counts as `Between`. Outside the calibration window the nearest shot's coefficients are used
/// unchanged; the pair then still names the nearest two shots, but only the first / last is applied.
pub fn coefficients_at(
    timestamp: NaiveDateTime,
    fitted: &[&CalibrationEvent],
) -> Result<(f64, f64, Position, (usize, usize))> {
    if fitted.is_empty() {
        return Err("no fitted calibration events".into());
    }
    let fit = |i: usize| fitted[i].fit.as_ref().expect("fitted");
    if fitted.len() == 1 {
        return Ok((fit(0).slope, fit(0).intercept, Position::Single, (0, 0)));
    }
    let ts: Vec<f64> = fitted.iter().map(|e| seconds(e.timestamp)).collect();
    let t = seconds(timestamp);
    let last = fitted.len() - 1;
    if t < ts[0] {
        return Ok((fit(0).slope, fit(0).intercept, Position::Before, (0, 1)));
    }
    if t > ts[last] {
        return Ok((
            fit(last).slope,
            fit(last).intercept,
            Position::After,
            (last - 1, last),
        ));
    }
    // bisect_right(ts, t) - 1, capped so the pair (i, i + 1) exists
    let right = ts.partition_point(|&v| v <= t);
    let i = right.saturating_sub(1).min(last - 1);
    let span = ts[i + 1] - ts[i];
    let f = if span > 0.0 { (t - ts[i]) / span } else { 0.0 };
    let (f0, f1) = (fit(i), fit(i + 1));
    let m = f0.slope + f * (f1.slope - f0.slope);
    let b = f0.intercept + f * (f1.intercept - f0.intercept);
    Ok((m, b, Position::Between, (i, i + 1)))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::FitResult;
    use chrono::Duration;

    fn ev(minute: i64, m: f64, b: f64) -> CalibrationEvent {
        let t0 = NaiveDateTime::parse_from_str("2026-06-01 10:00:00", "%Y-%m-%d %H:%M:%S").unwrap();
        let mut e = CalibrationEvent::new("x.tif", t0 + Duration::minutes(minute), "XMP", "");
        e.fit = Some(FitResult {
            slope: m,
            intercept: b,
            n: 3,
            ..Default::default()
        });
        e
    }

    #[test]
    fn interpolates_and_clamps() {
        let events = vec![ev(20, 1.2, 2.0), ev(0, 1.0, 0.0), ev(10, 1.1, 1.0)];
        let f = fitted_sorted(&events);
        let t0 = f[0].timestamp;
        let at = |min: i64| coefficients_at(t0 + Duration::minutes(min), &f).unwrap();
        assert_eq!(at(-5), (1.0, 0.0, Position::Before, (0, 1)));
        assert_eq!(at(25), (1.2, 2.0, Position::After, (1, 2)));
        let (m, b, pos, pair) = at(5);
        assert!(
            (m - 1.05).abs() < 1e-12
                && (b - 0.5).abs() < 1e-12
                && pos == Position::Between
                && pair == (0, 1)
        );
        let (m, _, pos, pair) = at(10);
        assert!(
            (m - 1.1).abs() < 1e-12 && pos == Position::Between && pair == (1, 2),
            "exactly at a shot"
        );
        let (m, _, pos, pair) = at(20);
        assert!((m - 1.2).abs() < 1e-12 && pos == Position::Between && pair == (1, 2));
        let single = vec![ev(0, 1.0, 0.0)];
        let f1 = fitted_sorted(&single);
        assert_eq!(
            coefficients_at(t0 + Duration::minutes(99), &f1).unwrap().2,
            Position::Single
        );
    }
}
