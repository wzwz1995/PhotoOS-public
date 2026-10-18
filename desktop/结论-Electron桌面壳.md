# Electron 包 PhotoOS：结论

2026-10-17 · 对应调研：[`调研-Electron桌面壳.md`](调研-Electron桌面壳.md)（实测数据与逐项核验都在那里）

---

## 一句话

**能包，值得包，而且不用改 PhotoOS 的业务代码。** 自用走"薄壳 + 本机现有 Python"这一条就够了；
想"发给别人双击就能用"再投自带 Python，别把 AI 塞进去。

---

## 1. 结论

| 问题 | 答案 |
|---|---|
| 能不能用 Electron 包？ | **能。** PhotoOS 是"本机 HTTP 服务 + 零构建前端 + 同源 API"，Electron 只是换一个打开方式，不是重写客户端 |
| 要改 PhotoOS 的代码吗？ | **不用。** 12 处 `prompt`、外链、剪贴板、下载、目录选择，全部在壳里补齐 |
| 工作量？ | 壳已成型并跑通（`C:\PhotoOS\desktop`）；接进日常使用半天 |
| 推荐哪条路？ | **A：薄壳 + 本机现有 venv**，369 MB（实测） |
| 不推荐？ | B（自带 Python，≈600 MB / 2–3 天）只在"要发给没 Python 的人"时才做；C（连 AI 一起，≈6.5 GB）不做 |

---

## 2. 依据（本机实测，不是估算）

| 指标 | 实测 |
|---|---|
| 后端冷启动 → `/api/health` 就绪 | 1.34 s |
| Electron 进程启动 → 页面加载完成 | 1.49 s |
| 内存 | Electron 4 进程 333 MB + Python 56 MB |
| 端口冲突 | 8760 被浏览器版占着 → 自动改用 60953/62982/51503 |
| 退出 | 关窗 → 后端进程树被完整收走，无残留、端口释放 |
| 体积 | Electron 本体 368 MB；免安装版 **369 MB** |
| 真实库 | 21,340 张照片，滚动手感/图片加载与浏览器版无可感知差异 |

---

## 3. 唯一真障碍：`window.prompt`（已解）

- Electron 明确不支持 prompt（[electron#8067](https://github.com/electron/electron/issues/8067)），页面里直接抛
  `Error: prompt() is not supported.`；PhotoOS 有 **12 处**调用（人物命名、新建/重命名主题、动作参数…）。
- 解法：`ipcRenderer.sendSync` 的 `event.returnValue` **可以异步赋值** —— 先弹输入框窗口，
  用户确定时再回值，渲染线程一直阻塞（与原生 prompt 语义一致）。
- 已验证：点「新建主题」→ 两次输入 → 数据库新增 1 条主题。**业务代码零改动。**

其余四项都是几行配置：`target=_blank` → 系统浏览器；剪贴板要**同时**设 request 与 check 两个权限 handler；
下载走「另存为」；导入目录挂一个系统目录选择器。

---

## 4. 代价

| 得到 | 付出 |
|---|---|
| 任务栏图标、无地址栏、像软件 | 内存 +约 250 MB（相对一个浏览器标签页） |
| 托盘常驻：关窗不中断哈希/预览长任务 | 启动 +约 1.5 s |
| 单实例、窗口记忆、目录选择、开机自启（一行） | 多一条 Node/Electron 构建链（**仅开发期**，运行时不需要 Node） |
| 统一启动入口 | 每年跟一次 Electron 大版本；未签名分发会被 SmartScreen 拦 |

**不改变**：性能、数据库、照片只读模型、"拔盘可用"的产品约束。

---

## 5. 必须守住的三条

1. **数据根要显式指定。** 打包后没有 `photoos.toml`，后端会落到 `%LOCALAPPDATA%\PhotoOS\data`，
   用户看到的是"空库"——症状吓人。用 `desktop.json` 或 `PHOTOOS_DATA_ROOT` 指死。
2. **浏览器版和桌面版不要同时常开。** 两个 uvicorn 写同一个 SQLite，WAL 能扛但不是设计前提。
   端口会自动避让，但数据只有一份。
3. **AI 继续外挂。** AI venv 3.7 GB + 模型 2.1 GB；保持 `ai_python` 指向另一个解释器的边界，
   不装时界面照旧提示缺依赖。

---

## 6. 下一步

1. **先自用两周**：`C:\PhotoOS\desktop\start-photoos-desktop.cmd`（免 Node），看托盘/关窗是否合手。
2. **可选的纯前端小改**：12 处 `prompt` → 应用内异步弹窗（顺带统一 `confirm`/`alert` 样式）。
   做完壳里的 prompt 桥可留可删；这件事与用不用 Electron 无关，本来就值得做。
3. **要发给别人时**再上 B 方案（embeddable Python 或 PyInstaller），先发 A 方案（对方按 `安装说明.md` 装 Python + wheel）。

---

## 7. 东西在哪

| 内容 | 位置 |
|---|---|
| 原型（壳） | `C:\PhotoOS\desktop\`（不进入 Python 包、不进 pytest） |
| 详细调研 | `C:\PhotoOS\desktop\调研-Electron桌面壳.md` |
| 使用说明 | `C:\PhotoOS\desktop\README.md` |
| 日常启动 | `C:\PhotoOS\desktop\start-photoos-desktop.cmd` |
| 免安装版 | `C:\PhotoOS\desktop\_dist\portable\PhotoOS.exe`（369 MB，用本机 venv） |
| 截图 | `C:\PhotoOS\desktop\_shots\` |

> 本文档**没有**放进 `D:\mykb`：该库的 `SCHEMA.md` 明确把"照片/视频整理、非数据类工具链"排除在范围外。
> 需要归档到库外目录（如 `D:\_mykb-out-of-scope\`）的话告诉我。
