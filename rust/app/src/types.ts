// JSON shapes exchanged with the Rust side (src-tauri/src/lib.rs). Field names are the Rust ones.

export type Mode = "thermal" | "msi";

export interface Emissivity {
  enabled: boolean;
  target_emissivity: number;
  scene_emissivity: number;
  sky_temperature: number;
}

export interface Settings {
  mode: Mode;
  single_point: boolean;
  emissivity: Emissivity;
  roi_inset_px: number;
}

export interface Survey {
  folder: string;
  output_folder: string;
  filter: string;
  copy_metadata: boolean;
  skip_existing: boolean;
}

export interface Fit {
  slope: number;
  intercept: number;
  n: number;
  single_point: boolean;
  r_squared: number | null;
  rmse: number | null;
  resid_std: number | null;
  se_slope: number | null;
  se_intercept: number | null;
  loo_rmse: number | null;
  residuals: number[];
  x_values: number[];
  y_values: number[];
  x_mean: number;
  sxx: number;
  x_min: number;
  x_max: number;
}

export type Point = [number, number];

export interface Target {
  name: string;
  label: string;
  color: string;
  polygon: Point[];
  inner: Point[][];
  mean: number | null;
  std: number | null;
  pixels: number;
  ground_truth: number | null;
  inset_applied: boolean;
  complete: boolean;
  warn_small: boolean;
}

export interface CalEvent {
  index: number;
  path: string;
  file_name: string;
  timestamp: string;
  timestamp_source: string;
  timestamp_tz: string;
  targets: Target[];
  fit: Fit | null;
  fit_warnings: string[];
  range: [number, number] | null;
}

export interface TimelinePoint {
  file: string;
  t: string;
  source: string;
  outside: boolean;
}

export interface AppState {
  tool_name: string;
  version: string;
  title: string;
  project_path: string | null;
  settings: Settings;
  survey: Survey;
  events: CalEvent[];
  survey_timeline: TimelinePoint[];
  last_output: string | null;
  running: boolean;
  can_undo: boolean;
  can_redo: boolean;
}

export interface AddResult {
  added: number;
  rejected: string[];
  duplicates: string[];
  state: AppState;
}

export interface ImagePayload {
  width: number;
  height: number;
  data_base64: string;
  lo: number;
  hi: number;
}

export interface Preflight {
  lines: [string, string][];
  files: string[];
  blocking: boolean;
  state: AppState;
}

export interface BatchDone {
  ok: number;
  errors: number;
  skipped: number;
  total: number;
  cancelled: boolean;
  output: string;
  error: string | null;
}

export type Row = Record<string, string | number | boolean | null>;

export interface InspectImage {
  name: string;
  path: string;
}

export interface InspectDto {
  images: InspectImage[];
}

export interface InspectAddResult {
  added: number;
  rejected: string[];
  inspect: InspectDto;
}

export interface PolygonStats {
  mean: number | null;
  std: number | null;
  min: number | null;
  max: number | null;
  count: number;
}

/** Prediction uncertainty of a fit at sensor value x (1 sigma); null below three targets. */
export function predictUncertainty(f: Fit, x: number): number | null {
  if (f.single_point || f.n < 3 || f.sxx <= 0 || f.resid_std === null) return null;
  return f.resid_std * Math.sqrt(1 + 1 / f.n + ((x - f.x_mean) ** 2) / f.sxx);
}

/** Float32 pixels decoded from the base64 the Rust side sends. */
export function decodePixels(p: ImagePayload): Float32Array {
  const bin = atob(p.data_base64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return new Float32Array(bytes.buffer, 0, p.width * p.height);
}
