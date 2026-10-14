//! The HTML fit report of one calibration shot: targets, coefficients, diagnostics, method and an
//! SVG plot, in one self-contained file meant for a methods section.

use crate::interp::fitted_sorted;
use crate::model::CalibrationEvent;
use crate::{Result, TOOL_NAME, VERSION};

fn esc(s: &str) -> String {
    s.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
}

fn num(v: Option<f64>, nd: usize) -> String {
    match v {
        Some(x) if x.is_finite() => format!("{x:.nd$}"),
        _ => "n/a".into(),
    }
}

/// Round tick positions: about `n` ticks between `lo` and `hi`.
fn ticks(lo: f64, hi: f64, n: usize) -> Vec<f64> {
    let span = (hi - lo).max(1e-9);
    let raw = span / n as f64;
    let mag = 10f64.powf(raw.log10().floor());
    let step = [1.0, 2.0, 2.5, 5.0, 10.0]
        .iter()
        .map(|m| m * mag)
        .find(|s| span / s <= n as f64 + 0.5)
        .unwrap_or(mag * 10.0);
    let mut t = (lo / step).ceil() * step;
    let mut out = Vec::new();
    while t <= hi + 1e-9 {
        out.push(t);
        t += step;
    }
    out
}

/// Regression plot with the ±1σ prediction band and a residual panel below, as inline SVG.
pub fn plot_svg(event: &CalibrationEvent, unit: &str, width: f64, height: f64) -> String {
    let Some(fit) = &event.fit else {
        return String::new();
    };
    if fit.single_point || fit.x_values.is_empty() {
        return String::new();
    }
    let complete: Vec<_> = event.targets.iter().filter(|t| t.is_complete()).collect();
    let (l, r, t, b) = (60.0, 16.0, 16.0, 44.0);
    let gap = 18.0;
    let h_main = (height - t - b - gap) * 0.72;
    let h_res = height - t - b - gap - h_main;
    let xmin0 = fit.x_values.iter().cloned().fold(f64::INFINITY, f64::min);
    let xmax0 = fit
        .x_values
        .iter()
        .cloned()
        .fold(f64::NEG_INFINITY, f64::max);
    let span = (xmax0 - xmin0).max(1e-6);
    let (xmin, xmax) = (xmin0 - 0.1 * span - 0.5, xmax0 + 0.1 * span + 0.5);
    let xs: Vec<f64> = (0..200)
        .map(|i| xmin + (xmax - xmin) * i as f64 / 199.0)
        .collect();
    let band: Vec<f64> = xs
        .iter()
        .map(|&x| fit.predict_uncertainty(x).unwrap_or(0.0))
        .collect();
    let mut ys: Vec<f64> = fit.y_values.clone();
    for (x, bnd) in xs.iter().zip(&band) {
        ys.push(fit.predict(*x) - bnd);
        ys.push(fit.predict(*x) + bnd);
    }
    let ymin0 = ys.iter().cloned().fold(f64::INFINITY, f64::min);
    let ymax0 = ys.iter().cloned().fold(f64::NEG_INFINITY, f64::max);
    let yspan = (ymax0 - ymin0).max(1e-6);
    let (ymin, ymax) = (ymin0 - 0.06 * yspan, ymax0 + 0.06 * yspan);
    let rmax = fit.residuals.iter().map(|v| v.abs()).fold(1e-9, f64::max) * 1.3;
    let px = |v: f64| l + (v - xmin) / (xmax - xmin) * (width - l - r);
    let py = |v: f64| t + h_main - (v - ymin) / (ymax - ymin) * h_main;
    let pr = |v: f64| t + h_main + gap + h_res / 2.0 - v / rmax * (h_res / 2.0);
    let mut s = format!(
        "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 {width} {height}\" width=\"{width}\" height=\"{height}\" \
         font-family=\"system-ui, sans-serif\" font-size=\"11\">\n<rect width=\"{width}\" height=\"{height}\" fill=\"#fbfbf9\"/>\n"
    );
    // grid and axes
    for yt in ticks(ymin, ymax, 5) {
        s += &format!("<line x1=\"{l}\" y1=\"{0:.1}\" x2=\"{1:.1}\" y2=\"{0:.1}\" stroke=\"#e6e8e3\"/><text x=\"{2:.1}\" y=\"{3:.1}\" text-anchor=\"end\" fill=\"#7a827d\">{yt}</text>\n", py(yt), width - r, l - 6.0, py(yt) + 4.0);
    }
    for xt in ticks(xmin, xmax, 6) {
        s += &format!("<line x1=\"{0:.1}\" y1=\"{t}\" x2=\"{0:.1}\" y2=\"{1:.1}\" stroke=\"#e6e8e3\"/><text x=\"{0:.1}\" y=\"{2:.1}\" text-anchor=\"middle\" fill=\"#7a827d\">{xt}</text>\n", px(xt), height - b + 2.0, height - b + 16.0);
    }
    s += &format!("<rect x=\"{l}\" y=\"{t}\" width=\"{:.1}\" height=\"{h_main:.1}\" fill=\"none\" stroke=\"#d9dcd6\"/>\n", width - l - r);
    s += &format!("<rect x=\"{l}\" y=\"{:.1}\" width=\"{:.1}\" height=\"{h_res:.1}\" fill=\"none\" stroke=\"#d9dcd6\"/>\n", t + h_main + gap, width - l - r);
    s += &format!("<line x1=\"{l}\" y1=\"{0:.1}\" x2=\"{1:.1}\" y2=\"{0:.1}\" stroke=\"#1d2320\" stroke-width=\"0.8\"/>\n", pr(0.0), width - r);
    // band, line, points
    if fit.resid_std.is_some() {
        let upper: Vec<String> = xs
            .iter()
            .zip(&band)
            .map(|(x, bnd)| format!("{:.1},{:.1}", px(*x), py(fit.predict(*x) + bnd)))
            .collect();
        let lower: Vec<String> = xs
            .iter()
            .zip(&band)
            .rev()
            .map(|(x, bnd)| format!("{:.1},{:.1}", px(*x), py(fit.predict(*x) - bnd)))
            .collect();
        s += &format!(
            "<polygon points=\"{} {}\" fill=\"#999999\" opacity=\"0.22\"/>\n",
            upper.join(" "),
            lower.join(" ")
        );
    }
    s += &format!("<line x1=\"{:.1}\" y1=\"{:.1}\" x2=\"{:.1}\" y2=\"{:.1}\" stroke=\"#1d2320\" stroke-width=\"1.4\" stroke-dasharray=\"6 4\"/>\n", px(xmin), py(fit.predict(xmin)), px(xmax), py(fit.predict(xmax)));
    let bar_w = ((width - l - r) * 0.03).max(4.0);
    for (i, (x, y)) in fit.x_values.iter().zip(&fit.y_values).enumerate() {
        let color = complete
            .get(i)
            .map(|t| t.color.as_str())
            .unwrap_or("#999999");
        let name = complete.get(i).map(|t| t.name.as_str()).unwrap_or("");
        s += &format!("<circle cx=\"{:.1}\" cy=\"{:.1}\" r=\"6\" fill=\"{}\" stroke=\"#1d2320\" stroke-width=\"1.2\"/>\n", px(*x), py(*y), esc(color));
        s += &format!(
            "<text x=\"{:.1}\" y=\"{:.1}\" fill=\"#1d2320\">{}</text>\n",
            px(*x) + 8.0,
            py(*y) - 6.0,
            esc(name)
        );
        if let Some(res) = fit.residuals.get(i) {
            let (y0, y1) = (pr(0.0).min(pr(*res)), pr(0.0).max(pr(*res)));
            s += &format!("<rect x=\"{:.1}\" y=\"{:.1}\" width=\"{bar_w:.1}\" height=\"{:.1}\" fill=\"{}\" stroke=\"#1d2320\" stroke-width=\"0.8\"/>\n", px(*x) - bar_w / 2.0, y0, (y1 - y0).max(0.5), esc(color));
        }
    }
    s += &format!("<text x=\"{:.1}\" y=\"{:.1}\" text-anchor=\"middle\" fill=\"#1d2320\">Sensor ROI mean ({})</text>\n", (l + width - r) / 2.0, height - 4.0, esc(unit));
    s += &format!("<text x=\"14\" y=\"{:.1}\" text-anchor=\"middle\" fill=\"#1d2320\" transform=\"rotate(-90 14 {:.1})\">Reference ({})</text>\n", t + h_main / 2.0, t + h_main / 2.0, esc(unit));
    s += &format!("<text x=\"14\" y=\"{:.1}\" text-anchor=\"middle\" fill=\"#1d2320\" transform=\"rotate(-90 14 {:.1})\">residual</text>\n", t + h_main + gap + h_res / 2.0, t + h_main + gap + h_res / 2.0);
    s += &format!("<text x=\"{:.1}\" y=\"{:.1}\" text-anchor=\"end\" fill=\"#7a827d\">fit (dashed), ±1σ prediction (grey)</text>\n", width - r - 6.0, t + h_main - 8.0);
    s += "</svg>";
    s
}

/// Self-contained HTML report of one calibration fit.
pub fn fit_report_html(
    event: &CalibrationEvent,
    unit: &str,
    events: &[CalibrationEvent],
    settings: &[(String, String)],
) -> Result<String> {
    let fit = event.fit.as_ref().ok_or("event has no fit")?;
    let complete: Vec<_> = event.targets.iter().filter(|t| t.is_complete()).collect();
    let fitted_y: Vec<f64> = if fit.single_point {
        complete
            .iter()
            .map(|t| t.ground_truth.unwrap_or(f64::NAN))
            .collect()
    } else {
        fit.y_values.clone()
    };
    let converted = complete
        .iter()
        .zip(&fitted_y)
        .any(|(t, y)| (y - t.ground_truth.unwrap_or(f64::NAN)).abs() > 1e-9);
    let residuals: Vec<f64> = if fit.single_point {
        vec![0.0]
    } else {
        fit.residuals.clone()
    };
    let mut rows = String::new();
    for (i, (t, y)) in complete.iter().zip(&fitted_y).enumerate() {
        let r = residuals.get(i).copied();
        rows += &format!(
            "<tr><td><span class='sw' style='background:{}'></span>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td>{}<td>{}</td></tr>",
            esc(&t.color),
            esc(&t.name),
            num(t.drone_mean, 3),
            num(t.drone_std, 3),
            t.pixel_count,
            num(t.ground_truth, 2),
            if converted { format!("<td>{}</td>", num(Some(*y), 3)) } else { String::new() },
            num(r, 3)
        );
    }
    let fitted_head = if converted {
        "<th>Fitted reference, apparent (°C)</th>"
    } else {
        ""
    };
    let (coeff, mut diag) = if fit.single_point {
        (
            format!("<p class='big'>ρ = K × DN, K = {:.8}</p>", fit.slope),
            "<p>Single-point calibration: no residual statistics are possible.</p>".to_string(),
        )
    } else {
        (
            format!("<p class='big'>out = {:.5} × in {} {:.4}</p>", fit.slope, if fit.intercept >= 0.0 { "+" } else { "−" }, fit.intercept.abs()),
            format!(
                "<table><tr><th>n</th><th>R²</th><th>RMSE</th><th>s (resid. std)</th><th>SE(m)</th><th>SE(b)</th><th>leave-one-out RMSE</th>\
                 <th>±1σ prediction at x̄</th></tr><tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{} at {}</td></tr></table>",
                fit.n,
                num(fit.r_squared, 4),
                num(fit.rmse, 3),
                num(fit.resid_std, 3),
                num(fit.se_slope, 4),
                num(fit.se_intercept, 3),
                num(fit.loo_rmse, 3),
                num(fit.predict_uncertainty(fit.x_mean), 3),
                num(Some(fit.x_mean), 1)
            ),
        )
    };
    if !fit.single_point && fit.n == 2 {
        diag += "<p class='warn'>Only two targets: R² is 1 by construction and no scatter can be estimated.</p>";
    }
    let n_fitted = fitted_sorted(events).len();
    let method = format!(
        "Empirical line method: reference = m × sensor + b fitted by ordinary least squares to the mean of the inner region of \
         each target polygon (edge inset in pixels). {} Prediction uncertainty: s·√(1 + 1/n + (x − x̄)²/Sxx), which covers the \
         regression only; reference instrument accuracy, drift between calibration events, emissivity mismatch, atmospheric \
         path and sensor non-uniformity are not included.",
        if n_fitted > 1 {
            format!("{n_fitted} calibration events; m and b are interpolated linearly in time between consecutive events and held constant outside that window.")
        } else {
            "One calibration event; m and b are applied unchanged to every image.".to_string()
        }
    );
    let srows: String = settings
        .iter()
        .map(|(k, v)| format!("<tr><td>{}</td><td>{}</td></tr>", esc(k), esc(v)))
        .collect();
    let plot = plot_svg(event, unit, 760.0, 520.0);
    Ok(format!(
        "<!doctype html><html lang='en'><head><meta charset='utf-8'><title>Calibration fit report</title>
<style>body{{font:14px/1.45 system-ui,sans-serif;color:#1d2320;max-width:860px;margin:32px auto;padding:0 16px}}
h1{{font-size:20px}}h2{{font-size:15px;margin-top:28px;border-bottom:1px solid #d9dcd6;padding-bottom:4px}}
table{{border-collapse:collapse;margin:8px 0}}td,th{{border:1px solid #d9dcd6;padding:4px 10px;text-align:right}}
th{{background:#f3f4f1;font-weight:600}}td:first-child,th:first-child{{text-align:left}}
.big{{font-size:18px;font-family:ui-monospace,monospace}}.warn{{color:#b45309}}.muted{{color:#7a827d}}
.sw{{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px;vertical-align:middle}}
svg{{max-width:100%;height:auto;border:1px solid #d9dcd6;margin-top:8px}}</style></head><body>
<h1>Calibration fit report</h1>
<p class='muted'>{file} · captured {captured}{tz} ({source}) · generated {now} by {tool} {version}</p>
<h2>Targets</h2>
<table><tr><th>Target</th><th>Sensor mean ({unit})</th><th>σ</th><th>pixels</th><th>Reference ({unit})</th>{fitted_head}<th>Residual</th></tr>{rows}</table>
<h2>Coefficients</h2>{coeff}<h2>Diagnostics</h2>{diag}{plot}
<h2>Method</h2><p>{method}</p>
{settings_block}
</body></html>",
        file = esc(&event.file_name()),
        captured = event.timestamp.format("%Y-%m-%d %H:%M:%S"),
        tz = esc(&event.timestamp_tz),
        source = esc(&event.timestamp_source),
        now = chrono::Local::now().format("%Y-%m-%d %H:%M"),
        tool = TOOL_NAME,
        version = VERSION,
        unit = esc(unit),
        method = esc(&method),
        settings_block = if srows.is_empty() { String::new() } else { format!("<h2>Settings</h2><table>{srows}</table>") },
    ))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::fit::compute_linear_regression;
    use crate::model::CalibrationTarget;

    #[test]
    fn report_has_plot_table_and_method() {
        let t0 = chrono::NaiveDateTime::parse_from_str("2026-06-01 10:00:00", "%Y-%m-%d %H:%M:%S")
            .unwrap();
        let mut ev = CalibrationEvent::with_targets("cal.tif", t0, "XMP", "+02:00", Vec::new());
        ev.targets
            .push(CalibrationTarget::new("Target 3", "#E9C46A"));
        for (i, (m, g)) in [(16.0, 15.0), (31.0, 30.2), (46.5, 45.1)]
            .iter()
            .enumerate()
        {
            let t = &mut ev.targets[i];
            t.polygon = vec![(0, 0), (5, 0), (5, 5), (0, 5)];
            t.drone_mean = Some(*m);
            t.drone_std = Some(0.3);
            t.pixel_count = 36;
            t.ground_truth = Some(*g);
        }
        ev.fit = Some(compute_linear_regression(&[16.0, 31.0, 46.5], &[15.0, 30.2, 45.1]).unwrap());
        let html = fit_report_html(
            &ev,
            "°C",
            std::slice::from_ref(&ev),
            &[("ROI edge inset (px)".into(), "3".into())],
        )
        .unwrap();
        assert!(
            html.contains("<svg")
                && html.contains("Target 3")
                && html.contains("leave-one-out")
                && html.contains("ROI edge inset")
        );
        assert!(html.contains("One calibration event"));
        assert!(ticks(0.0, 10.0, 5).len() >= 4);
    }
}
