import { describe, expect, it } from "vitest";
import { renderTimeline } from "../../src/timeline";
import type { CalEvent, Fit, TimelinePoint } from "../../src/types";

const fit = (b: number): Fit => ({ slope: 1, intercept: b, n: 3, single_point: false, r_squared: 1, rmse: 0.1, resid_std: 0.1, se_slope: 0.01, se_intercept: 0.1, loo_rmse: 0.1, residuals: [], x_values: [], y_values: [], x_mean: 0, sxx: 1, x_min: 0, x_max: 1 });
const ev = (i: number, t: string, f: Fit | null): CalEvent => ({ index: i, path: "", file_name: `cal_${i}.tif`, timestamp: t, timestamp_source: "XMP", timestamp_tz: "", targets: [], fit: f, fit_warnings: [], range: null });
const pt = (t: string, outside: boolean): TimelinePoint => ({ file: "s.tif", t, source: "XMP", outside });

describe("renderTimeline", () => {
  it("says what will appear when there is nothing yet", () => {
    const el = document.createElement("div");
    renderTimeline(el, [], []);
    expect(el.textContent).toContain("Calibration shots appear here as diamonds");
    expect(el.querySelector("svg.tl-plot")).not.toBeNull();
    expect(el.querySelectorAll(".legend").length).toBe(4);
  });
  it("draws diamonds, ticks (orange outside the window), the offset line and the summary", () => {
    const el = document.createElement("div");
    const events = [ev(0, "2026-06-01T10:00:00", fit(-1.4)), ev(1, "2026-06-01T10:14:00", fit(-3.9))];
    const survey = [pt("2026-06-01T10:01:00", false), pt("2026-06-01T10:07:00", false), pt("2026-06-01T10:20:00", true)];
    renderTimeline(el, events, survey);
    const plot = el.querySelector("svg.tl-plot")!;
    expect(plot.querySelectorAll("polygon").length).toBe(2);
    const ticks = [...plot.querySelectorAll("rect")];
    expect(ticks.length).toBe(3);
    expect(ticks.filter((r) => r.getAttribute("fill") === "#d7a98a").length).toBe(1);
    expect(plot.querySelector("polyline")?.getAttribute("stroke")).toBe("#c0652a");
    expect(plot.textContent).toContain("3 images · 10:01 to 10:20 · 0 before / 1 after the window");
    expect(plot.textContent).toContain("10:00:00 · b = -1.40");
  });
  it("marks an unfitted shot and draws no offset line for a single fit", () => {
    const el = document.createElement("div");
    renderTimeline(el, [ev(0, "2026-06-01T10:00:00", fit(0)), ev(1, "2026-06-01T10:10:00", null)], []);
    const plot = el.querySelector("svg.tl-plot")!;
    expect(plot.querySelector("polyline")).toBeNull();
    expect(plot.textContent).toContain("not fitted");
  });
});
