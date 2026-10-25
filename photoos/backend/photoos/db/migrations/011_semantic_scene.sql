-- 011：场景挖掘状态（关键词不再只是一次检索，而是一个持续学习的场景）
--
-- 只存**每个场景的识别线和轮次**；人工标签仍然复用 semantic_feedback：
--   keep = 这张是这个场景
--   drop = 这张不是这个场景
--
-- 为什么不把标签再抄一张表：标签的 UNIQUE(query_id, shot_id) 和“改主意是
-- UPDATE”已经在 008 里成立；再造一张 scene_label 只会多一份需要同步的真源。
-- 为什么把 threshold/round 放独立表：它们是场景的**可调展示状态**，不是
-- 词向量或模型权重；重建/调整不会修改 semantic_query。
--
-- 注意：本文件不写独立事务控制语句（迁移器负责整体事务）。

CREATE TABLE semantic_scene (
    query_id    INTEGER PRIMARY KEY REFERENCES semantic_query(id) ON DELETE CASCADE,
    threshold   REAL    NOT NULL DEFAULT 0.70,
    round       INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL,
    CHECK (threshold >= 0.0 AND threshold <= 1.0),
    CHECK (round >= 0)
);

-- 场景发现按 updated_at 找最近使用的场景（工作台排序用；表很小，但避免全表排序）
CREATE INDEX idx_semantic_scene_updated ON semantic_scene(updated_at DESC);
