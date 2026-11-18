import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
import path from "path";

export default defineConfig({
  plugins: [vue()],
  build: {
    outDir: path.resolve(__dirname, "../meta_omnichannel/public/frontend"),
    emptyOutDir: true,
    rollupOptions: {
      input: path.resolve(__dirname, "index.html"),
      output: {
        entryFileNames: "omnichannel.js",
        assetFileNames: (assetInfo) => {
          if (assetInfo.name && assetInfo.name.endsWith(".css")) {
            return "omnichannel.css";
          }
          return "assets/[name][extname]";
        },
      },
    },
  },
  server: {
    port: 8081,
  },
});
