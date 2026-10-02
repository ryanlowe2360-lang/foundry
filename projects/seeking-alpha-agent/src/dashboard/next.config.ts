import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
  // The service-role key is read only in server code (lib/data.ts); nothing data-related ships to the client.
  env: {},
};

export default nextConfig;
