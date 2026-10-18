# 41 · CLI 命令参考

> **本篇由脚本生成，不要手改** —— 它是 `photoos <命令> --help` 的转写。
> 重新生成（命令有变动时）：
> `python docs/20-reference/tools/make_cli_reference.py`
>
> 【实测】顶层命令 **30 个**（`photoos --help` 实测），
> 参数列表来自每个命令的 `--help` 原文。

---

## 0. 三条纪律（先读）

1. **默认只预演**：`import` 这类会写数据的命令**必须显式加开关**才落盘
   （`import` 要 `--apply`）。这是项目刻意的安全设计，不是啰嗦。
2. **导入是唯一写入照片库的能力**：其余命令碰原片只读
   （见 `70-安全边界与硬规则.md`）。
3. **AI 命令可能跑在另一个解释器上**：`embed`/`search`/`people` 需要
   `torch`/`onnxruntime`/`cv2`，而产品 venv 故意不装（见 `50-AI子系统.md`）。
   命令行直跑时，请用配置里 `ai_python` 指向的解释器。

---

## 1. 命令总表

| 分组 | 命令 | 一句话 |
| --- | --- | --- |
| 安装与自检 | [`init`](#init) | 初始化数据目录与数据库（可重复执行，幂等） |
| 安装与自检 | [`doctor`](#doctor) | 环境与数据库自检 |
| 安装与自检 | [`config`](#config) | 显示当前配置 |
| 安装与自检 | [`version`](#version) | 显示版本与迁移清单 |
| 库与卷 | [`library`](#library) | Library 管理（添加 / 探测 / 列表） |
| 索引（读原片） | [`scan`](#scan) | 发现阶段：遍历 Library 建立/更新索引（只读文件系统） |
| 索引（读原片） | [`hash`](#hash) | 哈希阶段：计算 BLAKE3 内容哈希（IO 受限，可暂停续跑） |
| 索引（读原片） | [`fused`](#fused) | 融合阶段：一次读盘同时算哈希与生成缩略图/预览（比分别跑省一遍读盘） |
| 索引（读原片） | [`preview`](#preview) | 预览阶段：只生成缩略图与大图预览（不重算内容哈希） |
| 索引（读原片） | [`index`](#index) | 完整索引：发现 → 元数据 → 预览 → 融合（一次读盘）→ 兜底 |
| 索引（读原片） | [`repair`](#repair) | 修复派生数据与数据库不一致（不动原片） |
| 索引（读原片） | [`duplicates`](#duplicates) | 报告内容重复的文件（只报告，不移动不删除） |
| 索引（读原片） | [`stats`](#stats) | 显示索引统计（数量/类型/状态/元数据与预览覆盖） |
| 索引（读原片） | [`crosscheck`](#crosscheck) | 与既有 PowerShell 工具产物交叉校验（两套独立实现互证，只读） |
| 导入（写照片库） | [`import`](#import) | 从 SD 卡/暂存目录/文件夹导入照片（唯一写入照片库的能力，默认只预演） |
| 导入（写照片库） | [`geo`](#geo) | 离线地理缓存：查看 / 人工指认（导入分类不联网） |
| 分组 | [`sessions`](#sessions) | Session（时段）管理：把既有 <城市>\<时段> 目录补成 session 行 |
| 分组 | [`shots`](#shots) | Shot（一次拍摄）管理：按时间 + subsec 分组 |
| 分组 | [`scenes`](#scenes) | Scene（拍摄场景）管理：把时段切成可一次筛完的块 |
| AI | [`ai`](#ai) | 嵌入模型与运行时（Phase 2 相似度切片） |
| AI | [`embed`](#embed) | 计算视觉嵌入并落库（只读 preview，不读原片；默认只预演） |
| AI | [`search`](#search) | 文字查照片（Phase 3：缩小候选集，不替你选；需 torch 环境） |
| AI | [`find`](#find) | 按时间/地点/场次精确取照片（确定性，不依赖模型；比语义检索准） |
| AI | [`people`](#people) | 分人：检测人脸 → 特征 → 聚类出候选 → 你命名 → 持续学习你的判断 |
| 服务与任务 | [`serve`](#serve) | 启动本地 Web UI（默认 127.0.0.1） |
| 服务与任务 | [`jobs`](#jobs) | 查看与管理后台任务 |
| 数据安全 | [`backup`](#backup) | 在线备份数据库（VACUUM INTO） |
| 数据安全 | [`migrate`](#migrate) | 换机迁移包：导出 / 校验 / 导入（PRD §5.1） |
| 数据安全 | [`integrity`](#integrity) | 数据库完整性与一致性检查 |
| 断点测试 | [`breakpoint`](#breakpoint) | 执行 PRD 断点测试并生成报告（--phase 1|2） |

> 【实测】完整列表：`init`、`doctor`、`config`、`version`、`library`、`migrate`、`backup`、`integrity`、`serve`、`breakpoint`、`repair`、`import`、`crosscheck`、`scan`、`index`、`hash`、`fused`、`preview`、`geo`、`duplicates`、`stats`、`ai`、`embed`、`search`、`find`、`people`、`sessions`、`shots`、`scenes`、`jobs`。

---

## 2. 逐命令参数

### 安装与自检

*第一次上手机器时按顺序跑*

<a id="init"></a>

#### `photoos init`

```
photoos init [-h] [--data-root PATH]
```

* --data-root PATH  覆盖数据根目录

<a id="doctor"></a>

#### `photoos doctor`

```
photoos doctor [-h]
```

<a id="config"></a>

#### `photoos config`

```
photoos config [-h] [--json]
```

* --json      以 JSON 输出

<a id="version"></a>

#### `photoos version`

```
photoos version [-h]
```

---

### 库与卷

*登记照片库、换盘符后重绑*

<a id="library"></a>

#### `photoos library`

```
photoos library [-h] <子命令> ...
```

子命令：

* `library probe` — 只读探测一个路径相对于现有 Library 的身份
* `library add` — 登记一个 Library
* `library list` — 列出已登记的 Library 与卷
* `library rebind` — 盘符变化/整盘更换后重新绑定（PRD §5.2）
* `library verify` — 检查 Library 是否可达与索引一致性
* `library offline` — 标记 Library 离线（硬盘暂时不接）

---

### 索引（读原片）

*建立/更新索引；这些是唯一会大量读原片的命令*

<a id="scan"></a>

#### `photoos scan`

```
photoos scan [-h] [--library ID|NAME] [--quiet] [--force]
```

* --library ID|NAME  指定 Library（默认需唯一）
* --quiet            不打印逐条进度
* --force            忽略已有未完成任务，强制新建

<a id="hash"></a>

#### `photoos hash`

```
photoos hash [-h] [--library ID|NAME] [--quiet] [--force]
```

* --library ID|NAME  指定 Library（默认需唯一）
* --quiet            不打印逐条进度
* --force            忽略已有未完成任务，强制新建
* --limit LIMIT      本次最多处理多少个文件

<a id="fused"></a>

#### `photoos fused`

```
photoos fused [-h] [--library ID|NAME] [--quiet] [--force]
```

* --library ID|NAME  指定 Library（默认需唯一）
* --quiet            不打印逐条进度
* --force            忽略已有未完成任务，强制新建
* --limit LIMIT      本次最多处理多少个文件

<a id="preview"></a>

#### `photoos preview`

```
photoos preview [-h] [--library ID|NAME] [--quiet] [--force]
```

* --library ID|NAME  指定 Library（默认需唯一）
* --quiet            不打印逐条进度
* --force            忽略已有未完成任务，强制新建
* --limit LIMIT      本次最多处理多少个文件
* --prefix 相对路径      只处理该子树（例如 `杭州市\2026-03-01_0900-1100`）
* --regenerate       为该范围内全部在库文件重新生成缩略图与预览（含已存在的）

<a id="index"></a>

#### `photoos index`

```
photoos index [-h] [--library ID|NAME] [--quiet] [--force] [--wait]
```

* --library ID|NAME  指定 Library（默认需唯一）
* --quiet            不打印逐条进度
* --force            忽略已有未完成任务，强制新建
* --wait             原地跑完该 Library 的全部剩余工作（先发现一次，再按剩余量驱动，最多 5 轮）

<a id="repair"></a>

#### `photoos repair`

```
photoos repair [-h] [--library ID|NAME] [--apply]
```

* --library ID|NAME  指定 Library（默认需唯一）
* --apply            实际执行修复（默认只预演）

<a id="duplicates"></a>

#### `photoos duplicates`

```
photoos duplicates [-h] [--library ID|NAME] [--quiet] [--force]
```

* --library ID|NAME  指定 Library（默认需唯一）
* --quiet            不打印逐条进度
* --force            忽略已有未完成任务，强制新建
* --samples SAMPLES  展示多少个重复组（默认 5）

<a id="stats"></a>

#### `photoos stats`

```
photoos stats [-h] [--library ID|NAME]
```

* --library ID|NAME  指定 Library

<a id="crosscheck"></a>

#### `photoos crosscheck`

```
photoos crosscheck [-h] [--library ID|NAME] [--legacy-index CSV]
```

* --library ID|NAME     指定 Library（默认需唯一）
* --legacy-index CSV    既有 library-index.csv
* --legacy-hashes CSV   既有 dup-hash-cache.csv
* --hash-samples HASH_SAMPLES
* 哈希抽样数量（默认 12）

---

### 导入（写照片库）

*把 SD 卡/暂存目录的照片复制进库；**默认只预演***

<a id="import"></a>

#### `photoos import`

```
photoos import [-h] [--library ID|NAME] [--video-library ID|NAME]
```

* --library ID|NAME     目标照片库（默认需唯一）
* --video-library ID|NAME
* 目标视频库（源中含视频时必填；照片与视频要一起切段，见方案 §1.3.6）
* --subdir SUBDIR       （--no-classify 时）在 Library 内使用的子目录
* --limit LIMIT         最多导入多少个文件
* --apply               实际执行复制（默认只预演）
* --no-classify         不做物理分类，照搬源的目录结构（旧行为）
* --geo-cache CSV       GPS→城市缓存路径（默认用数据根下的 geo-cache.csv）
* --plan-out CSV        把归类计划写成 CSV（可审计/可复核）
* --gap-hours GAP_HOURS
* 时段切分间隔（小时），默认 12
* --city 城市名            整批强制指定城市（例如「雅典」）—— 用于相机整段没有 GPS、或境外拍摄不在离线缓存里的情况；给出后压过
* GPS 判定
* --no-index            复制后不自动补预览（留给你稍后手动 index）
* --quiet               不打印逐项进度

<a id="geo"></a>

#### `photoos geo`

```
photoos geo [-h] <子命令> ...
```

子命令：

* `geo list` — 列出缓存里的城市与网格数
* `geo assign` — 把某个 GPS 网格指认给一个城市

---

### 分组

*时段 / 拍摄组 / 场景*

<a id="sessions"></a>

#### `photoos sessions`

```
photoos sessions [-h] <子命令> ...
```

子命令：

* `sessions backfill` — 从既有目录派生 session 行（默认只预演；不读取任何照片文件）

<a id="shots"></a>

#### `photoos shots`

```
photoos shots [-h] <子命令> ...
```

子命令：

* `shots regroup` — 重算 Shot 分组（默认只预演；不读取任何照片文件）

<a id="scenes"></a>

#### `photoos scenes`

```
photoos scenes [-h] <子命令> ...
```

子命令：

* `scenes rebuild` — 重算 Scene 划分（默认只预演；不读取任何照片文件）
* `scenes merge` — 把第二个 Scene 合并进第一个
* `scenes split` — 在某个 Shot 之后把 Scene 一分为二
* `scenes unpin` — 解除人工标记，让重算重新接管这个 Scene

---

### AI

*向量与人脸（需要 AI 解释器，见 50 号文档）*

<a id="ai"></a>

#### `photoos ai`

```
photoos ai [-h] <子命令> ...
```

子命令：

* `ai fetch-model` — 下载并校验模型（clip = 视觉 / gemma = 中文查询 / face = 人脸）
* `ai doctor` — 报告嵌入运行时的真实设备（跑一次真推理计时）

<a id="embed"></a>

#### `photoos embed`

```
photoos embed [-h] [--library ID|NAME] [--limit LIMIT]
```

* --library ID|NAME     只处理指定 Library（默认全部）
* --limit LIMIT         本次最多算多少张
* --engine {clip,gemma}
* clip（默认）= 视觉相似度，供 Scene 划分；gemma = EmbeddingGemma
* 2，供中文文字查询（需 torch，慢约 7 倍）
* --batch BATCH         批大小（clip 默认 16；gemma 默认 8，实测最优）
* --device {auto,cuda,cpu}
* 设备（默认 auto）
* --dry-run             只统计待办数量，不写库
* --apply               实际写入数据库
* --quiet               不打印进度

<a id="search"></a>

#### `photoos search`

```
photoos search [-h] [--top TOP] [--prompt 英文串] [--save] [--preset JSON]
```

* --top TOP             返回多少个拍摄组（默认 24）
* --prompt 英文串          实际送进模型的 prompt（默认用查询本身）。实测英文 prompt 更稳，可让界面标签是中文、底层用英文
* --save                把文本向量与命中快照写进库（迁移 007）—— 界面据此显示，无需 torch
* --preset JSON         批量预置概念（界面下拉框的内容）。JSON 形如
* [{"label":"日出","prompt":"sunrise","ratio":1.37}, ...]
* --min-ratio MIN_RATIO
* 配合 --preset：只预置凝聚度比值 ≥ 此值的概念（建议 1.20）
* --hits HITS           配合 --preset：每个概念存多少条命中（默认 100）
* --engine {gemma}      目前只有 gemma（EmbeddingGemma 2）—— CLIP 的中文实测失效
* --library ID|NAME     只在该 Library 内搜索（默认全部）
* --device {auto,cuda,cpu}
* 设备（默认 auto）

<a id="find"></a>

#### `photoos find`

```
photoos find [-h] [--city 城市] [--session 关键词] [--date-from YYYY-MM-DD]
```

* --city 城市             按城市（库里 19 个，见 `photoos stats`）
* --session 关键词         按场次目录名/城市名模糊匹配，如 2025-10-18
* --date-from YYYY-MM-DD
* 起始日期（含）
* --date-to YYYY-MM-DD  结束日期（含）
* --scene ID            直接指定 Scene
* --min-shots N         只要 Shot 数 ≥ N 的 Scene
* --library ID          限定 Library
* --limit LIMIT         最多列几个 Scene（默认 60）

<a id="people"></a>

#### `photoos people`

```
photoos people [-h] <子命令> ...
```

子命令：

* `people detect` — 检测人脸并落库（YuNet；只读 preview，不碰原片）
* `people embed` — 计算人脸特征（SFace 128 维）
* `people discover` — 把未归类的正面清晰脸聚成候选人物（结果是**未命名**的）
* `people name` — 给候选人物起名（名字只能由你给）
* `people merge` — 把两个候选合并成一个人（模型把同一人拆成两簇时用）
* `people assign` — 用当前原型给未归类的脸找主人（增量；模型的判断，等你确认）
* `people learn` — 从你的确认/否决里重新估计原型与阈值（重算，不是训练）
* `people status` — 分人流水线状态与每个人的可信度

---

### 服务与任务

*Web 界面与后台任务*

<a id="serve"></a>

#### `photoos serve`

```
photoos serve [-h] [--host HOST] [--port PORT]
```

* --host HOST           监听地址（默认取配置，127.0.0.1）
* --port PORT           监听端口（默认取配置，8760）
* --log-level {critical,error,warning,info,debug,trace}
* --log-file [LOG_FILE]
* 把日志写进文件（不带值 = 数据目录的
* logs/serve.log）。要长期开着、或者从会被回收的后台启动时用它 —— uvicorn
* 默认把每个请求都打到 stdout，量很大。

<a id="jobs"></a>

#### `photoos jobs`

```
photoos jobs [-h] <子命令> ...
```

子命令：

* `jobs list` — 列出最近任务（默认含已结束）
* `jobs show` — 查看单个任务详情与失败明细
* `jobs pause` — 暂停任务（在下一个检查点停下，进度保留）
* `jobs resume` — 恢复已暂停或失败的任务（已完成的明细不重做）
* `jobs cancel` — 取消任务
* `jobs recover` — 回收因进程中断而卡住的任务（回退为待执行）

---

### 数据安全

*备份、迁移、完整性*

<a id="backup"></a>

#### `photoos backup`

```
photoos backup [-h] [--label LABEL] [--keep KEEP] [--list]
```

* --label LABEL  备份标签（默认 manual）
* --keep KEEP    备份后仅保留最近 N 份
* --list         只列出已有备份

<a id="migrate"></a>

#### `photoos migrate`

```
photoos migrate [-h] <子命令> ...
```

子命令：

* `migrate export` — 导出迁移包（照片原文件默认不入包）
* `migrate verify` — 校验迁移包完整性与版本兼容性
* `migrate import` — 导入迁移包（自动备份现有数据根）

<a id="integrity"></a>

#### `photoos integrity`

```
photoos integrity [-h]
```

---

### 断点测试

*PRD 验收断点，需要真实照片库*

<a id="breakpoint"></a>

#### `photoos breakpoint`

```
photoos breakpoint [-h] [--phase {1,2}] [--library ID|NAME]
```

* --phase {1,2}      1 = PRD §9.1 照片库基础（11 项，默认）；2 = PRD §9.2 精选系统（9 项）
* --library ID|NAME  指定 Library（默认需唯一）
* --periods PERIODS  代表性时段数量（默认 4，仅 phase 1）
* --full             对整个库做全量验收（更慢，仅 phase 1）
* --output PATH      报告输出路径（默认写入 data/logs）

---

返回 [`00-文档规范与索引.md`](00-文档规范与索引.md) · 相关：[`31-任务引擎与流水线.md`](31-任务引擎与流水线.md)（界面能触发的子集）
