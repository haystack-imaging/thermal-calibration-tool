// End-to-end test of the desktop app on the golden flight, run inside the app's own web view:
//   thermal-calibration-app --e2e-script tests/e2e/flow.js --e2e-env-file env.json --e2e-report report.json
// `window.__e2e` (helpers, env) comes from the Rust side, `window.__app` from main.ts. The native file
// dialogs are replaced by hooks that return paths from env. Every step reports ok/not ok; any uncaught
// error or console.error fails the run. See tests/e2e/README.md.

const e2e = window.__e2e;
const env = e2e.env;
const app = () => window.__app;
const state = () => app().state();
const close = (a, b, tol) => Math.abs(a - b) <= tol;

// ---------------------------------------------------------------- the app came up
await e2e.until(() => app() && state(), 20000);
e2e.log(`screen ${screen.width}x${screen.height}, window ${innerWidth}x${innerHeight}, devicePixelRatio ${devicePixelRatio}`);
await e2e.report("app loaded", state().events.length === 0 && app().activeStep() === 1, { version: state().version });
await e2e.report("empty guidance", /Choose the sensor type/.test(app().guidance().images.head) && /Choose the sensor type/.test(e2e.$(".guide .head").textContent));

// ---------------------------------------------------------------- 1 · Images
e2e.pickImages = async () => env.calibration;
e2e.click("#btn-add-images");
await e2e.until(() => state().events.length === env.calibration.length);
const evs = state().events;
await e2e.report("add images", evs.length === 3 && evs.every((e) => e.timestamp_source === "TIFF tag") && evs[0].timestamp < evs[2].timestamp, evs.map((e) => e.file_name + " " + e.timestamp));
await e2e.report("stays in step 1 with the next button", app().activeStep() === 1 && /3 shots loaded/.test(app().guidance().images.head));
e2e.type("#title", "golden flight");
await e2e.until(() => state().title === "golden flight");
await e2e.report("project title set from the top bar", document.title.startsWith("golden flight"));
e2e.click("#btn-add-images"); // the same files again: reported, not added
await e2e.until(() => !document.querySelector("#modal").hidden);
await e2e.report("duplicates are reported", /Already in the list/.test(e2e.$("#modal").textContent) && state().events.length === 3);
e2e.click("#modal button");
await e2e.until(() => document.querySelector("#modal").hidden);
e2e.pickImages = async () => [];
e2e.click("#btn-add-images");
await e2e.wait(200);
await e2e.report("cancelled dialog does nothing", state().events.length === 3 && document.querySelector("#modal").hidden);

// ---------------------------------------------------------------- 2 · Targets: draw on every shot
e2e.click("#guide-1");
await e2e.until(() => app().activeStep() === 2);
await e2e.report("drawing of T1 starts by itself on entering step 2", app().drawing() && app().viewer.activeTarget === 0 && /Click corner 1 of T1/.test(app().guidance().targets.head));
e2e.click("#btn-add-target");
await e2e.until(() => state().events[0].targets.length === 3);
await e2e.report("third target on every shot", state().events.every((e) => e.targets.length === 3));
await e2e.report("drawing of the new target starts by itself", app().drawing() && app().viewer.activeTarget === 2);
e2e.click(".help");
await e2e.until(() => !document.querySelector("#tip").hidden);
await e2e.report("help icons open their text on click", e2e.$("#tip").textContent.length > 30);
e2e.click(".help");
await e2e.until(() => document.querySelector("#tip").hidden);

// A click lands on a whole CSS pixel in some web views, and on a small screen the image is shown below
// 1:1, where one CSS pixel spans more than one image pixel and some pixels cannot be hit at all. So zoom
// in on the point first (3 screen pixels per image pixel): every corner then lands on its own pixel,
// whatever the screen size.
const clickImage = (x, y) => {
  const v = app().viewer;
  const canvas = e2e.$("#viewer");
  const r = canvas.getBoundingClientRect();
  let [cx, cy] = v.toCanvas(x + 0.5, y + 0.5);
  const m = 24;
  if (v.scale < 3 || cx < m || cy < m || cx > r.width - m || cy > r.height - m) {
    v.scale = Math.max(v.scale, 3);
    v.ox = x + 0.5 - r.width / (2 * v.scale);
    v.oy = y + 0.5 - r.height / (2 * v.scale);
    v.fitted = false;
    v.redraw();
    [cx, cy] = v.toCanvas(x + 0.5, y + 0.5);
  }
  e2e.mouse(canvas, "mousedown", r.left + Math.round(cx), r.top + Math.round(cy));
};
// the same corners, in any order (four corners are stored sorted as a rectangle)
const samePoints = (a, b) => {
  const key = (pts) => pts.map(([x, y]) => `${x},${y}`).sort().join(" ");
  return a.length === b.length && key(a) === key(b);
};
const keyWindow = (key, mods = {}) => window.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true, ...mods }));
const drawPolygon = async (target, corners, closeWith = "enter") => {
  e2e.click(`[data-draw="${target}"]`);
  await e2e.until(() => app().drawing() && app().viewer.activeTarget === target);
  for (const [x, y] of corners) clickImage(x, y);
  await e2e.until(() => app().viewer.drawing && app().viewer.drawing.length === corners.length);
  if (closeWith === "first") clickImage(corners[0][0], corners[0][1]);
  else keyWindow("Enter");
  await e2e.until(() => !app().drawing() && state().events[app().current()].targets[target].polygon.length === corners.length);
};

// ---------------------------------------------------------------- corners can be removed; polygons can have any number of corners
await app().selectShot(0);
await e2e.until(() => app().current() === 0 && app().viewer.image);
{
  const c = env.shots[0].polygons[0];
  e2e.click('[data-draw="0"]');
  await e2e.until(() => app().drawing() && app().viewer.activeTarget === 0);
  for (const [x, y] of c.slice(0, 3)) clickImage(x, y);
  await e2e.until(() => app().viewer.drawing.length === 3);
  clickImage(c[1][0], c[1][1]);
  await e2e.until(() => app().viewer.drawing.length === 2);
  await e2e.report("a corner clicked again is removed", /removed/.test(app().guidance().targets.detail));
  keyWindow("z", { ctrlKey: true });
  await e2e.until(() => app().viewer.drawing.length === 1);
  await e2e.report("Ctrl+Z removes the last corner while drawing", true);
  keyWindow("Escape");
  await e2e.until(() => !app().drawing());
  const five = [c[0], [Math.round((c[0][0] + c[1][0]) / 2), c[0][1] - 3], c[1], c[2], c[3]];
  await drawPolygon(0, five, "first");
  const t = state().events[0].targets[0];
  await e2e.report("a five-corner polygon, closed by clicking the first corner", t.polygon.length === 5 && t.pixels > 1000 && t.inner.length === 1, { pixels: t.pixels, mean: t.mean });
}
for (let i = 0; i < 3; i++) {
  await app().selectShot(i);
  await e2e.until(() => app().current() === i && app().viewer.image);
  const shot = env.shots[i];
  for (let j = 0; j < 3; j++) {
    if (i === 1 && j === 1) {
      // T1's reading left T2's drawing active; cancel it and start it again with a click on the image
      keyWindow("Escape");
      await e2e.until(() => !app().drawing());
      const c = shot.polygons[1];
      clickImage(c[0][0], c[0][1]);
      await e2e.until(() => app().drawing() && app().viewer.activeTarget === 1 && app().viewer.drawing.length === 1);
      await e2e.report("a click on the image starts the next target with that corner", true);
      for (const [x, y] of c.slice(1)) clickImage(x, y);
      keyWindow("Enter");
      await e2e.until(() => !app().drawing() && state().events[1].targets[1].polygon.length === 4);
    } else await drawPolygon(j, shot.polygons[j], j === 1 ? "first" : "enter");
    const t = state().events[i].targets[j];
    await e2e.report(`shot ${i + 1}: polygon T${j + 1}`, samePoints(t.polygon, shot.polygons[j]) && t.pixels > 1000 && t.inner.length === 1, { polygon: t.polygon, wanted: shot.polygons[j], mean: t.mean, pixels: t.pixels });
    if (j === 0) await e2e.report(`shot ${i + 1}: reading field focused after the polygon closed`, document.activeElement === document.querySelector(`[data-gt="${j}"]`));
    e2e.type(`[data-gt="${j}"]`, String(shot.gun[j]).replace(".", ","));
    await e2e.until(() => state().events[i].targets[j].ground_truth !== null);
    if (j < 2) {
      await e2e.until(() => app().drawing() && app().viewer.activeTarget === j + 1);
      if (i === 0 && j === 0) await e2e.report("the committed reading moves on to drawing the next target", true);
    }
  }
  await e2e.report(`shot ${i + 1}: nothing to draw after the last reading`, !app().drawing());
  await e2e.until(() => state().events[i].fit);
  const fit = state().events[i].fit;
  const exp = env.expected[i];
  await e2e.report(`shot ${i + 1}: fit matches the Python reference`, close(fit.slope, exp.slope, 1e-4) && close(fit.intercept, exp.intercept, 2e-3) && fit.n === 3, { slope: fit.slope, expected: exp.slope });
}
await e2e.report("all shots done guidance", /All 3 shots done, 3 targets each/.test(app().guidance().targets.head) && /3\/3 fitted/.test(e2e.$('[data-step="2"] .step-summary').textContent));
await e2e.report("fit panel shows the other shots dashed", /dashed: shot/.test(e2e.$("#fit-panel").textContent) && e2e.$("#fit-panel").querySelectorAll("circle").length >= 9);

// ---------------------------------------------------------------- redraw hides the old polygon, Esc restores it
await app().selectShot(0);
await e2e.until(() => app().current() === 0);
const before = state().events[0].targets[2].mean;
e2e.click('[data-draw="2"]');
await e2e.until(() => app().drawing());
const labelsWhileDrawing = e2e.$("#target-rows").textContent;
window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
await e2e.until(() => !app().drawing());
await e2e.report("redraw: Esc keeps the old polygon", /redrawing/.test(labelsWhileDrawing) && state().events[0].targets[2].mean === before);

// ---------------------------------------------------------------- naming, removing, settings
e2e.$(".tid").dispatchEvent(new MouseEvent("dblclick", { bubbles: true }));
await e2e.until(() => document.querySelector("#rename-input"));
e2e.$("#rename-input").value = "black";
e2e.key("#rename-input", "Enter");
await e2e.until(() => state().events.every((e) => e.targets[0].name === "black"));
await e2e.report("naming a target applies to every shot", /T1 black/.test(state().events[1].targets[0].label));
e2e.click("#btn-add-target");
await e2e.until(() => state().events[0].targets.length === 4);
e2e.click('[data-remove="3"]');
await e2e.until(() => state().events[0].targets.length === 3);
await e2e.report("remove an undrawn target", state().events.every((e) => e.targets.length === 3 && e.fit));
keyWindow("z", { ctrlKey: true });
await e2e.until(() => state().events[0].targets.length === 4);
await e2e.report("Ctrl+Z brings a removed target back on every shot", state().events.every((e) => e.targets.length === 4) && state().can_redo);
keyWindow("Z", { ctrlKey: true, shiftKey: true });
await e2e.until(() => state().events[0].targets.length === 3);
await e2e.report("Ctrl+Shift+Z redoes the removal", state().events.every((e) => e.targets.length === 3));
e2e.click('[data-step="settings"] .step-header');
await e2e.until(() => document.querySelector("#inset"));
const pixelsBefore = state().events[0].targets[0].pixels;
e2e.type("#inset", "6");
await e2e.until(() => state().events[0].targets[0].pixels < pixelsBefore);
await e2e.report("ROI inset re-measures every target", state().events.every((e) => e.targets.every((t) => t.pixels < pixelsBefore)) && state().settings.roi_inset_px === 6, { before: pixelsBefore, after: state().events[0].targets[0].pixels });
e2e.type("#inset", "3");
await e2e.until(() => state().events[0].targets[0].pixels === pixelsBefore);
e2e.click('[data-step="2"] .step-header');
await e2e.until(() => app().activeStep() === 2 && !document.querySelector("#inset"));

// ---------------------------------------------------------------- leaving Targets with a shot that lacks a polygon is flagged; undo/redo
await app().selectShot(1);
await e2e.until(() => app().current() === 1 && app().viewer.image);
keyWindow("Escape");
e2e.click('[data-clear="2"]');
await e2e.until(() => state().events[1].targets[2].polygon.length === 0);
e2e.click('[data-step="3"] .step-header');
await e2e.until(() => !document.querySelector("#modal").hidden);
const warning = e2e.$("#modal").textContent;
await e2e.report("leaving Targets with a missing polygon is flagged", /shot 2/.test(warning) && /T3 \(no polygon\)/.test(warning) && app().activeStep() !== 3, warning.slice(0, 160));
e2e.click("#modal button:not(.accent)");
await e2e.until(() => document.querySelector("#modal").hidden && app().activeStep() === 2);
keyWindow("Escape");
keyWindow("z", { ctrlKey: true });
await e2e.until(() => state().events[1].targets[2].polygon.length === 4);
await e2e.report("Ctrl+Z restores a cleared polygon with its fit", state().events[1].fit && state().events[1].fit.n === 3);
keyWindow("y", { ctrlKey: true });
await e2e.until(() => state().events[1].targets[2].polygon.length === 0);
await e2e.report("Ctrl+Y redoes the clearing", state().events[1].fit && state().events[1].fit.n === 2);
e2e.click("#btn-undo");
await e2e.until(() => state().events[1].targets[2].polygon.length === 4);
await e2e.report("the Undo button works as well", state().can_redo && state().events[1].fit.n === 3);
const reading = state().events[1].targets[0].ground_truth;
e2e.type('[data-gt="0"]', "99");
await e2e.until(() => state().events[1].targets[0].ground_truth === 99);
keyWindow("z", { ctrlKey: true });
await e2e.until(() => state().events[1].targets[0].ground_truth === reading);
await e2e.report("Ctrl+Z restores an overwritten reading", true, { reading });

// ---------------------------------------------------------------- 3 · Process
e2e.click("#guide-2");
await e2e.until(() => app().activeStep() === 3);
await e2e.report("process guidance asks for the folder", /Choose the survey folder/.test(app().guidance().process.head) && /Nothing to process yet/.test(e2e.$("#process-view").textContent));
e2e.pickFolder = async (title) => (/output/i.test(title) ? env.output : env.survey);
e2e.click("#btn-browse-survey");
await e2e.until(() => app().preflight() && app().preflight().files.length === 6);
e2e.type("#output-folder", env.output);
await e2e.until(() => state().survey.output_folder === env.output);
const pf = app().preflight();
await e2e.report("pre-flight lists 6 images", !pf.blocking && pf.files.length === 6 && /6 images ready/.test(app().guidance().process.head), pf.lines);
await e2e.report("timeline shows the survey", state().survey_timeline.length === 6 && e2e.$("#timeline").querySelectorAll("rect").length >= 6);
{
  // the plot is drawn at the timeline's width minus 28, but never below 200; a window on a small screen
  // leaves little room, so widen the fit panel by a step instead of setting a fixed width
  const w0 = e2e.$("#timeline svg.tl-plot").viewBox.baseVal.width;
  const fp = e2e.$("#fit-panel");
  fp.style.width = `${fp.offsetWidth + 120}px`;
  await e2e.until(() => e2e.$("#timeline svg.tl-plot").viewBox.baseVal.width !== w0);
  const w1 = e2e.$("#timeline svg.tl-plot").viewBox.baseVal.width;
  await e2e.report("timeline redraws at the new width when the window changes size", Math.abs(w1 - Math.max(e2e.$("#timeline").clientWidth - 28, 200)) < 2 && w1 < w0, { before: w0, after: w1 });
  e2e.$("#fit-panel").style.width = "";
  await e2e.until(() => e2e.$("#timeline svg.tl-plot").viewBox.baseVal.width === w0);
}
e2e.click("#btn-run");
await e2e.wait(300);
if (!document.querySelector("#modal").hidden) e2e.click("#modal button.accent");
await e2e.until(() => state().last_output, 60000);
await e2e.report("batch done", app().rows().length === 6 && app().rows().every((r) => r.status === "ok") && /Done: 6 ok/.test(e2e.$("#process-view").textContent), app().rows().map((r) => r.input_filename + " " + r.slope_m));
{
  const table = e2e.$("#process-view .results-table");
  const first = app().rows()[0];
  window.__app.updateRow({ ...first, slope_m: 9.9999 });
  const same = e2e.$("#process-view .results-table") === table;
  await e2e.report("a finished image updates its row in place, the table does not jump", same && /9\.9999/.test(e2e.$(`#process-view tr[data-file="${first.input_filename}"]`).textContent));
  window.__app.updateRow(first);
}
await e2e.report("done guidance offers to inspect the results", /Done\./.test(app().guidance().process.head) && /Inspect the results/.test(app().guidance().process.action.label));

// ---------------------------------------------------------------- hover readout and the Inspect section
e2e.click("#guide-3");
await e2e.until(() => app().inspecting() && app().inspect().images.length === 6 && app().viewer.image);
await e2e.report("the results open in Inspect", app().inspect().images.every((im) => /_calibrated\.tif$/.test(im.name)) && /Inspect/.test(e2e.$("#fit-panel").textContent));
{
  const canvas = e2e.$("#viewer");
  const r = canvas.getBoundingClientRect();
  const [cx, cy] = app().viewer.toCanvas(100.5, 100.5);
  e2e.mouse(canvas, "mousemove", r.left + cx, r.top + cy);
  await e2e.until(() => app().viewer.hoverValue !== null);
  const img = app().viewer.image;
  await e2e.report("hovering shows the value under the cursor", Math.abs(app().viewer.hoverValue - img.data[100 * img.width + 100]) < 1e-6 && app().viewer.unit === "°C", { value: app().viewer.hoverValue });
}
e2e.click("#btn-inspect-draw");
await e2e.until(() => app().drawing());
for (const [x, y] of [[20, 20], [120, 20], [120, 120], [20, 120]]) clickImage(x, y);
keyWindow("Enter");
await e2e.until(() => app().inspectPolys().length === 1 && app().inspectPolys()[0].stats);
{
  const st = app().inspectPolys()[0].stats;
  await e2e.report("a polygon in Inspect reports mean, σ, min, max and pixels", st.count === 101 * 101 && st.min <= st.mean && st.mean <= st.max && /P1/.test(e2e.$("#fit-panel").textContent), st);
}
e2e.pickCsvToSave = async () => env.csv;
e2e.click("#btn-inspect-save");
await e2e.until(() => /Table saved/.test(e2e.$("#status-text").textContent));
await e2e.report("the table is saved as CSV", true);
e2e.click("#btn-inspect-next");
await e2e.until(() => e2e.$("#image-name").textContent === app().inspect().images[1].name && app().viewer.image);
await e2e.report("polygons belong to their image", app().inspectPolys().length === 0);
e2e.click("#btn-inspect-clear");
await e2e.until(() => app().inspect().images.length === 0 && !app().viewer.image);
e2e.click("#btn-inspect-back");
await e2e.until(() => !app().inspecting());
e2e.click('[data-step="inspect"] .step-header');
await e2e.until(() => app().inspecting() && app().inspect().images.length === 6 && app().viewer.image);
await e2e.report("Inspect opens the last run's outputs by itself", true);
e2e.click("#btn-inspect-back");
await e2e.until(() => !app().inspecting() && app().activeStep() === 3 && !document.querySelector("#process-view").hidden);
await e2e.report("back to the calibration", true);

// ---------------------------------------------------------------- report, save, open, new
e2e.pickReportToSave = async () => env.report;
await app().selectShot(1);
e2e.click("#btn-report");
await e2e.until(() => /Fit report written/.test(e2e.$("#status-text").textContent));
await e2e.report("fit report exported", true);
e2e.pickProjectToSave = async () => env.project;
e2e.click("#btn-save");
await e2e.until(() => state().project_path === env.project);
await e2e.report("project saved", /flight-e2e\.calib\.json/.test(e2e.$("#saved").textContent));
e2e.click("#btn-new");
await e2e.until(() => !document.querySelector("#modal").hidden);
e2e.click("#modal button.accent");
await e2e.until(() => state().events.length === 0);
await e2e.until(() => app().activeStep() === 1);
await e2e.report("new project clears everything", state().events.length === 0 && (state().title === "" || state().title === "untitled project") && !state().project_path, { title: state().title, step: app().activeStep() });
e2e.pickProjectToOpen = async () => env.project;
e2e.click("#btn-open");
await e2e.until(() => state().events.length === 3 && app().activeStep() === 2 && app().viewer.image);
await e2e.report("project reopened with fits and names", state().events.every((e) => e.fit && e.targets[0].name === "black") && state().title === "golden flight", { title: state().title, names: state().events.map((e) => e.targets[0].name), fits: state().events.map((e) => !!e.fit) });
await e2e.done(true);
