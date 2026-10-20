# 03 · 数据模型

数据库：SQLite（WAL），`data/photoos.db`。schema 版本 **9**。
定义在 `backend/photoos/db/migrations/NNN_*.sql`。

迁移纪律见本文末尾。

> 下面每张表的行数是**真实库在 2026-10-15 的快照**，用
> `SELECT COUNT(*)` 复算得。它们不是"设计预期"，是现状 —— 改动时拿它当基线，
> 数量级不对就说明流程出了问题。

---

## 一、表清单（31 张）

### 1. 库与卷（这一层回答"照片在哪"）

| 表 | 行数 | 作用 |
|---|---|---|
| `library` | 2 | 一个"照片库"（本机是「我的照片库」+「我的视频库」）。`uuid` 稳定；`root_path_hint` 只是**提示**，盘符变了靠重绑定修 |
| `volume` | 1 | 物理卷（序列号、指纹、文件系统、标签）。用于识别"是不是同一块盘换了盘符" |
| `volume_library` | 2 | 卷 ↔ 库的关联与根路径提示（多对多，所以是独立表） |

> **为什么 `root_path_hint` 只是提示**：外置盘盘符会变（E: → F:）。真正决定
> "这是不是同一个库"的是 `uuid` + `marker_uuid`，不是路径。所以拔盘/换盘符之后
> 重绑定即可，**不需要重新导入**。

### 2. 资产与派生（这一层回答"库里有什么"）

| 表 | 行数 | 作用 |
|---|---|---|
| `asset` | 21,400 | **核心表**：一张照片/视频一行。`relative_path`（相对库根）、`content_hash`+`hash_algo`、`size`、`mtime_ns`、`mime`、`file_type`、`status`(OK/PENDING/MISSING/UNREADABLE) |
| `asset_instance` | 0 | 同一内容出现在多个卷/路径的实例（去重与"这份副本在哪"） |
| `metadata` | 21,400 | 1:1 扩展表，**很宽**：相机/镜头/光圈/快门/ISO/焦距/白平衡/尺寸/拍摄时间（本地 + UTC + 时区来源）/GPS/视频时长与编码。`raw_json` 存原始 EXIF |
| `preview` | 42,800 | 派生图（缩略图/预览），按 `kind` 区分。`asset_id` 上 2 行/张 |
| `embedding` | 42,800 | **向量**。`model` + `model_version` + `model_sha256` + `dim` + `normalization` + `input_kind` + `prep_version` —— 一行向量必须能自证"我是谁算的、按什么预处理算的" |

> `embedding` 42,800 = 21,400 × 2 个模型（CLIP 与 EmbeddingGemma）。
> **两个模型的向量并存**，靠 `model` 列区分，绝不能混用。

> **`asset_instance` 现在是空的**：本机只有一个库，没有内容重复出现在多卷的情况。
> 表留着是因为 PRD 要求该能力，不是死代码。

### 3. 分组（这一层回答"怎么把照片组织起来看"）

| 表 | 行数 | 作用 |
|---|---|---|
| `session` | 78 | 时段（半天/一次出行），`review_config_json` 存当时的分组阈值 |
| `scene` | 303 | 场景（一次拍摄），属于一个 session；`group_revision` 记录分组算法版本 |
| `shot` | 7,371 | **拍摄组**：连拍/同一机位的重复构图合成一组，是精选与主题的**单位**。`retired_at`/`retired_reason` 支持"这个分组被撤销" |

> **`shot` 是这套系统的枢纽**：精选按组、主题按组、打包按组、语义命中也按组去重。
> 用户在界面上看到的"一格"几乎总是 shot，不是 asset。

### 4. 精选（Phase 2）

| 表 | 行数 | 作用 |
|---|---|---|
| `selection` | 8 | **当前状态**：shot → UNREVIEWED / CANDIDATE / SELECTED / FINAL |
| `selection_event` | 8 | 状态变更流水（from → to + 原因），可回溯 |

> 状态只有四档，且**只表达"你把这张放到哪一档"**，不表达好坏。见
> [`06-frontend.md`](06-frontend.md) 的视觉纪律。

### 5. 主题（Phase 2）

| 表 | 行数 | 作用 |
|---|---|---|
| `collection` | 3 | 主题（`uuid`、名字、封面 shot、排序、来源） |
| `collection_item` | 64 | 主题成员。**成员是 shot 不是 asset** —— 主题里"一组一格" |

### 6. 语义检索（Phase 3）

| 表 | 行数 | 作用 |
|---|---|---|
| `semantic_query` | 38 | **已准备的搜索词**：文本向量 + `prompt_text` + `concept_ratio`。界面下拉框的内容就是这张表 |
| `semantic_query_hit` | 1,797 | 某个词命中的 shot 快照（rank + score + 是否代表照片） |
| `semantic_feedback` | 67 | 用户对结果的表态（keep / drop）—— "越用越懂你"的数据基础 |
| `semantic_tuning` | 2 | 由表态学出来的**正/负向量**与权重（alpha/beta） |

> **为什么命中要存快照**：界面上的搜索结果是**服务端算好的**，本机没有 torch
> 也能显示。这就是 `search --save` 的意义（迁移 007 的注释：
> "把文本向量与命中快照写进库 —— 界面据此显示，无需 torch"）。

### 7. 人物（Phase 4）

| 表 | 行数 | 作用 |
|---|---|---|
| `face` | 6,368 | 检出的人脸：框、landmarks、检测分、是否正视、对称性、眼距比 |
| `face_embedding` | 6,368 | 人脸特征向量（SFace） |
| `face_verdict` | 34 | 单张脸的人工判定（"不是人"这类） |
| `person` | 40 | 人物（你命名的聚类） |
| `person_face` | 1,487 | 人物 ↔ 脸，带 `source`（用户确认 / 模型提议）与相似度 |
| `person_profile` | 40 | 每个人的**正/负原型向量** + 判定线 `threshold` + `margin` —— 模型"学到了什么"的实体 |
| `person_reject` | 80 | 明确否决的归属 |
| `person_event` | 424 | 人物操作流水（改名、确认、否决、合并…） |

> `person_face.source` 区分"**你说的**"与"**模型提议的**"。界面上两者视觉不同，
> 而且只有你说的才算学习依据。这不是 UX 细节，是数据模型层面的区分。

### 8. 任务与事件

| 表 | 行数 | 作用 |
|---|---|---|
| `job` | 58 | 后台任务：`type`、`status`、`scope_json`（任务参数）、进度、`retry_count`/`max_retries`、`claimed_by`、心跳 |
| `job_item` | 37,362 | 任务内的单条处理记录（每个文件一行）：`ref_key`、`state`、`attempts`、`last_error`。**单文件失败不停整批** |
| `app_event` | 64 | 全局事件流（导入、打包、命令执行、失败…），排查时的第一手材料 |

### 9. 元数据

| 表 | 行数 | 作用 |
|---|---|---|
| `schema_migrations` | 9 | 已应用的迁移：版本、名字、时间、**校验和**、应用版本 |

---

## 二、核心关系

```
library ──┬─ volume_library ── volume
          └─ asset ──┬─ metadata        (1:1)
                     ├─ preview         (1:N，按 kind)
                     ├─ embedding       (1:N，按 model)
                     └─ face ── face_embedding (1:1)
                          └─ person_face ── person ── person_profile (1:1)
                                          └─ person_reject

session ── scene ── shot ──┬─ selection ── selection_event
                           ├─ asset        (shot_id 反向指回)
                           └─ collection_item ── collection

semantic_query ──┬─ semantic_query_hit ── shot
                 ├─ semantic_feedback
                 └─ semantic_tuning   (1:1)

job ── job_item
app_event（独立）
```

**两个"枢纽"**：

- `shot` 是**业务**枢纽（精选/主题/打包/检索都按它聚合）；
- `asset` 是**物理**枢纽（路径、哈希、元数据、预览、向量都挂在它上面）。

`shot_id` 在 `asset` 上是**反向引用**（一个 asset 属于一个 shot），
而在 `selection`/`collection_item`/`semantic_query_hit` 上是被引用的主键。

---

## 三、迁移纪律

1. **只追加，不修改**。已发布的迁移文件改了会让 `verify_checksums()` 失败
   （`schema_migrations.checksum` 记着）。
2. 新增结构 = 新增 `NNN_*.sql`，编号递增。
3. 每次迁移都应**可重复运行**（`CREATE TABLE IF NOT EXISTS` 之类），
   因为 `apply_migrations` 可能被重跑。
4. 迁移要写清楚**为什么**。本仓库的迁移文件里都有注释解释设计取舍 ——
   这是这个项目最好的文档来源之一。

### 相关代码

| 关注点 | 位置 |
|---|---|
| 打开/连接/PRAGMA(WAL) | `db/connection.py` |
| 应用迁移、校验和、目标版本 | `db/migrator.py` |
| 迁移 SQL | `db/migrations/NNN_*.sql` |
| 目标版本（代码里写死） | `db.migrator.target_version()` |

---

## 四、改 schema 时容易踩的坑

- **`asset` 与 `metadata` 是两个表**。按拍摄时间筛选要 join `metadata`；
  只查 `asset` 拿不到时间。查询构造在 `api/queries.py`。
- **`embedding.vector` 是 BLOB**，不是 JSON。读出来要按 `dim` 与
  `normalization` 解释；不同 `model` 之间不可比较。
- **`job.scope_json` 是 JSON 文本**。`_job_dict()` 只对外暴露其中几项
  （`library_id`/`asset_ids` 之外的东西不外泄）。
- **`max_retries = 0` 曾经被读成 3**（`row["max_retries"] or 3` 的 `0 or 3`）。
  已修，但同类 `x or default` 的写法值得再审一遍 —— 计数、开关、枚举的 0
  都是有意义的。见 [`08-decisions.md`](08-decisions.md) 第二节。

---

返回 [`README.md`](README.md)
