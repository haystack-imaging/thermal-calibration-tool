# End-to-end tests of the desktop app

The app can run a JavaScript test script inside its own web view:

```
thermal-calibration-app --e2e-script tests/e2e/flow.js --e2e-env-file env.json --e2e-report report.json [--e2e-timeout 180]
```

Once the page has loaded, the script runs with two objects:

- `window.__e2e` (from the Rust side): `report(name, ok, details)`, `log(text)`, `done(ok)`,
  `wait(ms)`, `until(fn, timeout)`, `$(sel)`, `click(sel)`, `type(sel, text)` (sets the value and fires
  `change`), `key(sel, key)`, `mouse(el, type, x, y)`, and `env` (the JSON from `--e2e-env-file`). Hooks
  `pickImages`, `pickFolder`, `pickProjectToOpen`, `pickProjectToSave`, `pickReportToSave` replace the
  native file dialogs. Uncaught errors, unhandled rejections and `console.error` calls are recorded and
  fail the run.
- `window.__app` (from `src/main.ts`): read access to the state, the selected shot, the active step, the
  pre-flight result and the result rows, the viewer, the Inspect state, and `selectShot`, `showStep`,
  `guidance()`, `toggleInspect()`, `rerender()`, `updateRow(row)` (the in-place update of a result row).

The report is JSON: `{ok, steps, failed, errors, entries: [...]}`; the exit code is 0 only when every step
passed and nothing was logged as an error.

`cargo test` in `app/src-tauri` runs `flow.js` on the golden flight (`python3 rust/tools/make_golden_flight.py`
first) when a display is available. Where the app has to start outside the test's process tree, put a
launcher prefix in `THERMAL_E2E_LAUNCH`, e.g.
`systemd-run --user --wait --collect -q --setenv=DISPLAY=:0 --setenv=WAYLAND_DISPLAY=wayland-0 --setenv=XDG_RUNTIME_DIR=/run/user/1000`.
In CI: `xvfb-run -a cargo test`.
