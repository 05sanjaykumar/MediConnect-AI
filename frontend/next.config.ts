import type { NextConfig } from "next";

const BACKEND = process.env.BACKEND_ORIGIN ?? "http://localhost:8000";

const nextConfig: NextConfig = {
  // The voice screen is demonstrated through an ngrok tunnel so anyone can
  // open the link on their own phone — no app, no credentials. In dev, Next
  // blocks cross-origin requests for its own resources, so the page rendered
  // but never hydrated: the mic button appeared and did nothing, because no
  // click handler was ever attached. Allowing the tunnel hosts fixes that.
  // A free ngrok URL changes on every restart, hence the wildcards.
  allowedDevOrigins: ["*.ngrok-free.app", "*.ngrok.app", "*.ngrok.io"],

  // Serve the API from this same origin. Pointing the browser straight at a
  // second ngrok host cost us twice: the CORS preflight failed because the
  // allowed origin no longer matched, and ngrok served its "you are about to
  // visit" page to any request carrying a browser user-agent — so fetch got
  // HTML instead of JSON and reported the server as down. One origin means no
  // CORS and one interstitial to click through.
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${BACKEND}/api/:path*` }];
  },
};

export default nextConfig;
