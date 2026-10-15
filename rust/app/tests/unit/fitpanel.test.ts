import { describe, expect, it } from "vitest";
import { plotSvg, renderFitPanel } from "../../src/fitpanel";
import type { CalEvent, Fit, Target } from "../../src/types";

function target(i: number, mean: number, gt: number): Target {
  return { name: `Target ${i + 1}`, label: `T${i + 1}`, color: ["#E63946", "#2A9D8F", "#E9C46A"][i], polygon: [[0, 0], [5, 0], [5, 5], [0, 5]], inner: [], mean, std: 0.2, pixels: 36, ground_truth: gt, inset_applied: true, complete: true, warn_small: false };
}

const threeFit: Fit = { slope: 1.02, intercept: -0.5, n: 3, single_point: false, r_squared: 0.999, rmse: 0.2, resid_std: 0.35, se_slope: 0.01, se_intercept: 0.3, loo_rmse: 0.4, residuals: [0.1, -0.2, 0.1], x_values: [16, 31, 46], y_values: [15.9, 31.0, 46.5], x_mean: 31, sxx: 450, x_min: 16, x_max: 46 };

function event(fit: Fit | null, warnings: string[] = []): CalEvent {
  return { index: 0, path: "/x/cal.tif", file_name: "cal.tif", timestamp: "2026-06-01T10:00:00", timestamp_source: "XMP", timestamp_tz: "", targets: [target(0, 16, 15.9), target(1, 31, 31.0), target(2, 46, 46.5)], fit, fit_warnings: warnings, range: [10, 50] };
}

describe("plotSvg", () => {
  it("draws the line, the band, one point per target and the equation", () => {
    const svg = plotSvg(event(threeFit), [], "°C", 290, 190);
    expect(svg.startsWith("<svg")).toBe(true);
    expect((svg.match(/<circle /g) ?? []).length).toBe(3);
    expect(svg).toContain("<polygon"); // the ±1σ band
    expect(svg).toContain("y = 1.0200 x − 0.50");
    expect(svg).toContain("RMSE 0.20 °C");
    expect(svg).not.toContain("dashed:");
  });
  it("overlays the other shots dashed with hollow points and a legend", () => {
    const other = { label: "shot 2 · 10:07", fit: { ...threeFit, intercept: -1.5 } };
    const svg = plotSvg(event(threeFit), [other], "°C", 290, 190);
    expect(svg).toContain('stroke-dasharray="4 3"');
    expect((svg.match(/fill="none"/g) ?? []).length).toBe(3);
    expect(svg).toContain("dashed: shot 2 · 10:07");
  });
  it("says so with two targets instead of a band", () => {
    const two: Fit = { ...threeFit, n: 2, rmse: null, resid_std: null, residuals: [0, 0], x_values: [16, 46], y_values: [15.9, 46.5] };
    const svg = plotSvg(event(two), [], "°C", 290, 190);
    expect(svg).toContain("2 targets: R² = 1 by construction");
    expect(svg).not.toContain("<polygon");
  });
  it("is empty for single-point fits and missing fits", () => {
    expect(plotSvg(event(null), [], "°C", 290, 190)).toBe("");
    expect(plotSvg(event({ ...threeFit, single_point: true }), [], "ρ", 290, 190)).toBe("");
  });
});

describe("renderFitPanel", () => {
  it("fills the tiles, the residual bars and the warnings", () => {
    const el = document.createElement("div");
    renderFitPanel(el, event(threeFit, ["2 targets: R² = 1 by construction"]), "shot 1/2 · start · 3 targets", [], "°C", "");
    expect(el.querySelector(".fit-head")?.textContent).toContain("shot 1/2");
    expect(el.querySelectorAll(".resid-row").length).toBe(3);
    const tiles = [...el.querySelectorAll(".tile .v")].map((t) => t.textContent);
    expect(tiles[0]).toBe("1.0200 ± 0.0100");
    expect(tiles[1]).toBe("-0.500 ± 0.300");
    expect(tiles[2]).toBe("0.9990 · 0.40");
    expect(tiles[3]).toContain("°C at 31.0");
    expect(el.querySelector(".warnbox")?.textContent).toContain("⚠ 2 targets");
    expect(el.querySelector<HTMLButtonElement>("#btn-report")?.disabled).toBe(false);
    expect(el.querySelector<HTMLButtonElement>("#btn-plot")?.disabled).toBe(false);
  });
  it("shows the message when nothing is selected and disables the buttons", () => {
    const el = document.createElement("div");
    renderFitPanel(el, null, "", [], "°C", "No calibration image selected");
    expect(el.textContent).toContain("No calibration image selected");
    expect(el.querySelector<HTMLButtonElement>("#btn-report")?.disabled).toBe(true);
    expect([...el.querySelectorAll(".tile .v")].every((t) => t.textContent === "–")).toBe(true);
  });
  it("explains single-point fits", () => {
    const el = document.createElement("div");
    renderFitPanel(el, event({ ...threeFit, single_point: true, slope: 0.00025 }), "shot 1/1", [], "ρ", "");
    expect(el.textContent).toContain("single point: K = ρ / DN");
    expect(el.querySelector(".tile .v")?.textContent).toContain("K = 0.00025");
    expect(el.querySelector<HTMLButtonElement>("#btn-plot")?.disabled).toBe(true);
  });
});
