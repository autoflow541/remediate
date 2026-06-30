import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Relative base so the built site works whether it's served from a domain root
// or a subdirectory (e.g. a DreamHost shared-hosting folder).
export default defineConfig({
  base: "./",
  plugins: [react()],
  build: {
    outDir: "dist",
    // pdf.js is large; keep it as its own chunk for caching.
    chunkSizeWarningLimit: 1200,
    rollupOptions: {
      output: {
        // Stable filenames — no content-hash — so deploys never break
        entryFileNames: "studio.js",
        chunkFileNames: "studio-[name].js",
        assetFileNames: "assets/[name][extname]",
      },
    },
  },
});
