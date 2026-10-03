import type { NextConfig } from "next";

const backendUrl = process.env.BACKEND_URL ?? "http://127.0.0.1:8000";

const nextConfig: NextConfig = {
  poweredByHeader: false, // don't advertise the framework
  // The browser only ever talks to this origin; API calls are proxied to FastAPI,
  // so the session cookie stays first-party and no CORS setup is needed.
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${backendUrl}/api/:path*` }];
  },
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Content-Type-Options", value: "nosniff" },
          // No other site may show these pages in a frame (clickjacking).
          { key: "X-Frame-Options", value: "DENY" },
          { key: "Referrer-Policy", value: "same-origin" },
          { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=()" },
        ],
      },
    ];
  },
  experimental: {
    // The proxy buffers request bodies and silently truncates them at 10 MB by default,
    // which breaks uploads. The UI sends one file per request and the backend caps files
    // at 25 MB, so 30 MB leaves room for multipart overhead without buffering whole batches.
    proxyClientMaxBodySize: "30mb",
  },
};

export default nextConfig;
