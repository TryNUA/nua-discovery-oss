import type { NextConfig } from "next";

// Static export: `pnpm build` writes out/, and scripts/copy-out.mjs copies it to nua/setup/static/,
// which `nua setup` serves on 127.0.0.1. No server features (no API routes, no image optimizer).
const nextConfig: NextConfig = {
  output: "export",
  images: { unoptimized: true },
  poweredByHeader: false,
};

export default nextConfig;
