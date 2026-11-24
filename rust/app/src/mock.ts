// An in-browser stand-in for the Rust engine: a synthetic flight, the same fit mathematics, a fake
// batch. Only for developing the interface in a plain browser; the desktop app never loads it.

import type { AddResult, AppState, CalEvent, Fit, ImagePayload, Point, Preflight, Settings, Survey, Target } from "./types";

const PALETTE = ["#E63946", "#2A9D8F", "#E9C46A", "#4C7BD9", "#F4A261", "#9B5DE5", "#00F5D4", "#F15BB5", "#00BBF9", "#B8E986"];
const W = 320;
const H = 256;
const PANELS: [number, number, number][] = [[40, 40, 18], [140, 40, 29], [240, 40, 41]]; // x, y, °C (40 px squares)

interface MockShot {
  path: string;
  minute: number;
  pixels: Float32Array;
  targets: { name: string; color: string; polygon: Point[]; ground_truth: number | null }[];
  fit: Fit | null;
}

const listeners = new Map<string, ((p: unknown) => void)[]>();
let state = {
  title: "untitled project",
  project_path: null as string | null,
  settings: { mode: "thermal", single_point: true, emissivity: { enabled: false, target_emissivity: 0.95, scene_emissivity: 0.98, sky_temperature: -40 }, roi_inset_px: 3 } as Settings,
  survey: { folder: "", output_folder: "", filter: "", copy_metadata: true, skip_existing: false } as Survey,
  shots: [] as MockShot[],
  last_output: null as string | null,
  running: false,
  cancel: false,
};
let counter = 0;
const inspectImages: { name: string; path: string }[] = [];
type Snapshot = { path: string; targets: MockShot["targets"]; fit: Fit | null }[];
const undoStack: Snapshot[] = [];
const redoStack: Snapshot[] = [];
const snap = (): Snapshot => state.shots.map((s) => ({ path: s.path, targets: structuredClone(s.targets), fit: s.fit }));
function snapshot() {
  undoStack.push(snap());
  if (undoStack.length > 50) undoStack.shift();
  redoStack.length = 0;
}
function restore(sn: Snapshot) {
  for (const r of sn) {
    const s = state.shots.find((x) => x.path === r.path);
    if (s) {
      s.targets = structuredClone(r.targets);
      s.fit = r.fit;
    }
  }
}

function synthShot(minute: number): Float32Array {
  const a = 0.97 - (minute / 14) * 0.02;
  const c = 1.0 + (minute / 14) * 2.2;
  const px = new Float32Array(W * H);
  for (let y = 0; y < H; y++)
    for (let x = 0; x < W; x++) {
      let t = 32 + 1.5 * Math.sin(x / 23) * Math.cos(y / 31);
      for (const [px0, py0, tc] of PANELS) if (x >= px0 && x < px0 + 40 && y >= py0 && y < py0 + 40) t = tc;
      const v = -0.4 * (((x - W / 2) / (W / 2)) ** 2 + ((y - H / 2) / (H / 2)) ** 2);
      px[y * W + x] = a * t + c + v + (Math.random() - 0.5) * 0.3;
    }
  return px;
}

function insideMask(poly: Point[], inset: number): Uint8Array {
  const m = new Uint8Array(W * H);
  if (poly.length < 3) return m;
  const xs = poly.map((p) => p[0]);
  const ys = poly.map((p) => p[1]);
  const [x0, x1, y0, y1] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
  // bounding-box fill (the mock does not need true polygon rasterisation), eroded by the inset
  for (let y = Math.max(0, y0 + inset); y <= Math.min(H - 1, y1 - inset); y++)
    for (let x = Math.max(0, x0 + inset); x <= Math.min(W - 1, x1 - inset); x++) m[y * W + x] = 1;
  return m;
}

function ols(x: number[], y: number[]): Fit | null {
  const n = x.length;
  if (n < 2) return null;
  const xm = x.reduce((a, b) => a + b, 0) / n;
  const ym = y.reduce((a, b) => a + b, 0) / n;
  let sxx = 0, sxy = 0, syy = 0;
  for (let i = 0; i < n; i++) {
    sxx += (x[i] - xm) ** 2;
    sxy += (x[i] - xm) * (y[i] - ym);
    syy += (y[i] - ym) ** 2;
  }
  if (sxx === 0) return null;
  const slope = sxy / sxx;
  const intercept = ym - slope * xm;
  const residuals = x.map((v, i) => y[i] - (slope * v + intercept));
  const f: Fit = { slope, intercept, n, single_point: false, r_squared: syy > 0 ? (sxy * sxy) / (sxx * syy) : null, rmse: null, resid_std: null, se_slope: null, se_intercept: null, loo_rmse: null, residuals, x_values: x, y_values: y, x_mean: xm, sxx, x_min: Math.min(...x), x_max: Math.max(...x) };
  if (n > 2) {
    const sse = residuals.reduce((a, r) => a + r * r, 0);
    const s = Math.sqrt(sse / (n - 2));
    f.rmse = Math.sqrt(sse / n);
    f.resid_std = s;
    f.se_slope = s / Math.sqrt(sxx);
    f.se_intercept = s * Math.sqrt(1 / n + (xm * xm) / sxx);
    const loo: number[] = [];
    for (let i = 0; i < n; i++) {
      const xk = x.filter((_, k) => k !== i), yk = y.filter((_, k) => k !== i);
      const g = ols(xk, yk);
      if (g) loo.push(y[i] - (g.slope * x[i] + g.intercept));
    }
    if (loo.length) f.loo_rmse = Math.sqrt(loo.reduce((a, r) => a + r * r, 0) / loo.length);
  }
  return f;
}

function measure(shot: MockShot, t: MockShot["targets"][number]): { mean: number | null; std: number | null; pixels: number } {
  if (t.polygon.length < 3) return { mean: null, std: null, pixels: 0 };
  const m = insideMask(t.polygon, state.settings.roi_inset_px);
  const vals: number[] = [];
  for (let i = 0; i < m.length; i++) if (m[i]) vals.push(shot.pixels[i]);
  if (!vals.length) return { mean: NaN, std: NaN, pixels: 0 };
  const mean = vals.reduce((a, b) => a + b, 0) / vals.length;
  const std = Math.sqrt(vals.reduce((a, v) => a + (v - mean) ** 2, 0) / vals.length);
  return { mean, std, pixels: vals.length };
}

function refit(shot: MockShot) {
  const xs: number[] = [], ys: number[] = [];
  for (const t of shot.targets) {
    const m = measure(shot, t);
    if (m.mean !== null && isFinite(m.mean) && t.ground_truth !== null) {
      xs.push(m.mean);
      ys.push(t.ground_truth);
    }
  }
  shot.fit = ols(xs, ys);
}

function targetDto(shot: MockShot, i: number, t: MockShot["targets"][number]): Target {
  const m = measure(shot, t);
  const isDefault = /^Target \d+$/.test(t.name.trim()) || !t.name.trim();
  const inset = state.settings.roi_inset_px;
  const xs = t.polygon.map((p) => p[0]), ys = t.polygon.map((p) => p[1]);
  const inner: Point[][] = t.polygon.length >= 3 && m.pixels > 0
    ? [[[Math.min(...xs) + inset, Math.min(...ys) + inset], [Math.max(...xs) - inset + 1, Math.min(...ys) + inset], [Math.max(...xs) - inset + 1, Math.max(...ys) - inset + 1], [Math.min(...xs) + inset, Math.max(...ys) - inset + 1]]]
    : [];
  return { name: t.name, label: isDefault ? `T${i + 1}` : `T${i + 1} ${t.name.trim()}`, color: t.color, polygon: t.polygon, inner, mean: m.mean, std: m.std, pixels: m.pixels, ground_truth: t.ground_truth, inset_applied: true, complete: t.polygon.length >= 3 && m.mean !== null && isFinite(m.mean) && t.ground_truth !== null, warn_small: t.polygon.length >= 3 && m.pixels < 25 };
}

function eventDto(shot: MockShot, index: number): CalEvent {
  const t0 = new Date(Date.UTC(2026, 5, 1, 10, 0, 0) + shot.minute * 60000);
  const warnings: string[] = [];
  if (shot.fit && shot.fit.n === 2) warnings.push("2 targets: R² = 1 by construction and no fit uncertainty can be estimated. A third target would fix that.");
  let lo = Infinity, hi = -Infinity;
  for (const v of shot.pixels) { if (v < lo) lo = v; if (v > hi) hi = v; }
  if (shot.fit && (lo < shot.fit.x_min - 1 || hi > shot.fit.x_max + 1)) warnings.push(`Targets span ${shot.fit.x_min.toFixed(1)} to ${shot.fit.x_max.toFixed(1)}; this scene reaches ${lo.toFixed(1)} to ${hi.toFixed(1)}. Pixels outside the span are extrapolated.`);
  return { index, path: "/demo/" + shot.path, file_name: shot.path, timestamp: t0.toISOString().slice(0, 19), timestamp_source: "XMP", timestamp_tz: "+02:00", targets: shot.targets.map((t, i) => targetDto(shot, i, t)), fit: shot.fit, fit_warnings: warnings, range: [lo, hi] };
}

function surveyTimeline() {
  if (!state.survey.folder) return [];
  const fitted = state.shots.filter((s) => s.fit).map((s) => s.minute);
  const [a, b] = [Math.min(...fitted), Math.max(...fitted)];
  const pts = [];
  for (let j = 0; j < 40; j++) {
    const minute = 1 + j * 0.5;
    const t = new Date(Date.UTC(2026, 5, 1, 10, 0, 0) + minute * 60000).toISOString().slice(0, 19);
    pts.push({ file: `survey_${String(j).padStart(3, "0")}.tif`, t, source: "XMP", outside: fitted.length > 1 && (minute < a || minute > b) });
  }
  return pts;
}

function stateDto(): AppState {
  return { tool_name: "Thermal / MSI Calibration Tool", version: "0.3.0-mock", title: state.title, project_path: state.project_path, settings: state.settings, survey: state.survey, events: state.shots.map(eventDto), survey_timeline: surveyTimeline(), last_output: state.last_output, running: state.running, can_undo: undoStack.length > 0, can_redo: redoStack.length > 0 };
}

function preflight(): Preflight {
  const lines: [string, string][] = [];
  const fitted = state.shots.filter((s) => s.fit);
  if (!fitted.length) lines.push(["block", "No calibration image has a complete fit yet."]);
  else {
    lines.push(["ok", `${fitted.length} calibration event(s): ` + fitted.map((s) => `10:${String(s.minute).padStart(2, "0")}:00 (XMP)`).join(", ")]);
    if (!state.survey.folder) lines.push(["block", "No survey images found (check folder, filter and that calibration images are excluded)."]);
    else {
      lines.push(["ok", "40 image(s) will be processed."], ["ok", "Capture times: 40 from XMP."]);
      const out = surveyTimeline().filter((p) => p.outside).length;
      if (out) lines.push(["warn", `${out} image(s) taken after the last calibration shot (10:14:00): the last coefficients are used unchanged.`]);
      else lines.push(["ok", "All images lie between the first and last calibration shot; coefficients are interpolated."]);
    }
  }
  return { lines, files: state.survey.folder ? surveyTimeline().map((p) => p.file) : [], blocking: lines.some((l) => l[0] === "block"), state: stateDto() };
}

function emit(event: string, payload: unknown) {
  for (const h of listeners.get(event) ?? []) h(payload);
}

export async function mockListen(event: string, handler: (p: unknown) => void): Promise<() => void> {
  const arr = listeners.get(event) ?? [];
  arr.push(handler);
  listeners.set(event, arr);
  return () => listeners.set(event, (listeners.get(event) ?? []).filter((h) => h !== handler));
}

export async function mockPickImages(): Promise<string[]> {
  counter += 1;
  return counter === 1 ? ["cal_start.tif", "cal_end.tif"] : [`cal_${counter}.tif`];
}

export async function mockPickFolder(title: string): Promise<string | null> {
  return title.toLowerCase().includes("output") ? "/demo/flight/calibrated" : "/demo/flight/survey";
}

export async function mockInvoke(cmd: string, args: Record<string, unknown>): Promise<unknown> {
  const a = args as Record<string, never>;
  switch (cmd) {
    case "get_state":
      return stateDto();
    case "add_calibration_images": {
      const paths = a.paths as string[];
      const rejected: string[] = [], duplicates: string[] = [];
      let added = 0;
      const n = Math.max(2, ...state.shots.map((s) => s.targets.length));
      for (const p of paths) {
        if (state.shots.some((s) => s.path === p)) { duplicates.push(p); continue; }
        if (p.endsWith(".jpg")) { rejected.push(`${p}: 3 bands`); continue; }
        const minute = p.includes("end") ? 14 : p.includes("start") ? 0 : 7;
        const shot: MockShot = { path: p, minute, pixels: synthShot(minute), targets: [], fit: null };
        for (let i = 0; i < n; i++) shot.targets.push({ name: `Target ${i + 1}`, color: PALETTE[i], polygon: [], ground_truth: null });
        state.shots.push(shot);
        added++;
      }
      state.shots.sort((x, y) => x.minute - y.minute);
      return { added, rejected, duplicates, state: stateDto() } as AddResult;
    }
    case "remove_calibration_image":
      state.shots.splice(a.index as number, 1);
      return stateDto();
    case "load_image_data": {
      const shot = state.shots[a.index as number];
      const bytes = new Uint8Array(shot.pixels.buffer);
      let bin = "";
      for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
      let lo = Infinity, hi = -Infinity;
      for (const v of shot.pixels) { if (v < lo) lo = v; if (v > hi) hi = v; }
      return { width: W, height: H, data_base64: btoa(bin), lo, hi } as ImagePayload;
    }
    case "set_polygon": {
      const shot = state.shots[a.event as number];
      snapshot();
      const poly = (a.polygon as Point[]).slice();
      if (poly.length === 4) {
        const cx = poly.reduce((s, p) => s + p[0], 0) / poly.length, cy = poly.reduce((s, p) => s + p[1], 0) / poly.length;
        poly.sort((p, q) => Math.atan2(p[1] - cy, p[0] - cx) - Math.atan2(q[1] - cy, q[0] - cx));
      }
      shot.targets[a.target as number].polygon = poly;
      refit(shot);
      return stateDto();
    }
    case "clear_polygon": {
      const shot = state.shots[a.event as number];
      snapshot();
      shot.targets[a.target as number].polygon = [];
      refit(shot);
      return stateDto();
    }
    case "set_ground_truth": {
      const shot = state.shots[a.event as number];
      if (shot.targets[a.target as number].ground_truth !== (a.value as number | null)) snapshot();
      shot.targets[a.target as number].ground_truth = (a.value as number | null);
      refit(shot);
      return stateDto();
    }
    case "add_target": {
      if (!state.shots.length) throw new Error("Load calibration images first.");
      const n = Math.max(...state.shots.map((s) => s.targets.length)) + 1;
      snapshot();
      for (const s of state.shots) while (s.targets.length < n) s.targets.push({ name: `Target ${s.targets.length + 1}`, color: PALETTE[s.targets.length % PALETTE.length], polygon: [], ground_truth: null });
      return stateDto();
    }
    case "remove_target": {
      snapshot();
      for (const s of state.shots) {
        if (s.targets.length <= 1) continue;
        s.targets.splice(a.index as number, 1);
        s.targets.forEach((t, i) => { if (/^Target \d+$/.test(t.name)) t.name = `Target ${i + 1}`; t.color = PALETTE[i]; });
        refit(s);
      }
      return stateDto();
    }
    case "rename_target": {
      const name = (a.name as string).trim().slice(0, 16);
      snapshot();
      for (const s of state.shots) if (s.targets[a.index as number]) s.targets[a.index as number].name = name || `Target ${(a.index as number) + 1}`;
      return stateDto();
    }
    case "copy_polygons": {
      const src = state.shots[a.source as number];
      state.shots.forEach((s, i) => { if (i === (a.source as number)) return; s.targets.forEach((t, k) => { if (!t.polygon.length && src.targets[k]?.polygon.length) t.polygon = src.targets[k].polygon.slice(); }); refit(s); });
      return stateDto();
    }
    case "set_settings": {
      const s = a.settings as Settings;
      if (s.mode !== state.settings.mode && state.shots.length) throw new Error("The sensor type is locked while images are loaded. New project to change it.");
      state.settings = s;
      state.shots.forEach(refit);
      return stateDto();
    }
    case "set_title":
      state.title = (a.title as string).trim();
      return stateDto();
    case "set_survey": {
      const sv = a.survey as Survey;
      if (sv.folder !== state.survey.folder) state.last_output = null;
      state.survey = sv;
      if (!state.survey.output_folder && state.survey.folder) state.survey.output_folder = state.survey.folder + "/calibrated";
      return preflight();
    }
    case "run_preflight":
      return preflight();
    case "start_batch": {
      const pf = preflight();
      if (pf.blocking) throw new Error(pf.lines.filter((l) => l[0] === "block").map((l) => l[1]).join("\n"));
      state.running = true;
      state.cancel = false;
      const files = pf.files;
      let i = 0;
      const step = () => {
        if (state.cancel || i >= files.length) {
          state.running = false;
          state.last_output = state.cancel ? null : state.survey.output_folder;
          emit("batch-done", { ok: i, errors: 0, skipped: 0, total: files.length, cancelled: state.cancel, output: state.survey.output_folder, error: null });
          return;
        }
        emit("batch-progress", { done: i, total: files.length, name: files[i] });
        const m = 1.05 + i * 0.0005, b = -1.4 - i * 0.06;
        emit("batch-row", { input_filename: files[i], output_filename: files[i].replace(".tif", "_calibrated.tif"), timestamp: "2026-06-01T10:0" + (i % 10) + ":00", timestamp_source: "XMP", position_vs_calibration: i < 28 ? "between" : "after", slope_m: m, intercept_b: b, original_mean_celsius: 33.2 + i * 0.01, calibrated_mean_celsius: 33.6 + i * 0.01, u_fit_1sigma: 0.21, status: "ok" });
        emit("batch-log", `${files[i]}: m=${m.toFixed(4)} b=${b.toFixed(3)}`);
        i++;
        setTimeout(step, 60);
      };
      setTimeout(step, 100);
      return null;
    }
    case "cancel_batch":
      state.cancel = true;
      return null;
    case "save_project":
      state.project_path = (a.path as string | null) ?? state.project_path ?? "demo.calib.json";
      return state.project_path;
    case "suggested_project_filename":
      return (state.title.replace(/[^\w.-]+/g, "_") || "calibration_project") + ".calib.json";
    case "open_project": {
      await mockInvoke("new_project", {});
      await mockInvoke("add_calibration_images", { paths: ["cal_start.tif", "cal_end.tif"] });
      for (const s of state.shots) {
        s.targets.push({ name: "Target 3", color: PALETTE[2], polygon: [], ground_truth: null });
        PANELS.forEach(([x, y, t], i) => { s.targets[i].polygon = [[x - 2, y - 2], [x + 42, y - 2], [x + 42, y + 42], [x - 2, y + 42]]; s.targets[i].ground_truth = t + 0.3; });
        refit(s);
      }
      state.title = "demo flight";
      state.project_path = a.path as string;
      return { problems: [], state: stateDto() };
    }
    case "open_demo_flight":
      throw new Error("The demo flight comes with the desktop app; the browser preview has no files to open.");
    case "new_project":
      state = { ...state, title: "untitled project", project_path: null, shots: [], last_output: null, running: false, survey: { folder: "", output_folder: "", filter: "", copy_metadata: true, skip_existing: false }, settings: { ...state.settings, mode: "thermal", emissivity: { enabled: false, target_emissivity: 0.95, scene_emissivity: 0.98, sky_temperature: -40 }, roi_inset_px: 3 } };
      return stateDto();
    case "open_in_file_manager":
      return null;
    case "inspect_state":
      return { images: inspectImages };
    case "inspect_add": {
      let added = 0;
      for (const p of a.paths as string[]) {
        const names = p.endsWith(".tif") ? [p] : Array.from({ length: 6 }, (_, j) => `${p}/survey_${j}_calibrated.tif`);
        for (const n of names) if (!inspectImages.some((im) => im.path === n)) { inspectImages.push({ name: n.split("/").pop()!, path: n }); added++; }
      }
      return { added, rejected: [], inspect: { images: inspectImages } };
    }
    case "inspect_remove":
      inspectImages.splice(a.index as number, 1);
      return { images: inspectImages };
    case "inspect_clear":
      inspectImages.length = 0;
      return { images: inspectImages };
    case "inspect_image_data": {
      const px = synthShot(7 + (a.index as number));
      const bytes = new Uint8Array(px.buffer);
      let bin = "";
      for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
      let lo = Infinity, hi = -Infinity;
      for (const v of px) { if (v < lo) lo = v; if (v > hi) hi = v; }
      return { width: W, height: H, data_base64: btoa(bin), lo, hi };
    }
    case "inspect_polygon_stats": {
      const px = synthShot(7 + (a.index as number));
      const m = insideMask(a.polygon as Point[], 0);
      const vals: number[] = [];
      for (let i = 0; i < m.length; i++) if (m[i]) vals.push(px[i]);
      if (!vals.length) return { mean: null, std: null, min: null, max: null, count: 0 };
      const mean = vals.reduce((x, y) => x + y, 0) / vals.length;
      return { mean, std: Math.sqrt(vals.reduce((x, v) => x + (v - mean) ** 2, 0) / vals.length), min: Math.min(...vals), max: Math.max(...vals), count: vals.length };
    }
    case "undo": {
      const sn = undoStack.pop();
      if (sn) {
        redoStack.push(snap());
        restore(sn);
      }
      return stateDto();
    }
    case "redo": {
      const sn = redoStack.pop();
      if (sn) {
        undoStack.push(snap());
        restore(sn);
      }
      return stateDto();
    }
    case "fit_report_html": {
      const shot = state.shots[a.index as number];
      return `<!doctype html><html><body><h1>Calibration fit report (mock)</h1><p>${shot.path}: m = ${shot.fit?.slope.toFixed(4)}</p></body></html>`;
    }
    case "write_text_file": {
      const blob = new Blob([a.text as string], { type: "text/html" });
      const link = Object.assign(document.createElement("a"), { href: URL.createObjectURL(blob), download: String(a.path).split("/").pop() });
      link.click();
      return null;
    }
    default:
      throw new Error(`mock: unknown command ${cmd}`);
  }
}
