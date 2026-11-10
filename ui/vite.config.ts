import { defineConfig } from "vite";
import solid from "vite-plugin-solid";

export default defineConfig({
  plugins: [solid()],
  clearScreen: false,
  optimizeDeps: {
    include: [
      "katex",
      "prismjs",
      "prismjs/components/prism-python",
      "prismjs/components/prism-typescript",
      "prismjs/components/prism-rust",
      "prismjs/components/prism-bash",
      "prismjs/components/prism-json",
      "prismjs/components/prism-yaml",
      "prismjs/components/prism-sql",
      "prismjs/components/prism-go",
      "prismjs/components/prism-c",
      "prismjs/components/prism-cpp",
      "prismjs/components/prism-toml",
      "prismjs/components/prism-diff",
      "prismjs/components/prism-nix"
    ]
  },
  server: {
    port: 1435,
    strictPort: true
  },
  build: {
    target: "safari15",
    manifest: true
  }
});
