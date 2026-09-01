#!/usr/bin/env node
// npm 启动器：按平台选择二进制并透传 stdio/退出码（esbuild 式 optionalDependencies 分发）。
const { spawnSync } = require("child_process");
const path = require("path");

const SUPPORTED = {
  win32: { x64: "win32-x64" },
  darwin: { x64: "darwin-x64", arm64: "darwin-arm64" },
  linux: { x64: "linux-x64" },
};

function resolveBinary() {
  const key = (SUPPORTED[process.platform] || {})[process.arch];
  if (!key) {
    console.error(`foresight: unsupported platform ${process.platform}/${process.arch}`);
    process.exit(1);
  }
  const pkg = `foresight-agent-${key}`;
  const target = path.join(pkg, "bin", `foresight${process.platform === "win32" ? ".exe" : ""}`);
  try {
    return require.resolve(target);
  } catch {
    console.error(
      `foresight: missing platform package "${pkg}". ` +
        `Run \`npm install -g ${pkg}\` manually (likely a proxy/mirror issue).`
    );
    process.exit(1);
  }
}

const r = spawnSync(resolveBinary(), process.argv.slice(2), {
  stdio: "inherit",
  shell: false,
});
process.exit(r.status === null ? 1 : r.status);
