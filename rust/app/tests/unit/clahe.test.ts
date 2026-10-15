import { describe, expect, it } from "vitest";
import { clahe } from "../../src/viewer";

function image(w: number, h: number, f: (x: number, y: number) => number): { g: Uint8Array; valid: Uint8Array } {
  const g = new Uint8Array(w * h), valid = new Uint8Array(w * h).fill(1);
  for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) g[y * w + x] = f(x, y);
  return { g, valid };
}

describe("clahe", () => {
  it("leaves a flat image flat and ignores invalid pixels", () => {
    const { g, valid } = image(64, 64, () => 120);
    valid[5] = 0;
    const out = clahe(g, 64, 64, valid);
    const set = new Set([...out].filter((_, i) => valid[i]));
    expect(set.size).toBe(1);
    expect(out[5]).toBe(0);
  });
  it("stretches a low-contrast image (gently: the clip limit of 2 caps the gain, as in OpenCV)", () => {
    const { g, valid } = image(256, 256, (x) => 100 + (x % 16)); // values 100..115 only
    const out = clahe(g, 256, 256, valid);
    let lo = 255, hi = 0;
    for (const v of out) { if (v < lo) lo = v; if (v > hi) hi = v; }
    expect(hi - lo).toBeGreaterThan(15 * 1.5);
    expect(hi - lo).toBeLessThan(255);
  });
  it("keeps the order of grey levels inside a tile", () => {
    const { g, valid } = image(64, 64, (x, y) => Math.floor((x + y) * 2));
    const out = clahe(g, 64, 64, valid);
    for (let x = 1; x < 8; x++) expect(out[x]).toBeGreaterThanOrEqual(out[x - 1]);
  });
});
