import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In dev, the Vite server proxies API calls to the FastAPI backend.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: { "/api": process.env.AI_FACTORY_API ?? "http://127.0.0.1:8700" },
  },
});
