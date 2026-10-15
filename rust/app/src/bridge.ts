// The one place that talks to the Rust side. In a plain browser (vite dev without Tauri) the calls go
// to the in-browser mock engine instead, so the interface can be developed and tested on its own.

/** Hooks a test script (`--e2e-script`) can set to stand in for the native file dialogs. */
export interface E2eHooks {
  pickImages?: () => Promise<string[]>;
  pickFolder?: (title: string) => Promise<string | null>;
  pickProjectToOpen?: () => Promise<string | null>;
  pickProjectToSave?: (defaultName: string) => Promise<string | null>;
  pickReportToSave?: (defaultName: string) => Promise<string | null>;
  pickCsvToSave?: (defaultName: string) => Promise<string | null>;
  env?: Record<string, unknown>;
}

declare global {
  interface Window {
    __e2e?: E2eHooks;
  }
}

export function isTauri(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

// Static imports on purpose: a first *dynamic* import of a package in Vite's dev mode makes the dev
// server re-optimise its dependencies and reload the page, which swallowed the first "Add images…".
import { invoke as tauriInvoke } from "@tauri-apps/api/core";
import { listen as tauriListen } from "@tauri-apps/api/event";
import { open as dialogOpen, save as dialogSave } from "@tauri-apps/plugin-dialog";

export async function invoke<R>(cmd: string, args: Record<string, unknown> = {}): Promise<R> {
  if (isTauri()) {
    return tauriInvoke(cmd, args) as Promise<R>;
  }
  const { mockInvoke } = await import("./mock");
  return mockInvoke(cmd, args) as Promise<R>;
}

export async function listen(event: string, handler: (payload: unknown) => void): Promise<() => void> {
  if (isTauri()) {
    return tauriListen(event, (e) => handler(e.payload));
  }
  const { mockListen } = await import("./mock");
  return mockListen(event, handler);
}

const IMAGE_FILTER = [{ name: "Images", extensions: ["tif", "tiff", "png", "jpg", "jpeg"] }];

export async function pickImages(): Promise<string[]> {
  if (window.__e2e?.pickImages) return window.__e2e.pickImages();
  if (isTauri()) {
    const r = await dialogOpen({ multiple: true, title: "Calibration images (single-band temperature TIFFs)", filters: IMAGE_FILTER });
    if (!r) return [];
    return Array.isArray(r) ? r : [r];
  }
  const { mockPickImages } = await import("./mock");
  return mockPickImages();
}

export async function pickFolder(title: string): Promise<string | null> {
  if (window.__e2e?.pickFolder) return window.__e2e.pickFolder(title);
  if (isTauri()) {
    const r = await dialogOpen({ directory: true, title });
    return typeof r === "string" ? r : null;
  }
  const { mockPickFolder } = await import("./mock");
  return mockPickFolder(title);
}

export async function pickProjectToOpen(): Promise<string | null> {
  if (window.__e2e?.pickProjectToOpen) return window.__e2e.pickProjectToOpen();
  if (isTauri()) {
    const r = await dialogOpen({ title: "Open project", filters: [{ name: "Project", extensions: ["json"] }] });
    return typeof r === "string" ? r : null;
  }
  return "demo.calib.json";
}

export async function pickProjectToSave(defaultName: string): Promise<string | null> {
  if (window.__e2e?.pickProjectToSave) return window.__e2e.pickProjectToSave(defaultName);
  if (isTauri()) {
    return dialogSave({ title: "Save project", defaultPath: defaultName, filters: [{ name: "Project", extensions: ["json"] }] });
  }
  return defaultName;
}

export async function pickCsvToSave(defaultName: string): Promise<string | null> {
  if (window.__e2e?.pickCsvToSave) return window.__e2e.pickCsvToSave(defaultName);
  if (isTauri()) {
    return dialogSave({ title: "Save table", defaultPath: defaultName, filters: [{ name: "CSV", extensions: ["csv"] }] });
  }
  return defaultName;
}

export async function pickReportToSave(defaultName: string): Promise<string | null> {
  if (window.__e2e?.pickReportToSave) return window.__e2e.pickReportToSave(defaultName);
  if (isTauri()) {
    return dialogSave({ title: "Export fit report", defaultPath: defaultName, filters: [{ name: "HTML report", extensions: ["html"] }] });
  }
  return defaultName;
}
