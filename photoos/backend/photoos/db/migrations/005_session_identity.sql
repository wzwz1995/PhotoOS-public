-- 005: 把「时段」的身份固定为 (城市, 时段目录名)。
--
-- 背景：M3 的基础物理分类会把每个 `<城市>\<时段>` 落成一条 session 行，供 Phase 2 的
-- Scene 挂载。`session` 表是**全局**的（没有 library_id）—— 这与旧工具的建模一致：
-- 照片与视频放在两个盘上，却共享同一套时段名，因为切段用的是**两者合并后的时间跨度**。
-- 实测：既有库 71 个时段目录名，用「照片+视频」合并跨度可 71/71 逐字复现；
-- 只看照片只有 68/71（见 PHASE1_EXT_TECH_PLAN §1.3.6）。
--
-- 没有唯一约束时，重复导入同一时段会不断插入重复行：Phase 2 会拿到多个同名 Session，
-- 且无法判断该把 Shot/Scene 挂到哪一个。
--
-- 用**部分**唯一索引：只约束"由分类写入的行"（city_hint 与 period_hint 都非空）。
-- Phase 2 若创建只有名字的 Session，不受此约束影响。

CREATE UNIQUE INDEX uq_session_city_period
    ON session(city_hint, period_hint)
    WHERE city_hint IS NOT NULL AND period_hint IS NOT NULL;
