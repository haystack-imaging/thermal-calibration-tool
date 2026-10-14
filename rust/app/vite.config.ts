import { defineConfig } from "vite";

// Tauri expects a fixed port; `clearScreen: false` keeps the Rust build output visible.
export default defineConfig({
  clearScreen: false,
  server: { port: 5173, strictPort: true },
  envPrefix: ["VITE_", "TAURI_"],
  build: { target: "es2022", sourcemap: true },
});
