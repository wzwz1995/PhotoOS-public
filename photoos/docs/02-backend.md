# 02 · 后端模块地图

包根：`backend/photoos/`。约 20k 行 Python。下面按子包说明**职责**与**关键模块**。

> 读代码时请优先看各模块顶部的 docstring —— 本项目的注释习惯是写"**为什么**"
> （含"怎么发现的"），那是这个仓库最好的文档来源。

---

## `api/` — HTTP 层

FastAPI，**纯本地服务**：默认只监听 `127.0.0.1`，没有鉴权且有意如此
（单用户本地优先，PRD §2.6）。破坏性操作在这一层**没有入口**。

| 模块 | 行数 | 职责 |
|---|---|---|
| `app.py` | ~3400 | `create_app(settings)`，约 82 个路由内联注册 |
| `queries.py` | ~940 | 只读查询层。**搜索与筛选只访问数据库索引，绝不扫描原片**；`SORT_SQL` 是排序白名单，用户输入永不进入 SQL 文本 |
| `filter_query.py` | ~270 | 结构化筛选条件的解析与编译 |
| `structured_find.py` | ~90 | 按时间/地点/场次的确定性查找（不依赖模型） |

**一条关键纪律**：所有可能慢的数据库调用都通过 `_read()`（内部 `anyio.to_thread`）
下放，避免阻塞事件循环。

**路由分组**：`/api/health|cli|stats|config|integrity`、`/api/libraries*`、
`/api/assets*`、`/api/browse/tree`、`/api/sessions|scenes|shots|selection|collections`、
`/api/semantic/*`、`/api/people*|faces/*`、`/api/find/scenes`、`/api/facets`、
`/api/jobs*`、`/api/actions*`、`/api/exports*`、`/api/import/*`、`/api/maintenance/*`。

---

## `jobs/` — 后台任务

队列存在 SQLite 里，**不引入 Redis/Celery**。支持暂停、恢复、取消、失败重试、
断点续跑与孤儿回收。详见 [`04-jobs.md`](04-jobs.md)。

| 模块 | 职责 |
|---|---|
| `queue.py` | `JobQueue` / `JobRecord` / `JOB_TYPES` / `executor_id()`；认领、心跳、孤儿回收的状态机 |
| `runner.py` | `JobRunner`（同步执行器）、`BackgroundRunner`（守护线程）、`JobReport`；各 `_run_*` 处理器 |
| `pipelines.py` | 四个索引阶段 `run_discovery/run_hash/run_metadata/run_previews`，以及 `run_fused_hash_previews`、`StageResult`、`DuplicateReport`、`library_stats` |

---

## `library/` — 库身份、只读遍历、分类、分组、向量、人脸

最大也最重要的一层。

### 身份与路径（**这一组是安全边界**）

| 模块 | 职责 |
|---|---|
| `store.py` | `LibraryStore`、`probe_path`、`write_marker`、`compute_fingerprint`、`assert_within_root` |
| `paths.py` | 相对路径校验。**必须**拒绝盘符、UNC 前缀、前导斜杠、`..` 上跳、绝对路径 —— "保证「盘符永远不会成为照片的永久身份」这一条在代码层面无法被绕过" |
| `volume.py` | Win32 卷序列号与文件身份。**只读打开**，允许共享读写删除 |
| `walker.py` | 只读遍历。"只做 `os.scandir` / `stat`，绝不打开文件内容、绝不修改任何东西" |

> ⚠️ **卷序列号是必要但不充分的判据**（迁移 002 记录的真实事故）：
> `C:\Photos` 与 `E:\视频` 在**同一块物理盘**上，所以"卷序列号命中 → 同一 Library"
> 把视频库误判成照片库并改写了它的路径提示。现在用 `volume_library` 多对多表。

### 导入与分类

| 模块 | 职责 |
|---|---|
| `classify.py` | 物理分类**内核**。"纯计算模块 —— 不读原片、不写文件、不碰数据库" |
| `classify_import.py` | 把计划落到磁盘：计划 → 复制 → 落库 → 增量补预览 |
| `importer.py` | "PhotoOS 唯一会**写入照片库**的能力"。复制到 `<target>.photoos-part` → 比对 size 与**内容哈希** → `os.replace` 原子改名 |
| `geo.py` | 离线地理缓存（网格直查 + 最近城市） |

### 派生数据

| 模块 | 职责 |
|---|---|
| `shot_grouping.py` | 拍摄组：连拍/同一机位合成一组。只用时间 + `subsec` 切分 |
| `scene_grouping.py` | 场景：时间间隔 → 相似度合并/拆分 → 尺寸上限 |
| `session_derive.py` | 从既有目录反推"时段" |
| `selection.py` | 四态精选状态机 |
| `semantic_rank.py` | 读取端的学习重排（numpy-only，**serve 不需要 torch**） |
| `faces.py` | 人脸检测与特征（YuNet + SFace） |

### 向量（见 [`05-ai.md`](05-ai.md)）

`embedding.py`（CLIP/ONNX）、`embedding_gemma.py`（EmbeddingGemma/torch）、
`embed_pipeline.py`（流水线）、`vectorstore.py`（向量契约）、`model_store.py`（模型下载与校验）。

### 其它

`export.py`（打包原片为 zip，只读）、`migration.py`（跨机迁移包）、
`rebind.py`（换盘符后重绑定，**只改数据库**）。

---

## `media/` — 统一解码适配层（PRD §8 LOCKED）

> 业务层**不得**直接依赖 Windows 系统图片查看器或系统编解码器。
> RAW 走 rawpy/LibRaw，HEIC 走内置 libheif，视频走随包 ffmpeg。

| 模块 | 职责 |
|---|---|
| `adapter.py` | `decode_capabilities`、`load_image`、`generate_previews`；两级 WebP 分桶 |
| `metadata.py` | `extract_metadata`；rawpy → Pillow `getexif()` → exifread 兜底 |
| `detect.py` | **先看魔术数字**再判类型；遍历阶段允许"只按扩展名" |
| `video.py` | `probe_video`、`extract_cover_frame`；纯 Python 的 ISO-BMFF box 解析（**刻意不用 ffprobe**） |

---

## `db/` 与 `repos/` — 数据访问

| 模块 | 职责 |
|---|---|
| `db/connection.py` | `Database`；每次调用独立连接、`sqlite3.Row`、WAL |
| `db/migrator.py` | 迁移发现、应用、校验和、目标版本；迁移前自动 `VACUUM INTO` 备份 |
| `db/migrations/*.sql` | 结构定义（只追加，不修改） |
| `repos/__init__.py` | **一个 ~3500 行的模块**，装着所有仓储类 |

**一条铁律**（`repos` docstring）：

> **永远不删除 asset 行来表示文件消失**：只把 `status` 置为 MISSING。
> 删除数据库记录 ≠ 删除原文件；反过来，文件暂时不可见也绝不能丢索引。

仓储 → 表：`AssetRepo`(asset, asset_instance)、`MetadataRepo`(metadata)、
`PreviewRepo`(preview)、`ShotRepo`(shot, scene)、`SessionRepo`(session)、
`SelectionRepo`(selection, selection_event)、`CollectionRepo`(collection, collection_item)、
`EmbeddingRepo`(embedding)、`SemanticRepo`(semantic_query*, semantic_feedback, semantic_tuning)、
`FaceRepo`(face, face_embedding, face_verdict)、
`PeopleRepo`(person, person_face, person_reject, person_profile, person_event)。

---

## `actions/` — 白名单命令（2026-10 新增）

| 模块 | 职责 |
|---|---|
| `catalog.py` | 命令目录（固定 `argv` 模板）+ 搜索词校验 + 解释器选择 + 环境探测 |
| `__init__.py` | 对外门面 |

**安全边界**：浏览器只能提交一个 **action id**，不能提交命令字符串。
详见 [`08-decisions.md`](08-decisions.md) 第四节。

---

## 顶层模块

| 模块 | 职责 |
|---|---|
| `cli.py` | 命令行入口（约 4000 行，含 `build_parser`） |
| `config.py` | `Settings`（pydantic-settings）：环境变量 `PHOTOOS_*` > `data/config.json` > 默认值 |
| `errors.py` | 异常层级。`PhotoOSError` 是根；`SafetyError` 表示违反资产安全铁律 |
| `hashing.py` | BLAKE3 封装。**只读** |
| `breakpoint.py` | PRD 断点验收（不参与运行） |

---

## 已知缺口（2026-10 核实）

1. ~~**四个任务类型没有处理器**~~ —— **已修**。`rebind` / `verify` / `backup` / `migrate`
   都已实现，并有 `test_every_job_type_has_a_handler` 逐项对账。
   唯一的例外是 `migrate` 的 `action="import"`：它要**替换整个数据根**，
   不能在服务运行时执行，所以显式报错并把用户引到停机后的 CLI。
   见 [`09-changelog.md`](09-changelog.md) 第二轮。

2. **`--measure` 与 help 页的三个限宽**：现在按角色令牌化为
   `--measure`（容器）/ `--measure-prose` / `--measure-list` / `--measure-code`。
   三个值不同是**有意的层次**（正文段 / 列表 / 命令行），不是漂移。

3. **色阶中未被取用的档位**（`--royal-100`、`--gold-900`、若干 `--*-aNN` 等）
   **有意保留** —— 它们是调色板本身。判断口诀写在 `tokens.css` 末尾：
   **"没人用"不等于"该删"**，要先分清是"命名遗留""调色板档位"还是"抽了令牌没接线"。
   前两类中的命名遗留（9 个兼容别名）已删；第三类已接上。

---

返回 [`README.md`](README.md)
