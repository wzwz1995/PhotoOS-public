-- PhotoOS Phase 2（精选系统 + 相似度切片）schema (006)
--
-- 铁律一致性（PHASE2_TECH_PLAN §14）：
--   * #1 原片永不被动：本迁移只改数据库结构，不涉及任何文件操作。
--   * #2 数据库内不出现身份性路径：新增的 collection / collection_item / embedding
--     **一个路径字段都没有**。主题只引用 shot_id；嵌入只引用 asset_id。
--   * #3 Asset 是事实单位：embedding 挂在 asset 上；collection_item 挂在 shot 上；
--     分组与精选都是**可重算的关系**，不产生新的"事实单位"。
--   * #4 派生数据必须版本化：embedding 带 model/model_version/model_sha256/dim/
--     normalization/input_kind/input_version/prep_version，且 UNIQUE 约束保证
--     **多版本并存、绝不覆盖历史**（PRD §9.3）。
--   * #5 破坏性操作二次确认：本迁移不新增任何删除/移动原片的入口。
--
-- 为什么 Phase 2 的四张表放**同一个**迁移（而不是像 001-005 那样一个关注点一个文件）：
--   collection_item 引用 shot、embedding 引用 asset，而 session/shot 的新列是
--   collection 与精选功能的前置。拆成多个文件会引入"中间态不可用"的窗口。
--   迁移器保证单事务应用，因此一个文件更安全。
--
-- 注意：文件内不得出现**独立的事务控制语句**（迁移器自己负责事务，
-- 见 migrator._assert_no_explicit_transaction）。触发器体内的 BEGIN...END; 是
-- SQLite 语法要求，不属于事务控制语句，已由迁移器按"独立语句"判据放行。

-- ============================================================ Collections（PRD §9.2.1）

-- 用户主动创建的长期作品集合：不受时间、地点、Session 限制。
-- uuid 的存在理由与 library.uuid 相同：跨机迁移时需要稳定身份，
-- 否则 PC-B 上的 id 会被重新分配，迁移包的逐项校验就失去基准。
CREATE TABLE collection (
    id                  INTEGER PRIMARY KEY,
    uuid                TEXT    NOT NULL UNIQUE,          -- 跨机迁移的稳定身份
    name                TEXT    NOT NULL,                 -- 自定义名称
    description         TEXT,                             -- 描述
    cover_shot_id       INTEGER REFERENCES shot(id) ON DELETE SET NULL,  -- 封面（可空=自动取首条）
    sort_order          INTEGER NOT NULL DEFAULT 0,       -- 主题之间的排序
    created_at          TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL
);

CREATE INDEX idx_collection_sort ON collection(sort_order, id);

-- PK(collection_id, shot_id) 同时表达两件事（PRD §9.2.1）：
--   ① 同一张最终精选可以同时属于多个主题（一行一个主题）；
--   ② 同一主题里同一张不会重复加入（天然幂等）。
CREATE TABLE collection_item (
    collection_id       INTEGER NOT NULL REFERENCES collection(id) ON DELETE CASCADE,
    shot_id             INTEGER NOT NULL REFERENCES shot(id) ON DELETE CASCADE,
    sort_order          INTEGER NOT NULL DEFAULT 0,       -- 主题**内**的排序
    note                TEXT,                             -- 该主题下的备注
    added_at            TEXT    NOT NULL,
    PRIMARY KEY (collection_id, shot_id)
);

-- 反查"这张作品属于哪些主题"要用（精选视图与详情抽屉）
CREATE INDEX idx_collection_item_shot ON collection_item(shot_id);
-- 主题内按 sort_order 取封面/列表
CREATE INDEX idx_collection_item_order ON collection_item(collection_id, sort_order, shot_id);

-- ============================================================ Embedding（PRD §9.3，受控偏离见 §2）

-- 粒度 = **asset**，不是 shot（PHASE2_RESEARCH.md §17.2 实测决策）：
--   ① 按 shot 存会造成循环依赖（shot 分组要用嵌入做信号，而分组一变嵌入就得全批重算）；
--   ② 用户目标"自然语言筛选（最近一年拍的所有日出）"是 text→image 检索，结果要落到
--      具体照片；按 shot 只留代表向量会让"同一 Shot 里另一张才是日出"直接搜不到；
--   ③ 成本差可忽略（21,400 vs 7,997 向量 = 43.7 MB vs 16.4 MB，全库 413 GB 的 0.01%）。
--
-- UNIQUE 约束是铁律 #4 的落点：任何一项身份变化（模型升级、预处理口径变化、
-- 输入图变化）都**新增一行**，绝不 UPDATE 覆盖历史（PRD §9.3「不覆盖历史结果」）。
CREATE TABLE embedding (
    id                  INTEGER PRIMARY KEY,
    asset_id            INTEGER NOT NULL REFERENCES asset(id) ON DELETE CASCADE,
    model               TEXT    NOT NULL,                 -- 'clip-vit-base-patch32'
    model_version       TEXT    NOT NULL,                 -- 'xenova-onnx-fp32'
    model_sha256        TEXT,                             -- 权重文件校验和（可精确重算的前提）
    dim                 INTEGER NOT NULL,                 -- 512
    normalization       TEXT    NOT NULL,                 -- 'l2'
    input_kind          TEXT    NOT NULL,                 -- 'preview'
    input_version       TEXT    NOT NULL,                 -- preview 的 generator_version（'p1.0'）
    prep_version        TEXT    NOT NULL,                 -- 预处理口径版本（resize/crop/mean/std）
    vector              BLOB    NOT NULL,                 -- float32 little-endian，长度 = dim*4
    created_at          TEXT    NOT NULL,
    UNIQUE (asset_id, model, model_version, prep_version)
);

CREATE INDEX idx_embedding_asset ON embedding(asset_id);
CREATE INDEX idx_embedding_identity ON embedding(model, model_version, prep_version);
-- 增量补算时的驱动查询：找"还没有该版本向量"的 asset
CREATE INDEX idx_embedding_created ON embedding(created_at);

-- ============================================================ session：审核参数覆盖（决策 #5）

-- 决策 #5：shot_gap_seconds 不做写死的默认值，改成用户可配置。
-- 三层结构：config.json 全局默认 -> 本列按 Session 覆盖 -> UI 预设。
-- JSON 形状（缺键即继承全局）：
--   {"shot_gap_seconds": 2.0, "scene_gap_seconds": 900,
--    "scene_merge_similarity": 0.85, "scene_max_shots": 60}
-- 存 JSON 而不是四个独立列：参数还会长（Phase 3 可能加更多），
-- 每加一个参数就 ALTER 一次表不划算；且这些值只被整体读写，不需要 SQL 索引。
ALTER TABLE session ADD COLUMN review_config_json TEXT;

-- ============================================================ shot：只增不删（决策 #4）

-- decision #4（数据安全关键）：selection.shot_id 是 ON DELETE CASCADE，
-- 若 Shot 重算导致某个 Shot 消失，**精选记录会被连带删掉**。
-- 因此重算一律"只增不删"：不再对应任何新 Shot 的旧行标记 retired_at，
-- 而不是物理删除。同时把它的 selection 行迁移到新 Shot 并写 selection_event。
--
-- retired_reason 取值：'regroup' | 'merged_into:<shot_id>'
ALTER TABLE shot ADD COLUMN retired_at TEXT;
ALTER TABLE shot ADD COLUMN retired_reason TEXT;

-- 活跃 Shot 的部分索引：Scene 详情页与相似组查询都只关心 retired_at IS NULL 的行
CREATE INDEX idx_shot_active ON shot(scene_id) WHERE retired_at IS NULL;
CREATE INDEX idx_shot_scene_rev ON shot(scene_id, group_revision);

-- ============================================================ scene：切分证据（可审计）

-- 记录"为什么这么切"：时间断点位置、相似度触发的合并/细分、规模约束触发的继续切分。
-- 形状：{"gap_breaks":[shot_id...], "sim_breaks":[{"a":id,"b":id,"cos":0.93}...],
--        "merged":[[id,id]...], "size_splits":[{"scene":n,"shots":k,"limit":60}...],
--        "params":{...}, "built_from_revision": n}
-- 存在理由与 Phase 1 的 preview.generator_version 同源：派生结果必须能解释自己，
-- 否则用户调了阈值之后无法判断"是算法变了还是我的参数变了"。
ALTER TABLE scene ADD COLUMN evidence_json TEXT;

-- ============================================================ selection：状态可追溯的补强

-- Phase 1 的 001 建了 selection / selection_event，但只给 shot_id 加了 CHECK，
-- **没有约束 from_status / to_status** —— 即可以写进任意垃圾字符串。
-- Phase 2 开始真正写入这两张表，因此在这里补上约束（PRD §9.2 的四态状态机）。
--
-- 注：SQLite 的 ALTER TABLE ADD CONSTRAINT 不被支持，只能靠"重建表"或
-- "触发器"；这里选**触发器**——重建表会动到 Phase 2 之前的既有数据（虽然当前为空），
-- 而触发器是纯增量、可回滚、且不改变表结构（迁移包的向后兼容性更好）。
CREATE TRIGGER trg_selection_event_status_insert
BEFORE INSERT ON selection_event
FOR EACH ROW
WHEN (NEW.to_status NOT IN ('UNREVIEWED','CANDIDATE','SELECTED','FINAL'))
  OR (NEW.from_status IS NOT NULL
      AND NEW.from_status NOT IN ('UNREVIEWED','CANDIDATE','SELECTED','FINAL'))
BEGIN
    SELECT RAISE(ABORT, 'selection_event 的状态必须是 UNREVIEWED/CANDIDATE/SELECTED/FINAL');
END;

-- 精选视图与 selection 轴都要按状态筛，而 001 没给 selection.status 建索引
CREATE INDEX idx_selection_status ON selection(status);
CREATE INDEX idx_selection_updated ON selection(updated_at);
-- "记录选择过程"的反查：按时间线看某个 Shot 经历了什么，以及"被放弃了什么"
CREATE INDEX idx_sel_event_to ON selection_event(to_status, created_at);
