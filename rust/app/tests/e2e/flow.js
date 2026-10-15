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
await e2e.report("open step 2 from the card", /Draw T1 on shot 1 of 3/.test(app().guidance().targets.head));
e2e.click("#btn-add-target");
await e2e.until(() => state().events[0].targets.length === 3);
await e2e.report("third target on every shot", state().events.every((e) => e.targets.length === 3));

const drawPolygon = async (target, corners) => {
  e2e.click(`[data-draw="${target}"]`);
  await e2e.until(() => app().drawing());
  const canvas = e2e.$("#viewer");
  const r = canvas.getBoundingClientRect();
  for (const [x, y] of corners) {
    const [cx, cy] = app().viewer.toCanvas(x + 0.5, y + 0.5);
    e2e.mouse(canvas, "mousedown", r.left + cx, r.top + cy);
  }
  await e2e.until(() => !app().drawing() && state().events[app().current()].targets[target].polygon.length === 4);
};
for (let i = 0; i < 3; i++) {
  await app().selectShot(i);
  await e2e.until(() => app().current() === i && app().viewer.image);
  const shot = env.shots[i];
  for (let j = 0; j < 3; j++) {
    await drawPolygon(j, shot.polygons[j]);
    const t = state().events[i].targets[j];
    await e2e.report(`shot ${i + 1}: polygon T${j + 1}`, t.polygon.length === 4 && t.pixels > 1000 && t.inner.length === 1, { mean: t.mean, pixels: t.pixels });
    if (j === 0) await e2e.report(`shot ${i + 1}: reading field focused after the 4th corner`, document.activeElement === document.querySelector(`[data-gt="${j}"]`));
    e2e.type(`[data-gt="${j}"]`, String(shot.gun[j]).replace(".", ","));
    await e2e.until(() => state().events[i].targets[j].ground_truth !== null);
  }
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
e2e.click("#btn-run");
await e2e.wait(300);
if (!document.querySelector("#modal").hidden) e2e.click("#modal button.accent");
await e2e.until(() => state().last_output, 60000);
await e2e.report("batch done", app().rows().length === 6 && app().rows().every((r) => r.status === "ok") && /Done: 6 ok/.test(e2e.$("#process-view").textContent), app().rows().map((r) => r.input_filename + " " + r.slope_m));
await e2e.report("done guidance offers the output folder", /Done\./.test(app().guidance().process.head));

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
