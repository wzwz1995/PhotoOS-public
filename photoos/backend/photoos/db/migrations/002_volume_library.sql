-- 002: 一个卷可以承载多个 Library；卷序列号不足以单独判定库身份。
--
-- 触发本迁移的真实现场：`C:\Photos` 与 `C:\Videos` 位于**同一块物理盘**（卷序列号相同）。
-- 原实现"卷序列号命中 → 判定为同一 Library"因此把视频库误判成照片库并改写了它的路径提示。
--
-- 结论（写入决策记录）：
--   * 卷序列号是**必要但不充分**判据 —— 同一卷上允许多个 Library 是正常情况。
--   * 库身份必须由「库标记」或「结构与内容重合度」确认（见 store.py 的 probe_path）。
--   * volume 与 library 的关系是**多对多**，用连接表表达。

CREATE TABLE volume_library (
    volume_id           INTEGER NOT NULL REFERENCES volume(id) ON DELETE CASCADE,
    library_id          INTEGER NOT NULL REFERENCES library(id) ON DELETE CASCADE,
    root_hint           TEXT,                             -- 该卷上该库的最近一次根路径（提示，非身份）
    first_seen_at       TEXT    NOT NULL,
    last_seen_at        TEXT,
    PRIMARY KEY (volume_id, library_id)
);

CREATE INDEX idx_volume_library_library ON volume_library(library_id);

-- 把既有的"一个卷属于一个库"关系回填进连接表，保证迁移不丢关系。
INSERT INTO volume_library (volume_id, library_id, root_hint, first_seen_at, last_seen_at)
SELECT v.id, v.library_id, COALESCE(l.root_path_hint, v.mount_hint), v.first_seen_at, v.last_seen_at
FROM volume AS v
JOIN library AS l ON l.id = v.library_id
WHERE v.library_id IS NOT NULL;

-- asset_instance 增加与 Library 的冗余关联，使"同一卷上区分多个库"的查询无需绕行 asset。
ALTER TABLE asset_instance ADD COLUMN library_id INTEGER REFERENCES library(id) ON DELETE CASCADE;

UPDATE asset_instance
SET library_id = (SELECT a.library_id FROM asset a WHERE a.id = asset_instance.asset_id)
WHERE library_id IS NULL;

CREATE INDEX idx_instance_library ON asset_instance(library_id);
