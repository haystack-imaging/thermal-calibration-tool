// Smoke test of an installed or bundled app (the installer workflow runs it on every system): the
// bundled front-end loads, the Rust side answers, and with env.demo the bundled demo flight opens.
//   thermal-calibration-app --e2e-script tests/e2e/smoke.js --e2e-env-file env.json --e2e-report report.json
const e2e = window.__e2e;
const env = e2e.env || {};
await e2e.until(() => window.__app && window.__app.state(), 30000);
const st = window.__app.state();
await e2e.report("app loaded", st.events.length === 0 && window.__app.activeStep() === 1, { version: st.version });
await e2e.report("rail drawn", document.querySelectorAll("#rail .step-header").length >= 3);
if (env.demo) {
  e2e.click("#btn-open");
  e2e.click("#btn-open-demo");
  await e2e.until(() => window.__app.state().events.length === 2 && window.__app.state().events.every((e) => e.fit), 60000);
  const s = window.__app.state();
  await e2e.report("the bundled demo flight opens", s.title === "Demo flight (synthetic)" && /demo-flight[\\/]survey$/.test(s.survey.folder), s.survey);
}
await e2e.done(true);
