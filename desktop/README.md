# PhotoOS 桌面壳（Electron）

把 `python -m photoos serve` 起的本机界面装进一个原生窗口。前端与后端都不改，壳只负责进程生命周期、托盘、单实例和几件浏览器做不到的事。

> 构建与分发细节见 [`打包方案.md`](打包方案.md)。

## 开发运行

前提：

- Python 3.12 虚拟环境（按根目录 README「从源码运行」创建）
- Node / pnpm（首次 `pnpm install` 需要联网）

```powershell
pnpm install
pnpm start            # 自己拉起后端（端口被占会自动换一个）
pnpm start:attach     # 连已经在跑的服务（默认 8760），不拉也不杀
pnpm start:inspect    # 带 CDP（9333），配合 tools/cdp.py 截图/取值
```

## 构建绿色包

```powershell
pnpm install
powershell -File build-portable.ps1
# 产物：desktop\_dist\PhotoOS-<版本>-win64.zip
```

- 需要 `desktop\node_modules\electron\dist\electron.exe`（`pnpm install` 后才有）
- 脚本会下载 embeddable Python 3.12 + 运行时依赖，并把后端源码、`resources\app`、`LICENSE`、`首次运行说明.txt` 一起装进 zip
- 产物内置 Python；目标机不需要 Python / Node，解压双击 `PhotoOS.exe` 即可
- 本项目不使用 electron-builder / NSIS 打安装包；绿色 zip 就是分发形态

## 配置

`%APPDATA%\PhotoOS\desktop.json`（可选，不存在就用默认值）：

```json
{
  "keepRunningOnClose": true,
  "python": "C:\\PhotoOS\\.venv\\Scripts\\python.exe",
  "dataRoot": null,
  "projectRoot": null,
  "port": 8760
}
```

- `python` / `projectRoot` 留空 = 自动找：先看打包资源里的 `runtime\python`，再按 `photoos.toml` 向上找项目根，最后用 `PATH` 上的 `python`
- `dataRoot` 留空：开发模式跟项目根；打包版默认 `%APPDATA%\PhotoOS\data`。想共用现有库就显式指过去
- `keepRunningOnClose: false` = 关窗即退出（默认收进托盘）

## 它做了什么

| 能力 | 实现 |
|---|---|
| 拉起 / 等待 / 收走后端 | `lib/backend.js`：探空闲端口、spawn `python -m photoos serve`、轮询 `/api/health`、退出时收进程树 |
| 窗口 | 1440×920、记住位置与最大化、深色底防白闪、单实例 |
| 关窗不中断长任务 | 默认收进托盘；托盘菜单退出 |
| `window.prompt()` | Electron 不支持；壳用 `sendSync` + 一个真输入框窗口补上 |
| 导入源目录 | 输入框旁挂「选目录…」，调系统目录选择器 |
| 外链 | `target="_blank"` 交给系统浏览器 |
| 导出/下载 | 后端把 zip 写到数据目录并调资源管理器；真走下载的给「另存为」 |
| 剪贴板 | 放行 clipboard 权限（request 与 check 两个 handler 都要设） |

## 目录

```text
main.js              窗口、菜单、托盘、权限、prompt 桥
preload.js           只暴露 pickDirectory / promptSync
preload-prompt.js    输入框窗口的 preload
prompt.html          输入框窗口
lib/backend.js       后端进程托管（端口、健康检查、日志、杀进程树）
assets/              图标
tools/cdp.py         CDP 取值/截图
tools/shot_target.py 截某个 CDP 目标
build-portable.ps1   绿色包构建
```

## 已知限制

1. **未签名**：别人机器上首次运行可能被 SmartScreen 拦一下。
2. **AI 不在壳里**：向量/人脸依赖由后端按需安装，壳不碰；没装 AI 时界面会提示缺依赖。
3. **开发模式需要本机 Python**；绿色包内置 Python，最终用户不需要。
4. `prompt` 桥会阻塞渲染线程，与浏览器原生 `prompt` 行为一致。
5. 历史调研文档里引用的 `PHASE1_TECH_PLAN.md` 等过程文档不在公开快照中；那是历史溯源，不是运行依赖。
