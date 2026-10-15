import { describe, expect, it } from "vitest";
import { decodePixels, predictUncertainty, type Fit } from "../../src/types";

const fit = (over: Partial<Fit>): Fit => ({
  slope: 1, intercept: 0, n: 3, single_point: false, r_squared: 1, rmse: 0.1, resid_std: 0.5, se_slope: null, se_intercept: null,
  loo_rmse: null, residuals: [], x_values: [], y_values: [], x_mean: 1, sxx: 2, x_min: 0, x_max: 2, ...over,
});

describe("predictUncertainty", () => {
  it("needs three targets, a spread and a residual std", () => {
    expect(predictUncertainty(fit({ n: 2 }), 1)).toBeNull();
    expect(predictUncertainty(fit({ sxx: 0 }), 1)).toBeNull();
    expect(predictUncertainty(fit({ resid_std: null }), 1)).toBeNull();
    expect(predictUncertainty(fit({ single_point: true }), 1)).toBeNull();
  });
  it("is s·sqrt(1 + 1/n + (x − x̄)²/Sxx), like the Rust side", () => {
    expect(predictUncertainty(fit({}), 2)).toBeCloseTo(0.5 * Math.sqrt(1 + 1 / 3 + 0.5), 12);
    expect(predictUncertainty(fit({}), 1)).toBeCloseTo(0.5 * Math.sqrt(1 + 1 / 3), 12);
  });
});

describe("decodePixels", () => {
  it("turns the Rust side's base64 float32 payload back into pixels", () => {
    const values = new Float32Array([1.5, -2.25, NaN, 1e6]);
    const bytes = new Uint8Array(values.buffer);
    let bin = "";
    for (const b of bytes) bin += String.fromCharCode(b);
    const px = decodePixels({ width: 2, height: 2, data_base64: btoa(bin), lo: -2.25, hi: 1e6 });
    expect(px.length).toBe(4);
    expect(px[0]).toBe(1.5);
    expect(px[1]).toBe(-2.25);
    expect(Number.isNaN(px[2])).toBe(true);
    expect(px[3]).toBe(1e6);
  });
});
