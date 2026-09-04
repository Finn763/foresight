# BRANDING — foresight 品牌说明

## 这是什么

`shell/pi/` 是 **Pi coding agent** 的 fork，重命名为 **foresight**，作为本项目的 AI 编码/操作外壳（CLI shell）。

## Fork 来源

- 上游仓库：https://github.com/earendil-works/pi （MIT License，作者 Mario Zechner）
- Fork 基线版本：**v0.84.4**（2026-09-04 从 v0.84.1 同步，基线 commit `b79e4cc83` Release v0.84.4；原基线 v0.84.1/`40a3d85`）
- 上游全局安装对照：`npm i -g @earendil-works/pi-coding-agent` = 0.84.4
- 本 fork 目录：`shell/pi/`，包名 `@foresight/foresight-agent`

## 改名原理（为什么只改 package.json 就够了）

Pi 的 `packages/coding-agent/src/config.ts`（约 467–491 行）从包自身的 package.json 读取 `piConfig` 字段派生品牌常量：

```ts
const piConfigName: string | undefined = pkg.piConfig?.name;
export const APP_NAME: string = piConfigName || "pi";
export const APP_TITLE: string = piConfigName ? APP_NAME : "π";
export const CONFIG_DIR_NAME: string = pkg.piConfig?.configDir || ".pi";
```

因此只需要改 `packages/coding-agent/package.json` 三处，源码其余部分自动跟随：

| 改动 | 原值 | 新值 | 效果 |
|---|---|---|---|
| `name` | `@earendil-works/pi-coding-agent` | `@foresight/foresight-agent` | 包名 |
| `bin` | `{ "pi": "dist/cli.js" }` | `{ "foresight": "dist/bundle/cli.js" }` | 命令 `pi` → `foresight`（注：上游 0.84.2+ 把入口搬到 `dist/bundle/cli.js`，同步时跟随上游路径） |
| `piConfig` | `{ "configDir": ".pi" }` | `{ "name": "foresight", "configDir": ".foresight" }` | TUI logo/欢迎语 → Foresight；配置目录 `.pi/` → `.foresight/`；env 前缀 `PI_CODING_AGENT_DIR` → `FORESIGHT_CODING_AGENT_DIR` |

注：`piConfig.name` 未设置时 `APP_TITLE` 为 `π`；设置后 APP_NAME/APP_TITLE 均为 `foresight`。`isOfficialDistribution()` 返回 false 只跳过首次引导向导，功能不受影响。

## 已做的改动（相对上游）

共 2 个文件：

1. `packages/coding-agent/package.json`：上述三处改名（name/bin/piConfig）。
2. `packages/coding-agent/src/core/extensions/loader.ts`：`createJiti(import.meta.url, {...})` 选项加 `fsCache: false`（2026-08-15 补丁）。
   - 原因：jiti 的磁盘缓存（`%LOCALAPPDATA%\Temp\jiti\`）不按源文件 mtime 失效——改 `.foresight/extensions/*.ts` 后 `/reload` 甚至重启仍跑旧编译产物（实测：改坏脚本路径后旧代码仍生效，对抗测试确认）。
   - 位置：`loadExtensionModule()` 内，紧邻已有的 `moduleCache: false`；同款代码在构建产物 `dist/core/extensions/loader.js`（勿直接改 dist，改 src 后重新 build）。
   - 验证方法：改扩展文件任一字符串（如工具 description 加标记）→ 重启或 `/reload` → 交互模式问模型复述该 description / 直接读加载器输出，新字符串应立即出现；`%LOCALAPPDATA%\Temp\jiti\` 不应再新增扩展缓存文件（旧缓存残留文件可整目录清空）。
   - 影响面：仅扩展加载性能（无磁盘缓存，每次冷加载重编译扩展），扩展体量小、可忽略；内存在进程 moduleCache:false 本来就不缓存。

构建产物为 `packages/coding-agent/dist/bundle/cli.js`（0.84.2+ 上游改打包路径；npm link 后全局命令 `foresight` 指向它）。

## 已知残留（不改，无功能影响）

- `packages/coding-agent/src/core/system-prompt.ts` 内置的 pi 自述段落
- `packages/coding-agent/src/core/provider-attribution.ts` 里 OpenRouter header 的 `"pi"` 字符串

## 维护方式

本 fork 改动很小（package.json 三处 + loader.ts 一处 `fsCache: false`），与上游冲突面几乎为零：

> 2026-08-16 起 shell/pi 已 git 化：分支 `foresight-fork` = 上游基线 `40a3d85` + 3 个补丁
> commit（rebrand / fsCache / lockfile 同步），`origin` 与 `upstream` 均指向
> https://github.com/earendil-works/pi 且 push 均设 `no_push`（防误推）。以下维护方式
> 按 git 流程重写。

1. **拉上游新版本**：`cd shell/pi && git fetch upstream && git rebase <版本tag>`（对 tag 不对 main，锁定发布版；rebase 前确认工作区干净；补丁 commit 若与上游冲突，先查上游是否已自带等价修复再决定去留）
2. **重打改名补丁**：合并后重新应用上述改动（可用 `git diff` 对照）——① package.json 三处（name/bin/piConfig，见「改名原理」表）；② `src/core/extensions/loader.ts` 的 `createJiti` 选项加 `fsCache: false`（见「已做的改动」第 2 条，注意上游可能已自带等价修复，打补丁前先查上游该处是否已有 fsCache 相关代码）；若上游 `config.ts` 的 piConfig 派生逻辑变化，检查本文件「改名原理」是否仍成立
3. **重新构建**：`cd packages/coding-agent && npm install && npm run build && npm link`（tsgo 在 monorepo 根 `shell/pi/node_modules/.bin`，build 后确认 `dist/bundle/cli.js` 仍是可执行入口、bundle 产物含 `fsCache:!1`）；lockfile 冲突不要手合，跳过旧 lockfile commit，用 `npm install` 重生成
4. 上游发布新版本时，先在全局 `npm i -g @earendil-works/pi-coding-agent` 验证可用性，再决定是否跟进 fork

## 许可证

MIT License 与 NOTICE 原样保留（`shell/pi/LICENSE`），fork 及再分发须保留上游版权声明。
