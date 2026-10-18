# 能不能用 Electron 把 PhotoOS 包一层？

调研 + 可运行原型 · 2026-10-17

> 只想看结论与下一步：读 [`结论-Electron桌面壳.md`](结论-Electron桌面壳.md)。
> 本文是它的证据部分 —— 实测数据、逐项兼容性核验、踩坑记录。
>
> 公开快照说明：文中引用的 `PHASE1_TECH_PLAN.md` 等过程文档不在公开仓库；这是历史溯源，不是运行依赖。
> 文中三张截图（桌面窗口、输入框、维护页）未随公开快照提供；开发目录里的 `_shots/` 已忽略。

---

## 0. 结论

**能，而且比预想的省事。** PhotoOS 的形态（本机 FastAPI 服务 + 零构建前端）几乎是为"套壳"准备的：
前端只有一份、同源 HTTP、没有 `file://` 路径问题、没有 Service Worker、没有 npm 构建链。
Electron 在这里不是"重写一个客户端"，而是**换一个打开方式**：把 `http://127.0.0.1:8760` 放进一个
有图标、有托盘、能自己拉起后端的窗口里。

三条路线的判断：

| 路线 | 体积（解压后） | 工期 | 建议 |
|---|---|---|---|
| **A. 薄壳 + 本机现有 Python 环境** | **369 MB 实测** | 壳已成型，接入半天 | ✅ **自用就选这个**，本仓库就是 A |
| B. 薄壳 + 自带 Python（免装 Python） | ≈ 600 MB | 2–3 天 | 要发给别人、且对方没 Python 时再做 |
| C. 连 AI 依赖一起塞 | ≈ 6.5 GB | 不建议 | ❌ AI 继续用 `ai_python` 外挂 |

**唯一的真不兼容点是 `window.prompt()`**（Electron 明确不支持，实测抛
`Error: prompt() is not supported.`；PhotoOS 里有 12 处调用，其中 10 处是 `prompt(...)`、2 处是
`window.prompt(...)`）。本原型用 `sendSync` + 一个真输入框窗口把它补上了，
**PhotoOS 的源码一行没改**，并且已端到端验证"新建主题"这类流程可用。

需要提前说清楚的是：PhotoOS 自己的技术计划里**明确决定过不引入 Electron**
（`PHASE1_TECH_PLAN.md` §「桌面壳 | 不引入 Electron/Tauri | 浏览器访问 127.0.0.1，减少迁移面」）。
那次决定的理由（减少迁移面、零构建）今天依然成立 —— 所以这个壳被放在 `C:\PhotoOS\desktop\`，
**不进入 Python 包、不进入 pytest、不引入 npm 到主仓库**，用不用都不影响浏览器版。

---

## 1. 被包的东西是什么（实测）

| 项目 | 实测值 |
|---|---|
| 服务 | `uvicorn`（Starlette / FastAPI 0.143），`127.0.0.1:8760`，单进程 |
| 启动方式 | `C:\PhotoOS\start-photoos.cmd` → `python -m photoos serve --host 127.0.0.1 --port 8760` |
| 健康检查 | `GET /api/health` → `{"status":"ok","schema_version":9,"ui_build":"d97a1450",…}` |
| 前端 | 25 个文件 / 0.7 MB，**零构建**：原生 ES module + 7 张 CSS + 4 个本地 woff2 |
| 数据 | `data/photoos.db` 214 MB（21,400 张照片、6,368 张脸、3 个主题） |
| 派生数据 | 缩略图 0.2 GB、预览 3.5 GB、备份 1.5 GB、AI 缓存 2.0 GB |
| Python 环境 | 产品 venv 229 MB / 5,021 文件；AI venv **3.7 GB**（另一套） |
| 依赖 | 12 个基础包，全是 cp312 win_amd64 wheel（rawpy / pillow-heif / imageio-ffmpeg 带原生 DLL） |
| 现有交付 | `photoos-0.1.0-py3-none-any.whl` + `install.ps1` → `%LOCALAPPDATA%\PhotoOS\{app,data}` + `.cmd` 启动器 + 浏览器 |

关键点：**照片原片永远在外置盘**，服务只读索引；任意时刻拔掉照片盘，界面仍然可用。
这条设计让"桌面壳"这件事变得很轻 —— 壳不需要碰文件、不需要相册权限、不需要文件监听。

---

## 2. 为什么这一层几乎是"免费"的

Electron 常见的坑，PhotoOS 一个都没踩：

| 常见坑 | 为什么这里不成立 |
|---|---|
| `file://` 下的 CORS / fetch 限制 | 页面从 `http://127.0.0.1:<port>` 加载，所有 `/api/*` 同源 |
| 需要把前端打进 asar 并改资源路径 | 前端由 Python 服务提供，壳只给一个 URL |
| 需要 IPC 转发业务请求 | 不需要。页面照旧 `fetch`，壳完全不参与 |
| 需要处理路由/刷新/深链 | 单页 + `#` 视图切换，没有 history 路由 |
| 需要 Node 集成 | 不需要。`nodeIntegration: false` + `contextIsolation: true` + `sandbox: true` |

壳真正要写的只有**生命周期**：挑端口 → 拉后端 → 等健康 → 开窗 → 退出时收尸。
这也是为什么 `main.js` 有 400 行而不是 40 行 —— 难的从来不是 `loadURL`。

---

## 3. 兼容性逐项核验（全部在本机实测）

| 能力 | PhotoOS 现状 | Electron 下 | 处理 |
|---|---|---|---|
| 页面与静态资源 | FastAPI 挂 `/static`，`no-cache` + ETag | ✅ 完全一致 | 无 |
| `/api/*` 全部接口 | 同源 `fetch` | ✅ | 无 |
| 缩略图 / 预览 / 视频 | `<img>` / `<video>` 指向 `/api/assets/{id}/thumb\|preview\|view` | ✅ 同一套 Chromium | 无 |
| `navigator.clipboard.writeText`（复制路径） | 用了 2 处 | ⚠️ 首版报 `NotAllowedError: Write permission denied` | 放行剪贴板权限，**且 request 与 check 两个 handler 都要设**（只设 request 不够） |
| `window.prompt()` | **12 处**（人物命名、新建/重命名主题、动作参数…） | ❌ `Error: prompt() is not supported.` | 壳内 `sendSync` + 输入框窗口桥接（见 §4.3） |
| `confirm()` / `alert()` | 十余处 | ✅ Chromium 原生对话框（样式朴素） | 可保留；前端若统一弹窗再替换 |
| `target="_blank"`（"用浏览器打开原图"） | `index.html:530` | ⚠️ 默认会开新 Electron 窗口 | `setWindowOpenHandler` → `shell.openExternal`，壳里不开新窗 |
| "在资源管理器里显示" | `POST /api/assets/{id}/reveal` | ✅ **服务端**执行 `explorer /select,` | 无（这正是本项目的聪明之处） |
| 导出 zip / 打包原片 | 服务端写 `data/exports/` 再 reveal | ✅ | 无 |
| 真走 HTTP 下载的附件 | `/view` 对不可渲染类型带 `attachment` | ⚠️ 默认静默落盘 | `will-download` → 「另存为」 |
| `localStorage` | 只用于 `photoosDebug` 排查开关 | ✅ | 注意端口变了 origin 也变（无实际影响） |
| 导入源目录 | `<input type="text">`，**手输路径** | 原样可用 | 壳额外挂「选目录…」按钮，调系统目录选择器 |
| 深色界面 / 设计令牌 | `--ink-stage #07090f` | ✅ | 窗口 `backgroundColor` 取同色，避免白闪 |

**结论：业务代码零改动；需要壳补的只有 3 处（prompt、外链、目录选择），外加 2 个权限/下载策略。**

---

## 4. 原型：已经跑通的东西

目录：`C:\PhotoOS\desktop\`（不进入 Python 包）。启动：双击 `start-photoos-desktop.cmd`（不需要 Node）。



### 4.1 两种模式

- **自己拉后端（默认）**：探空闲端口 → `spawn python -m photoos serve` → 轮询 `/api/health` → 开窗；
  退出时 `taskkill /T` 收掉整棵进程树（venv 的 `python.exe` 在 Windows 上会再起一个 base 解释器子进程，
  不 `/T` 就留孤儿）。
- **连已在跑的服务（`--attach`）**：只开窗，不拉也不杀。开发时用这个，改 Python 重启服务、窗口刷新即可。

### 4.2 实测数据

| 指标 | 实测 |
|---|---|
| 后端冷启动 → `/api/health` 就绪 | **1.34 s** |
| Electron 进程启动 → 页面加载完成 | **1.49 s** |
| 端口冲突处理 | 8760 被浏览器版占着 → 自动改用 60953 / 62982 / 51503（能同时开，但同一份库不建议，见 §7） |
| 退出清理 | `window.close()` → 日志 `停止后端` → 无残留进程、端口释放 |
| 内存 | Electron 4 个进程共 **333 MB**；Python 服务 56 MB（工作集）——合计约 **390 MB** |
| Electron 本体 | `node_modules/electron/dist` **368 MB**（win-x64，44.9.0）；`node_modules` 共 451 MB |
| 免安装版（`_dist\portable\PhotoOS.exe`） | **369 MB**（= Electron 368 + 壳 0.4），双击即可运行；已实测起窗、连后端、显示界面 |

打开真实库（21,340 张）时，滚动手感、图片加载与浏览器版没有可感知差异 —— 同一套 Chromium。

### 4.3 `window.prompt` 桥：为什么能"零改前端"

Electron 不支持 prompt，但支持 `ipcRenderer.sendSync`。关键发现是：
**`event.returnValue` 可以异步赋值** —— 主进程收到同步消息后先弹输入框窗口，用户点确定时再赋值，
渲染线程会一直阻塞等待（这正是原生 prompt 的语义）。于是：

```
页面 prompt(msg, default)
  → preload(隔离世界) api.promptSync → ipcRenderer.sendSync
  → 主进程 ipcMain.on('photoos:prompt-sync') → 弹出 prompt.html（模态）
  → 用户确定 → event.returnValue = 值 → 页面拿到字符串
```



端到端验证：在「主题」页点「新建主题」→ 两次 prompt（名称、描述）→ 数据库新增 1 条主题。
**这条链路没有改 PhotoOS 的任何一行代码。**

代价与原生一致：调用期间页面线程阻塞（浏览器里 prompt 也这样）。如果以后想更好看，
可以把这 12 处换成应用内异步弹窗 —— 那是前端改动，与是否用 Electron 无关，建议单开一次提交。

### 4.4 顺手补的两件"浏览器做不到"

- **导入目录选择**：在 `#import-source` 下面挂一个「选目录…」按钮（见下图），调 `dialog.showOpenDialog`，
  写回输入框并触发 `input` 事件（页面自己的 `invalidateImportPlan` 会照常跑）。**不改前端源码，只往 DOM 上挂。**
- **关闭 = 收进托盘**：默认关窗不退出（长任务不中断），托盘菜单可退出；`desktop.json` 里可改回"关掉就退"。



### 4.5 踩到的坑（给后来者）

1. `window.prompt` 不支持（上面已解）。
2. 剪贴板：只设 `setPermissionRequestHandler` 不够，`writeText` 走的是 **permission check**。
3. PowerShell 5.1 `Set-Content -Encoding utf8` 会写 **BOM**，`JSON.parse` 直接失败且被 `catch` 吞掉 ——
   配置"没生效"却毫无提示。写 JSON 一律用 `UTF8Encoding($false)`。
4. `Start-Process -WindowStyle Hidden` 会让 Electron 的**主窗口也隐藏**（STARTUPINFO 的 SW_HIDE），
   自动化测试时别用它来判断"窗口没起来"。
5. pnpm 10+ 默认拦截 `postinstall`，Electron 的二进制不会下载 → 手动 `node node_modules/electron/install.js`，
   或 `pnpm approve-builds`；国内建议 `ELECTRON_MIRROR=https://npmmirror.com/mirrors/electron/`。
6. `electron-builder` 会**自己再下一份** Electron（不复用 `node_modules` 里那份），也要设 `ELECTRON_MIRROR`；
   本机实测它下载会卡住，改用 `"electronDist": "node_modules/electron/dist"` 直接复用本地那份。
7. **命令行传 app 目录时别带结尾反斜杠**：`electron.exe "D:\app\"` 会让参数解析把右引号吃掉，
   Electron 看起来起来了，但 `require('electron')` 拿到的是 **npm 包的路径字符串**，
   于是 `app.setName` 直接 `TypeError: Cannot read properties of undefined`。
   批处理里 `%~dp0` 天生带结尾反斜杠，所以启动器写的是 `"%~dp0."`。
8. `electron-builder 26.15` + `pnpm 11` 在本机收集 node_modules 时报
   `No JSON content found in output` 并中断打包（应用文件还没拷进去）。
   `_dist\portable` 因此是**手工组装**的（复制 electron dist → 把 app 放进 `resources\app`），
   不是 electron-builder 产物；要做正式安装包，换成 npm 或等上游修。

---

## 5. 三条打包路线

### A. 薄壳 + 本机现有 Python（本原型）

- 交付：`_dist\portable\PhotoOS.exe`（免安装目录，369 MB，已实测可运行）或 NSIS 安装包；**不含 Python**。
- 前提：目标机已有 PhotoOS 的 venv（现有 `install.ps1` 就是干这个的）。
- 体积：Electron 368 MB + 壳代码 0.4 MB = **369 MB 实测**；压缩后估计 110–150 MB（未实测）。
- 工期：壳已完成，接入半天（改 `desktop.json` 指 `python` 即可）。
- 风险：低。Python 侧一行没动。

### B. 薄壳 + 自带 Python（"发给别人双击就能用"）

两条实现路线：

1. **embeddable Python 3.12**（约 11 MB）+ `pip install photoos` 到 `resources\runtime\site-packages`。
   最贴近现状，产物 ≈ 370 + 230 ≈ **600 MB**（解压后）；不需要编译器。
2. **PyInstaller onedir** 把 `photoos` 打成 `photoos.exe`，Electron 直接 spawn 它。
   产物略小（≈ 500–550 MB），但需要处理 `rawpy`(libraw)、`pillow-heif`(libheif)、
   `imageio-ffmpeg`(ffmpeg.exe)、`web/` 与 `db/migrations/*.sql` 的打包数据 —— 都是已知的坑，不是不可解。

工期 2–3 天，风险中。**AI 依赖不在此列**（见 C）。

### C. 连 AI 一起（不建议）

AI venv 3.7 GB + 模型 2.1 GB ≈ **6 GB**，而且 `torch` 打包是出了名的难。
现有设计已经把 AI 隔离成 `ai_python`（`data\config.json` 指向另一个解释器），
桌面壳只需**保持这个边界**：不装 AI 时相关按钮照旧提示缺依赖，不要试图塞进安装包。

---

## 6. 收益 vs 成本

| 得到 | 付出 |
|---|---|
| 任务栏独立图标、无地址栏、Alt+Tab 里像一个软件 | 内存 +约 250 MB（相对一个浏览器标签页） |
| 托盘常驻：关窗不中断哈希/预览这类长任务 | 冷启动 +约 1.5 s |
| 单实例、窗口位置记忆、托盘菜单、开机自启（`setLoginItemSettings`，一行） | 多一条 Node/Electron 构建链（**仅开发期**；运行时不需要 Node） |
| 导入目录用系统选择器，不用手抄路径 | 每年跟一次 Electron 大版本（44 → 以后） |
| 统一的启动入口（不用记 `.cmd` 在哪） | 未签名分发会被 SmartScreen 拦一下（自用无感） |

**不改变**：性能、数据库、照片只读模型、"拔盘可用"的产品约束。

如果只是想要"无地址栏的窗口"，还有个零成本方案：给 Chrome/Edge 建快捷方式
`chrome.exe --app=http://127.0.0.1:8760`。它能去掉地址栏和标签页，但没有托盘、没有进程生命周期、
关掉窗口后服务还在傻跑 —— 也就是本原型多出来的那 400 行所解决的问题。

---

## 7. 风险清单

1. **数据根不一致**：浏览器版用 `C:\PhotoOS\data`；打包后没有 `photoos.toml`，后端会落到
   `%LOCALAPPDATA%\PhotoOS\data` —— 用户会看到"空的库"。**必须显式配置**：壳的 `desktop.json`
   或环境变量 `PHOTOOS_DATA_ROOT`。这是最容易踩、且症状最吓人的一条。
2. **同一数据库两个进程**：壳有单实例锁，但如果浏览器版和桌面版同时开着，就是两个 uvicorn 写同一个
   SQLite（WAL 能扛，但不是设计前提）。壳里已做端口避让，但"数据共享"这条要在文档里写清楚：
   **两个入口，二选一常开**。
3. **prompt 桥的阻塞语义**：桥接后行为与原生一致；但如果页面在 `beforeunload` 之类时机调用，注意别卡死。
4. **AI 仍是外挂**：`ai_python` 指错/失效时，界面上的 AI 按钮会报"缺依赖"，与浏览器版一致。
5. **无签名**：给别人时首次运行要过 SmartScreen；要正式分发再考虑代码签名（个人自用不必）。
6. **Electron 安全基线**：`contextIsolation: true`、`nodeIntegration: false`、`sandbox: true`、
   只暴露 2 个能力、外链一律出壳、禁用 `webview` —— 已在原型里落实，别为了省事关掉。

---

## 8. 建议的落地顺序

1. **现在**：把 `desktop/` 留在 `C:\PhotoOS\desktop`（不并进 `photoos/` 仓库），日常用
   `start-photoos-desktop.cmd`。先跑两周，看托盘/关窗行为是否合手。
2. **顺手的前端小改**（可选，与 Electron 无关）：12 处 `prompt` → 应用内异步弹窗；`confirm`/`alert` 一起统一。
   做完之后，壳里的 prompt 桥可以留着兜底，也可以删掉。
3. **要发给别人时**：先发 A 路线（对方按 `安装说明.md` 装 Python + wheel），确实有人用再投 B 路线。
4. **永远不要**：把 AI 依赖打进安装包。

---

## 9. 复现

```powershell
# 1) 依赖（只需一次；国内先设镜像）
$env:ELECTRON_MIRROR = 'https://npmmirror.com/mirrors/electron/'
pnpm install
node node_modules\electron\install.js        # pnpm 拦了 postinstall 时补这一步

# 2) 跑（自己拉后端；8760 被占会自动换端口）
pnpm start
# 或：双击 start-photoos-desktop.cmd

# 3) 连已在跑的服务，并开 CDP 便于检查
pnpm start:attach -- --remote-debugging-port=9333
python tools\cdp.py list
python tools\cdp.py shot _shots\now.png

# 4) 免安装版（已组装好，双击即可；用的是本机 venv）
_dist\portable\PhotoOS.exe
```

验证过的环境：Windows 10.0.26200 / Node 24.21 / pnpm 11.28 / Electron 44.9.0 / Python 3.12.10。

---

## 附：原型文件

```
C:\PhotoOS\desktop\
  main.js                窗口、菜单、托盘、权限、prompt 桥、目录选择注入
  preload.js             暴露 pickDirectory / promptSync
  preload-prompt.js      输入框窗口的 preload
  prompt.html            输入框窗口（深色，与 PhotoOS 同配色）
  lib\backend.js         端口选择、spawn、健康轮询、日志、杀进程树
  assets\                图标（光圈：金 #F4D35E + 皇家蓝 #0A2463）
  tools\cdp.py           CDP 取值/截图
  tools\shot_target.py   截指定 CDP 目标
  tools\make_icons.py    生成图标
  _shots\                本报告里的截图
  README.md              使用说明
```
