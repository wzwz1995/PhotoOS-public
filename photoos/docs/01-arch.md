# 01 · 系统全景

## 这是什么

PhotoOS 是**本机单人使用**的个人摄影管理系统。它解决的不是"看照片"（那用资源管理器
就行），而是四件在 2 万张量级上会失控的事：

1. **把硬盘上的照片登记成可查询的索引** —— 按类型、状态、机型、年份、关键词、
   目录、时间翻看（`照片库`）。
2. **逐组精选** —— 同一场景的连拍按时序摆好、重复构图并成一组，用户只对少数几组
   做决定（`精选`）。
3. **按内容或按时间找** —— "找出所有某类照片"用向量搜索，"分出某一次拍摄"用
   目录与拍摄时间精确筛选（`找照片`）。
4. **按人归拢、组成主题** —— 人脸聚类 + 长期作品集合（`人物`、`主题`）。

加上 `维护`（导入、任务、完整性、备份）与 `说明`（每个功能解决什么、有什么限制），
共**七个页面**。

## 进程与部署模型

```
┌─────────────────────────────────────────────────────────────┐
│  photoos serve  (uvicorn, 单进程)                            │
│  ├─ FastAPI 路由          api/app.py                        │
│  ├─ 后台任务执行线程        jobs/runner.py BackgroundRunner    │
│  ├─ SQLite (WAL)          data/photoos.db                   │
│  └─ 静态前端              web/  (零构建，直接读源码树)          │
└─────────────────────────────────────────────────────────────┘
        ▲                                    │
        │ HTTP 127.0.0.1:8760                │ subprocess（白名单命令）
        │                                    ▼
   ┌─────────┐                    ┌──────────────────────────┐
   │ 浏览器   │                    │ 另一个 python 解释器        │
   │ (任意)   │                    │ = config.ai_python 或自身  │
   └─────────┘                    └──────────────────────────┘
```

**关键点**：服务端与"跑 AI 的那个环境"可以是**两个不同的解释器**。
这不是设计得漂亮，是现实 —— 见 [`05-ai.md`](05-ai.md) 的"环境隔离"一节。

### 目录布局

```
C:\PhotoOS\
  photoos.toml          ← 项目根标记（config.find_project_root 靠它定位）
  data\                 ← 数据根，整体可搬迁
    photoos.db          ← SQLite（含 WAL）
    config.json         ← 运行时配置
    thumbnails\ previews\   ← 派生缩略图，按 sha256 前两位分桶
    ai_cache\models\    ← CLIP / EmbeddingGemma / 人脸模型权重
    jobs\ logs\ backups\
  photoos\              ← 代码仓库
    backend\photoos\    ← Python 包
    docs\               ← 本目录
    tests\
    packaging\          ← Windows 安装脚本
  legacy\               ← 既有 PowerShell 工具链只读归档
```

**照片原文件永远在外部存储上，绝不进入这个目录。**

## 一次请求的路径

以"照片库翻页"为例：

```
浏览器 GET /api/assets?offset=0&limit=300&sort=capture
  → api/app.py 的路由（在 _read() 里跑同步 DB 逻辑，避免阻塞事件循环）
  → api/queries.py 组装 SQL（只查索引，**绝不扫描原片**）
  → db/connection.py（WAL，读不阻塞写）
  → 返回 JSON
浏览器渲染成虚拟网格（js/app.js 的 grid 模块，只渲染可见格）
缩略图走 GET /api/assets/{id}/thumb → data/thumbnails/xx/…
```

两个贯穿全局的纪律：

- **查询永远不碰原片**。原片只在 import / hash / preview 三个阶段读，
  且都是后台任务。任意时刻拔掉照片硬盘，浏览、筛选、精选、主题依然可用。
- **同步 DB 逻辑跑在 `_read()` 里**。FastAPI 的 async 端点里直接跑 sqlite3
  会阻塞事件循环；`_read` 把它挪到线程池。

## 七个页面各自解决什么

| 页面 | 一句话 | 主要来源文件 |
|---|---|---|
| 照片库 `library` | 按目录/时间/精选状态/主题翻看全库 | `js/app.js` + `api/queries.py` |
| 精选 `review` | 一个场景一次过完，逐组做决定 | `js/review.js` + `library/shot_grouping.py` |
| 找照片 `search` | 按画面内容（向量）或按时间地点（精确） | `js/search.js` + `library/semantic_rank.py` |
| 人物 `people` | 人脸聚类 → 你命名 → 它找更多 | `js/people.js` + `library/faces.py` |
| 主题 `collections` | 你主动创建的长期作品集合 | `js/collections.js` |
| 维护 `maintenance` | 导入、后台任务、完整性、备份、命令 | `js/app.js` |
| 说明 `help` | 功能怎么用、有哪些真实限制 | `js/help.js` |

`说明`页不是装饰：它是**唯一**适合放长解释的地方，因此其余页面只写"这一步要做什么"。

## 硬规则（违反即 bug，不是风格问题）

1. **原片只读。** 唯一会写照片库的是 `library/importer.py`；它只复制，不改名、
   不移动、不删除源文件。其余模块碰原片只允许 `open(..., 'rb')`。
2. **界面不做"评分/推荐"视觉语言。** 没有星级、没有"匹配度 87%"的大字。
   金色 = "你的选择"，不是"更好"。
3. **设计令牌只定义一处**（`web/css/tokens.css`，第一个加载）。
4. **数据库 schema 只通过迁移改。** 迁移一经发布不修改，只追加。
5. **不破坏"拔盘可用"**：列表、缩略图、拍摄信息、精选记录都必须在照片硬盘离线时可用。
6. **破坏性操作必须二次确认**（`Settings.require_confirmation_for_destructive`）。
7. **Windows 控制台输出必须能显示中文**（`cli._force_utf8_io()`）。

## 技术选型里"不显然"的几条

| 选择 | 为什么 |
|---|---|
| SQLite（不是 Postgres） | 单机单人、零运维；WAL 让 UI 浏览与后台索引并行 |
| 零构建前端（无 npm） | 本机单人使用，装 Node 只为打包 CSS 不划算；调试即改即见 |
| 深色界面 | **领域性**理由：全部内容是"看照片然后判断"，浅底会干扰对照片明暗与色彩的判断 |
| 哈希单线程、元数据多线程 | 外置盘冷读 55~71 MB/s（多线程读同一块盘无收益）；rawpy/Pillow 解码释放 GIL，6 线程实测 9× 加速 |
| 服务端与 AI 环境分离 | 产品 venv 不装 torch/onnxruntime（可选依赖），必要时用 `ai_python` 指向另一个环境 |

---

下一篇：[`02-backend.md`](02-backend.md) · 返回 [`README.md`](README.md)
