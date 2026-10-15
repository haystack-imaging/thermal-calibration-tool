// The image view: zoom (wheel), pan (right or middle drag), polygons with their eroded regions, the
// loupe while drawing, the cursor readout and a colour bar. All drawing happens client-side on the
// float pixels the Rust side sends once per image.

import type { Point, Target } from "./types";

export type VizMode = "inferno" | "grey" | "clahe";

export interface ViewerImage {
  width: number;
  height: number;
  data: Float32Array;
  lo: number;
  hi: number;
}

const C: number[][] = [
  [0.0002189403691192265, 0.001651004631001012, -0.01948089843709184],
  [0.1065134194856116, 0.5639564367884091, 3.932712388889277],
  [11.60249308247187, -3.972853965665698, -15.9423941062914],
  [-41.70399613139459, 17.43639888205313, 44.35414519872813],
  [77.162935699427, -33.40235894210092, -81.80730925738993],
  [-71.31942824499214, 32.62606426397723, 73.20951985803202],
  [25.13112622477341, -12.24266895238567, -23.07032500287172],
];

function inferno(t: number): [number, number, number] {
  const out: [number, number, number] = [0, 0, 0];
  for (let i = 0; i < 3; i++) {
    const v = C[0][i] + t * (C[1][i] + t * (C[2][i] + t * (C[3][i] + t * (C[4][i] + t * (C[5][i] + t * C[6][i])))));
    out[i] = Math.round(Math.min(1, Math.max(0, v)) * 255);
  }
  return out;
}

const INFERNO: [number, number, number][] = Array.from({ length: 256 }, (_, i) => inferno(i / 255));

/** Contrast-limited adaptive histogram equalisation of 8-bit grey values, as OpenCV's CLAHE
 *  (8×8 tiles, clip limit 2.0, bilinear interpolation between the tile mappings). */
export function clahe(g: Uint8Array, w: number, h: number, valid: Uint8Array, tiles = 8, clip = 2.0): Uint8Array {
  const tw = Math.ceil(w / tiles), th = Math.ceil(h / tiles);
  const luts: Uint8Array[] = [];
  for (let ty = 0; ty < tiles; ty++)
    for (let tx = 0; tx < tiles; tx++) {
      const hist = new Uint32Array(256);
      let n = 0;
      for (let y = ty * th; y < Math.min(h, (ty + 1) * th); y++)
        for (let x = tx * tw; x < Math.min(w, (tx + 1) * tw); x++) {
          const i = y * w + x;
          if (valid[i]) { hist[g[i]]++; n++; }
        }
      const lut = new Uint8Array(256);
      if (n > 0) {
        const limit = Math.max(1, Math.floor((clip * n) / 256));
        let excess = 0;
        for (let i = 0; i < 256; i++) if (hist[i] > limit) { excess += hist[i] - limit; hist[i] = limit; }
        const share = Math.floor(excess / 256), rest = excess - share * 256;
        for (let i = 0; i < 256; i++) hist[i] += share + (i < rest ? 1 : 0);
        let cdf = 0;
        for (let i = 0; i < 256; i++) { cdf += hist[i]; lut[i] = Math.round((cdf * 255) / n); }
      } else for (let i = 0; i < 256; i++) lut[i] = i;
      luts.push(lut);
    }
  const out = new Uint8Array(w * h);
  for (let y = 0; y < h; y++) {
    const fy = (y + 0.5) / th - 0.5;
    const y0 = Math.max(0, Math.min(tiles - 1, Math.floor(fy))), y1 = Math.min(tiles - 1, y0 + 1);
    const wy = Math.min(1, Math.max(0, fy - y0));
    for (let x = 0; x < w; x++) {
      const i = y * w + x;
      if (!valid[i]) continue;
      const fx = (x + 0.5) / tw - 0.5;
      const x0 = Math.max(0, Math.min(tiles - 1, Math.floor(fx))), x1 = Math.min(tiles - 1, x0 + 1);
      const wx = Math.min(1, Math.max(0, fx - x0));
      const v = g[i];
      const top = luts[y0 * tiles + x0][v] * (1 - wx) + luts[y0 * tiles + x1][v] * wx;
      const bot = luts[y1 * tiles + x0][v] * (1 - wx) + luts[y1 * tiles + x1][v] * wx;
      out[i] = Math.round(top * (1 - wy) + bot * wy);
    }
  }
  return out;
}

const SCENE_URL = new URL("./assets/scene-flight.png", import.meta.url).href;
/** Text on the image canvas, which stays dark in both themes. */
const CANVAS_TEXT = "#A8B0A6";

function themeColor(name: string, fallback: string): string {
  const v = typeof getComputedStyle === "function" ? getComputedStyle(document.documentElement).getPropertyValue(name).trim() : "";
  return v || fallback;
}

export class Viewer {
  readonly canvas: HTMLCanvasElement;
  private ctx: CanvasRenderingContext2D;
  private off = document.createElement("canvas");
  private offCtx: CanvasRenderingContext2D;
  image: ViewerImage | null = null;
  mode: VizMode = "inferno";
  scale = 1;
  ox = 0; // image coordinate at the canvas' left edge
  oy = 0;
  fitted = true;
  targets: Target[] = [];
  activeTarget: number | null = null;
  drawing: Point[] | null = null;
  cursor: { x: number; y: number } | null = null;
  placeholder = "";
  stretch: [number, number] = [0, 1];
  /** Shown next to the cursor, e.g. "°C". */
  unit = "°C";
  /** The finite value under the cursor, if any. */
  hoverValue: number | null = null;
  onClick: (x: number, y: number) => void = () => {};
  onMotion: (x: number, y: number, value: number | null) => void = () => {};
  onViewChange: (scale: number) => void = () => {};
  onEscape: () => void = () => {};
  private panStart: { x: number; y: number; ox: number; oy: number } | null = null;

  constructor(canvas: HTMLCanvasElement) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d")!;
    this.offCtx = this.off.getContext("2d")!;
    new ResizeObserver(() => this.resize()).observe(canvas);
    canvas.addEventListener("wheel", (e) => this.wheel(e), { passive: false });
    canvas.addEventListener("mousedown", (e) => this.down(e));
    window.addEventListener("mousemove", (e) => this.move(e));
    window.addEventListener("mouseup", () => (this.panStart = null));
    canvas.addEventListener("mouseleave", () => {
      this.cursor = null;
      this.hoverValue = null;
      this.redraw();
    });
    canvas.addEventListener("contextmenu", (e) => e.preventDefault());
    window.addEventListener("keydown", (e) => {
      if (e.key === "Escape") this.onEscape();
    });
    this.resize();
  }

  private resize() {
    const r = this.canvas.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    this.canvas.width = Math.max(1, Math.round(r.width * dpr));
    this.canvas.height = Math.max(1, Math.round(r.height * dpr));
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    if (this.fitted) this.fit();
    else this.redraw();
  }

  /** Canvas size in CSS pixels. */
  size(): [number, number] {
    const r = this.canvas.getBoundingClientRect();
    return [r.width, r.height];
  }

  setImage(img: ViewerImage | null) {
    this.image = img;
    this.renderPixels();
    this.fit();
  }

  setMode(mode: VizMode) {
    this.mode = mode;
    this.renderPixels();
    this.redraw();
  }

  private renderPixels() {
    const img = this.image;
    if (!img) return;
    this.off.width = img.width;
    this.off.height = img.height;
    const lo = img.lo, hi = img.hi;
    this.stretch = [lo, hi];
    const span = hi > lo ? hi - lo : 1;
    const n = img.data.length;
    const grey = new Uint8Array(n), valid = new Uint8Array(n);
    for (let i = 0; i < n; i++) {
      const v = img.data[i];
      if (isFinite(v)) { valid[i] = 1; grey[i] = Math.round(Math.min(1, Math.max(0, (v - lo) / span)) * 255); }
    }
    const levels = this.mode === "clahe" ? clahe(grey, img.width, img.height, valid) : grey;
    const out = this.offCtx.createImageData(img.width, img.height);
    const d = out.data;
    for (let i = 0, j = 0; i < n; i++, j += 4) {
      if (!valid[i]) {
        d[j] = d[j + 1] = d[j + 2] = 0;
        d[j + 3] = 255;
        continue;
      }
      const g = levels[i];
      if (this.mode === "inferno") {
        const c = INFERNO[g];
        d[j] = c[0]; d[j + 1] = c[1]; d[j + 2] = c[2];
      } else {
        d[j] = d[j + 1] = d[j + 2] = g;
      }
      d[j + 3] = 255;
    }
    this.offCtx.putImageData(out, 0, 0);
  }

  fit() {
    const [W, H] = this.size();
    if (!this.image) {
      this.redraw();
      return;
    }
    if (!W || !H) {
      this.fitted = true; // the canvas is hidden: the ResizeObserver fits the image once it has a size
      return;
    }
    this.scale = Math.max(0.05, Math.min(W / this.image.width, H / this.image.height));
    this.ox = (this.image.width - W / this.scale) / 2;
    this.oy = (this.image.height - H / this.scale) / 2;
    this.fitted = true;
    this.redraw();
  }

  oneToOne() {
    if (!this.image) return;
    const [W, H] = this.size();
    const cx = this.ox + W / (2 * this.scale), cy = this.oy + H / (2 * this.scale);
    this.scale = 1;
    this.ox = cx - W / 2;
    this.oy = cy - H / 2;
    this.fitted = false;
    this.clamp();
    this.redraw();
  }

  toCanvas(x: number, y: number): [number, number] {
    return [(x - this.ox) * this.scale, (y - this.oy) * this.scale];
  }

  toImage(cx: number, cy: number): [number, number] {
    return [cx / this.scale + this.ox, cy / this.scale + this.oy];
  }

  /** Index of the corner being drawn that lies under image point (x, y), or -1. */
  hitPoint(x: number, y: number): number {
    if (!this.drawing) return -1;
    const tol = Math.max(2, 8 / this.scale);
    return this.drawing.findIndex(([px, py]) => Math.hypot(px - x, py - y) <= tol);
  }

  private clamp() {
    if (!this.image) return;
    const [W, H] = this.size();
    const vw = W / this.scale, vh = H / this.scale;
    if (vw >= this.image.width) this.ox = (this.image.width - vw) / 2;
    else this.ox = Math.min(Math.max(this.ox, 0), this.image.width - vw);
    if (vh >= this.image.height) this.oy = (this.image.height - vh) / 2;
    else this.oy = Math.min(Math.max(this.oy, 0), this.image.height - vh);
  }

  private wheel(e: WheelEvent) {
    e.preventDefault();
    if (!this.image) return;
    const r = this.canvas.getBoundingClientRect();
    const cx = e.clientX - r.left, cy = e.clientY - r.top;
    const [ix, iy] = this.toImage(cx, cy);
    const f = e.deltaY < 0 ? 1.25 : 0.8;
    this.scale = Math.min(40, Math.max(0.05, this.scale * f));
    this.ox = ix - cx / this.scale;
    this.oy = iy - cy / this.scale;
    this.fitted = false;
    this.clamp();
    this.redraw();
  }

  private down(e: MouseEvent) {
    const r = this.canvas.getBoundingClientRect();
    const cx = e.clientX - r.left, cy = e.clientY - r.top;
    if (e.button === 2 || e.button === 1) {
      this.panStart = { x: cx, y: cy, ox: this.ox, oy: this.oy };
      e.preventDefault();
      return;
    }
    if (e.button === 0 && this.image) {
      const [ix, iy] = this.toImage(cx, cy);
      if (ix >= 0 && iy >= 0 && ix < this.image.width && iy < this.image.height) this.onClick(Math.floor(ix), Math.floor(iy));
    }
  }

  private move(e: MouseEvent) {
    const r = this.canvas.getBoundingClientRect();
    const cx = e.clientX - r.left, cy = e.clientY - r.top;
    if (this.panStart) {
      this.ox = this.panStart.ox - (cx - this.panStart.x) / this.scale;
      this.oy = this.panStart.oy - (cy - this.panStart.y) / this.scale;
      this.fitted = false;
      this.clamp();
      this.redraw();
      return;
    }
    if (cx < 0 || cy < 0 || cx > r.width || cy > r.height) return;
    this.cursor = { x: cx, y: cy };
    if (this.image) {
      const [ix, iy] = this.toImage(cx, cy);
      const inside = ix >= 0 && iy >= 0 && ix < this.image.width && iy < this.image.height;
      const v = inside ? this.image.data[Math.floor(iy) * this.image.width + Math.floor(ix)] : null;
      this.hoverValue = v === null || !isFinite(v) ? null : v;
      this.onMotion(Math.floor(ix), Math.floor(iy), this.hoverValue);
    }
    this.redraw();
  }

  /** The empty-canvas scene (a drone over three calibration panels), drawn with the Haystack pixel kit. */
  private scene: HTMLImageElement | null = null;

  redraw() {
    const ctx = this.ctx;
    const [W, H] = this.size();
    ctx.fillStyle = themeColor("--canvas", "#121A16");
    ctx.fillRect(0, 0, W, H);
    this.onViewChange(this.scale);
    if (!this.image) {
      if (!this.scene) {
        this.scene = new Image();
        this.scene.onload = () => this.redraw();
        this.scene.src = SCENE_URL;
      }
      if (this.scene.complete && this.scene.naturalWidth) {
        const k = Math.max(2, Math.min(4, Math.floor(Math.min(W / 240, H / 180))));
        const sw = this.scene.naturalWidth * k, sh = this.scene.naturalHeight * k;
        ctx.imageSmoothingEnabled = false;
        ctx.drawImage(this.scene, Math.round(W / 2 - sw / 2), Math.round(H / 2 - 34 - sh), sw, sh);
      }
      ctx.fillStyle = CANVAS_TEXT;
      ctx.font = "13px system-ui, sans-serif";
      ctx.textAlign = "center";
      this.placeholder.split("\n").forEach((line, i) => ctx.fillText(line, W / 2, H / 2 - 10 + i * 18));
      return;
    }
    ctx.imageSmoothingEnabled = this.scale < 1;
    const [dx, dy] = this.toCanvas(0, 0);
    ctx.drawImage(this.off, dx, dy, this.image.width * this.scale, this.image.height * this.scale);
    this.drawOverlay(ctx);
    this.drawColorBar(ctx, W, H);
    if (this.cursor && this.hoverValue !== null) this.drawReadout(ctx, W, H);
    if (this.drawing && this.cursor) this.drawLoupe(ctx);
  }

  private path(ctx: CanvasRenderingContext2D, pts: Point[], offset = 0.5) {
    ctx.beginPath();
    pts.forEach(([x, y], i) => {
      const [cx, cy] = this.toCanvas(x + offset, y + offset);
      if (i === 0) ctx.moveTo(cx, cy);
      else ctx.lineTo(cx, cy);
    });
    ctx.closePath();
  }

  private drawOverlay(ctx: CanvasRenderingContext2D) {
    ctx.lineJoin = "round";
    this.targets.forEach((t, i) => {
      if (this.drawing && i === this.activeTarget) return; // the old polygon hides while a new one is drawn
      if (t.polygon.length < 3) return;
      this.path(ctx, t.polygon);
      ctx.lineWidth = 2;
      ctx.strokeStyle = t.color;
      ctx.stroke();
      for (const inner of t.inner) {
        this.path(ctx, inner, 0);
        ctx.setLineDash([4, 3]);
        ctx.lineWidth = 1;
        ctx.strokeStyle = "#ffffff";
        ctx.stroke();
        ctx.setLineDash([]);
      }
      const [cx, cy] = this.toCanvas(t.polygon.reduce((s, p) => s + p[0], 0) / t.polygon.length, t.polygon.reduce((s, p) => s + p[1], 0) / t.polygon.length);
      const label = t.mean !== null && isFinite(t.mean) ? `${t.label} ${t.mean.toFixed(2)}` : t.label;
      ctx.font = "bold 12px system-ui, sans-serif";
      const w = ctx.measureText(label).width + 8;
      ctx.fillStyle = "rgba(255,255,255,0.9)";
      ctx.fillRect(cx - w / 2, cy - 9, w, 18);
      ctx.fillStyle = "#1d2320";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(label, cx, cy);
    });
    if (this.drawing) {
      const color = this.activeTarget !== null ? this.targets[this.activeTarget]?.color ?? "#fff" : "#fff";
      ctx.strokeStyle = color;
      ctx.fillStyle = color;
      ctx.lineWidth = 2;
      const pts = this.drawing.map(([x, y]) => this.toCanvas(x + 0.5, y + 0.5));
      if (pts.length > 1) {
        ctx.beginPath();
        pts.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
        ctx.stroke();
      }
      if (pts.length >= 3) {
        // the closing edge, dashed: a click on the ringed first corner (or Enter) closes the polygon
        ctx.setLineDash([5, 4]);
        ctx.beginPath();
        ctx.moveTo(pts[pts.length - 1][0], pts[pts.length - 1][1]);
        ctx.lineTo(pts[0][0], pts[0][1]);
        ctx.stroke();
        ctx.setLineDash([]);
      }
      pts.forEach(([x, y], i) => {
        ctx.beginPath();
        if (i === 0 && pts.length >= 3) {
          ctx.arc(x, y, 7, 0, Math.PI * 2);
          ctx.fillStyle = "#ffffff";
          ctx.fill();
          ctx.lineWidth = 2.5;
          ctx.stroke();
          ctx.fillStyle = color;
          ctx.lineWidth = 2;
        } else {
          ctx.arc(x, y, 4, 0, Math.PI * 2);
          ctx.fill();
        }
      });
    }
  }

  private drawColorBar(ctx: CanvasRenderingContext2D, W: number, H: number) {
    const x = W - 26, y0 = 14, h = Math.max(60, H - 40);
    for (let i = 0; i < h; i++) {
      const g = Math.round((1 - i / h) * 255);
      const c = this.mode === "inferno" ? INFERNO[g] : [g, g, g];
      ctx.fillStyle = `rgb(${c[0]},${c[1]},${c[2]})`;
      ctx.fillRect(x, y0 + i, 12, 1);
    }
    ctx.strokeStyle = "#555";
    ctx.strokeRect(x - 0.5, y0 - 0.5, 13, h + 1);
    ctx.fillStyle = "#e6e8e3";
    ctx.font = "11px system-ui, sans-serif";
    ctx.textAlign = "right";
    ctx.textBaseline = "top";
    ctx.fillText(this.stretch[1].toFixed(1), x - 4, y0);
    ctx.textBaseline = "bottom";
    ctx.fillText(this.stretch[0].toFixed(1), x - 4, y0 + h);
  }

  /** A half-transparent label next to the cursor with the value under it. */
  private drawReadout(ctx: CanvasRenderingContext2D, W: number, H: number) {
    const text = `${this.hoverValue!.toFixed(2)} ${this.unit}`;
    ctx.font = "bold 13px system-ui, sans-serif";
    const w = ctx.measureText(text).width + 14;
    const h = 24;
    let x = this.cursor!.x + 16;
    let y = this.cursor!.y + 16;
    if (x + w > W - 8) x = this.cursor!.x - 16 - w;
    if (y + h > H - 8) y = this.cursor!.y - 16 - h;
    ctx.fillStyle = "rgba(29, 35, 32, 0.6)";
    ctx.fillRect(x, y, w, h);
    ctx.fillStyle = "#ffffff";
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    ctx.fillText(text, x + 7, y + h / 2);
  }

  private drawLoupe(ctx: CanvasRenderingContext2D) {
    if (!this.image || !this.cursor) return;
    const [ix, iy] = this.toImage(this.cursor.x, this.cursor.y);
    const n = 15, size = 120, px = size / n;
    const sx = Math.floor(ix) - (n - 1) / 2, sy = Math.floor(iy) - (n - 1) / 2;
    ctx.save();
    ctx.fillStyle = "#1d2320";
    ctx.fillRect(10, 10, size + 4, size + 4);
    ctx.imageSmoothingEnabled = false;
    ctx.drawImage(this.off, sx, sy, n, n, 12, 12, size, size);
    ctx.strokeStyle = "rgba(255,255,255,0.9)";
    ctx.lineWidth = 1;
    ctx.strokeRect(12 + ((n - 1) / 2) * px, 12 + ((n - 1) / 2) * px, px, px);
    ctx.restore();
  }
}
