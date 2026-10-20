# PhotoOS 文档

> 面向**后续维护者**的一组文档。目标：让接手的人不必重读 100 MB 的历史计划书，
> 也能在半小时内知道"这个系统由什么组成、改动会碰哪里、坏了怎么查"。
>
> **公开快照说明**：本目录是结论与现状。开发线的 `HANDOVER.md`、`PHASE*` 与过程文档
> 不在公开仓库；文中出现的这些引用是历史溯源，不是可点击链接。
> 本目录与代码冲突时以**代码**为准，并请顺手修文档。

---

## 从这里开始

| 你想做什么 | 读哪一篇 |
|---|---|
| 第一次接触这个项目 | [`01-arch.md`](01-arch.md) 然后 [`07-runbook.md`](07-runbook.md) |
| 加一个新页面 / 改界面 | [`06-frontend.md`](06-frontend.md) |
| 加一个后台任务 | [`04-jobs.md`](04-jobs.md) |
| 改数据库结构 | [`03-data-model.md`](03-data-model.md) |
| 碰 AI / 向量 / 人脸 | [`05-ai.md`](05-ai.md) |
| 出问题了 | [`07-runbook.md`](07-runbook.md) 的排障一节 |
| 想知道"为什么是这样" | [`08-decisions.md`](08-decisions.md) |
| 想知道最近改了什么 | [`09-changelog.md`](09-changelog.md) |

---

## 文档清单

| 文件 | 内容 |
|---|---|
| [`01-arch.md`](01-arch.md) | 系统全景：进程模型、目录布局、七个页面、一次请求的路径、硬规则 |
| [`02-backend.md`](02-backend.md) | 后端模块地图：每个子包与关键模块的职责 |
| [`03-data-model.md`](03-data-model.md) | 数据库：全部表、关系、迁移纪律 |
| [`04-jobs.md`](04-jobs.md) | 任务系统：创建/认领/执行/重试/取消/恢复，以及每类任务做什么 |
| [`05-ai.md`](05-ai.md) | 向量与模型：CLIP vs EmbeddingGemma、人脸、Scene 划分、**环境隔离** |
| [`06-frontend.md`](06-frontend.md) | 前端：零构建结构、设计令牌、组件契约、页面骨架、三个布局陷阱 |
| [`07-runbook.md`](07-runbook.md) | 运行手册：常用命令、验证手段、排障（按症状查） |
| [`08-decisions.md`](08-decisions.md) | 关键决策与踩过的坑：为什么这样、不能改成什么样 |
| [`09-changelog.md`](09-changelog.md) | 变更记录：新增能力、行为变化、修掉的缺陷、**仍然存在的缺口** |
| [`10-import-audit.md`](10-import-audit.md) | 照片导入的增量行为审计：去重已实测正确，但**导入后不会自动分组**（精选/找照片因此是空的） |
| [`11-shell-unification.md`](11-shell-unification.md) | 统一外壳（方案 A）**实施方案**：四个视角共用一个外壳、抽三样东西、六步落地、验收判据 |
| [`12-multidim-filter-plan.md`](12-multidim-filter-plan.md) | **多维筛选重构执行计划**：照片库做成主筛选台（内容×元数据×人物）、筛选上顶栏、弹窗渐进式标签、精选页由粗到细 |
| [`13-raw-orientation.md`](13-raw-orientation.md) | **RAW 预览方向反了**（3,861 张）：LibRaw `sizes.flip` 被用在了内嵌预览上。实测 CLIP 不受影响、**SFace 完全失效**。修复步骤待 `E:` 盘上线 |

---

## ⭐ 完整技术参考（`20-reference/`）

上面那 13 篇是"够用就好"的接手笔记；**要完整正规的技术参考、要看架构图与 ER 图，
去 [`20-reference/`](20-reference/README.md)** —— 含架构总览、数据建模（31 张表 + ER 图）、
后端模块地图、HTTP/CLI 接口参考、任务引擎、媒体流水线与 AI 子系统、前端架构、
安全边界，以及**需要人拍板的 35 条问题**清单。

> 【实测】`20-reference/` 建于 2026-10-17。它与本目录的关系：
> **它是在代码里逐行取证后写的**，本目录的部分行数基线（尤其 `03-data-model.md`）
> 已被它订正 —— 冲突时以 `20-reference/` 与**代码**为准。

---

## 三条最该先记住的事

1. **原片只读。** 只有 `library/importer.py` 会写照片库，且只复制、不改名、不移动源文件。
   其余任何模块碰原片都只允许 `open(..., 'rb')`。这条是产品底线，不是实现细节。

2. **界面不做"评分/推荐"。** 没有星级、没有"匹配度 87%"的大字、没有"最佳"。
   金色表示"**你的选择**"，不表示"更好"。见 [`06-frontend.md`](06-frontend.md) 的视觉纪律一节。

3. **令牌只定义一处**（`web/css/tokens.css`）。曾经定义在两处，谁生效取决于文件加载
   顺序与选择器层级两件互不相干的事 —— 那是本仓库最贵的一类 bug。见
   [`08-decisions.md`](08-decisions.md) 的"令牌归一"。

---

## 快速事实

- 包名 / 版本：`photoos` / `0.1.0`（`pyproject.toml`）
- 数据库 schema 版本：**9**（`db/migrations/` 下最高编号）
- 数据根：`C:\PhotoOS\data`（可整体搬迁；配置见 `data/config.json`）
- 前端：**零构建**，原生 CSS + ES module，无 npm、无打包步骤
- 测试：`pytest`，`tests/unit` 全绿（`tests/breakpoint` 需要真实照片库，默认不快跑）
- 服务：`photoos serve`，默认 `127.0.0.1:8760`
