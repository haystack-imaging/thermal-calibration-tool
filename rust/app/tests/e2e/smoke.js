// Smoke test of a release build (the installer workflow runs it on every system): the bundled
// front-end loads in the web view and the Rust side answers.
//   thermal-calibration-app --e2e-script tests/e2e/smoke.js --e2e-report report.json
const e2e = window.__e2e;
await e2e.until(() => window.__app && window.__app.state(), 30000);
const st = window.__app.state();
await e2e.report("app loaded", st.events.length === 0 && window.__app.activeStep() === 1, { version: st.version });
await e2e.report("rail drawn", document.querySelectorAll("#rail .step-header").length >= 3);
await e2e.done(true);
