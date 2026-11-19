import type { NextConfig } from "next";

const api = (process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000").replace(/\/$/, "");
// Websites allowed to show the chat widget in a frame (the firm's own site), space-separated.
const parents = (process.env.WIDGET_PARENT_ORIGINS || "").split(/[\s,]+/).filter(Boolean).join(" ");
const dev = process.env.NODE_ENV !== "production";

const base = [
  "default-src 'self'",
  `script-src 'self' 'unsafe-inline'${dev ? " 'unsafe-eval'" : ""}`,
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data:",
  `connect-src 'self' ${api} ${api.replace(/^http/, "ws")}`,     // ws(s): browser voice calls
  "base-uri 'self'",
  "form-action 'self'",
];

const common = [
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  ...(dev ? [] : [{ key: "Strict-Transport-Security", value: "max-age=31536000; includeSubDomains" }]),
];

const nextConfig: NextConfig = {
  poweredByHeader: false,
  devIndicators: false,
  async headers() {
    return [
      // Every page except the widget refuses to be framed.
      { source: "/((?!widget).*)", headers: [...common,
        { key: "Content-Security-Policy", value: [...base, "frame-ancestors 'none'"].join("; ") }] },
      // The widget may be framed by this site and the listed websites only.
      { source: "/widget", headers: [...common,
        { key: "Content-Security-Policy", value: [...base, `frame-ancestors 'self' ${parents}`.trim()].join("; ") }] },
      // embed.js is loaded by other websites.
      { source: "/embed.js", headers: [{ key: "Cache-Control", value: "public, max-age=300" },
        { key: "Access-Control-Allow-Origin", value: "*" }] },
    ];
  },
};

export default nextConfig;
