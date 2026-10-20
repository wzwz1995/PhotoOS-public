-- PhotoOS Phase 3（语义搜索）schema (007)
--
-- 铁律一致性（PHASE2_TECH_PLAN §14 的同一套纪律）：
--   * #1 原片永不被动：本迁移只改数据库结构，不涉及任何文件操作。
--   * #2 库内不出现身份性路径：两张新表**一个路径字段都没有**。
--     `semantic_query.vector` 是文本向量；`semantic_query_hit` 只引用 shot_id。
--   * #3 Asset/Shot 是事实单位：查询命中落在 **shot** 上（一个决策单元），不是 asset。
--   * #4 派生数据必须版本化：`semantic_query` 带 model/model_version/dim/normalization/
--     text_kind，与 `embedding` 同一套身份口径 —— 换模型就是新的一行，绝不覆盖。
--   * #5 破坏性操作二次确认：本迁移不新增任何删除/移动原片的入口。
--   * #6 不扩大范围：只做"文字 → 候选集"，**没有评分/排名/推荐字段**，
--     也不建 `analysis` 表（那是 Phase 4 的 PRD §9.3 六件套）。
--
-- 为什么一个文件装两张表（而不是像 001-005 那样一个关注点一个文件）：
--   两者是**同一个功能的读写两端**（查询身份 + 归属快照），拆开会出现
--   "有查询但没结果"的中间态。迁移器保证单事务应用，因此一个文件更安全。
--   这与 006 把 Phase 2 四张表放一起的理由相同。
--
-- 注意：文件内不得出现**独立的事务控制语句**（迁移器自己负责事务，
-- 见 migrator._assert_no_explicit_transaction）。

-- ============================================================ 查询身份
-- 形状在 PHASE2_TECH_PLAN §12.2 就已**冻结**（当时刻意"只写形状不建表"），
-- 现在真正用到它的那一刻才建 —— 这正是那份文档写的时机纪律。
--
-- 一行 = "一个被问过的词" + "它的文本向量"。
-- 语义：**界面拿这一行去和 `embedding` 做点积即可**，不需要重跑文本塔
--      （产品 venv 没有 torch，这是 Phase 3「方案 a」架构的关键）。
CREATE TABLE semantic_query (
    id                    INTEGER PRIMARY KEY,
    query_text            TEXT    NOT NULL,          -- 用户看到的词（如 '日出'）
    prompt_text           TEXT,                      -- 实际送进模型的那串（如 'sunrise'）
    text_kind             TEXT    NOT NULL DEFAULT 'query',  -- 'query' / 'document'（前缀口径）
    -- ⚠️ 命名沿用 §12.2 已冻结的形状：这一列叫 `model`（不是 model_name），
    --    与 `embedding.model` 同名同义 —— 两表 join 的身份口径必须一致。
    model                 TEXT    NOT NULL,
    model_version         TEXT    NOT NULL,
    dim                   INTEGER NOT NULL,
    normalization         TEXT    NOT NULL,
    vector                BLOB    NOT NULL,          -- 文本嵌入，长度必须 = dim*4
    parsed_filters_json   TEXT,                      -- 规则层抽出的结构化条件（本期留空）
    concept_ratio         REAL,                      -- 概念凝聚度比值（发现时的实测值，仅供界面排序）
    created_at            TEXT    NOT NULL,
    updated_at            TEXT    NOT NULL
);

-- 同一个词 + 同一个模型身份只留一行（重复搜索是幂等的 UPDATE，不是新增）
CREATE UNIQUE INDEX idx_semantic_query_text
    ON semantic_query(query_text, model, model_version);
CREATE INDEX idx_semantic_query_recent ON semantic_query(updated_at DESC);

-- ============================================================ 归属快照
-- 一行 = "某次查询命中过某个 Shot"。
--
-- **为什么存 shot_id 而不是 asset_id**：一个 Shot 就是一个决策单元，
-- 界面本来就按 Shot 去重（实测 top-20 里只有 12 个独立 Shot，RAW+JPG 成对占名额）。
--
-- **为什么快照可以过期**：重算 Shot 之后，老的 hit 行可能指向退役的 Shot。
-- 所以读取端**必须过滤 `shot.retired_at IS NULL`**，并且 `shot_id` 上是
-- `ON DELETE CASCADE` —— 与 `selection`/`collection_item` 对 shot 的处理**同一套语义**
-- （本项目的引用搬迁纪律见 PHASE2_DELIVERY §6.7–§6.12）。
CREATE TABLE semantic_query_hit (
    query_id              INTEGER NOT NULL REFERENCES semantic_query(id) ON DELETE CASCADE,
    shot_id               INTEGER NOT NULL REFERENCES shot(id) ON DELETE CASCADE,
    rank                  INTEGER NOT NULL,          -- 0-based；**只用于稳定渲染顺序，不是评分**
    score                 REAL    NOT NULL,          -- 余弦；用于"为什么它在前面"的可核对性
    is_primary_asset      INTEGER NOT NULL DEFAULT 0,-- 命中是否正好是该 Shot 的代表图
    computed_at           TEXT    NOT NULL,
    PRIMARY KEY (query_id, shot_id)
);

CREATE INDEX idx_semantic_hit_query_rank ON semantic_query_hit(query_id, rank);
CREATE INDEX idx_semantic_hit_shot ON semantic_query_hit(shot_id);
