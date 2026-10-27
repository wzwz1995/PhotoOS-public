# 10 · 照片导入的增量行为审计（2026-10）

> 审计问题：**新增照片的增量初始化有没有需要改的？**
> 方法：用一次性数据根真跑导入流程（不碰真实库），逐项核对行数与分组。
> 结论：**增量去重是对的；但"导入后立刻能用"这条链是断的。**

---

## 一、先确认没问题的部分（实测通过）

测试设置：6 张 JPEG 放进 `<源>/2026-05-01_1000-1100/`，走真实的
`classify_import.build_plan` + `execute_plan`（即界面「确认执行」走的同一对函数）。

| 场景 | 结果 | 判定 |
|---|---|---|
| 首次导入 | `copied=6 assets=6 metadata=6 hashed=6` | 正常 |
| **再次导入同一源** | `plan.duplicates=6 skipped_duplicate=6 copied=0 assets=0` | ✅ **不重复导入，不生成 `__2` 副本** |
| **源里新增 1 张后再导入** | `copied=1 assets=1`，原有 6 张不动 | ✅ **只处理增量** |
| 哈希 | 首次导入即写入 `content_hash`（复制时算的） | ✅ 去重的前提成立 |

**为什么重复导入能正确跳过**（`classify_import.drop_content_duplicates`）：

- 只在**检测到同名冲突**时才计算源文件哈希 —— 导入新照片时零额外开销；
- 比对的依据是目标库里那条记录的 `size` + `content_hash`；
- 而 `content_hash` 在 `execute_plan` 复制阶段就算好并写入了
  （`UPDATE asset SET content_hash = ?`），所以**同一个函数产生的数据天然自洽**。

> ⚠️ 这里有一个**隐含前提**值得记住：去重依赖"库里那条已经有哈希"。
> 如果某个资产的 `content_hash` 为空，`drop_content_duplicates` 会**保留**它
> （见该函数里 `if not reserved_hash: kept.append(item)`），
> 于是同一张卡再导一次就会多出 `__2` 副本。
> 正常路径不会这样（导入总是写哈希），但**手工改过数据库、或早期版本导入的数据**
> 需要留意。已在 `docs/02-backend.md` 的缺口一节记下。

---

## 二、⭐ 真正的缺口：导入之后，精选页与找照片页仍然是空的

### 现象

第一次导入完成后：

```
asset   : 6
metadata: 6
session : 1
scene   : 0     ← 空的
shot    : 0     ← 空的
```

而**精选页与「按时间/地点/场次」找照片都以 shot / scene 为单位**。
所以用户导入完照片、点进精选，看到的是"每个时段 0 Shot / 0 Scene"。

### 根因

`library/classify_import.py` 里**完全没有** `shot` / `scene` 的引用 ——
它只负责：计划 → 复制 → 落库（asset/metadata）→ 增量补预览。
分组是**另外三层派生数据**，由三条 CLI 命令产生：

```
photoos sessions backfill --apply
photoos shots regroup --apply
photoos scenes rebuild --apply
```

### 这三条命令没有任何自动调用者

全包搜索 `plan_regroup` / `group_shots(` / `group_by_directory(` 的调用点：

| 位置 | 是调用还是仅提及 |
|---|---|
| `cli.py` `cmd_scenes_rebuild` / `cmd_shots_regroup` | ✅ 真调用（命令行专用） |
| `shot_grouping.py` 自身 | 定义 |
| `repos/__init__.py` docstring、`shot_grouping.py` docstring | ❌ 只是注释里提到 |

**也就是说：`photoos shots regroup` 只能人手敲，界面里没有入口，
任务系统里也没有对应任务类型。**

### 顺序依赖（不能乱）

```
导入 → session 行（分类导入会建，或由 sessions backfill 建）
         ↓ 必须先有 session 行
       shots regroup --apply    ← 它的落库会检查 session 是否已回填
         ↓
       scenes rebuild --apply
```

好消息：`sessions backfill` 是**幂等**的 —— 它按 `(city_hint, period_hint)` 判断
已存在就更新、不存在才新建，所以对已导入的库重复跑是安全的。

### 界面现在的表现（已经做对了一半）

`review.js` 有一个专门的 `allEmpty` 分支，措辞是对的：

> 照片已索引，但**还没有分好组**（上面每个时段都显示 0 Shot / 0 Scene）。
> 在命令行执行：`photoos sessions backfill --apply` · `photoos shots regroup --apply` · `photoos scenes rebuild --apply`

**它说清楚了原因**（这一点做得对），但只给了命令行 —— 而这三条命令现在
**完全可以做成按钮**（白名单执行框架已经就位）。

---

## 三、修复方案（按性价比排序）

### A. ✅ 已做：新增任务 `regroup`，并挂到导入/索引末尾

新增任务类型 `regroup`，一次跑完三步（顺序：时段回填 → 拍摄组重算 → 场景重算）。
它会自动出现在：

- **`full_index` 的末尾** —— 跑完「完整索引」，精选与找照片立刻可用；
- **`classify_import` 的末尾** —— 导入完不用再记得点第二次。

三步都幂等，所以重复跑是安全的。失败只记一条 stage，**不让整个索引算失败** ——
索引本身已经成功了，分组可以单独重跑。

### B. ✅ 已做：三条命令进白名单目录

`actions/catalog.py` 新增「分组」组：

| id | 用途 |
|---|---|
| `regroup` | **一键跑完三层**（入队 `regroup` 任务，一条进度、可暂停） |
| `sessions_backfill` | 只回填时段 |
| `shots_regroup` | 只重算拍摄组 |
| `scenes_rebuild` | 只重算场景 |

单步命令留给"只调了阈值、只想重切场景"这类精细操作。

### C. ✅ 已做：精选页空态给按钮

`review.js` 的 `allEmpty` 分支从"打印三条命令"改成：
一个「现在分组」按钮 + 实时进度面板 +（折叠的）命令行。
按下就跑 `regroup` 任务，完成后自动重载这一页 —— 不需要用户自己刷新。

### D. ✅ 已做：导入结果面板补提示

导入开始后 `#import-result` 会说明任务会依次做
「复制 → 建索引 → 生成缩略图 → **自动分组**」，并点明分组是精选与找照片的前提。

---

## 五、修复过程中**新发现**的三个缺陷

这三个都是被 A/B/C/D 的实施过程逼出来的，且都在真实数据上验证过。

### 1. ⭐ 子进程用的是**另一个数据库**（最危险）

`_run_regroup` 通过子进程跑 CLI。但**子进程不继承父进程的 `settings`**：
`config.find_project_root()` 是从 `photoos.toml` 往上找项目根的，
所以不带环境变量时子进程会算出**它自己**的数据根。

在开发机上它恰好等于服务在用的那个 —— 于是"看起来能跑"，**而且不报错**，
实际却在另一个库上操作：子进程在 A 库分组，服务从 B 库读结果，界面毫无变化。

**怎么发现的**：用隔离数据根验证时，分组三步全部报"完成"，
但服务那个库的 shot/scene 仍然是 0。

**修法**：两个跑子进程的处理器（`_run_cli_action` 与 `_run_regroup`）
都显式传 `PHOTOOS_DATA_ROOT=<服务的数据根>`。
有测试守着（`test_subprocess_handlers_pin_the_data_root`）。

### 2. ⭐ `scenes rebuild` 会**累积孤儿 Scene 行**

`ShotRepo.replace_scenes()` 按 `name` 复用既有行，但**不删除这次没用到的旧行**。
于是每重算一次就积一批"零 Shot 的 Scene"。

**实证**：真实库 342 个 Scene 行里 **303 个有 Shot、39 个是空的**。
隔离数据根里重算两次，Scene 从 10 涨到 19（其中 9 条孤儿）。
界面按 Scene 列时段，所以同一个时段会**重复出现好几次**。

**修法**：循环结束后删掉 `existing` 里剩下的行（Scene 是纯派生结构 ——
没有任何表外键引用它，`shot.scene_id` 是 `ON DELETE SET NULL`，删除安全）。
有测试守着（`test_regroup_is_idempotent`）。

**并且清理了你库里已有的 39 条**（备份 `photoos.db.bak-before-scene-prune`）：
342 → 303，孤儿 0，shots 未动（7,371）。

### 3. ⭐ 自动分组会导致**无限递归**

`full_index` 的末尾调 `_run_regroup`，而 `_run_regroup` 是通过
`photoos shots regroup --apply` **子进程**跑的；CLI 的 `photoos index --wait`
又会执行 `full_index`……

```
full_index → 子进程 shots regroup → full_index → 子进程 … （无限）
```

**怎么发现的**：接上自动分组之后，跑一次完整测试套件**挂死**，
进程树里能看到一层套一层的 `photoos shots regroup --apply`。

**修法**：派生这类子进程时设 `PHOTOOS_SKIP_AUTO_REGROUP=1`，
子进程里的 `_run_regroup` 看到它就立刻返回。这样"自动分组"只在最外层发生一次。

### 附带影响：测试变慢

`full_index` / `classify_import` 的测试现在每次都要派生三个 CLI 子进程
（Python 启动开销 ×3）。`tests/unit/test_jobs.py` 从秒级变成几分钟。
**不影响正确性**，但值得知道。若将来嫌慢，可以在测试里设
`PHOTOOS_SKIP_AUTO_REGROUP=1` 跳过（守卫已支持），
另外单独用一个测试覆盖 `regroup` 本身。

---

## 六、仍未处理（低优先级）

1. **`sessions backfill --undo` 会连带删掉分类导入建的 session 行**：
   删除条件是 `source='auto' AND city_hint IS NOT NULL`，而分类导入建的也是这个形状。
   命令有说明"只删数据库行、不碰文件"，但**没有提示它会波及导入的时段**。
2. **无 EXIF 时的时段命名回落**：没有 EXIF 的照片，时段名会用文件 mtime。
   真实照片有 EXIF 不受影响；但"相机没写时间"那批会落到当天，
   可能把不同时期的照片挤进同一时段。
   （审计脚本里那些合成图片就是这样，所以看到 `2026-10-16_0110-0110` 这类名字。）
3. **`replace_scenes` 只在"名称变化"时才产生孤儿**。上面的修复已经覆盖，
   但根因是"Scene 身份 = 名称"这个设计 —— 如果时段名本身变了（例如上面第 2 条），
   旧的 Scene 就会被删掉重建，`shot.scene_id` 会被重新指派。这是预期行为，
   记在这里免得下一个人以为丢了数据。

---

返回 [`README.md`](README.md) · 相关：[`04-jobs.md`](04-jobs.md)
