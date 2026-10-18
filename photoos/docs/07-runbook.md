# 07 · 运行手册

## 日常命令

```powershell
# 启动服务（默认 127.0.0.1:8760）
cd C:\PhotoOS
.\.venv\Scripts\python.exe -m photoos serve

# 别的端口 / 监听地址
.\.venv\Scripts\python.exe -m photoos serve --port 8761

# 环境自检（会真跑一次推理计时，回答"这台机器现在到底能不能算向量"）
.\.venv\Scripts\python.exe -m photoos ai doctor

# 看库的总体情况
.\.venv\Scripts\python.exe -m photoos stats
```

> ⚠️ **用哪个 python**。项目根下 `.venv` 是产品环境（可编辑安装，`-m photoos` 可用）。
> 系统 Python312 **没有** photoos 包，`-m photoos` 会报
> `No module named photoos.__main__`。附带 AI 能力的另一个 venv 见 [`05-ai.md`](05-ai.md)。

## 测试

```powershell
cd C:\PhotoOS\photoos
C:\PhotoOS\.venv\Scripts\python.exe -m pytest tests/unit -q          # 快速，全绿
C:\PhotoOS\.venv\Scripts\python.exe -m pytest tests/unit -q --tb=long # 看失败细节
```

- `tests/unit`：**必须全绿**，不需要真实照片库。
- `tests/breakpoint`：PRD §9.1/§9.2 的断点验收，**需要真实照片库**，默认不快跑
  （`@pytest.mark.breakpoint`）。

### 测试里几条"守纪律"的用例，改动前先读

| 文件 | 守什么 |
|---|---|
| `test_design_system.py` | 令牌只有一处定义、`tokens.css` 第一个加载、七个视图都有页头、空态只有一种、令牌不越级 |
| `test_actions_catalog.py` | 白名单命令能被**真** argparse 解析、注入样本必须被拒、模板无 shell 元字符 |
| `test_packaging.py` | `web/css/*.css` 等必须落进 wheel（历史上漏过，本机永远看不见） |
| `test_frontend_cache_contract.py` | 前端静态资源的缓存契约 |
| `test_package_static_health.py` | 包内静态资源完整性 |

## 界面验证（改前端时）

没有 Node、没有 Playwright。实际可用的手段是**用 CDP 驱动无头 Chrome**，
拿真实计算样式与截图 —— 这比读 CSS 可靠得多（本次 UI 统一的全部结论都来自它）。

要点：
- Chrome 在 `C:\Users\user\AppData\Local\Google\Chrome\Application\chrome.exe`
- 用 `--headless=new --remote-debugging-port=<port>` 启动，从 `/json/list` 取
  `webSocketDebuggerUrl`，用 `websockets`（产品 venv 里已有）连上去；
- `Runtime.evaluate` 取 `getComputedStyle`、`Page.captureScreenshot` 截图、
  `Input.dispatchKeyEvent` 发**真实** Tab 键（`el.focus()` **不会**触发 `:focus-visible`，
  用它测焦点环会得到一屏假阳性）。

---

# 排障：按症状查

## 界面上的按钮点了没反应 / 报 404

服务是旧进程。Python 代码改了**必须重启** `photoos serve`（没有 `--reload`）。
CSS/JS 是每次请求读源码树，改完刷新浏览器即可。

## 某个命令按钮显示"缺依赖"

看维护页 →「命令（界面直跑）」→ **执行环境**那一栏，它会写出：

- 用的是哪个 python；
- 缺哪些可选依赖。

装依赖或改 `ai_python`，见下一节。

## AI 功能报"未安装 torch / sentence-transformers"

服务所在的环境没有 AI 依赖。两条路：

**A. 指向已经装好的另一个环境**（推荐，本机就是这样）
编辑 `C:\PhotoOS\data\config.json`：

```json
{
  "ai_python": "D:\\PhotoOS\\scratch\\venv-eg2\\Scripts\\python.exe"
}
```

或设环境变量 `PHOTOOS_AI_PYTHON`。重启服务后，那一栏会显示"取自配置 ai_python"。
前提：那个解释器能 `import photoos`（可编辑安装或已装包）。

**B. 给产品环境装可选依赖**
`pip install -e ".[ai-cpu]"`（CPU）或 `".[ai-gpu]"`（NVIDIA）。

## 搜索报"这个词还没有准备过"

该词的文本向量还没算。界面上直接点「立即准备…」按钮；它需要能跑 torch 的环境
（见上一条）。跑完下拉框里就会出现这个词。

## 中文在命令行里是乱码

控制台代码页问题，不是数据问题。`cli._force_utf8_io()` 会把 stdout/stderr
重设为 UTF-8；若仍乱码，先 `chcp 65001`。
**判断数据是否真的坏了**，用 Python 读出来看，不要看 PowerShell 的显示：

```powershell
.\.venv\Scripts\python.exe -c "import urllib.request,json;print(json.load(urllib.request.urlopen('http://127.0.0.1:8760/api/semantic/queries'))['items'][0])"
```

## 任务卡在 PROCESSING 不动

进程被杀（断电、任务管理器结束）后，任务会停在 PROCESSING。下次启动时
`JobQueue.recover_orphans()` 会把心跳过期的**回退为 PENDING 并释放归属**。

手动看：

```powershell
.\.venv\Scripts\python.exe -m photoos jobs list
```

## 照片硬盘拔了之后某些页面空了

列表、缩略图、拍摄信息、精选记录都应当**仍然可用**（这是产品要求）。
「看原片」会失败，因为原片不在盘上。插回后用维护页的「离线照片库 → 执行重绑定」
恢复访问，**不需要重新导入**。

## 界面样式改了但没生效

1. `Ctrl+F5` 强刷（浏览器缓存了旧 CSS/JS）；
2. 确认改的是 `web/css/` 或 `web/js/`，而不是 `photoos/build/lib/...` 下的旧副本
   （那是构建产物，不是源）；
3. 确认 `tokens.css` 仍在 `index.html` 里**第一个**加载。

## 有些照片的缩略图/大图是**倒的或躺的**

**已知缺陷，已定位未修复** —— 见 [`13-raw-orientation.md`](13-raw-orientation.md)。

一句话：`media/adapter.py` 把 LibRaw 的 `sizes.flip` 用在了**相机内嵌的 JPEG 预览**上，
而 LibRaw 明确说 `flip` 只管传感器原始数据、内嵌预览是 as-is。于是二次旋转。
受影响 3,861 张（其中 1,345 张 RAW），只出现在 `orientation ∈ {6,8}` 的文件里。

**症状怎么认**：

- 缩略图/预览方向不对（**看**出来的，日志里什么都没有 —— 检测器的置信度照样 0.87）；
- 「人物」页某个人**一部分照片**的相似度低到 0.1 以下，**另一部分正常**
  → 这个人牌面上的**判定余量为负**（如 `-0.671`）。

**为什么只有一部分坏**：实测同一个人的确认样本里，"需旋转"那组的相似度**中位**
与正常组几乎一样（0.771 vs 0.775），只有**最低值**差一个数量级（0.062 vs 0.383）。
所以不是"凡 `orientation=8` 全坏"，得逐张看。

**两种判断受影响程度完全不同（都实测过）**：

| 判断 | 影响 | 实测 |
|---|---|---|
| 内容检索（CLIP） | **基本没有** | 转 90/180/270° 后余弦中位 0.980–0.988 |
| 人脸（SFace） | **完全失效** | 同一张脸正立 vs 倒转中位 **0.105**，36/36 张 < 0.3 |

**修复（需 `E:` 盘在线）**：核实 `sizes.flip` 与内嵌预览自带 EXIF 哪个可信 →
改 `_apply_raw_flip` 的调用条件 → 只重生成那 3,861 张 preview/thumb →
`people detect` **再** `people embed` 再 `people learn`。

⚠️ **不能只跑 `people embed`**：`face.landmarks_json` 存的是**检测坐标系**下的
15 个浮点，预览一转坐标系就全变了。

## pytest 有一个失败但我没碰那里

先看它是不是**先于你的改动就存在**的（`git`/文件时间戳）。仓库里的既有失败会淹没
你新引入的失败 —— 顺手修掉，别放着。

---

# 性能与容量（本机实测，来自代码注释与计划书）

## 界面响应：哪些接口慢、为什么（2026-10 实测）

改前/改后（本机，21,400 张）：

| 接口 | 改前 | 改后 | 谁在调 |
|---|---|---|---|
| `/api/actions` | 220 ms | **4 ms** | 打开「命令」弹窗、维护页面板 |
| `/api/facets` | 120 ms | **53 ms**（再进 1 ms） | 每次进照片库 |
| `/api/stats` | 235 ms | **245 ms 首算**（再进 1 ms） | 每次进维护页 |
| 照片详情弹窗 | 点击→可见 41–57 ms，图片画出 58–75 ms | 不变 | 点一张照片 |

**三条经验**（改这些接口前请先读）：

1. **不要每次请求都起子进程。** `environment_report()` 探测外部解释器要靠
   `python -c`，5 个模块 = 5 个进程 = 220 ms。已加进程内缓存。
2. **不需要的 join 不要写。** 机型/年份/镜头全在 `metadata` 里，
   `asset LEFT JOIN metadata` 白付 7 ms（21,400 行）。只有"按库筛选"才需要 join。
3. **反复调用而不变的接口要能缓存。** `/api/facets` 与 `/api/stats` 都带了
   ETag + `max-age=60`。它们的值只有导入/索引后才变 ——
   60 秒窗口足够短，而反复切页面完全不发请求。

`/api/stats` 首算的 235 ms 里，62 ms 是 `metadata.time_quality`、
29 ms 是 `previews.generator_versions` —— 那是"数据质量如何"的诚实统计，
要真扫表。有缓存之后不值得继续优化。

## 索引与派生的量级

| 操作 | 量级 | 说明 |
|---|---|---|
| 发现阶段（scan） | 16k 文件 ≈ 1 秒 | 只遍历文件系统 |
| 哈希 | 252 GB ≈ 70~80 分钟 | 外置盘冷读 55~71 MB/s，**单线程顺序**（多线程读同一块盘无收益） |
| 元数据 / 预览 | 589 ms/张 → 66 ms/张 | rawpy/Pillow 解码释放 GIL，6 线程实测 9× |
| CLIP 向量 | 视 GPU | onnxruntime |
| EG2 向量 | CPU 上全量 21,400 张约 **114 小时** | 必须用 CUDA；见 `embedding_gemma.probe()` 的说明 |

**所以**：哈希与向量都是"可暂停、可恢复、可后台长时间运行"的独立任务，
不是同步请求。

---

返回 [`README.md`](README.md)
