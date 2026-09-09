/** @type {import('next').NextConfig} */
module.exports = {
  output: "standalone",
  reactStrictMode: true,
  // In the container the browser talks to Caddy, which proxies /api and /ws to
  // the backend on the same origin, so cookies work without CORS gymnastics.
};
