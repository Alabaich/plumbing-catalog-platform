import { defineConfig } from "vite";
import { reactRouter } from "@react-router/dev/vite";
import tsconfigPaths from "vite-tsconfig-paths";

export default defineConfig({
  server: {
    port: 5173,
    // Allowed host for development via a tunnel (e.g. ngrok) — set DEV_TUNNEL_HOST
    allowedHosts: process.env.DEV_TUNNEL_HOST ? [process.env.DEV_TUNNEL_HOST] : [],
  },
  plugins: [reactRouter(), tsconfigPaths()],
});
