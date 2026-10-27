# 40 · HTTP 接口参考

> **本篇由脚本生成，不要手改** —— 它是 FastAPI 路由表的转写。
> 重新生成：`python docs/20-reference/tools/make_api_reference.py`
>
> 【实测】源码共 **86 个 operation**（86 条路由，其中 GET 43 条）。路径参数写作 `{name}`。

---

## 0. 调用纪律（先读这五条）

1. **只有回环地址、没有鉴权** —— 单用户本地优先，这是有意的（`api/app.py:4`）。你在本机 `curl` 它就是全部权限。
2. **同步 DB 逻辑必须走 `_read()`**（`anyio.to_thread.run_sync`，`api/app.py:3395-3404`）。在 async 路由里直接调 sqlite3 会阻塞整个服务。
3. **错误响应有三种形状**（兜底 500 / 校验 422 / 领域 `{code,message,detail}`），另有路由内专用码 428（需二次确认）、501（保留的浏览轴）、409（离线/已退役）。详见 [`20-后端架构与模块地图.md`](20-后端架构与模块地图.md) §3.1。
4. **服务端没有删除原片的入口** —— 全仓有守卫测试断言路由里不出现破坏性动词（`tests/unit/route_guard.py`）。
5. **`/docs` 与 `/openapi.json` 永远比本篇新** —— 本篇的价值是“分组 + 读写标注 + 源码位置”，不是替代交互式文档。

---

## 1. 速查：按分组的接口数

| 分组 | 端点数 | 分组 | 端点数 |
| --- | --- | --- | --- |
| `people` | 14 | `import` | 2 |
| `assets` | 11 | `filter` | 1 |
| `collections` | 8 | `health` | 1 |
| `shots` | 6 | `cli` | 1 |
| `libraries` | 5 | `stats` | 1 |
| `scenes` | 5 | `browse` | 1 |
| `semantic` | 5 | `review` | 1 |
| `jobs` | 4 | `selection` | 1 |
| `sessions` | 3 | `find` | 1 |
| `faces` | 3 | `facets` | 1 |
| `exports` | 3 | `integrity` | 1 |
| `maintenance` | 3 | `config` | 1 |
| `actions` | 2 | `(顶层/前端)` | 1 |

---

## 2. 全部接口

### 健康与自述 · `1` 个端点

| 方法 | 路径 | 说明 | 关键参数 | 源码 |
| --- | --- | --- | --- | --- |
| `GET` | `/api/health` | — | — | `api/app.py:463` |

### 配置 · `1` 个端点

| 方法 | 路径 | 说明 | 关键参数 | 源码 |
| --- | --- | --- | --- | --- |
| `GET` | `/api/config` | — | — | `api/app.py:3404` |

### 库与卷 · `5` 个端点

| 方法 | 路径 | 说明 | 关键参数 | 源码 |
| --- | --- | --- | --- | --- |
| `GET` | `/api/libraries` | — | — | `api/app.py:544` |
| `GET` | `/api/libraries/probe` | — | `path`(可选) | `api/app.py:582` |
| `POST` | `/api/libraries/{library_id}/offline` | — | `library_id`(必填) | `api/app.py:645` |
| `POST` | `/api/libraries/{library_id}/rebind` | 重新绑定到新位置。默认 dry-run —— 必须显式传 apply=true 才改数据库。 | `library_id`(必填)、`payload`(必填) | `api/app.py:614` |
| `GET` | `/api/libraries/{library_id}/verify` | — | `library_id`(必填) | `api/app.py:651` |

### 浏览轴 · `1` 个端点

| 方法 | 路径 | 说明 | 关键参数 | 源码 |
| --- | --- | --- | --- | --- |
| `GET` | `/api/browse/tree` | 可插拔的浏览树。 | `axis`(可选)、`library_id`(可选)、`parent`(可选)、`limit`(可选) | `api/app.py:1025` |

### 分面（筛选下拉项） · `1` 个端点

| 方法 | 路径 | 说明 | 关键参数 | 源码 |
| --- | --- | --- | --- | --- |
| `GET` | `/api/facets` | 筛选面板要用的分面（机型 / 年份 / 镜头 / 焦距）。 | `library_id`(可选) | `api/app.py:2922` |

### 资产（列表 / 详情 / 媒体） · `11` 个端点

| 方法 | 路径 | 说明 | 关键参数 | 源码 |
| --- | --- | --- | --- | --- |
| `GET` | `/api/assets` | 分页浏览。**只查数据库索引，绝不扫描原片**（PRD §11.2）。 | `library_id`(可选)、`offset`(可选)、`limit`(可选)、`q`(可选)、`file_type`(可选)、`status`(可选) … | `api/app.py:671` |
| `POST` | `/api/assets/query` | 结构化筛选（**绝不接受原始 SQL 字符串**）。 | `payload`(必填) | `api/app.py:2608` |
| `GET` | `/api/assets/query/fields` | 列出可用的筛选字段、每个字段支持的操作符、以及排序白名单。 | — | `api/app.py:2636` |
| `GET` | `/api/assets/{asset_id}` | — | `asset_id`(必填) | `api/app.py:2693` |
| `GET` | `/api/assets/{asset_id}/original` | **下载**原片（库离线时返回 409，UI 据此提示"插回硬盘即可访问"）。 | `asset_id`(必填) | `api/app.py:2848` |
| `GET` | `/api/assets/{asset_id}/path` | 这张原片**在磁盘上的位置**（界面用它代替"从浏览器下载"）。 | `asset_id`(必填) | `api/app.py:2701` |
| `GET` | `/api/assets/{asset_id}/preview` | — | `asset_id`(必填) | `api/app.py:2844` |
| `POST` | `/api/assets/{asset_id}/reveal` | 在**文件管理器里定位到这张原片**（在它所在的窗口里选中它）。 | `asset_id`(必填)、`payload`(可选) | `api/app.py:2813` |
| `GET` | `/api/assets/{asset_id}/tags` | 这张照片上的**全部标签**，一次拿全 —— 弹窗用它渲染"标签"那一行。 | `asset_id`(必填) | `api/app.py:2744` |
| `GET` | `/api/assets/{asset_id}/thumb` | — | `asset_id`(必填) | `api/app.py:2840` |
| `GET` | `/api/assets/{asset_id}/view` | **内联查看**：能被浏览器渲染的就给原片，否则回退到派生预览。 | `asset_id`(必填) | `api/app.py:2872` |

### 统计 · `1` 个端点

| 方法 | 路径 | 说明 | 关键参数 | 源码 |
| --- | --- | --- | --- | --- |
| `GET` | `/api/stats` | 全库统计。**进「维护」页就会拉一次**。 | — | `api/app.py:514` |

### 多维筛选 · `1` 个端点

| 方法 | 路径 | 说明 | 关键参数 | 源码 |
| --- | --- | --- | --- | --- |
| `GET` | `/api/filter` | — | `q`(可选)、`person_id`(可选)、`year`(可选)、`camera`(可选)、`lens`(可选)、`focal_min`(可选) … | `api/app.py:346` |

### 确定性取片（按时间地点） · `1` 个端点

| 方法 | 路径 | 说明 | 关键参数 | 源码 |
| --- | --- | --- | --- | --- |
| `GET` | `/api/find/scenes` | 结构化筛选（Phase 3 · L4）：按时间/地点/场次找 Scene。 | `city`(可选)、`session`(可选)、`date_from`(可选)、`date_to`(可选)、`scene`(可选)、`min_shots`(可选) … | `api/app.py:1307` |

### 场景 · `5` 个端点

| 方法 | 路径 | 说明 | 关键参数 | 源码 |
| --- | --- | --- | --- | --- |
| `GET` | `/api/scenes` | Scene 列表（可按 Session 过滤），附带每个 Scene 的精选进度。 | `session_id`(可选)、`limit`(可选) | `api/app.py:959` |
| `POST` | `/api/scenes/merge` | 把 `secondary_id` 合并进 `primary_id`（只允许同一时段内）。 | `payload`(必填) | `api/app.py:984` |
| `GET` | `/api/scenes/{scene_id}` | Scene 详情：Shot 列表 + 每个 Shot 的精选状态 + 选择过程。 | `scene_id`(必填) | `api/app.py:673` |
| `POST` | `/api/scenes/{scene_id}/split` | 在 `after_shot_id` 之后把 Scene 一分为二（前段保留原 id）。 | `scene_id`(必填)、`payload`(必填) | `api/app.py:1007` |
| `POST` | `/api/scenes/{scene_id}/unpin` | 解除人工标记，让下一次 `scenes rebuild` 重新接管这个 Scene。 | `scene_id`(必填) | `api/app.py:1031` |

### 时段 · `3` 个端点

| 方法 | 路径 | 说明 | 关键参数 | 源码 |
| --- | --- | --- | --- | --- |
| `GET` | `/api/sessions` | Session 列表（含城市/时段/时间范围/Shot 数）。 | `limit`(可选) | `api/app.py:842` |
| `GET` | `/api/sessions/{session_id}/review-config` | 某个 Session 的分组参数 + **有效值及其来源**。 | `session_id`(必填) | `api/app.py:867` |
| `PATCH` | `/api/sessions/{session_id}/review-config` | 设置本 Session 的 `shot_gap_seconds` 覆盖值。 | `session_id`(必填)、`payload`(必填) | `api/app.py:903` |

### 拍摄组（含精选与分组调整） · `6` 个端点

| 方法 | 路径 | 说明 | 关键参数 | 源码 |
| --- | --- | --- | --- | --- |
| `POST` | `/api/shots/merge` | 把多个拍摄组并成一个（PRD §9.2 人工修正）。 | `payload`(必填) | `api/app.py:1184` |
| `GET` | `/api/shots/{shot_id}/assets` | 分组里的**每一张**（人工拆分时要先看到才能选，所以不能只给封面）。 | `shot_id`(必填) | `api/app.py:2652` |
| `GET` | `/api/shots/{shot_id}/collections` | 反查这张作品属于哪些主题（PRD：一张可属多个主题）。 | `shot_id`(必填) | `api/app.py:1873` |
| `GET` | `/api/shots/{shot_id}/history` | 某个 Shot 的选择过程时间线（断点 #8「从最终精选反查完整拍摄组」要用）。 | `shot_id`(必填) | `api/app.py:1100` |
| `POST` | `/api/shots/{shot_id}/selection` | 设置某个 Shot 的精选状态（可选带理由）。 | `shot_id`(必填)、`payload`(必填) | `api/app.py:1049` |
| `POST` | `/api/shots/{shot_id}/split` | 把若干资产从分组中**拆出去**，形成一个新的拍摄组（PRD §9.2 人工修正）。 | `shot_id`(必填)、`payload`(必填) | `api/app.py:1131` |

### 精选 · `1` 个端点

| 方法 | 路径 | 说明 | 关键参数 | 源码 |
| --- | --- | --- | --- | --- |
| `GET` | `/api/selection/summary` | 精选总览：各状态计数 + 被放弃的清单（PRD「放弃了什么」）。 | — | `api/app.py:1240` |

### 主题 · `8` 个端点

| 方法 | 路径 | 说明 | 关键参数 | 源码 |
| --- | --- | --- | --- | --- |
| `GET` | `/api/collections` | 主题列表（含条目数与**可用封面**）。 | — | `api/app.py:1642` |
| `POST` | `/api/collections` | 新建主题。`uuid` 可选（跨机迁移的稳定身份，不传自动生成）。 | `payload`(必填) | `api/app.py:1695` |
| `GET` | `/api/collections/{collection_id}` | — | `collection_id`(必填) | `api/app.py:1662` |
| `PATCH` | `/api/collections/{collection_id}` | 改名称/描述/封面/排序。只更新传入的字段。 | `collection_id`(必填)、`payload`(必填) | `api/app.py:1730` |
| `DELETE` | `/api/collections/{collection_id}` | 删除**主题**（只删数据库关系）。 | `collection_id`(必填)、`confirm`(可选) | `api/app.py:1763` |
| `POST` | `/api/collections/{collection_id}/items` | 把作品加进主题（幂等）。 | `collection_id`(必填)、`payload`(必填) | `api/app.py:1790` |
| `POST` | `/api/collections/{collection_id}/items/detach` | 把作品移出主题（**只删关系行**：不删照片、不改精选状态）。 | `collection_id`(必填)、`payload`(必填) | `api/app.py:1824` |
| `POST` | `/api/collections/{collection_id}/order` | 按给定顺序重排主题内的作品。 | `collection_id`(必填)、`payload`(必填) | `api/app.py:1852` |

### 语义搜索与反馈 · `5` 个端点

| 方法 | 路径 | 说明 | 关键参数 | 源码 |
| --- | --- | --- | --- | --- |
| `POST` | `/api/semantic/collection` | 把某个词的（已按你的修改调整过的）结果**存成一个主题**。 | `payload`(必填) | `api/app.py:1575` |
| `POST` | `/api/semantic/feedback` | 记录一次人工表态：`keep`（我要这种）或 `drop`（不是这种）。 | `payload`(必填) | `api/app.py:1480` |
| `DELETE` | `/api/semantic/feedback` | 撤销这个词的全部表态（"我反悔了，从零开始"）。**模型原始结果一直都在。** | `query`(可选)、`query_id`(可选) | `api/app.py:1552` |
| `GET` | `/api/semantic/queries` | 可搜索的词（界面下拉框的内容）。按凝聚度降序 —— 越靠前越"在库里真有内容"。 | — | `api/app.py:1283` |
| `GET` | `/api/semantic/results` | 某个词的命中（**只返回看得见的**，即挂在活跃 Shot 上的）。 | `query`(可选)、`query_id`(可选)、`limit`(可选)、`tuning`(可选) | `api/app.py:1361` |

### 人物 · `14` 个端点

| 方法 | 路径 | 说明 | 关键参数 | 源码 |
| --- | --- | --- | --- | --- |
| `GET` | `/api/people` | 人物列表。**未命名的人也在列表里** —— 它们正是要用户处理的。 | — | `api/app.py:1949` |
| `POST` | `/api/people` | 新建一个人（名字可留空 —— 模型不给人起名）。 | `payload`(必填) | `api/app.py:1976` |
| `POST` | `/api/people/learn` | 把所有有标注的人的原型重算一遍（等价于 `photoos people learn`）。 | — | `api/app.py:2553` |
| `POST` | `/api/people/move` | **把选中的脸搬到另一个人名下**（真正的搬，不是再加一条）。 | `payload`(必填) | `api/app.py:2264` |
| `GET` | `/api/people/{person_id}` | — | `person_id`(必填)、`limit`(可选) | `api/app.py:1993` |
| `PATCH` | `/api/people/{person_id}` | 改名 / 改备注 / 换封面。改名会**重新学习原型**（名字代表用户已经认过这个人）。 | `person_id`(必填)、`payload`(必填) | `api/app.py:2031` |
| `DELETE` | `/api/people/{person_id}` | 删掉一个人（**只删"人"这个标签**：脸、照片、精选一律不动）。 | `person_id`(必填)、`confirm`(可选) | `api/app.py:2510` |
| `POST` | `/api/people/{person_id}/collection` | 把这个人的照片**一键进专题**（PRD §9.2.1 的 Collection）。 | `person_id`(必填)、`payload`(可选) | `api/app.py:2460` |
| `POST` | `/api/people/{person_id}/confirm-all` | **一键确认**：一次把你的判断写进去。 | `person_id`(必填)、`payload`(可选) | `api/app.py:2158` |
| `POST` | `/api/people/{person_id}/dissolve` | **解散候选集**：这一簇不算数（"有的分组里啥都有"）。 | `person_id`(必填) | `api/app.py:2205` |
| `POST` | `/api/people/{person_id}/faces` | 用户确认"这些是他"。**这是唯一的真标签来源。** | `person_id`(必填)、`payload`(必填) | `api/app.py:2134` |
| `POST` | `/api/people/{person_id}/reject` | 用户否决"这不是他"。否决是**一等事实**（`person_reject` 单独一张表）。 | `person_id`(必填)、`payload`(必填) | `api/app.py:2145` |
| `POST` | `/api/people/{person_id}/restore` | 撤销解散（把候选放回列表）。**解散必须能反悔** —— 一次误点不该丢掉一簇。 | `person_id`(必填) | `api/app.py:2243` |
| `POST` | `/api/people/{person_id}/suggest` | 用**当前原型**给还没归类的脸打分，返回最像的一批（**等用户确认**）。 | `person_id`(必填)、`payload`(可选) | `api/app.py:2381` |

### 人脸 · `3` 个端点

| 方法 | 路径 | 说明 | 关键参数 | 源码 |
| --- | --- | --- | --- | --- |
| `GET` | `/api/faces/not-person` | 你标过「不是人」的那些脸（**带原片路径**，方便核对与撤销）。 | `limit`(可选) | `api/app.py:2367` |
| `POST` | `/api/faces/verdict` | **不是人**：这张脸压根不是一个人（海报、雕像、屏幕、反光……）。 | `payload`(必填) | `api/app.py:2323` |
| `GET` | `/api/faces/{face_id}/thumb` | 人脸缩略图：从**本地 preview 缓存**现裁，不落盘、不读原片。 | `face_id`(必填)、`size`(可选) | `api/app.py:1900` |

### 后台任务 · `4` 个端点

| 方法 | 路径 | 说明 | 关键参数 | 源码 |
| --- | --- | --- | --- | --- |
| `GET` | `/api/jobs` | — | `limit`(可选)、`active_only`(可选) | `api/app.py:2963` |
| `POST` | `/api/jobs` | 创建一个后台任务。`type` 见 PRD §9.1 的导入/索引阶段。 | `payload`(必填) | `api/app.py:2990` |
| `GET` | `/api/jobs/{job_id}` | — | `job_id`(必填) | `api/app.py:2972` |
| `POST` | `/api/jobs/{job_id}/{action}` | — | `job_id`(必填)、`action`(必填) | `api/app.py:3345` |

### 界面可触发的命令 · `2` 个端点

| 方法 | 路径 | 说明 | 关键参数 | 源码 |
| --- | --- | --- | --- | --- |
| `GET` | `/api/actions` | 界面可直跑的命令目录 + 当前环境说明。 | — | `api/app.py:3009` |
| `POST` | `/api/actions/{action_id}/run` | 把一条白名单命令排进后台任务队列。 | `action_id`(必填)、`payload`(可选) | `api/app.py:3031` |

### 导入（预演 / 执行） · `2` 个端点

| 方法 | 路径 | 说明 | 关键参数 | 源码 |
| --- | --- | --- | --- | --- |
| `POST` | `/api/import/apply` | **执行**导入：创建 `classify_import` 后台任务。 | `payload`(必填) | `api/app.py:3293` |
| `POST` | `/api/import/plan` | **预演**分类导入：算出每个文件的目标路径与判定依据。**不写任何文件。** | `payload`(必填) | `api/app.py:3247` |

### 维护（备份 / 查重） · `3` 个端点

| 方法 | 路径 | 说明 | 关键参数 | 源码 |
| --- | --- | --- | --- | --- |
| `POST` | `/api/maintenance/backup` | — | — | `api/app.py:3367` |
| `GET` | `/api/maintenance/backups` | — | — | `api/app.py:3376` |
| `POST` | `/api/maintenance/duplicates` | 重复内容报告（**只报告，不删除**，PRD §11.1）。 | `payload`(可选) | `api/app.py:3384` |

### 打包导出 · `3` 个端点

| 方法 | 路径 | 说明 | 关键参数 | 源码 |
| --- | --- | --- | --- | --- |
| `POST` | `/api/exports` | **一键打包**：把人物 / 主题 / 选中拍摄组的原片打成一个 zip。 | `payload`(必填) | `api/app.py:3198` |
| `POST` | `/api/exports/plan` | **先算账**：这次打包有多少张、多大、有几个不在盘上。不写任何文件。 | `payload`(必填) | `api/app.py:3148` |
| `POST` | `/api/exports/reveal` | 在资源管理器里选中打包好的 zip。 | `payload`(必填) | `api/app.py:3169` |

### 完整性 · `1` 个端点

| 方法 | 路径 | 说明 | 关键参数 | 源码 |
| --- | --- | --- | --- | --- |
| `GET` | `/api/integrity` | — | — | `api/app.py:3362` |

### 精选页配置 · `1` 个端点

| 方法 | 路径 | 说明 | 关键参数 | 源码 |
| --- | --- | --- | --- | --- |
| `GET` | `/api/review/config/presets` | `shot_gap_seconds` 的可选预设 + 全局默认值（决策 #5 的"界面预设"那一层）。 | — | `api/app.py:848` |

### 命令直跑 · `1` 个端点

| 方法 | 路径 | 说明 | 关键参数 | 源码 |
| --- | --- | --- | --- | --- |
| `GET` | `/api/cli` | 界面上要显示的**命令行怎么写** —— 由服务端按自己所在的环境给出。 | — | `api/app.py:478` |

### 其它

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/` | — |

---

## 3. 写操作清单（改动数据的那些）

【实测】非 GET 的 operation 一共 **43** 个。它们按风险分三档：

| 档 | 判据 | 端点 |
| --- | --- | --- |
| 删除类（路径含 delete/remove/detach/dissolve） | 需二次确认 | `POST /api/collections/{collection_id}/items/detach`、`POST /api/people/{person_id}/dissolve` |
| 确认类（路径含 confirm） | 表单里带确认参数 | `POST /api/people/{person_id}/confirm-all` |
| 其余写操作 | 直接生效 | `POST /api/libraries/{library_id}/rebind`、`POST /api/libraries/{library_id}/offline`、`PATCH /api/sessions/{session_id}/review-config`、`POST /api/scenes/merge`、`POST /api/scenes/{scene_id}/split`、`POST /api/scenes/{scene_id}/unpin`、`POST /api/shots/{shot_id}/selection`、`POST /api/shots/{shot_id}/split`、`POST /api/shots/merge`、`POST /api/semantic/feedback`、`DELETE /api/semantic/feedback`、`POST /api/semantic/collection`、`POST /api/collections`、`PATCH /api/collections/{collection_id}`… |

### 用项目自己的守卫判据复核（`tests/unit/route_guard.py`）

禁忌词表：`delete`、`destroy`、`erase`、`purge`、`remove`、`rm`、`trash`、`unlink`

【实测】**没有任何一条路由在路径里出现破坏性动作词**。

⚠️ 这句必须读准 —— 它**不等于**“这个服务没有任何删除操作”。事实是：

| 有删除操作的 | 没有的 |
| --- | --- |
| `DELETE /api/collections/{id}`（删**主题**，要 `?confirm=true`）、`DELETE /api/semantic/feedback`（撤销一条反馈） | **任何删/移/改名原片的入口** |

判据是「路径段里有没有 delete/remove/purge 这类**动作词**」（`tests/unit/route_guard.py:31-43`），所以 `DELETE /api/collections/{id}`（词是 `collections`）合法通过，而 `/api/assets/{id}/delete` 会被拦下 —— 后者才是「动原片」的入口。产品级的说法是 PRD §11.1 / §14.7：
**Phase 1 不暴露删除/移动原文件的接口**（`tests/unit/test_api.py:600-609`）。

> ⚠️ 【推断】注意判据是**按路径段切词**而不是「路径里包含这些字母」：`confirm-all` 里恰好有 `rm` 两个字母，第一版守卫因此把`/api/people/{id}/confirm-all` 误判成破坏性路由（`tests/unit/route_guard.py:9-19` 记着这个现场）。

> ⚠️ 【推断】把端点按路径词分档只是**给人看的提示**，不是权限边界 ——
> 真正的边界是「服务端没有删除原片的代码路径」（见 [`70-安全边界与硬规则.md`](70-安全边界与硬规则.md)）。

---

返回 [`README.md`](README.md) · 相关：[`20-后端架构与模块地图.md`](20-后端架构与模块地图.md)、[`31-任务引擎与流水线.md`](31-任务引擎与流水线.md)
