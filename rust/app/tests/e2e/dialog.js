// Does "Add images…" reach a native file dialog? Calls the app's own pickImages (the plugin import,
// the permission and the dialog itself); a helper outside closes the dialog with Escape, so the call
// returns an empty list. A plugin or permission problem rejects instead and fails the step.
const e2e = window.__e2e;
await e2e.until(() => window.__app && window.__app.state(), 20000);
let outcome;
try {
  const r = await window.__app.pickImages();
  outcome = { returned: r };
} catch (e) {
  outcome = { error: String(e && (e.message || e)) };
}
await e2e.report("the file dialog opens and returns", !("error" in outcome) && Array.isArray(outcome.returned), outcome);
await e2e.done(!("error" in outcome));
