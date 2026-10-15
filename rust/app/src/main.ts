// The application: state from the Rust side, the three-step rail with its guidance cards, the image
// view, the fit panel, the timeline and the process view. Rendering is "replace the HTML of a
// region from the state"; the canvas viewer keeps its own state (zoom, pan, pixels).

import { invoke, listen, pickCsvToSave, pickFolder, pickImages, pickProjectToOpen, pickProjectToSave, pickReportToSave } from "./bridge";
import { plotSvg, renderFitPanel } from "./fitpanel";
import { renderTimeline } from "./timeline";
import { decodePixels, type AddResult, type AppState, type BatchDone, type CalEvent, type ImagePayload, type InspectAddResult, type InspectDto, type Point, type PolygonStats, type Preflight, type Row, type Target } from "./types";
import { Viewer, type VizMode } from "./viewer";

type Step = 1 | 2 | 3;
const STEP_TITLES: Record<Step, string> = { 1: "Images", 2: "Targets", 3: "Process" };

const $ = <T extends HTMLElement>(sel: string): T => document.querySelector(sel) as T;

let state: AppState | null = null;
let current = 0; // selected calibration shot
let activeStep: Step = 1;
let settingsOpen = false;
let drawing: { target: number; points: Point[]; redo: boolean; inspect?: boolean } | null = null;
// the Inspect section: any single-band images, viewed and measured, not part of the project
let inspecting = false;
let inspect: InspectDto = { images: [] };
let inspectCurrent = 0;
const inspectPolys = new Map<string, { points: Point[]; stats: PolygonStats | null }[]>();
const PALETTE = ["#E63946", "#2A9D8F", "#E9C46A", "#4C7BD9", "#F4A261", "#9B5DE5", "#00F5D4", "#F15BB5", "#00BBF9", "#B8E986"];
const CAL_PLACEHOLDER = "No calibration image yet.\n\nChoose the sensor type and add the images that show your reference targets (1 · Images, left).";
let preflight: Preflight | null = null;
let rows: Row[] = [];
let pending: string[] = [];
let progressText = "Ready";
let progressPct = 0;
let renaming: number | null = null;
const imageCache = new Map<string, ImagePayload>();

const viewer = new Viewer($("#viewer"));
viewer.placeholder = CAL_PLACEHOLDER;

// ----------------------------------------------------------------------------- helpers

function status(text: string) {
  $("#status-text").textContent = text;
}

function esc(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/"/g, "&quot;");
}

function unit(): string {
  return state?.settings.mode === "thermal" ? "°C" : "ρ";
}

function currentEvent(): CalEvent | null {
  return state?.events[current] ?? null;
}

function visibleTargets(ev: CalEvent) {
  return state?.settings.mode === "msi" && state.settings.single_point ? ev.targets.slice(0, 1) : ev.targets;
}

function fmtTime(iso: string): string {
  return iso.slice(11, 16);
}

function alert(title: string, text: string, buttons: { label: string; accent?: boolean; value: boolean }[] = [{ label: "OK", accent: true, value: true }]): Promise<boolean> {
  return new Promise((resolve) => {
    const modal = $("#modal");
    modal.innerHTML = `<div class="dialog"><h3>${esc(title)}</h3><pre>${esc(text)}</pre><div class="btns">${buttons.map((b, i) => `<button class="btn ${b.accent ? "accent" : ""}" data-i="${i}">${esc(b.label)}</button>`).join("")}</div></div>`;
    modal.hidden = false;
    modal.querySelectorAll("button").forEach((b) =>
      b.addEventListener("click", () => {
        modal.hidden = true;
        resolve(buttons[Number(b.dataset.i)].value);
      }),
    );
  });
}

/** A native dialog that fails must say so instead of doing nothing. */
async function pick<T>(what: string, fn: () => Promise<T>, fallback: T): Promise<T> {
  try {
    return await fn();
  } catch (e) {
    await alert("Problem", `The ${what} dialog could not be opened: ${String(e instanceof Error ? e.message : e)}`);
    return fallback;
  }
}

async function call<T>(cmd: string, args: Record<string, unknown> = {}): Promise<T | null> {
  try {
    return await invoke<T>(cmd, args);
  } catch (e) {
    await alert("Problem", String(e instanceof Error ? e.message : e));
    return null;
  }
}

function applyState(s: AppState) {
  state = s;
  if (current >= s.events.length) current = Math.max(0, s.events.length - 1);
  $("#mode-label").textContent = s.settings.mode === "thermal" ? "Thermal" : "Multispectral";
  const titleInput = $<HTMLInputElement>("#title");
  if (document.activeElement !== titleInput) titleInput.value = s.title || "untitled project";
  document.title = (s.title && s.title !== "untitled project" ? s.title + " – " : "") + "Thermal Calibration Tool";
  $("#saved").textContent = s.project_path ? s.project_path.split(/[\\/]/).pop() ?? "" : "";
  viewer.unit = s.settings.mode === "thermal" ? "°C" : "DN";
  $<HTMLButtonElement>("#btn-undo").disabled = !s.can_undo;
  $<HTMLButtonElement>("#btn-redo").disabled = !s.can_redo;
  renderAll();
}

// ----------------------------------------------------------------------------- guidance (one next action per step)

interface Guide {
  head: string;
  detail: string;
  highlight?: number | null;
  action?: { label: string; run: () => void } | null;
}

function openStepAction(n: Step): Guide["action"] {
  return { label: `Open ${n} · ${STEP_TITLES[n]} ›`, run: () => void showStep(n) };
}

function guideImages(): Guide {
  const n = state?.events.length ?? 0;
  const files = state?.settings.mode === "thermal" ? "Single-band temperature TIFFs; convert DJI R-JPEGs first (see README)." : "Single-band images of the band you calibrate, one band at a time.";
  if (n === 0) return { head: "Choose the sensor type, then press Add images… and pick the shots that show your reference targets.", detail: files + " Usually one shot before and one after the survey." };
  if (n === 1) return { head: "1 shot loaded. Add the other calibration shots of this flight (e.g. the one taken after the survey), then open 2 · Targets.", detail: "One shot is enough for a flight; a second one at the end also corrects drift. Shots are sorted by capture time.", action: openStepAction(2) };
  return { head: `${n} shots loaded. Add any further calibration shots of this flight, then open 2 · Targets.`, detail: "Shots are sorted by capture time; the coefficients are interpolated between them during the flight.", action: openStepAction(2) };
}

function guideTargets(): Guide {
  if (!state || !state.events.length) return { head: "Add calibration images in 1 · Images.", detail: "Then draw the targets here.", action: openStepAction(1) };
  const ev = state.events[current];
  const n = state.events.length;
  const shot = n > 1 ? `shot ${current + 1} of ${n}` : "the shot";
  const thermal = state.settings.mode === "thermal";
  const value = thermal ? "the thermometer reading" : "the panel's known reflectance (0–1)";
  const targets = visibleTargets(ev);
  if (drawing) {
    const t = targets[drawing.target];
    const k = drawing.points.length;
    const label = t?.label ?? "the target";
    if (k < 3) return { head: `Click corner ${k + 1} of ${label} (at least 3).`, detail: "Any order for a rectangle; a corner clicked again is removed. The loupe top-left magnifies the pixels under the cursor. Esc cancels.", highlight: drawing.target };
    return { head: `${label}: ${k} corners. Click the ringed first corner (or press Enter) to close it, or add more corners.`, detail: "A corner clicked again is removed; Ctrl+Z removes the last one. Esc cancels.", highlight: drawing.target };
  }
  for (let j = 0; j < targets.length; j++) {
    const t = targets[j];
    if (t.polygon.length < 3) return { head: `Draw ${t.label} on ${shot}: click the corners of the panel, then the first corner again to close.`, detail: "Drawing starts by itself; Draw restarts it. Zoom in with the mouse wheel first so the panel is large; the dashed outline is the region used.", highlight: j };
    if (t.mean === null || !isFinite(t.mean)) return { head: `${t.label} lies outside the image or over empty pixels: press Draw and redraw it.`, detail: "", highlight: j };
    if (t.ground_truth === null) return { head: `Type ${value} for ${t.label} into the ${unit()} field.`, detail: thermal ? "Comma or dot. Enter jumps to the next field." : "e.g. 50 % → 0.5. Enter jumps to the next field.", highlight: j };
  }
  const need = state.settings.mode === "msi" && state.settings.single_point ? 1 : 2;
  const k = targets.length;
  const done = `${k} target${k !== 1 ? "s" : ""}`;
  if (k < need) return { head: "Press + Add target: at least 2 targets are needed for a line.", detail: "" };
  const todo = state.events.findIndex((e, i) => i !== current && !visibleTargets(e).every((t) => t.complete));
  if (todo >= 0) return { head: `Shot ${current + 1} is done (${done}). Press › and draw the same targets on shot ${todo + 1}.`, detail: "Each shot needs its own polygons and readings: the panels and their temperatures change between shots. More panels in the image? + Add target adds a target to every shot. Copy to other shots helps when the camera did not move." };
  if (n === 1) return { head: `${done} done on the shot. More panels in the image? Press + Add target. If not, check the fit on the right, then open 3 · Process.`, detail: "A single shot is fine: its coefficients are applied to the whole flight. Add a second (end-of-flight) shot in 1 · Images to correct drift.", action: openStepAction(3) };
  return { head: `All ${n} shots done, ${done} each. More panels in the images? Press + Add target. If not, check the fits on the right, then open 3 · Process.`, detail: "Switch shots with ‹ › to compare their fits; the timeline below the image shows the drift.", action: openStepAction(3) };
}

function guideProcess(): Guide {
  if (!state || !state.events.length) return { head: "Add the calibration images first.", detail: "", action: openStepAction(1) };
  if (state.events.some((e) => !e.fit)) return { head: "Finish the targets first: every calibration shot needs a complete fit.", detail: "", action: openStepAction(2) };
  if (state.running) return { head: "Calibrating… the results table fills up as the images are written.", detail: "Cancel stops after the current image." };
  if (!state.survey.folder) return { head: "Choose the survey folder: press Browse. Every image in it is calibrated.", detail: "The outputs go to <survey folder>/calibrated unless you pick another output folder. The filename filter narrows the set down (e.g. _T_deg).", action: { label: "Browse…", run: browseSurvey } };
  if (state.last_output) return { head: `Done. The calibrated images, the log and the project file are in ${state.last_output.split(/[\\/]/).pop()}.`, detail: "Inspect opens the calibrated images for a look: hover for temperatures, draw polygons for averages. Open output folder is in the results header. Choose another survey folder to process the next flight.", action: { label: "Inspect the results ›", run: () => void inspectResults() } };
  if (!preflight) return { head: "Reading the capture times of the survey images…", detail: "The pre-flight check runs when that is done." };
  if (preflight.blocking) return { head: "Fix the problems listed in the pre-flight check (centre), then press Run.", detail: "" };
  const n = preflight.files.length;
  return { head: `${n} image${n !== 1 ? "s" : ""} ready. Press Run.`, detail: "The timeline below shows where the survey images lie relative to the calibration shots; orange ticks are outside the window and get the nearest shot's coefficients unchanged.", action: { label: "Run", run: startBatch } };
}

function guideHtml(g: Guide, id: string): string {
  return `<div class="guide"><div class="kicker">NEXT</div><div class="head">${esc(g.head)}</div>${g.detail ? `<div class="detail">${esc(g.detail)}</div>` : ""}${g.action ? `<button class="btn accent small" id="${id}">${esc(g.action.label)}</button>` : ""}</div>`;
}

// ----------------------------------------------------------------------------- rail

function stepDone(n: Step): [boolean, string] {
  if (!state) return [false, ""];
  const evs = state.events;
  if (n === 1) return evs.length ? [true, `${evs.length} calibration shot${evs.length !== 1 ? "s" : ""}, ${evs.slice(0, 3).map((e) => fmtTime(e.timestamp)).join(", ")}${evs.length > 3 ? " …" : ""}`] : [false, "none yet"];
  if (n === 2) {
    if (!evs.length) return [false, "waiting for images"];
    const need = state.settings.mode === "msi" && state.settings.single_point ? 1 : 2;
    const complete = evs.map((e) => visibleTargets(e).filter((t) => t.complete).length);
    const fitted = evs.filter((e) => e.fit);
    const rm = fitted.map((e) => e.fit!.rmse).filter((v): v is number => v !== null);
    let txt = `${complete.reduce((a, b) => a + b, 0)} complete target(s) on ${evs.length} shot(s)`;
    if (fitted.length) txt += ` · ${fitted.length}/${evs.length} fitted` + (rm.length ? `, RMSE ${Math.min(...rm).toFixed(2)} to ${Math.max(...rm).toFixed(2)} ${unit()}` : "");
    return [complete.every((c) => c >= need), txt];
  }
  if (state.last_output) return [true, `written to ${state.last_output.split(/[\\/]/).pop()}`];
  return [false, state.survey_timeline.length ? `${state.survey_timeline.length} survey images, not processed yet` : "choose the survey folder"];
}

function renderRail() {
  const rail = $("#rail");
  const parts: string[] = [];
  for (const n of [1, 2, 3] as Step[]) {
    const [done, summary] = stepDone(n);
    const active = n === activeStep;
    parts.push(`<div class="step ${active ? "active" : ""} ${done ? "done" : ""}" data-step="${n}"><div class="step-header"><div class="badge">${done ? "✓" : n}</div><div><div class="step-title">${n} · ${STEP_TITLES[n]}</div><div class="step-summary">${esc(summary)}</div></div><span class="chevron">${active && !settingsOpen && !inspecting ? "⌄" : "›"}</span></div>${active && !settingsOpen && !inspecting ? `<div class="step-body">${n === 1 ? bodyImages() : n === 2 ? bodyTargets() : bodyProcess()}</div>` : ""}</div>`);
  }
  const insp = inspect.images.length ? `${inspect.images.length} image${inspect.images.length !== 1 ? "s" : ""} open` : "view and measure any thermal image";
  parts.push(`<div class="step ${inspecting ? "active" : ""}" data-step="inspect"><div class="step-header"><div class="badge">◎</div><div><div class="step-title">Inspect</div><div class="step-summary">${esc(insp)}</div></div><span class="chevron">${inspecting && !settingsOpen ? "⌄" : "›"}</span></div>${inspecting && !settingsOpen ? `<div class="step-body">${bodyInspect()}</div>` : ""}</div>`);
  const s = state?.settings;
  const summary = s ? `ROI inset ${s.roi_inset_px} px · emissivity ${s.emissivity.enabled ? "on" : "off"}${state?.survey.skip_existing ? " · skip existing outputs" : ""}${state && !state.survey.copy_metadata ? " · no metadata copy" : ""}` : "";
  parts.push(`<div class="step ${settingsOpen ? "active" : ""}" data-step="settings"><div class="step-header"><div class="badge">⚙</div><div><div class="step-title">Settings</div><div class="step-summary">${esc(summary)}</div></div><span class="chevron">${settingsOpen ? "⌄" : "›"}</span></div>${settingsOpen ? `<div class="step-body">${bodySettings()}</div>` : ""}</div>`);
  parts.push(`<div class="hint" style="padding:10px 14px">Single shot? Fine: one set of coefficients is applied to all images. Two shots also correct drift during the flight.</div>`);
  rail.innerHTML = parts.join("");
  rail.querySelectorAll<HTMLElement>(".step-header").forEach((h) =>
    h.addEventListener("click", () => {
      const step = h.parentElement!.dataset.step!;
      if (step === "settings") {
        settingsOpen = !settingsOpen;
        renderRail();
      } else if (step === "inspect") void toggleInspect();
      else void showStep(Number(step) as Step);
    }),
  );
  wireRail();
}

function bodyImages(): string {
  const locked = (state?.events.length ?? 0) > 0;
  const mode = state?.settings.mode ?? "thermal";
  const g = guideImages();
  return `${guideHtml(g, "guide-1")}
<div class="row"><span class="label7">Sensor</span><div class="seg"><button class="seg-btn ${mode === "thermal" ? "selected" : ""}" data-mode="thermal" ${locked ? "disabled" : ""}>Thermal</button><button class="seg-btn ${mode === "msi" ? "selected" : ""}" data-mode="msi" ${locked ? "disabled" : ""}>Multispectral</button></div><span class="help" data-tip="${locked ? "Locked while calibration images are loaded: the sensor type decides how images and reference values are read. New project to start over with the other type." : "Thermal: temperature TIFFs and thermometer readings in °C. Multispectral: one band at a time, reflectance panels with known ρ."}">?</span></div>
${locked ? `<div class="lock-note">Locked while images are loaded. New project to change the sensor type.</div>` : ""}
${mode === "msi" ? `<div class="row"><span class="label7">Method</span><div class="seg"><button class="seg-btn ${state?.settings.single_point ? "selected" : ""}" data-single="1">Single point</button><button class="seg-btn ${state?.settings.single_point ? "" : "selected"}" data-single="0">Multi point</button></div><span class="help" data-tip="Single point: one panel of known reflectance, the line goes through the origin. Multi point: two or more panels, slope and offset are fitted.">?</span></div>` : ""}
<div class="row" style="margin-top:8px"><button class="btn accent" id="btn-add-images">Add images…</button><button class="btn" id="btn-remove-image" ${state?.events.length ? "" : "disabled"}>Remove</button><span class="help" data-tip="Images that show the reference targets. One image is enough. Two or more, e.g. before and after the flight, also correct drift: coefficients are interpolated in time between them. Images must be single-band temperature (or DN) files, not colour previews.">?</span></div>
<div class="card listbox" id="calib-list">${(state?.events ?? []).map((e, i) => { const n = state!.events.length; const label = n === 1 ? "Only" : i === 0 ? "Start" : i === n - 1 ? "End" : `Mid${i}`; return `<div class="${i === current ? "selected" : ""}" data-i="${i}">[${label.padEnd(5)}] ${e.timestamp.slice(11, 19)} ${e.timestamp_source.padEnd(9)} ${esc(e.file_name)}</div>`; }).join("")}</div>
<div class="hint">${state?.events.length ? `${current + 1}/${state.events.length} ${esc(state.events[current].file_name)}` : "No image selected"}</div>`;
}

function bodyTargets(): string {
  const g = guideTargets();
  const ev = currentEvent();
  const n = state?.events.length ?? 0;
  const label = ev ? (n === 1 ? "only" : current === 0 ? "start" : current === n - 1 ? "end" : `mid ${current}`) : "";
  const rowsHtml = ev
    ? visibleTargets(ev)
        .map((t, i) => {
          const hl = g.highlight === i;
          const stats = t.polygon.length < 3 ? "no polygon yet" : t.mean === null || !isFinite(t.mean) ? "⚠ outside the image or no valid pixels" : `mean ${t.mean.toFixed(2)} ${unit()} · σ ${(t.std ?? 0).toFixed(2)} · n = ${t.pixels}${t.warn_small ? " ⚠ small" : ""}`;
          const isRedrawing = drawing && drawing.target === i;
          const gt = t.ground_truth === null ? "" : String(t.ground_truth);
          return `<div class="trow ${hl ? "highlight" : ""}" data-t="${i}"><div class="line1"><span class="swatch" style="background:${t.color}"></span>${renaming === i ? `<input class="gt" style="width:90px;text-align:left" id="rename-input" value="${esc(t.name.startsWith("Target ") ? "" : t.name)}" placeholder="name" maxlength="16">` : `<span class="tid" data-tip="Double-click to name this target (black, grey, white…)">T${i + 1}</span>${t.label.includes(" ") ? `<span class="alias">${esc(t.label.split(" ").slice(1).join(" "))}</span>` : ""}`}<button class="btn small" data-draw="${i}">Draw</button><button class="btn small" data-clear="${i}" ${t.polygon.length < 3 ? "disabled" : ""}>Clear</button><input class="gt ${t.ground_truth === null && gt !== "" ? "bad" : ""}" data-gt="${i}" value="${esc(gt)}" style="background:${lighten(t.color)}"><button class="btn small xbtn" data-remove="${i}" ${visibleTargets(ev).length <= 1 ? "disabled" : ""} data-tip="Remove this target from every shot">×</button></div><div class="stats ${t.warn_small ? "warn" : ""}">${isRedrawing ? "redrawing … (Esc keeps the old polygon)" : esc(stats)}</div></div>`;
        })
        .join("")
    : "";
  return `${guideHtml(g, "guide-2")}
<div class="nav"><button class="btn small" id="btn-prev">‹</button><span class="shot-label">${ev ? `shot ${current + 1} of ${n} · ${label}` : "no shot"}</span><button class="btn small" id="btn-next">›</button><span class="spacer"></span><span class="help" data-tip="Zoom in (wheel), press Draw and click the 4 corners of a panel. The dashed outline is the eroded region actually used. Type the reference reading; Enter jumps to the next field.">?</span></div>
<div class="card"><table class="targets"><tr><th style="width:30px">ID</th><th>Polygon</th><th style="text-align:right">truth ${unit()}</th><th></th></tr></table><div id="target-rows">${rowsHtml}</div></div>
<div class="row" style="margin-top:6px"><button class="btn" id="btn-add-target">+ Add target</button><button class="btn" id="btn-copy" ${n > 1 ? "" : "disabled"}>Copy to other shots</button></div>
<div class="hint">Wheel: zoom · right-drag: pan · Esc: cancel drawing · comma or dot accepted · ROI edge inset: see Settings</div>`;
}

function lighten(hex: string): string {
  const n = parseInt(hex.slice(1), 16);
  const r = (n >> 16) & 255, g = (n >> 8) & 255, b = n & 255;
  const f = (c: number) => Math.round(c + (255 - c) * 0.8);
  return `rgb(${f(r)},${f(g)},${f(b)})`;
}

function bodyProcess(): string {
  const g = guideProcess();
  const sv = state?.survey;
  return `${guideHtml(g, "guide-3")}
<div class="field-label">Survey folder (every image in it; narrow down with the filter)</div><div class="field"><input id="survey-folder" value="${esc(sv?.folder ?? "")}"><button class="btn small" id="btn-browse-survey">Browse</button></div>
<div class="field-label">Output folder</div><div class="field"><input id="output-folder" value="${esc(sv?.output_folder ?? "")}"><button class="btn small" id="btn-browse-output">Browse</button></div>
<div class="field-label">Filename filter</div><div class="field"><input id="name-filter" class="mono" style="max-width:140px" value="${esc(sv?.filter ?? "")}"><span class="help" data-tip="Only files whose name contains this text are processed, e.g. '_T_deg' to skip the colour previews next to the temperature TIFFs, or '_3' for one multispectral band.">?</span></div>
<div class="hint">Choosing a folder runs the pre-flight check and lists the images; then press Run.</div>`;
}

function currentInspectPolys(): { points: Point[]; stats: PolygonStats | null }[] {
  const im = inspect.images[inspectCurrent];
  if (!im) return [];
  let list = inspectPolys.get(im.path);
  if (!list) {
    list = [];
    inspectPolys.set(im.path, list);
  }
  return list;
}

function inspectTargets(): Target[] {
  return currentInspectPolys().map((p, j) => ({ name: `P${j + 1}`, label: `P${j + 1}`, color: PALETTE[j % PALETTE.length], polygon: p.points, inner: [], mean: p.stats?.mean ?? null, std: p.stats?.std ?? null, pixels: p.stats?.count ?? 0, ground_truth: null, inset_applied: true, complete: false, warn_small: false }));
}

function bodyInspect(): string {
  const imgs = inspect.images;
  const g: Guide = !imgs.length
    ? { head: "Open images… (for example the calibrated outputs) and move the mouse over the image to read values.", detail: "Any single-band image works; a folder opens every image in it. Nothing here is stored in the project." }
    : { head: "Hover for the value under the cursor. Press Draw polygon and click the corners for the mean of an area.", detail: "Polygons belong to the image they are drawn on; the table on the right lists them. Save table… writes them as CSV." };
  return `${guideHtml(g, "guide-inspect")}
<div class="row" style="margin-top:8px"><button class="btn accent" id="btn-inspect-open">Open images…</button><button class="btn" id="btn-inspect-folder">Open folder…</button><button class="btn" id="btn-inspect-clear" ${imgs.length ? "" : "disabled"}>Clear</button></div>
<div class="card listbox" id="inspect-list">${imgs.map((im, i) => `<div class="${i === inspectCurrent ? "selected" : ""}" data-i="${i}">${esc(im.name)}</div>`).join("")}</div>
<div class="nav" style="margin-top:6px"><button class="btn small" id="btn-inspect-prev" ${imgs.length > 1 ? "" : "disabled"}>‹</button><span class="shot-label">${imgs.length ? `${inspectCurrent + 1} of ${imgs.length}` : "no image"}</span><button class="btn small" id="btn-inspect-next" ${imgs.length > 1 ? "" : "disabled"}>›</button><span class="spacer"></span><button class="btn small" id="btn-inspect-remove" ${imgs.length ? "" : "disabled"}>Remove</button></div>
<div class="row" style="margin-top:8px"><button class="btn" id="btn-inspect-draw" ${imgs.length ? "" : "disabled"}>Draw polygon</button><button class="btn" id="btn-inspect-save" ${currentInspectPolys().length ? "" : "disabled"}>Save table…</button></div>
<div class="hint">Wheel: zoom · right-drag: pan · a corner clicked again is removed · first corner or Enter closes · Esc cancels</div>`;
}

function bodySettings(): string {
  const s = state?.settings;
  const sv = state?.survey;
  if (!s || !sv) return "";
  const em = s.emissivity;
  return `<div class="hint" style="margin:2px 0 6px">Saved in the project file; applied to every shot and every survey image.</div>
<div class="row"><span class="small">ROI edge inset (px)</span><input class="num" id="inset" type="number" min="0" max="20" value="${s.roi_inset_px}"><span class="help" data-tip="Pixels removed from the polygon edge before the mean is taken. Thermal sensors blur edges over 2 to 3 px, so edge pixels are mixed with the background. 3 px is a good default; the dashed outline on the image shows the region actually used.">?</span></div>
<div class="chk" style="margin-top:10px"><input type="checkbox" id="em-enabled" ${em.enabled ? "checked" : ""}><label for="em-enabled">Emissivity correction (advanced)</label><span class="help" data-tip="Usually OFF. The empirical line already absorbs the panels' emissivity, and the DJI SDK applies its own emissivity setting (0.95 by default) when converting R-JPEGs. When ON the reference readings are converted to apparent temperature with the panel emissivity before the fit, m and b are applied to raw pixels, and the result is converted to surface temperature with the scene emissivity. T_app⁴ = ε·T_obj⁴ + (1−ε)·T_sky⁴">?</span></div>
<div class="row"><span class="small" style="width:190px">Panel emissivity ε (0.5 to 1)</span><input class="num" id="em-target" value="${em.target_emissivity}"></div>
<div class="row"><span class="small" style="width:190px">Scene emissivity ε (0.5 to 1)</span><input class="num" id="em-scene" value="${em.scene_emissivity}"></div>
<div class="row"><span class="small" style="width:190px">Sky temperature T_sky (°C)</span><input class="num" id="em-sky" value="${em.sky_temperature}"></div>
<div class="chk" style="margin-top:10px"><input type="checkbox" id="copy-meta" ${sv.copy_metadata ? "checked" : ""}><label for="copy-meta">Copy EXIF/XMP/GPS to outputs</label></div>
<div class="chk"><input type="checkbox" id="skip-existing" ${sv.skip_existing ? "checked" : ""}><label for="skip-existing">Skip images whose output already exists</label></div>`;
}

function wireRail() {
  const on = (sel: string, ev: string, fn: (el: HTMLElement, e: Event) => void) => document.querySelectorAll<HTMLElement>(sel).forEach((el) => el.addEventListener(ev, (e) => fn(el, e)));
  for (const [id, g] of [["#guide-1", guideImages], ["#guide-2", guideTargets], ["#guide-3", guideProcess]] as const) {
    const el = document.querySelector<HTMLElement>(id);
    if (el) el.addEventListener("click", () => g().action?.run());
  }
  on("[data-mode]", "click", (el) => setSettings({ mode: el.dataset.mode as "thermal" | "msi" }));
  on("[data-single]", "click", (el) => setSettings({ single_point: el.dataset.single === "1" }));
  on("#btn-add-images", "click", () => void addImages());
  on("#btn-remove-image", "click", () => void removeImage());
  on("#calib-list div", "click", (el) => selectShot(Number(el.dataset.i)));
  on("#btn-prev", "click", () => stepShot(-1));
  on("#btn-next", "click", () => stepShot(1));
  on("[data-draw]", "click", (el) => startDrawing(Number(el.dataset.draw)));
  on("[data-clear]", "click", (el) => void update("clear_polygon", { event: current, target: Number(el.dataset.clear) }));
  on("[data-remove]", "click", (el) => void removeTarget(Number(el.dataset.remove)));
  on(".tid", "dblclick", (el) => { renaming = Number(el.closest<HTMLElement>(".trow")!.dataset.t); renderRail(); $<HTMLInputElement>("#rename-input")?.focus(); });
  on(".alias", "dblclick", (el) => { renaming = Number(el.closest<HTMLElement>(".trow")!.dataset.t); renderRail(); $<HTMLInputElement>("#rename-input")?.focus(); });
  on("#rename-input", "keydown", (el, e) => { const k = (e as KeyboardEvent).key; if (k === "Enter") { void finishRename((el as HTMLInputElement).value); } else if (k === "Escape") { renaming = null; renderRail(); } });
  on("#rename-input", "blur", (el) => void finishRename((el as HTMLInputElement).value));
  on("[data-gt]", "change", (el) => void commitReading(Number(el.dataset.gt), (el as HTMLInputElement).value, false));
  on("[data-gt]", "keydown", (el, e) => {
    if ((e as KeyboardEvent).key === "Enter") void commitReading(Number(el.dataset.gt), (el as HTMLInputElement).value, true);
  });
  on("#btn-add-target", "click", () => void addTargetAndDraw());
  on("#btn-inspect-open", "click", () => void inspectAdd("files"));
  on("#btn-inspect-folder", "click", () => void inspectAdd("folder"));
  on("#btn-inspect-clear", "click", () => void inspectClear());
  on("#inspect-list div", "click", (el) => void inspectSelect(Number(el.dataset.i)));
  on("#btn-inspect-prev", "click", () => void inspectSelect((inspectCurrent - 1 + inspect.images.length) % inspect.images.length));
  on("#btn-inspect-next", "click", () => void inspectSelect((inspectCurrent + 1) % inspect.images.length));
  on("#btn-inspect-remove", "click", () => void inspectRemoveImage());
  on("#btn-inspect-draw", "click", () => startInspectDrawing());
  on("#btn-inspect-save", "click", () => void inspectSaveCsv());
  on("#btn-copy", "click", () => void update("copy_polygons", { source: current }));
  on("#btn-browse-survey", "click", () => void browseSurvey());
  on("#btn-browse-output", "click", () => void browseOutput());
  on("#survey-folder", "change", () => void applySurvey());
  on("#output-folder", "change", () => void applySurvey());
  on("#name-filter", "change", () => void applySurvey());
  on("#inset", "change", (el) => setSettings({ roi_inset_px: Math.max(0, Math.min(20, Number((el as HTMLInputElement).value) || 0)) }));
  on("#em-enabled", "change", (el) => setSettings({ emissivity: { ...state!.settings.emissivity, enabled: (el as HTMLInputElement).checked } }));
  for (const [id, key] of [["#em-target", "target_emissivity"], ["#em-scene", "scene_emissivity"], ["#em-sky", "sky_temperature"]] as const)
    on(id, "change", (el) => { const v = parseFloat((el as HTMLInputElement).value.replace(",", ".")); if (isFinite(v)) setSettings({ emissivity: { ...state!.settings.emissivity, [key]: v } }); });
  on("#copy-meta", "change", (el) => void applySurvey({ copy_metadata: (el as HTMLInputElement).checked }));
  on("#skip-existing", "change", (el) => void applySurvey({ skip_existing: (el as HTMLInputElement).checked }));
}

// ----------------------------------------------------------------------------- actions

async function update(cmd: string, args: Record<string, unknown> = {}) {
  const s = await call<AppState>(cmd, args);
  if (s) applyState(s);
}

function setSettings(patch: Partial<AppState["settings"]>) {
  if (!state) return;
  void update("set_settings", { settings: { ...state.settings, ...patch } });
}

async function addImages() {
  const paths = await pick("file", pickImages, [] as string[]);
  if (!paths.length) return;
  const r = await call<AddResult>("add_calibration_images", { paths });
  if (!r) return;
  applyState(r.state);
  const notes: string[] = [];
  if (r.duplicates.length) notes.push(`Already in the list, not added a second time (${r.duplicates.length}):\n${r.duplicates.join("\n")}`);
  if (r.rejected.length) notes.push(`These files cannot serve as calibration images and were skipped. Colour R-JPEG previews must be converted to temperature TIFFs first (see README):\n${r.rejected.join("\n")}`);
  if (notes.length) await alert("Images skipped", notes.join("\n\n"));
  if (r.added) {
    current = 0;
    await selectShot(0);
    status(`${state!.events.length} calibration image(s) loaded. Add every calibration shot of the flight, then open 2 · Targets.`);
  } else if (r.duplicates.length && !r.rejected.length) status(`Nothing added: ${r.duplicates.length} image(s) were loaded already.`);
}

async function removeImage() {
  if (!state?.events.length) return;
  const name = state.events[current].file_name;
  await update("remove_calibration_image", { index: current });
  imageCache.delete(name);
  await selectShot(Math.min(current, (state?.events.length ?? 1) - 1));
  status(state?.events.length ? `Removed ${name}.` : `Removed ${name}. No calibration images left.`);
}

async function selectShot(i: number) {
  cancelDrawing(true);
  current = Math.max(0, i);
  const ev = currentEvent();
  if (!ev) {
    viewer.setImage(null);
    renderAll();
    return;
  }
  let img = imageCache.get(ev.path);
  if (!img) {
    img = (await call<ImagePayload>("load_image_data", { index: current })) ?? undefined;
    if (!img) return;
    imageCache.set(ev.path, img);
  }
  viewer.setImage({ width: img.width, height: img.height, data: decodePixels(img), lo: img.lo, hi: img.hi });
  renderAll();
  status(`Shot ${current + 1} of ${state!.events.length} selected.`);
}

function stepShot(delta: number) {
  if (!state?.events.length) return;
  void selectShot((current + delta + state.events.length) % state.events.length);
}

function startDrawing(target: number) {
  const ev = currentEvent();
  if (!ev || target >= visibleTargets(ev).length) return;
  const t = ev.targets[target];
  drawing = { target, points: [], redo: t.polygon.length >= 3 };
  viewer.drawing = drawing.points;
  viewer.activeTarget = target;
  (document.activeElement as HTMLElement | null)?.blur(); // Enter must close the polygon, not press a button
  status(`Drawing ${t.label}: click the corners of the panel, then the first corner again (or Enter) to close${drawing.redo ? "; Esc brings the old polygon back" : "; Esc cancels"}.`);
  renderAll();
}

/** The polygon is closed (first corner clicked again, or Enter): measure it and move on to the reading. */
function finishDrawing() {
  if (!drawing || drawing.points.length < 3) return;
  const { target, points, inspect: forInspect } = drawing;
  drawing = null;
  viewer.drawing = null;
  viewer.activeTarget = null;
  if (forInspect) {
    const list = currentInspectPolys();
    const entry = { points, stats: null as PolygonStats | null };
    list.splice(target, 0, entry);
    renderAll();
    void call<PolygonStats>("inspect_polygon_stats", { index: inspectCurrent, polygon: points }).then((st) => {
      entry.stats = st;
      renderAll();
      status(st && st.mean !== null ? `P${list.indexOf(entry) + 1}: mean ${st.mean.toFixed(2)} ${unit()} over ${st.count} px.` : "No valid pixels in that polygon.");
    });
    return;
  }
  void update("set_polygon", { event: current, target, polygon: points }).then(() => {
    const t = currentEvent()?.targets[target];
    status(t ? `${t.label}: mean ${t.mean?.toFixed(2)} ${unit()} over ${t.pixels} px.` : "Polygon set.");
    if (t && t.ground_truth === null) {
      const gt = document.querySelector<HTMLInputElement>(`[data-gt="${target}"]`);
      gt?.focus();
      gt?.select();
    }
  });
}

/** On entering step 2: the first target without a polygon is ready to draw, no click needed. */
function nextUndrawnTarget(): number {
  const ev = currentEvent();
  return ev ? visibleTargets(ev).findIndex((t) => t.polygon.length < 3) : -1;
}

function maybeAutoDraw() {
  if (drawing || inspecting) return;
  const j = nextUndrawnTarget();
  if (j >= 0) startDrawing(j);
}

/** A reading typed (Enter or leaving the field): commit it, then the next target without a polygon is
 *  ready to draw. When every target of the shot is drawn, Enter moves to the next reading field. */
async function commitReading(i: number, text: string, viaEnter: boolean) {
  await setGroundTruth(i, text);
  const ev = currentEvent();
  if (!ev || drawing || inspecting) return;
  const t = ev.targets[i];
  if (!t || t.ground_truth === null) return;
  const next = nextUndrawnTarget();
  if (next >= 0) {
    startDrawing(next);
    return;
  }
  if (viaEnter) {
    const field = document.querySelector<HTMLInputElement>(`[data-gt="${i + 1}"]`);
    if (field) {
      field.focus();
      field.select();
    } else (document.activeElement as HTMLElement | null)?.blur();
  }
}

/** A click on the image while nothing is being drawn starts the next polygon with that corner. */
function drawingForClick(): typeof drawing {
  if (drawing) return drawing;
  if (inspecting) {
    if (inspect.images.length) startInspectDrawing();
  } else {
    const next = nextUndrawnTarget();
    if (next >= 0) startDrawing(next);
  }
  return drawing;
}

async function addTargetAndDraw() {
  cancelDrawing(true);
  await update("add_target");
  const ev = currentEvent();
  if (ev) startDrawing(visibleTargets(ev).length - 1);
}

// ----------------------------------------------------------------------------- Inspect

async function toggleInspect() {
  if (inspecting) {
    await showStep(activeStep);
    return;
  }
  cancelDrawing(true);
  inspecting = true;
  settingsOpen = false;
  if (!inspect.images.length && state?.last_output) {
    // A run has been done: start with its outputs. The Inspect layout goes first, so the viewer has
    // its size when the first image arrives.
    viewer.placeholder = "Opening the outputs of the last run…";
    viewer.setImage(null);
    renderAll();
    await inspectAddPaths([state.last_output]);
    if (!inspect.images.length) await inspectSelect(0);
    return;
  }
  await inspectSelect(inspectCurrent);
}

async function inspectSelect(i: number) {
  cancelDrawing(true);
  inspectCurrent = Math.max(0, Math.min(i, inspect.images.length - 1));
  const im = inspect.images[inspectCurrent];
  if (!im) {
    viewer.placeholder = "No image open.\n\nOpen images… or Open folder… (left) to look at any thermal image.";
    viewer.setImage(null);
    renderAll();
    return;
  }
  const key = "inspect:" + im.path;
  let img = imageCache.get(key);
  if (!img) {
    img = (await call<ImagePayload>("inspect_image_data", { index: inspectCurrent })) ?? undefined;
    if (!img) return;
    imageCache.set(key, img);
  }
  viewer.setImage({ width: img.width, height: img.height, data: decodePixels(img), lo: img.lo, hi: img.hi });
  renderAll();
}

async function inspectAddPaths(paths: string[]) {
  const r = await call<InspectAddResult>("inspect_add", { paths });
  if (!r) return;
  const first = inspect.images.length === 0;
  inspect = r.inspect;
  if (r.rejected.length) await alert("Images skipped", `Only single-band images can be inspected:\n${r.rejected.join("\n")}`);
  if (first && inspect.images.length) await inspectSelect(0);
  else renderAll();
  status(`${r.added} image(s) opened for inspection.`);
}

async function inspectAdd(kind: "files" | "folder") {
  const paths = kind === "files" ? await pick("file", pickImages, [] as string[]) : [await pick("folder", () => pickFolder("Folder with images to inspect"), null)].filter((p): p is string => !!p);
  if (paths.length) await inspectAddPaths(paths);
}

/** From the Process step's "Done" card: the output folder, opened in Inspect. */
async function inspectResults() {
  if (!state?.last_output) return;
  const hadImages = inspect.images.length > 0;
  if (!inspecting) await toggleInspect(); // opens the outputs by itself when nothing else is open
  if (hadImages || !inspect.images.length) await inspectAddPaths([state.last_output]);
}

async function inspectRemoveImage() {
  const im = inspect.images[inspectCurrent];
  if (!im) return;
  inspectPolys.delete(im.path);
  imageCache.delete("inspect:" + im.path);
  const r = await call<InspectDto>("inspect_remove", { index: inspectCurrent });
  if (!r) return;
  inspect = r;
  await inspectSelect(inspectCurrent);
}

async function inspectClear() {
  const r = await call<InspectDto>("inspect_clear");
  if (!r) return;
  inspect = r;
  inspectPolys.clear();
  for (const k of [...imageCache.keys()]) if (k.startsWith("inspect:")) imageCache.delete(k);
  await inspectSelect(0);
}

function startInspectDrawing() {
  if (!inspect.images.length) return;
  drawing = { target: currentInspectPolys().length, points: [], redo: false, inspect: true };
  viewer.drawing = drawing.points;
  viewer.activeTarget = drawing.target;
  (document.activeElement as HTMLElement | null)?.blur();
  status("Click the corners of the area, then the first corner again (or Enter) to close; Esc cancels.");
  renderAll();
}

function inspectRemovePoly(j: number) {
  currentInspectPolys().splice(j, 1);
  renderAll();
}

async function inspectSaveCsv() {
  const lines = ["image,area,mean,std,min,max,pixels,corners"];
  for (const im of inspect.images) {
    (inspectPolys.get(im.path) ?? []).forEach((p, j) => {
      const f = (v: number | null | undefined) => (v === null || v === undefined || !isFinite(v) ? "" : v.toFixed(4));
      lines.push([im.name, `P${j + 1}`, f(p.stats?.mean), f(p.stats?.std), f(p.stats?.min), f(p.stats?.max), String(p.stats?.count ?? ""), `"${p.points.map(([x, y]) => `${x} ${y}`).join(";")}"`].join(","));
    });
  }
  if (lines.length === 1) return;
  const path = await pick("save", () => pickCsvToSave("inspect_table.csv"), null);
  if (!path) return;
  const written = await call<string>("write_text_file", { path, text: lines.join("\n") + "\n" });
  if (written) status(`Table saved: ${written}`);
}

function renderInspectPanel() {
  const el = $("#fit-panel");
  const im = inspect.images[inspectCurrent];
  const polys = currentInspectPolys();
  const f = (v: number | null | undefined, d = 2) => (v === null || v === undefined || !isFinite(v) ? "–" : v.toFixed(d));
  const rows = polys.map((p, j) => `<tr><td><span style="display:inline-block;width:10px;height:10px;background:${PALETTE[j % PALETTE.length]};vertical-align:middle;margin-right:5px"></span>P${j + 1}</td><td class="r">${f(p.stats?.mean)}</td><td class="r">${f(p.stats?.std)}</td><td class="r">${f(p.stats?.min, 1)}</td><td class="r">${f(p.stats?.max, 1)}</td><td class="r">${p.stats ? p.stats.count : "…"}</td><td><button class="btn small" data-inspect-remove="${j}">×</button></td></tr>`).join("");
  el.innerHTML = `<div class="fit-head"><span class="t">Inspect</span><span class="muted small">${im ? esc(im.name) : "no image"}</span></div>
<div class="hint" style="margin:8px 0">The value under the cursor follows the mouse on the image. Polygons drawn here are listed below (${esc(unit())}).</div>
<div style="overflow:auto"><table class="res"><thead><tr><th>Area</th><th class="r">mean</th><th class="r">σ</th><th class="r">min</th><th class="r">max</th><th class="r">px</th><th></th></tr></thead><tbody>${rows || `<tr><td colspan="7" class="muted">no polygons on this image yet</td></tr>`}</tbody></table></div>
<div class="fit-btns"><button class="btn" id="btn-inspect-save2" ${polys.length ? "" : "disabled"}>Save table as CSV…</button><button class="btn accent" id="btn-inspect-back">Back to the calibration ›</button></div>`;
  el.querySelectorAll<HTMLElement>("[data-inspect-remove]").forEach((b) => b.addEventListener("click", () => inspectRemovePoly(Number(b.dataset.inspectRemove))));
  el.querySelector("#btn-inspect-save2")?.addEventListener("click", () => void inspectSaveCsv());
  el.querySelector("#btn-inspect-back")?.addEventListener("click", () => void showStep(activeStep));
}

async function undo() {
  if (!state?.can_undo) {
    status("Nothing to undo.");
    return;
  }
  cancelDrawing(true);
  await update("undo");
  status("Undone." + (state?.can_undo ? "" : " Nothing more to undo."));
}

async function redo() {
  if (!state?.can_redo) {
    status("Nothing to redo.");
    return;
  }
  cancelDrawing(true);
  await update("redo");
  status("Redone.");
}

/** Shots whose targets are not all complete, for the warning when leaving Targets. */
function missingTargets(): string[] {
  if (!state) return [];
  const out: string[] = [];
  state.events.forEach((e, i) => {
    const miss = visibleTargets(e)
      .filter((t) => !t.complete)
      .map((t) => `${t.label} (${t.polygon.length < 3 ? "no polygon" : t.ground_truth === null ? "no reading" : "no valid pixels"})`);
    if (miss.length) out.push(`shot ${i + 1} (${e.file_name}): ${miss.join(", ")}`);
  });
  return out;
}

function cancelDrawing(silent = false) {
  if (!drawing) return;
  drawing = null;
  viewer.drawing = null;
  viewer.activeTarget = null;
  if (!silent) status("Drawing cancelled; the previous polygon is back.");
  renderAll();
}

async function finishRename(name: string) {
  if (renaming === null) return;
  const i = renaming;
  renaming = null;
  await update("rename_target", { index: i, name });
}

async function removeTarget(i: number) {
  const ev = currentEvent();
  if (!ev || ev.targets.length <= 1) {
    status("The last target stays; Clear removes its polygon.");
    return;
  }
  const drawn = state!.events.filter((e) => e.targets[i]?.polygon.length >= 3).length;
  if (drawn && !(await alert("Remove target", `${ev.targets[i].label} has a polygon on ${drawn} shot(s). Remove it from all shots?`, [{ label: "Cancel", value: false }, { label: "Remove", accent: true, value: true }]))) return;
  cancelDrawing(true);
  await update("remove_target", { index: i });
  status(`Removed T${i + 1} from all shots; the remaining targets were renumbered.`);
}

async function setGroundTruth(i: number, text: string) {
  const s = text.trim().replace(",", ".");
  const v = s === "" ? null : Number(s);
  if (v !== null && !isFinite(v)) {
    renderRail();
    return;
  }
  const ev = currentEvent();
  if (!ev || ev.targets[i]?.ground_truth === v) return;
  await update("set_ground_truth", { event: current, target: i, value: v });
}

async function browseSurvey() {
  const f = await pick("folder", () => pickFolder("Select the survey folder (every image in it is processed)"), null);
  if (!f) return;
  await applySurvey({ folder: f, output_folder: state?.survey.output_folder || f + "/calibrated" });
}

async function browseOutput() {
  const f = await pick("folder", () => pickFolder("Select the output folder"), null);
  if (f) await applySurvey({ output_folder: f });
}

async function applySurvey(patch: Partial<AppState["survey"]> = {}) {
  if (!state) return;
  const survey = {
    ...state.survey,
    folder: $<HTMLInputElement>("#survey-folder")?.value ?? state.survey.folder,
    output_folder: $<HTMLInputElement>("#output-folder")?.value ?? state.survey.output_folder,
    filter: $<HTMLInputElement>("#name-filter")?.value ?? state.survey.filter,
    ...patch,
  };
  const pf = await call<Preflight>("set_survey", { survey });
  if (!pf) return;
  preflight = pf;
  rows = [];
  pending = pf.files;
  applyState(pf.state);
  if (survey.folder) status(`${pf.state.survey_timeline.length} survey image(s) found; ${pf.files.length} ready to process.${pf.blocking ? " Fix the blocking items first." : ""}`);
}

async function runPreflight() {
  const pf = await call<Preflight>("run_preflight");
  if (!pf) return;
  preflight = pf;
  pending = pf.files;
  applyState(pf.state);
}

async function startBatch() {
  if (!state || state.running) return;
  const pf = await call<Preflight>("run_preflight");
  if (!pf) return;
  preflight = pf;
  if (pf.blocking) {
    await alert("Cannot start", pf.lines.filter((l) => l[0] === "block").map((l) => "✗ " + l[1]).join("\n"));
    return;
  }
  const warns = pf.lines.filter((l) => l[0] === "warn").map((l) => "⚠ " + l[1]);
  if (warns.length && !(await alert("Pre-flight warnings", warns.join("\n\n") + "\n\nStart anyway?", [{ label: "Cancel", value: false }, { label: "Start", accent: true, value: true }]))) return;
  rows = [];
  pending = pf.files;
  progressPct = 0;
  progressText = "Starting…";
  await call<boolean>("start_batch"); // an error is shown by call(); either way refresh
  await update("get_state");
}

// ----------------------------------------------------------------------------- centre: image / process view

function renderCenter() {
  const process = activeStep === 3 && !inspecting;
  $("#image-view").hidden = process;
  $("#process-view").hidden = !process;
  if (inspecting) {
    const im = inspect.images[inspectCurrent];
    $("#image-name").textContent = im ? im.name : "No image";
    $("#image-info").textContent = im && viewer.image ? `${viewer.image.width} × ${viewer.image.height} · inspect` : "";
    viewer.targets = inspectTargets();
  } else {
    const ev = currentEvent();
    $("#image-name").textContent = ev ? ev.file_name : "No image";
    $("#image-info").textContent = ev && viewer.image ? `${viewer.image.width} × ${viewer.image.height} · ${ev.timestamp.replace("T", " ")}${ev.timestamp_tz} (${ev.timestamp_source})` : "";
    viewer.targets = ev ? visibleTargets(ev) : [];
  }
  viewer.redraw();
  if (process) renderProcess();
}

const RESULT_COLS: [string, string, boolean][] = [["input_filename", "Image", false], ["timestamp", "Time · source", false], ["slope_m", "m", true], ["intercept_b", "b", true], ["original_mean_celsius", "mean in", true], ["calibrated_mean_celsius", "mean out", true], ["u_fit_1sigma", "± 1σ", true], ["status", "Status", false]];

function resultRowHtml(r: Row): string {
  const st = String(r.status ?? "");
  const pos = String(r.position_vs_calibration ?? "");
  const shown = st !== "ok" ? st : pos === "before" || pos === "after" ? pos : "ok";
  const cls = st.startsWith("error") ? "err" : shown === "before" || shown === "after" || st.startsWith("skipped") ? "warn" : "";
  const cell = (k: string, num: boolean) => {
    const v = r[k];
    if (k === "timestamp") return `${String(v ?? "").slice(11, 19)} · ${esc(String(r.timestamp_source ?? ""))}`;
    if (k === "status") return esc(shown);
    if (v === null || v === undefined) return "";
    if (typeof v === "number") return k === "slope_m" ? v.toFixed(4) : v.toFixed(k === "intercept_b" ? 3 : 2);
    return num ? String(v) : esc(String(v));
  };
  return `<tr class="${cls}" data-file="${esc(String(r.input_filename ?? ""))}">${RESULT_COLS.map(([k, , num]) => `<td class="${num ? "r" : ""}">${cell(k, num)}</td>`).join("")}</tr>`;
}

function pendingRowHtml(f: string): string {
  return `<tr class="pending" data-file="${esc(f)}"><td>${esc(f)}</td><td></td><td></td><td></td><td></td><td></td><td></td><td>not processed</td></tr>`;
}

function progressHtml(): string {
  return `<div class="muted small">${esc(progressText)}</div><div class="bar"><div style="width:${progressPct}%"></div></div>`;
}

/** During a run the progress and the finished rows change in place, so the table can be scrolled
 *  while it fills; a full re-render keeps the table's scroll position. */
function renderProcess() {
  const el = $("#process-view");
  const scrollTop = el.querySelector(".results-table")?.scrollTop ?? 0;
  const lines = preflight?.lines ?? [];
  const counts = preflight ? `${lines.filter((l) => l[0] === "ok").length} passed · ${lines.filter((l) => l[0] === "warn").length} warnings · ${lines.filter((l) => l[0] === "block").length} blocking` : "waiting for the survey folder";
  const sym: Record<string, string> = { ok: "✓", warn: "⚠", block: "✗" };
  const done = new Set(rows.map((r) => String(r.input_filename)));
  const pendingHtml = pending.filter((f) => !done.has(f)).map(pendingRowHtml).join("");
  const hint = !state?.survey.folder ? "Nothing to process yet.\n\nChoose the survey folder on the left (Browse): every image in it is listed here, checked, and calibrated when you press Run." : !pending.length && !rows.length ? "No images found in this folder (calibration shots and earlier outputs are excluded).\nChoose another survey folder or clear the filename filter." : "";
  el.innerHTML = `<div class="card pf"><div class="pf-head"><span>Pre-flight check</span><span class="muted small" style="font-weight:normal">${esc(counts)}</span></div>${lines.map(([l, t]) => `<div class="pf-line ${l}">${sym[l] ?? "-"} ${esc(t)}</div>`).join("")}</div>
<div class="card runbar"><button class="btn" id="btn-check">Check</button><button class="btn accent" id="btn-run" ${state?.running ? "disabled" : ""}>Run</button><button class="btn" id="btn-cancel" ${state?.running ? "" : "disabled"}>Cancel</button><div class="progress">${progressHtml()}</div></div>
<div class="card results"><div class="results-head"><span>Results</span><span class="muted small" style="font-weight:normal">one row per image; grey rows are not processed yet</span><span class="spacer"></span><button class="btn small" id="btn-open-output" ${state?.last_output ? "" : "disabled"}>Open output folder</button></div><div class="results-table"><table class="res"><thead><tr>${RESULT_COLS.map(([, t, num]) => `<th class="${num ? "r" : ""}">${t}</th>`).join("")}</tr></thead><tbody>${rows.map(resultRowHtml).join("")}${pendingHtml}</tbody></table></div>${hint ? `<div class="placeholder">${esc(hint).replace(/\n/g, "<br>")}</div>` : ""}</div>`;
  const table = el.querySelector(".results-table");
  if (table) table.scrollTop = scrollTop;
  $("#btn-check").addEventListener("click", () => void runPreflight());
  $("#btn-run").addEventListener("click", () => void startBatch());
  $("#btn-cancel").addEventListener("click", () => void call("cancel_batch"));
  $("#btn-open-output").addEventListener("click", () => void call("open_in_file_manager", { path: state?.last_output }));
}

function updateProgressInPlace() {
  const box = document.querySelector("#process-view .progress");
  if (!box || $("#process-view").hidden) {
    if (activeStep === 3 && !inspecting) renderProcess();
    return;
  }
  box.innerHTML = progressHtml();
}

function updateRowInPlace(r: Row) {
  const name = String(r.input_filename ?? "");
  const tr = document.querySelector(`#process-view tr[data-file="${CSS.escape(name)}"]`);
  if (!tr || $("#process-view").hidden) {
    if (activeStep === 3 && !inspecting) renderProcess();
    return;
  }
  tr.outerHTML = resultRowHtml(r);
}

function renderFit() {
  if (inspecting) {
    renderInspectPanel();
    return;
  }
  const ev = currentEvent();
  const n = state?.events.length ?? 0;
  const label = ev ? (n === 1 ? "only" : current === 0 ? "start" : current === n - 1 ? "end" : `mid ${current}`) : "";
  const complete = ev ? visibleTargets(ev).filter((t) => t.complete).length : 0;
  const subtitle = ev ? `shot ${current + 1}/${n} · ${label} · ${complete} target${complete !== 1 ? "s" : ""}` : "";
  const others = (state?.events ?? []).filter((e, i) => i !== current && e.fit && !e.fit.single_point).map((e) => ({ label: `shot ${e.index + 1} · ${fmtTime(e.timestamp)}`, fit: e.fit! }));
  renderFitPanel($("#fit-panel"), ev, subtitle, others, unit(), "No calibration image selected");
  document.querySelector("#btn-report")?.addEventListener("click", () => void exportReport());
  document.querySelector("#btn-plot")?.addEventListener("click", () => showDetailedPlot());
}

async function exportReport() {
  const ev = currentEvent();
  if (!ev?.fit) return;
  const html = await call<string>("fit_report_html", { index: current });
  if (!html) return;
  const path = await pick("save", () => pickReportToSave(ev.file_name.replace(/\.[^.]+$/, "") + "_fit_report.html"), null);
  if (!path) return;
  const written = await call<string>("write_text_file", { path, text: html });
  if (written) status(`Fit report written: ${written}`);
}

function showDetailedPlot() {
  const ev = currentEvent();
  if (!ev?.fit || ev.fit.single_point) return;
  const others = (state?.events ?? []).filter((e, i) => i !== current && e.fit && !e.fit.single_point).map((e) => ({ label: `shot ${e.index + 1} · ${fmtTime(e.timestamp)}`, fit: e.fit! }));
  const f = ev.fit;
  const diag = f.rmse !== null
    ? `R² = ${(f.r_squared ?? NaN).toFixed(4)}, RMSE = ${f.rmse.toFixed(3)} ${unit()}, s = ${(f.resid_std ?? NaN).toFixed(3)}, SE(m) = ${(f.se_slope ?? NaN).toFixed(4)}, SE(b) = ${(f.se_intercept ?? NaN).toFixed(3)}, leave-one-out = ${f.loo_rmse !== null ? f.loo_rmse.toFixed(3) : "n/a"}`
    : "2 targets: R² = 1 by construction, no scatter estimate";
  const modal = $("#modal");
  modal.innerHTML = `<div class="dialog" style="max-width:820px"><h3>Fit for ${esc(ev.file_name)}</h3>${plotSvg(ev, others, unit(), 760, 420).replace('class="fit-plot"', 'style="width:760px;height:420px;background:#fbfbf9;border:1px solid #d9dcd6"')}<pre style="margin-top:8px">${esc(diag)}</pre><div class="btns"><button class="btn accent" id="modal-close">Close</button></div></div>`;
  modal.hidden = false;
  $("#modal-close").addEventListener("click", () => (modal.hidden = true));
}

function renderAll() {
  renderRail();
  renderCenter();
  renderFit();
  renderTimeline($("#timeline"), state?.events ?? [], state?.survey_timeline ?? []);
}

async function showStep(n: Step) {
  if (n === 3 && activeStep !== 3 && state?.events.length) {
    const missing = missingTargets();
    if (missing.length) {
      const go = await alert("Targets missing", `Not every target is complete on every shot:\n\n${missing.join("\n")}\n\nAn incomplete target is left out of that shot's fit.`, [{ label: "Back to targets", value: false }, { label: "Continue anyway", accent: true, value: true }]);
      if (!go) {
        void showStep(2);
        return;
      }
    }
  }
  const previous = activeStep;
  const wasInspecting = inspecting;
  inspecting = false;
  activeStep = n;
  settingsOpen = false;
  if (wasInspecting) {
    viewer.placeholder = CAL_PLACEHOLDER;
    await selectShot(current);
  }
  if (n === 3 && state?.survey.folder) void runPreflight();
  renderAll();
  if (n === 2 && previous !== 2) maybeAutoDraw();
}

// ----------------------------------------------------------------------------- viewer wiring

viewer.onClick = (x, y) => {
  const d = drawingForClick();
  if (!d) return;
  const hit = viewer.hitPoint(x, y);
  if (hit === 0 && d.points.length >= 3) {
    finishDrawing(); // the ringed first corner closes the polygon
    return;
  }
  if (hit >= 0) d.points.splice(hit, 1); // a corner clicked again is removed
  else d.points.push([x, y]);
  viewer.drawing = d.points;
  renderAll();
};
viewer.onMotion = (x, y, v) => {
  const ev = currentEvent();
  if (!ev) return;
  let text = v === null ? `(${x}, ${y}) · nodata` : `${v.toFixed(2)} ${unit()} · ${x}, ${y}`;
  if (drawing) text += ` · T${drawing.target + 1}: corner ${drawing.points.length + 1} of 4`;
  $("#zoom-label").textContent = text;
};
viewer.onViewChange = (scale) => {
  if (!drawing) $("#zoom-label").textContent = viewer.image ? `${Math.round(scale * 100)} %` : "";
};
viewer.onEscape = () => cancelDrawing();

$("#zoom-seg").addEventListener("click", (e) => {
  const b = (e.target as HTMLElement).closest<HTMLElement>("[data-zoom]");
  if (!b) return;
  if (b.dataset.zoom === "fit") viewer.fit();
  else viewer.oneToOne();
});
$("#viz-seg").addEventListener("click", (e) => {
  const b = (e.target as HTMLElement).closest<HTMLElement>("[data-viz]");
  if (!b) return;
  document.querySelectorAll("#viz-seg .seg-btn").forEach((x) => x.classList.toggle("selected", x === b));
  viewer.setMode(b.dataset.viz as VizMode);
});

// ----------------------------------------------------------------------------- top bar

$<HTMLInputElement>("#title").addEventListener("change", (e) => void update("set_title", { title: (e.target as HTMLInputElement).value }));
$("#btn-new").addEventListener("click", async () => {
  if (state?.events.length && !(await alert("New project", "Discard the current targets and fits?", [{ label: "Cancel", value: false }, { label: "Discard", accent: true, value: true }]))) return;
  cancelDrawing(true);
  imageCache.clear();
  preflight = null;
  rows = [];
  pending = [];
  current = 0;
  await update("new_project");
  viewer.setImage(null);
  showStep(1);
  status("New project.");
});
$("#btn-open").addEventListener("click", async () => {
  const p = await pick("open", pickProjectToOpen, null);
  if (!p) return;
  imageCache.clear();
  const r = await call<{ problems: string[]; state: AppState }>("open_project", { path: p });
  if (!r) return;
  const s = r.state;
  applyState(s);
  if (r.problems.length) await alert("Project opened with problems", r.problems.join("\n"));
  preflight = null;
  rows = [];
  pending = [];
  await selectShot(0);
  showStep(s.events.length ? 2 : 1);
  status(`Project opened: ${s.events.length} calibration image(s).`);
});
async function save(as: boolean) {
  if (!state) return;
  let path: string | null = as ? null : state.project_path;
  if (!path) {
    const suggested = await invoke<string>("suggested_project_filename");
    path = await pick("save", () => pickProjectToSave(suggested), null);
  }
  if (!path) return;
  const saved = await call<string>("save_project", { path });
  if (saved) {
    await update("get_state");
    status(`Project saved: ${saved}`);
  }
}
$("#btn-save").addEventListener("click", () => void save(false));
$("#btn-save-as").addEventListener("click", () => void save(true));
$("#btn-help").addEventListener("click", () => void alert("How to use", `1 · Images: choose the sensor type (locked once images are loaded), then add every calibration shot of the flight that shows your reference panels (single-band temperature TIFFs) and open 2 · Targets.\n2 · Targets: the card at the top of the step says what to do next. Zoom in (wheel), press Draw for a target and click its 4 corners, type the reference reading, repeat on every shot.\n3 · Process: choose the survey folder and the output folder, then Run.\nSettings (bottom of the rail): ROI edge inset, emissivity correction, metadata copy, skipping existing outputs; all saved in the project file.\n\nThe fit panel on the right updates as you enter readings; with 3 or more targets you get an uncertainty estimate.\n\n${state?.tool_name ?? "Thermal Calibration Tool"} ${state?.version ?? ""} · Haystack Imaging · haystackimaging.eu\nGPL-3.0 · source and issues: github.com/haystack-imaging/thermal-calibration-tool`));
window.addEventListener("keydown", (e) => {
  const ctrl = e.ctrlKey || e.metaKey;
  const key = e.key.toLowerCase();
  const target = e.target as HTMLElement | null;
  const inInput = target?.tagName === "INPUT" || target?.tagName === "TEXTAREA";
  if (ctrl && key === "s") {
    e.preventDefault();
    void save(e.shiftKey);
  } else if (ctrl && key === "o") {
    e.preventDefault();
    $("#btn-open").click();
  } else if (ctrl && key === "z" && !e.shiftKey) {
    e.preventDefault();
    if (drawing) {
      drawing.points.pop();
      viewer.drawing = drawing.points;
      renderAll();
    } else {
      if (inInput) target?.blur(); // a value still being typed is committed first, then undone
      void undo();
    }
  } else if (ctrl && (key === "y" || (key === "z" && e.shiftKey))) {
    e.preventDefault();
    if (inInput) target?.blur();
    void redo();
  } else if (e.key === "Enter" && drawing && !inInput) {
    e.preventDefault();
    finishDrawing();
  }
});
$("#btn-undo").addEventListener("click", () => void undo());
$("#btn-redo").addEventListener("click", () => void redo());

// ----------------------------------------------------------------------------- help popups

const tip = $("#tip");
let tipPinned: HTMLElement | null = null;
let tipTimer: number | null = null;
function showTip(el: HTMLElement) {
  const text = el.dataset.tip;
  if (!text) return;
  tip.textContent = text;
  tip.hidden = false;
  const r = el.getBoundingClientRect();
  let left = r.left;
  if (left + tip.offsetWidth > window.innerWidth - 12) left = window.innerWidth - 12 - tip.offsetWidth;
  let top = r.bottom + 6;
  if (top + tip.offsetHeight > window.innerHeight - 8) top = r.top - 6 - tip.offsetHeight;
  tip.style.left = `${Math.max(8, left)}px`;
  tip.style.top = `${Math.max(8, top)}px`;
}
document.addEventListener("mouseover", (e) => {
  const el = (e.target as HTMLElement).closest<HTMLElement>("[data-tip]");
  if (!el || tipPinned) return;
  if (tipTimer) clearTimeout(tipTimer);
  tipTimer = window.setTimeout(() => showTip(el), 250);
});
document.addEventListener("mouseout", (e) => {
  if (!(e.target as HTMLElement).closest("[data-tip]")) return;
  if (tipTimer) clearTimeout(tipTimer);
  if (!tipPinned) tip.hidden = true;
});
document.addEventListener("click", (e) => {
  const el = (e.target as HTMLElement).closest<HTMLElement>("[data-tip]");
  if (el?.classList.contains("help")) {
    e.preventDefault();
    if (tipPinned === el) {
      tipPinned = null;
      tip.hidden = true;
    } else {
      tipPinned = el;
      showTip(el);
    }
    return;
  }
  if (tipPinned) {
    tipPinned = null;
    tip.hidden = true;
  }
});

// the timeline draws at its real width: a window resize (full screen) must redraw it, not stretch it
new ResizeObserver(() => renderTimeline($("#timeline"), state?.events ?? [], state?.survey_timeline ?? [])).observe($("#timeline"));

// ----------------------------------------------------------------------------- batch events

void listen("batch-progress", (p) => {
  const { done, total, name } = p as { done: number; total: number; name: string };
  progressPct = total ? (100 * done) / total : 0;
  progressText = name ? `Processing ${Math.min(done + 1, total)} of ${total} · ${name}` : `${done} of ${total}`;
  updateProgressInPlace();
});
void listen("batch-row", (p) => {
  rows.push(p as Row);
  updateRowInPlace(p as Row);
});
void listen("batch-log", (p) => {
  const line = String(p);
  if (line.startsWith("ERROR") || line.includes("exiftool")) status(line.slice(0, 140));
});
void listen("batch-done", (p) => {
  const d = p as BatchDone;
  progressPct = d.cancelled ? progressPct : 100;
  progressText = d.error ? `Failed: ${d.error}` : d.cancelled ? `Cancelled after ${d.ok + d.errors + d.skipped} of ${d.total} images` : `Done: ${d.ok} ok, ${d.errors} errors, ${d.skipped} skipped`;
  status(d.error ? "Batch processing failed." : d.cancelled ? `Batch cancelled: ${d.ok} image(s) written to ${d.output}; the rest is not processed.` : `Batch complete: ${d.ok} image(s), the log and the project file written to ${d.output}`);
  void update("get_state");
});

// ----------------------------------------------------------------------------- test access

declare global {
  interface Window {
    __app?: {
      state: () => AppState | null;
      current: () => number;
      activeStep: () => number;
      drawing: () => boolean;
      preflight: () => Preflight | null;
      rows: () => Row[];
      viewer: Viewer;
      selectShot: (i: number) => Promise<void>;
      showStep: (n: Step) => Promise<void>;
      guidance: () => { images: Guide; targets: Guide; process: Guide };
      pickImages: () => Promise<string[]>;
      inspecting: () => boolean;
      inspect: () => InspectDto;
      inspectPolys: () => { points: Point[]; stats: PolygonStats | null }[];
      toggleInspect: () => Promise<void>;
      rerender: () => void;
      updateRow: (r: Row) => void;
    };
  }
}

window.__app = {
  state: () => state,
  current: () => current,
  activeStep: () => activeStep,
  drawing: () => drawing !== null,
  preflight: () => preflight,
  rows: () => rows,
  viewer,
  selectShot,
  showStep,
  guidance: () => ({ images: guideImages(), targets: guideTargets(), process: guideProcess() }),
  pickImages,
  inspecting: () => inspecting,
  inspect: () => inspect,
  inspectPolys: currentInspectPolys,
  toggleInspect,
  rerender: renderAll,
  updateRow: updateRowInPlace,
};

// ----------------------------------------------------------------------------- start

void (async () => {
  const s = await call<AppState>("get_state");
  if (!s) {
    renderAll();
    return;
  }
  applyState(s);
  if (s.events.length) {
    // a project given on the command line: show it like an opened project
    await selectShot(0);
    showStep(2);
  }
})();
