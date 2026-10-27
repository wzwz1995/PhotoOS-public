# 04 · 任务系统

后台任务是这个应用能做"重活"的唯一方式。凡是可能超过几秒的操作，都做成任务，
而不是同步 HTTP 请求 —— 因为同步请求会超时，而任务天然有进度、暂停、取消与恢复。

## 组件

| 类 | 文件 | 职责 |
|---|---|---|
| `JobRecord` | `jobs/queue.py` | 一行任务的只读视图（含 `scope`、进度、重试计数） |
| `JobQueue` | `jobs/queue.py` | 创建 / 认领 / 更新进度 / 完成 / 失败 / 取消 / 暂停 / 恢复 / 孤儿回收。**只管状态，不干活** |
| `JobRunner` | `jobs/runner.py` | 真正执行：按 `type` 分发到 `_run_*` 处理器 |
| `BackgroundRunner` | `jobs/runner.py` | 守护线程，循环"认领一个 → 跑一个"。Web 层用它 |
| `JobReport` | `jobs/runner.py` | 一次执行的报告（`stages` 字典，供 CLI 输出与留档） |

`JobRunner` 与 `BackgroundRunner` 分开，是为了让 CLI 的 `--wait` 模式能**同步**跑
同一个执行器，而不需要起线程。

## 状态机

```
PENDING ──claim──> PROCESSING ──┬──> COMPLETED   （终态）
   ▲                            ├──> FAILED      （终态）
   │                            ├──> CANCELLED   （终态）
   │                            ├──> PAUSED ──resume──> PENDING
   └────── RETRY ◄──────────────┘   （未超重试上限时）
```

- `TERMINAL_STATUSES = {COMPLETED, FAILED, CANCELLED}`
- `ACTIVE_STATUSES = {PENDING, PROCESSING, RETRY, PAUSED}`
- 界面上的七种状态**都有样式**（`app.css` 的 `.state.*`）。历史上 `PENDING` 与
  `CANCELLED` 漏在名单外，界面上是两坨没有样式的灰字。

## 认领与所有权（`claimed_by`）

任务带一个**执行者身份**（`executor_id()`）。认领时只取"无归属"或"归属自己"的任务。

**为什么需要它**：真实事故 —— `photoos serve` 的后台执行器与命令行的
`photoos index` **同时**从同一张 job 表认领任务、并行执行，在 60 MB/s 的 USB 盘上
互相抢带宽并重复读文件。归属字段让两者不再抢同一个任务。

## 重试

- 默认 `max_retries = 3`。
- 单个文件失败**只记 `job_item`**（`state='FAILED'` + `last_error`），**不停整批**。
- ⚠️ `cli_action` 类型**不重试**（创建时强制 `max_retries = 0`）。理由：这类命令是
  人工触发、幂等、且报错信息已经很清楚；闷头重跑 3 次只会让用户在任务表里
  看到同一条命令失败 4 遍。
- ⚠️ 读取时**不能写** `int(row["max_retries"] or 3)` —— `0 or 3` 求值为 3，
  会把"不重试"读成"重试 3 次"。必须判 `None`。这是修过的一个真 bug。

## 崩溃恢复（孤儿回收）

进程被杀（断电、任务管理器结束）后，任务会停在 `PROCESSING`。
`BackgroundRunner.start()` 与**运行期每 60 秒**都会调
`JobQueue.recover_orphans(owner=...)`：把**心跳过期**（默认 300 s）的 `PROCESSING`
回退为 `PENDING`、**清空归属**、写一条 `error='进程中断，已自动恢复为待执行'`。

两条容易搞错的地方：

- **回收不递增 `attempts`**。它统计的是"**失败的尝试**"，而回收时任务并没有真的
  再执行过一次；把它算一次会让"重试次数"失去意义。真正失败才 +1（`fail()`）。
  （旧版这里写着 `attempts+1`，与实现不符，已订正。）
- 旧的"只在启动时回收一次"有个坑：崩溃后**立刻**重启时，上一进程的心跳距今
  还不到 300 秒 → 那一行不会被回收，会一直停在 `PROCESSING`。所以现在
  `BackgroundRunner` 在主循环里**周期性**回收（`recover_interval=60s`）。

## 进度上报

处理器通过 `progress(done, total, note)` 上报（由 `_make_progress()` 包装），
它做两件事：写 `job.progress_done/total/progress_note`，并回调调用方（CLI 打印）。

界面轮询 `GET /api/jobs` 显示任务表。**没有 WebSocket / SSE** —— 任务本身在
服务端跑，关掉页面也不中断，多开一条流式通道只会多一套要同步的状态。

## 任务类型一览（`JOB_TYPES`）

### 索引流水线

| type | 做什么 | 并发策略 |
|---|---|---|
| `scan` | 发现新文件/消失文件，写 `asset`，标记 MISSING | 遍历文件系统，16k 文件 ≈ 1 秒 |
| `hash` | 算内容哈希（BLAKE3） | **单线程顺序**。外置盘冷读 55~71 MB/s，多线程读同一块盘无收益 |
| `metadata` | 提取 EXIF/视频元数据 | **线程池**。rawpy/Pillow 解码释放 GIL，6 线程实测 9× |
| `preview` | 生成缩略图与预览 | 同上（CPU 瓶颈） |
| `fused` | 上述阶段的合并执行 | —— |
| `full_index` | 一次跑完 scan → metadata → preview → hash | 组合 |

> 为什么 `hash` 与 `scan` 分开：发现只要 1 秒，哈希 252 GB 要 70~80 分钟。
> 分开后用户可以**先看到库**，再让哈希在后台慢慢跑。

### 导入

| type | 做什么 |
|---|---|
| `classify_import` | 基础物理分类导入：计划 → 复制 → 落库 → 只对新增文件补预览。**唯一会写照片库的任务**，且只复制、不改名、不移动源文件 |

### 产出

| type | 做什么 |
|---|---|
| `export` | 把一批原片打包成一个 zip（人物/主题的「一键打包」）。只读原片（`open(...,'rb')` + `zipfile.write`） |

### 界面直跑命令

| type | 做什么 |
|---|---|
| `cli_action` | 执行一条**白名单命令**（见 [`05-ai.md`](05-ai.md) 与 [`08-decisions.md`](08-decisions.md)） |

`scope` = `{action_id, query?, argv, python, command}`。`argv` 由服务端从模板展开，
浏览器只能提交 `action_id`。

### 其它

`rebind`（重新绑定盘符）、`verify`（校验）、`migrate`（迁移）、`backup`（备份）
**都已有处理器**（2026-10 补齐；此前只登记在 `JOB_TYPES` 里而没有实现，
用 `POST /api/jobs` 建它们会得到一个永远跑不起来的任务）。
有测试逐项对账：`test_every_job_type_has_a_handler`。

`migrate` 的 `action="import"` **故意拒绝**：它要替换整个数据根，
不能在服务运行时做。

### `regroup` —— 派生分组（2026-10 新增）

一次跑完三层：**时段回填 → 拍摄组重算 → 场景重算**。

**为什么需要它**：界面上的「精选」与「按时间/地点找照片」**以 Shot / Scene 为单位**，
而导入只建到 `session` 一层。在此之前分组只能靠手敲三条命令
（`plan_regroup` 在整个包里没有一个调用者），于是新库导入完点进精选
看到的是"0 Shot / 0 Scene"。

它会自动出现在 `full_index` 与 `classify_import` 的**末尾**，所以导入完就能用。

---

## 派生 CLI 子进程时的两条硬规矩

`_run_cli_action` 与 `_run_regroup` 都是通过**子进程**跑 CLI 的。子进程不会继承
父进程的 `settings`，所以有两个环境变量必须显式传：

| 变量 | 不传的后果 |
|---|---|
| `PHOTOOS_DATA_ROOT` | 子进程用 `find_project_root()` 自己算数据根 —— 在开发机上**恰好等于**服务的那个，于是"看起来能跑"，实际在**另一个库**上操作，界面毫无变化且**不报错** |
| `PHOTOOS_SKIP_AUTO_REGROUP` | `full_index` → 子进程 `shots regroup` → `full_index` → … **无限递归**（`photoos index --wait` 会跑 `full_index`） |

两条都有测试守着。这两条都是实测撞出来的，不是理论风险 ——
详见 [`10-import-audit.md`](10-import-audit.md) 第五节。

---

## 加一个新任务类型：清单

1. 在 `jobs/queue.py` 的 `JOB_TYPES` 里**登记**类型名（不登记会在 `create()` 就报错）。
2. 在 `jobs/runner.py` 的**分发字典**（`run()` 里那个 `{...}.get(job.type)`）
   里加上 `"你的类型": self._run_你的类型`。
3. 写处理器 `_run_xxx(self, job, report, on_progress)`：
   - `job.scope` 是入参；**业务判断请在入队之前做完**，任务里不要再做
     （否则"入队时是这个人、跑起来时人已被删"会变成任务里的隐藏分支）；
   - 用 `progress = self._make_progress(job.id, on_progress)` 上报；
   - 用 `self.queue.is_cancelled(job.id)` / `is_paused(job.id)` 做协作式停止检查点；
   - 失败抛 `PhotoOSError`（会被记成任务失败，不外抛拖垮进程）。
4. 若任务耗时长，考虑**不要**重试（`create(..., max_retries=0)`）。
5. 在 `api/app.py` 加/复用创建任务的端点；界面在 `app.js` 的 `JOB_LABELS`
   里加中文名（**漏了会直接显示英文类型名**）。
6. 加测试。`tests/unit/test_jobs.py` 是现成的样板。

## 长任务要记得的两件事

- **子进程输出要合并 stdout/stderr 成一个流读**。两个管道分开读时，任一写满缓冲区
  而没人读，子进程阻塞在写、父进程阻塞在读 —— 死锁。
- **`PYTHONUNBUFFERED=1`**。不加这句子进程会攒够缓冲才吐输出，
  界面上看起来就是"卡住了"，其实在跑。

---

返回 [`README.md`](README.md)
