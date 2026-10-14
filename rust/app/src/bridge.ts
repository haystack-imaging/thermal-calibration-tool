// The one place that talks to the Rust side. In a plain browser (vite dev without Tauri) the calls go
// to the in-browser mock engine instead, so the interface can be developed and tested on its own.

export function isTauri(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

type Invoke = (cmd: string, args?: Record<string, unknown>) => Promise<unknown>;
let tauriInvoke: Invoke | null = null;

export async function invoke<R>(cmd: string, args: Record<string, unknown> = {}): Promise<R> {
  if (isTauri()) {
    if (!tauriInvoke) {
      const m = await import("@tauri-apps/api/core");
      tauriInvoke = m.invoke as Invoke;
    }
    return tauriInvoke(cmd, args) as Promise<R>;
  }
  const { mockInvoke } = await import("./mock");
  return mockInvoke(cmd, args) as Promise<R>;
}

export async function listen(event: string, handler: (payload: unknown) => void): Promise<() => void> {
  if (isTauri()) {
    const m = await import("@tauri-apps/api/event");
    return m.listen(event, (e) => handler(e.payload));
  }
  const { mockListen } = await import("./mock");
  return mockListen(event, handler);
}

const IMAGE_FILTER = [{ name: "Images", extensions: ["tif", "tiff", "png", "jpg", "jpeg"] }];

export async function pickImages(): Promise<string[]> {
  if (isTauri()) {
    const { open } = await import("@tauri-apps/plugin-dialog");
    const r = await open({ multiple: true, title: "Calibration images (single-band temperature TIFFs)", filters: IMAGE_FILTER });
    if (!r) return [];
    return Array.isArray(r) ? r : [r];
  }
  const { mockPickImages } = await import("./mock");
  return mockPickImages();
}

export async function pickFolder(title: string): Promise<string | null> {
  if (isTauri()) {
    const { open } = await import("@tauri-apps/plugin-dialog");
    const r = await open({ directory: true, title });
    return typeof r === "string" ? r : null;
  }
  const { mockPickFolder } = await import("./mock");
  return mockPickFolder(title);
}

export async function pickProjectToOpen(): Promise<string | null> {
  if (isTauri()) {
    const { open } = await import("@tauri-apps/plugin-dialog");
    const r = await open({ title: "Open project", filters: [{ name: "Project", extensions: ["json"] }] });
    return typeof r === "string" ? r : null;
  }
  return "demo.calib.json";
}

export async function pickProjectToSave(defaultName: string): Promise<string | null> {
  if (isTauri()) {
    const { save } = await import("@tauri-apps/plugin-dialog");
    return save({ title: "Save project", defaultPath: defaultName, filters: [{ name: "Project", extensions: ["json"] }] });
  }
  return defaultName;
}

export async function pickReportToSave(defaultName: string): Promise<string | null> {
  if (isTauri()) {
    const { save } = await import("@tauri-apps/plugin-dialog");
    return save({ title: "Export fit report", defaultPath: defaultName, filters: [{ name: "HTML report", extensions: ["html"] }] });
  }
  return defaultName;
}
