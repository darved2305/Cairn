import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv } from "vite";

// One image, one deploy path (docs/project/PROJECT.md §6.1): `npm run build` emits a
// fully static bundle that FastAPI mounts itself (console/api.py::_static_dir),
// so there is no second container and no CDN dependency for the app shell.
//
// In development Vite serves on :5173 and proxies /api to a locally-running
// `uvicorn cairn.console.api:app` pointed at the real CockroachDB Cloud
// cluster — same-origin in both modes, so no environment-specific base URL
// ever has to be baked into the bundle.
export default defineConfig(({ mode }) => {
  // `loadEnv` rather than `process.env` so the config typechecks without
  // pulling @types/node into a browser-only project.
  const env = loadEnv(mode, ".", "CAIRN_");
  return {
    plugins: [react(), tailwindcss()],
    server: {
      proxy: {
        "/api": {
          target: env.CAIRN_CONSOLE_API || "http://127.0.0.1:8000",
          changeOrigin: true,
        },
      },
    },
    build: {
      outDir: "dist",
      // The console is one page of panels; a single bundle beats a waterfall
      // of chunks for a judge opening the URL cold.
      chunkSizeWarningLimit: 900,
    },
  };
});
