// The flight timeline under the centre view: calibration shots as diamonds, survey images as ticks
// (orange outside the calibration window) and the offset b(t) that is interpolated.

import type { CalEvent, TimelinePoint } from "./types";

const HEAD = `<div class="tl-head"><span class="bold">Flight timeline</span>
<span class="legend"><svg width="16" height="12"><polygon points="8,1 15,6 8,11 1,6" fill="#2f6f5e"/></svg>calibration shot</span>
<span class="legend"><svg width="16" height="12"><rect x="7" y="1" width="2" height="10" fill="#9aa39d"/></svg>survey image</span>
<span class="legend"><svg width="16" height="12"><rect x="7" y="1" width="2" height="10" fill="#d7a98a"/></svg>outside the window</span>
<span class="legend"><svg width="16" height="12"><line x1="0" y1="6" x2="16" y2="6" stroke="#c0652a" stroke-width="2"/></svg>offset b(t)</span></div>`;

function hhmm(iso: string): string {
  return iso.slice(11, 16);
}

export function renderTimeline(el: HTMLElement, events: CalEvent[], survey: TimelinePoint[]): void {
  const W = Math.max(el.clientWidth - 28, 200), H = 92;
  const times = [...events.map((e) => e.timestamp), ...survey.map((p) => p.t)].map((t) => Date.parse(t.endsWith("Z") ? t : t + "Z"));
  if (!times.length) {
    el.innerHTML = HEAD + `<svg class="tl-plot" viewBox="0 0 ${W} ${H}"><text x="${W / 2}" y="${H / 2}" text-anchor="middle" font-size="11" fill="#7a827d">Calibration shots appear here as diamonds, survey images as ticks once the survey folder is chosen in 3 · Process.</text></svg>`;
    return;
  }
  const t0 = Math.min(...times), t1 = Math.max(...times);
  const span = Math.max(t1 - t0, 60000);
  const pad = span * 0.05;
  const x = (t: number) => 10 + ((t - t0 + pad) / (span + 2 * pad)) * (W - 20);
  const mid = H * 0.48;
  const parts: string[] = [`<line x1="10" y1="${mid}" x2="${W - 10}" y2="${mid}" stroke="#d9dcd6"/>`];
  for (const p of survey) {
    const px = x(Date.parse(p.t + "Z"));
    parts.push(`<rect x="${px - 1}" y="${mid - 11}" width="2" height="10" fill="${p.outside ? "#d7a98a" : "#9aa39d"}"/>`);
  }
  const fitted = events.filter((e) => e.fit).sort((a, b) => a.timestamp.localeCompare(b.timestamp));
  if (fitted.length >= 2) {
    const bs = fitted.map((e) => e.fit!.intercept);
    const bmin = Math.min(...bs), bmax = Math.max(...bs);
    const yTop = mid + 6, yBot = H - 16;
    const y = (b: number) => (bmax > bmin ? yBot - ((b - bmin) / (bmax - bmin)) * (yBot - yTop) : (yTop + yBot) / 2);
    parts.push(`<polyline points="${fitted.map((e) => `${x(Date.parse(e.timestamp + "Z"))},${y(e.fit!.intercept)}`).join(" ")}" fill="none" stroke="#c0652a" stroke-width="2"/>`);
  }
  let lastX = [-1e9, -1e9];
  for (const e of events) {
    const px = x(Date.parse(e.timestamp + "Z"));
    parts.push(`<polygon points="${px},${mid - 7} ${px + 7},${mid} ${px},${mid + 7} ${px - 7},${mid}" fill="${e.fit ? "#2f6f5e" : "#ffffff"}" stroke="#2f6f5e" stroke-width="2"/>`);
    const label = e.timestamp.slice(11, 19) + (e.fit ? (e.fit.single_point ? ` · K = ${e.fit.slope.toPrecision(4)}` : ` · b = ${(e.fit.intercept >= 0 ? "+" : "") + e.fit.intercept.toFixed(2)}`) : " · not fitted");
    const lx = Math.min(Math.max(px, 60), W - 60);
    const row = lx - lastX[0] >= 125 ? 0 : 1;
    lastX[row] = lx;
    parts.push(`<text x="${lx}" y="${H - 2 - 12 * row}" text-anchor="middle" font-size="10" fill="#1d2320">${label}</text>`);
  }
  if (survey.length) {
    const outside = survey.filter((p) => p.outside).length;
    const first = survey[0].t, last = survey[survey.length - 1].t;
    let summary = `${survey.length} images · ${hhmm(first)} to ${hhmm(last)}`;
    if (fitted.length > 1 && outside) {
      const before = survey.filter((p) => p.outside && p.t < fitted[0].timestamp).length;
      summary += ` · ${before} before / ${outside - before} after the window`;
    }
    parts.push(`<text x="${W / 2}" y="12" text-anchor="middle" font-size="10" fill="#7a827d">${summary}</text>`);
  }
  el.innerHTML = HEAD + `<svg class="tl-plot" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">${parts.join("")}</svg>`;
}
