-- 010：废片标记（人工判断；只写库，不动原片）
--
-- 为什么是独立表而不是 asset 上加一列：
--   * `asset.status` 描述的是**文件可访问性**（OK/MISSING/QUARANTINED…），
--     把"人觉得这张是废片"混进去，会让扫描/校验去改写它 —— 两种语义打架。
--   * 标记是**用户判断**，可以随时取消：删掉这一行就是撤销。
--   * 铁律 #1：本迁移只建表，不碰任何文件。
--
-- 删除仍然走"移入 Windows 回收站"（可还原），不在这里做永久删除。

CREATE TABLE asset_reject (
    asset_id    INTEGER PRIMARY KEY REFERENCES asset(id) ON DELETE CASCADE,
    note        TEXT,
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL
);

-- 「列出我标过的废片」按时间倒序展示，避免每次全表排序
CREATE INDEX idx_asset_reject_created ON asset_reject(created_at DESC);
