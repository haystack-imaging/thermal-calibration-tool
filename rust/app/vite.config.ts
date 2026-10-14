import { defineConfig } from "vite";

// Tauri expects a fixed port; `clearScreen: false` keeps the Rust build output visible.
export default defineConfig({
  clearScreen: false,
  server: { host: "127.0.0.1", port: 5173, strictPort: true },   // an address, not "localhost": WebKit may try ::1 first
  envPrefix: ["VITE_", "TAURI_"],
  build: { target: "es2022", sourcemap: true },
});
