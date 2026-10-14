//! Per-shot calibration fits: ordinary least squares with the diagnostics, and the single-point
//! multispectral variant.

use crate::model::{CalibrationEvent, EmissivitySettings, FitResult};
use crate::physics::apparent_temperature;
use crate::Result;

fn mean(v: &[f64]) -> f64 {
    v.iter().sum::<f64>() / v.len() as f64
}

fn ptp(v: &[f64]) -> f64 {
    let lo = v.iter().cloned().fold(f64::INFINITY, f64::min);
    let hi = v.iter().cloned().fold(f64::NEG_INFINITY, f64::max);
    hi - lo
}

fn slope_intercept(x: &[f64], y: &[f64]) -> (f64, f64, f64, f64) {
    let (xm, ym) = (mean(x), mean(y));
    let sxx: f64 = x.iter().map(|v| (v - xm).powi(2)).sum();
    let sxy: f64 = x.iter().zip(y).map(|(a, b)| (a - xm) * (b - ym)).sum();
    let slope = sxy / sxx;
    (slope, ym - slope * xm, sxx, xm)
}

/// Ordinary least squares `truth = m · sensor + b` with residual diagnostics.
///
/// With two targets the line passes through both points, so R² is 1 by construction and no scatter
/// can be estimated; RMSE and the standard errors are then None. R² is None when all reference
/// values are equal (undefined).
pub fn compute_linear_regression(x: &[f64], y: &[f64]) -> Result<FitResult> {
    let n = x.len();
    if n < 2 || y.len() != n {
        return Err("at least two targets are needed for a linear fit".into());
    }
    if !x.iter().chain(y).all(|v| v.is_finite()) {
        return Err("a target has no valid sensor value or reference value (NaN)".into());
    }
    if ptp(x) == 0.0 {
        return Err(
            "all targets have the same sensor value; the slope cannot be determined".into(),
        );
    }
    let (slope, intercept, sxx, xm) = slope_intercept(x, y);
    let residuals: Vec<f64> = x
        .iter()
        .zip(y)
        .map(|(a, b)| b - (slope * a + intercept))
        .collect();
    let ym = mean(y);
    let syy: f64 = y.iter().map(|v| (v - ym).powi(2)).sum();
    let sxy: f64 = x.iter().zip(y).map(|(a, b)| (a - xm) * (b - ym)).sum();
    let r_squared = if syy > 0.0 {
        Some(((sxy * sxy) / (sxx * syy)).min(1.0))
    } else {
        None
    };
    let mut fit = FitResult {
        slope,
        intercept,
        n,
        single_point: false,
        r_squared,
        residuals,
        x_values: x.to_vec(),
        y_values: y.to_vec(),
        x_mean: xm,
        sxx,
        x_min: x.iter().cloned().fold(f64::INFINITY, f64::min),
        x_max: x.iter().cloned().fold(f64::NEG_INFINITY, f64::max),
        ..Default::default()
    };
    if n > 2 {
        let sse: f64 = fit.residuals.iter().map(|r| r * r).sum();
        let s = (sse / (n - 2) as f64).sqrt();
        fit.rmse = Some((sse / n as f64).sqrt());
        fit.resid_std = Some(s);
        fit.se_slope = Some(s / sxx.sqrt());
        fit.se_intercept = Some(s * (1.0 / n as f64 + xm * xm / sxx).sqrt());
        let mut loo = Vec::with_capacity(n);
        for i in 0..n {
            let xk: Vec<f64> = x
                .iter()
                .enumerate()
                .filter(|(k, _)| *k != i)
                .map(|(_, v)| *v)
                .collect();
            let yk: Vec<f64> = y
                .iter()
                .enumerate()
                .filter(|(k, _)| *k != i)
                .map(|(_, v)| *v)
                .collect();
            if ptp(&xk) == 0.0 {
                continue;
            }
            let (m, b, _, _) = slope_intercept(&xk, &yk);
            loo.push(y[i] - (m * x[i] + b));
        }
        if !loo.is_empty() {
            fit.loo_rmse = Some((loo.iter().map(|v| v * v).sum::<f64>() / loo.len() as f64).sqrt());
        }
    }
    Ok(fit)
}

/// MSI single-point calibration: reflectance = K · DN, K from one panel.
pub fn compute_single_point(drone_value: f64, ground_truth: f64) -> Result<FitResult> {
    if !(drone_value.is_finite() && ground_truth.is_finite()) {
        return Err("panel DN or reflectance is not a number".into());
    }
    if drone_value == 0.0 {
        return Err("panel DN is zero".into());
    }
    Ok(FitResult {
        slope: ground_truth / drone_value,
        intercept: 0.0,
        n: 1,
        single_point: true,
        x_values: vec![drone_value],
        y_values: vec![ground_truth],
        x_mean: drone_value,
        x_min: drone_value,
        x_max: drone_value,
        ..Default::default()
    })
}

/// Fit the event's coefficients from its complete targets. Returns a warning text when no fit is
/// possible. With the emissivity correction on, the fit maps raw sensor values to the *apparent*
/// temperature of each panel (its true temperature through the panel's emissivity); the batch applies
/// m, b to raw pixels and converts the result to surface temperature with the scene emissivity.
pub fn recompute_fit(
    event: &mut CalibrationEvent,
    single_point: bool,
    thermal: bool,
    emissivity: Option<&EmissivitySettings>,
) -> Option<String> {
    let targets: &[_] = if single_point {
        &event.targets[..event.targets.len().min(1)]
    } else {
        &event.targets
    };
    let mut xs = Vec::new();
    let mut ys = Vec::new();
    for t in targets.iter().filter(|t| t.is_complete()) {
        let mut y = t.ground_truth.expect("complete");
        if let Some(em) = emissivity {
            if thermal && em.enabled {
                // the Python tool computes this in float32; keep the same rounding
                y = apparent_temperature(y, em.target_emissivity, em.sky_temperature) as f32 as f64;
            }
        }
        xs.push(t.drone_mean.expect("complete"));
        ys.push(y);
    }
    event.fit = None;
    let result = if single_point {
        match xs.first() {
            Some(&x) => compute_single_point(x, ys[0]).map(Some),
            None => Ok(None),
        }
    } else if xs.len() >= 2 {
        compute_linear_regression(&xs, &ys).map(Some)
    } else {
        Ok(None)
    };
    match result {
        Ok(fit) => {
            event.fit = fit;
            None
        }
        Err(e) => Some(e.to_string()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn close(a: f64, b: f64, tol: f64) -> bool {
        (a - b).abs() <= tol * b.abs().max(1.0)
    }

    #[test]
    fn closed_form_three_points() {
        // y = 2x + 1 with residuals (-1, 2, -1) at x = 0, 1, 2 → y = 0, 5, 4
        let x = [0.0, 1.0, 2.0];
        let y = [0.0, 5.0, 4.0];
        let f = compute_linear_regression(&x, &y).unwrap();
        assert!(close(f.slope, 2.0, 1e-12) && close(f.intercept, 1.0, 1e-12));
        let sse = 6.0;
        assert!(close(f.rmse.unwrap(), (sse / 3.0f64).sqrt(), 1e-12));
        assert!(close(f.resid_std.unwrap(), (sse / 1.0f64).sqrt(), 1e-12));
        assert!(close(f.se_slope.unwrap(), (sse / 2.0f64).sqrt(), 1e-12)); // sxx = 2
        assert!(close(
            f.se_intercept.unwrap(),
            sse.sqrt() * (1.0 / 3.0 + 1.0 / 2.0f64).sqrt(),
            1e-12
        ));
        let r2 = 1.0 - sse / 14.0; // syy = 14
        assert!(close(f.r_squared.unwrap(), r2, 1e-12));
        assert!(f.loo_rmse.is_some() && f.predict_uncertainty(1.0).is_some());
    }

    #[test]
    fn two_points_have_no_scatter_estimate() {
        let f = compute_linear_regression(&[1.0, 3.0], &[2.0, 4.4]).unwrap();
        assert!(close(f.slope, 1.2, 1e-12) && f.rmse.is_none() && f.se_slope.is_none());
        assert_eq!(f.r_squared, Some(1.0));
        assert!(f.predict_uncertainty(2.0).is_none());
    }

    #[test]
    fn degenerate_inputs_are_errors() {
        assert!(compute_linear_regression(&[1.0], &[1.0]).is_err());
        assert!(compute_linear_regression(&[1.0, 1.0], &[1.0, 2.0]).is_err());
        assert!(compute_linear_regression(&[1.0, f64::NAN], &[1.0, 2.0]).is_err());
        assert!(compute_single_point(0.0, 0.5).is_err());
        let k = compute_single_point(2000.0, 0.5).unwrap();
        assert!(k.single_point && close(k.slope, 0.00025, 1e-12) && k.intercept == 0.0);
    }
}
