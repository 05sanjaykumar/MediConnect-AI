import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // The voice screen is demonstrated through an ngrok tunnel so anyone can
  // open the link on their own phone — no app, no credentials. In dev, Next
  // blocks cross-origin requests for its own resources, so the page rendered
  // but never hydrated: the mic button appeared and did nothing, because no
  // click handler was ever attached. Allowing the tunnel hosts fixes that.
  // A free ngrok URL changes on every restart, hence the wildcards.
  allowedDevOrigins: ["*.ngrok-free.app", "*.ngrok.app", "*.ngrok.io"],
};

export default nextConfig;
