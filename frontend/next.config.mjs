/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // Lets automated E2E builds (which bake in a different API URL) use a separate
  // output directory without overwriting the normal `.next` build.
  distDir: process.env.NEXT_DIST_DIR || ".next",
};

export default nextConfig;
