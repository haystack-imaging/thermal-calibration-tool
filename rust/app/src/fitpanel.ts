// The fit panel on the right: subtitle, mini regression plot (the other shots dashed), residual bars,
// coefficient tiles and warnings.

import { predictUncertainty, type CalEvent, type Fit } from "./types";

function fmt(v: number | null | undefined, d: number): string {
  return v === null || v === undefined || !isFinite(v) ? "–" : v.toFixed(d);
}

function esc(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;");
}

export function plotSvg(ev: CalEvent, others: { label: string; fit: Fit }[], unit: string, width: number, height: number): string {
  const fit = ev.fit;
  if (!fit || fit.single_point) return "";
  const L = 40, R = 12, T = 12, B = 28;
  const allX = [...fit.x_values, ...others.flatMap((o) => o.fit.x_values)];
  const span = Math.max(Math.max(...allX) - Math.min(...allX), 1e-6);
  const xmin = Math.min(...allX) - 0.12 * span - 0.3, xmax = Math.max(...allX) + 0.12 * span + 0.3;
  const xs = Array.from({ length: 40 }, (_, i) => xmin + ((xmax - xmin) * i) / 39);
  const band = xs.map((x) => predictUncertainty(fit, x) ?? 0);
  const ys = [...fit.y_values, ...xs.map((x, i) => fit.slope * x + fit.intercept - band[i]), ...xs.map((x, i) => fit.slope * x + fit.intercept + band[i]),
    ...others.flatMap((o) => [...o.fit.y_values, o.fit.slope * xmin + o.fit.intercept, o.fit.slope * xmax + o.fit.intercept])];
  let ymin = Math.min(...ys), ymax = Math.max(...ys);
  const yspan = Math.max(ymax - ymin, 1e-6);
  ymin -= 0.08 * yspan;
  ymax += 0.08 * yspan;
  const px = (v: number) => L + ((v - xmin) / (xmax - xmin)) * (width - L - R);
  const py = (v: number) => height - B - ((v - ymin) / (ymax - ymin)) * (height - T - B);
  const parts: string[] = [];
  parts.push(`<line x1="${L}" y1="${T}" x2="${L}" y2="${height - B}" stroke="#d9dcd6"/>`);
  parts.push(`<line x1="${L}" y1="${height - B}" x2="${width - R}" y2="${height - B}" stroke="#d9dcd6"/>`);
  for (const o of others) {
    parts.push(`<line x1="${px(xmin)}" y1="${py(o.fit.slope * xmin + o.fit.intercept)}" x2="${px(xmax)}" y2="${py(o.fit.slope * xmax + o.fit.intercept)}" stroke="#9aa39d" stroke-dasharray="4 3"/>`);
    o.fit.x_values.forEach((x, i) => parts.push(`<circle cx="${px(x)}" cy="${py(o.fit.y_values[i])}" r="3" fill="none" stroke="#9aa39d"/>`));
  }
  if (fit.resid_std !== null) {
    const upper = xs.map((x, i) => `${px(x)},${py(fit.slope * x + fit.intercept + band[i])}`);
    const lower = xs.map((x, i) => `${px(x)},${py(fit.slope * x + fit.intercept - band[i])}`).reverse();
    parts.push(`<polygon points="${[...upper, ...lower].join(" ")}" fill="#d9dcd6" opacity="0.6"/>`);
  }
  parts.push(`<line x1="${px(xmin)}" y1="${py(fit.slope * xmin + fit.intercept)}" x2="${px(xmax)}" y2="${py(fit.slope * xmax + fit.intercept)}" stroke="#1d2320" stroke-width="2"/>`);
  const complete = ev.targets.filter((t) => t.complete);
  fit.x_values.forEach((x, i) => {
    const color = complete[i]?.color ?? "#999";
    parts.push(`<circle cx="${px(x)}" cy="${py(fit.y_values[i])}" r="6" fill="${color}" stroke="#1d2320" stroke-width="1.5"/>`);
  });
  const eq = `y = ${fit.slope.toFixed(4)} x ${fit.intercept >= 0 ? "+" : "−"} ${Math.abs(fit.intercept).toFixed(2)}`;
  const eq2 = fit.rmse !== null ? `RMSE ${fit.rmse.toFixed(2)} ${unit}` : "2 targets: R² = 1 by construction";
  const top = fit.slope >= 0;
  const ey = top ? T + 4 : height - B - 22;
  parts.push(`<rect x="${L + 3}" y="${ey - 2}" width="${Math.max(eq.length, eq2.length) * 6.6 + 8}" height="30" fill="#fbfbf9"/>`);
  parts.push(`<text x="${L + 6}" y="${ey + 10}" font-family="ui-monospace, monospace" font-size="11" fill="#1d2320">${esc(eq)}</text>`);
  parts.push(`<text x="${L + 6}" y="${ey + 24}" font-family="ui-monospace, monospace" font-size="11" fill="#1d2320">${esc(eq2)}</text>`);
  if (others.length) {
    const names = others.length <= 2 ? others.map((o) => o.label).join(", ") : `${others.length} other shots`;
    const ly = top ? height - B - 6 : T + 12;
    parts.push(`<text x="${width - R - 4}" y="${ly}" text-anchor="end" font-size="10" fill="#7a827d">dashed: ${esc(names)}</text>`);
  }
  parts.push(`<text x="${(L + width - R) / 2}" y="${height - 4}" text-anchor="middle" font-size="10" fill="#7a827d">measured ${esc(unit)}</text>`);
  parts.push(`<text x="10" y="${T + 4}" font-size="10" fill="#7a827d" transform="rotate(-90 10 ${T + 4})" text-anchor="end">reference ${esc(unit)}</text>`);
  parts.push(`<text x="${L + 2}" y="${height - B + 12}" font-size="10" fill="#7a827d">${xmin.toFixed(0)}</text>`);
  parts.push(`<text x="${width - R}" y="${height - B + 12}" font-size="10" fill="#7a827d" text-anchor="end">${xmax.toFixed(0)}</text>`);
  return `<svg class="fit-plot" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none">${parts.join("")}</svg>`;
}

export function renderFitPanel(el: HTMLElement, ev: CalEvent | null, subtitle: string, others: { label: string; fit: Fit }[], unit: string, message: string): void {
  const fit = ev?.fit ?? null;
  const parts: string[] = [`<div class="fit-head"><span class="t">Fit</span><span class="muted small">${esc(subtitle)}</span></div>`];
  if (!ev || !fit || fit.single_point) {
    const text = !ev ? message : !fit ? "Need at least 2 complete targets" : "single point: K = ρ / DN";
    parts.push(`<div class="fit-plot" style="display:flex;align-items:center;justify-content:center;color:#7a827d;font-size:12px;text-align:center;padding:0 16px">${esc(text)}</div>`);
  } else {
    parts.push(plotSvg(ev, others, unit, 290, 190));
  }
  parts.push(`<div class="muted small">Residuals</div><div class="resid">`);
  if (fit && !fit.single_point) {
    const complete = ev!.targets.filter((t) => t.complete);
    const scale = Math.max(...fit.residuals.map(Math.abs), 1e-9);
    fit.residuals.forEach((r, i) => {
      const t = complete[i];
      const w = Math.abs(r) / scale * 48;
      const left = r >= 0 ? 50 : 50 - w;
      parts.push(`<div class="resid-row"><span class="n">${esc(t?.label.split(" ")[0] ?? `T${i + 1}`)}</span><span class="track"><span class="mid"></span><span class="bar" style="left:${left}%;width:${w}%;background:${t?.color ?? "#999"}"></span></span><span class="v">${(r >= 0 ? "+" : "") + r.toFixed(2)}</span></div>`);
    });
  } else parts.push(`<div class="muted small">–</div>`);
  parts.push(`</div>`);
  const u = fit && !fit.single_point ? predictUncertainty(fit, fit.x_mean) : null;
  const tiles: [string, string][] = fit
    ? fit.single_point
      ? [["slope m", `K = ${fit.slope.toPrecision(6)}`], ["offset b", "0 (by definition)"], ["R² · leave-one-out", "–"], ["±1σ prediction at mean", "–"]]
      : [
          ["slope m", `${fit.slope.toFixed(4)}${fit.se_slope !== null ? ` ± ${fit.se_slope.toFixed(4)}` : ""}`],
          ["offset b", `${(fit.intercept >= 0 ? "+" : "") + fit.intercept.toFixed(3)}${fit.se_intercept !== null ? ` ± ${fit.se_intercept.toFixed(3)}` : ""}`],
          ["R² · leave-one-out", `${fmt(fit.r_squared, 4)} · ${fit.loo_rmse !== null ? fit.loo_rmse.toFixed(2) : "n/a"}`],
          ["±1σ prediction at mean", u !== null ? `± ${u.toFixed(2)} ${unit} at ${fit.x_mean.toFixed(1)}` : "– (needs 3 targets)"],
        ]
    : [["slope m", "–"], ["offset b", "–"], ["R² · leave-one-out", "–"], ["±1σ prediction at mean", "–"]];
  parts.push(`<div class="tiles">${tiles.map(([c, v]) => `<div class="tile"><div class="c">${esc(c)}</div><div class="v">${esc(v)}</div></div>`).join("")}</div>`);
  const warnings = ev?.fit_warnings ?? [];
  if (warnings.length) parts.push(`<div class="warnbox">${warnings.map((w) => "⚠ " + esc(w)).join("<br><br>")}</div>`);
  parts.push(`<div class="fit-btns"><button class="btn" id="btn-plot" ${fit && !fit.single_point ? "" : "disabled"}>Detailed plot</button><button class="btn accent" id="btn-report" ${fit ? "" : "disabled"}>Export fit report</button></div>`);
  el.innerHTML = parts.join("");
}
