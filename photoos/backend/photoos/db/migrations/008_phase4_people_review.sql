-- PhotoOS Phase 4（人分类 + 人工反馈学习）schema (008)
--
-- 铁律一致性（沿用 PHASE2_TECH_PLAN §14 与 007 头部的同一套纪律）：
--   * #1 原片永不被动：本迁移只改数据库结构，**不涉及任何文件操作**。
--     人脸裁剪缩略图**不落盘**（读取端按需从 `preview` 现裁），因此不产生新文件。
--   * #2 库内不出现身份性路径：五张新表**一个绝对路径字段都没有**。
--     `face` 只引用 asset_id / shot_id；框坐标存**像素浮点**，不含路径。
--   * #3 Asset/Shot 是事实单位：人脸是**检测事实**（`face`，挂在 asset 上），
--     聚类与归属是**可重算的关系**（`person_face` / `person_profile`），
--     重算它们不产生新的"事实单位"。
--   * #4 派生数据必须版本化：`face` 带 detector/detector_version（同一张图换检测器
--     就是新的一行，不覆盖）；`face_embedding` 与 `person_profile` 带
--     model/model_version/prep_version/dim/normalization，**与 `embedding` 同一套身份口径**。
--   * #5 破坏性操作二次确认：本迁移不新增任何删除/移动原片的入口。
--   * #6 不扩大范围：**不建标签体系、不建 analysis 表、不做视频**。
--     新增的是"人的归属"与"人工反馈"，两者都是 PRD §9.2.1 Collection 的原料。
--
-- 为什么这五张表放**同一个**迁移（而不是一个关注点一个文件）：
--   `person_face` 引用 `face`，`person_profile` 引用 `person`，而
--   `semantic_feedback` 引用 007 的 `semantic_query` —— 它们共同支撑
--   "小模型初筛 + 人来确认 + 持续学习"这**一条**流程。拆开会出现
--   "有人没脸"或"有脸没归属"的中间态。迁移器保证单事务应用，故一个文件更安全
--   （与 006、007 把同一功能的读写两端放一起的理由相同）。
--
-- 注意：文件内不得出现**独立的事务控制语句**（迁移器自己负责事务）。
--       本文件一个触发器都没有，校验全部用 CHECK 约束。

-- ============================================================ 检测事实：face
--
-- 一行 = "某张 asset 上检测到的第 N 张脸"。**不是**"某个人" ——
-- 人的归属是 `person_face`，两者分开是 PRD §9.3「AI 不能把主观判断伪装成事实」的落点：
-- `face` 是可复算的客观检测，`person_face` 是**可能错的判断**（所以带 source 与 confidence）。
--
-- 坐标口径（关键，不写清楚就无法复算）：
--   * `box_*` 与 `landmarks_json` 全部是**原 preview 图像的像素坐标**（已还原检测缩放）。
--     检测时把长边缩到 `detect_max`，还原即 `* (preview_w / scaled_w)`；
--     还原后的坐标与 preview 一起构成可复算的输入。
--   * `symmetry` / `eye_ratio` 是**比值**，与缩放无关（见 library/faces.py 的判据定义）。
CREATE TABLE face (
    id                  INTEGER PRIMARY KEY,
    asset_id            INTEGER NOT NULL REFERENCES asset(id) ON DELETE CASCADE,
    -- shot 可以为空：新导入的照片在 Shot 分组之前就已经能被检测到。
    -- 分组重算时 shot 可能退役，故用 SET NULL 而不是 CASCADE —— 删分组不该删掉"这张脸存在"的事实。
    shot_id             INTEGER REFERENCES shot(id) ON DELETE SET NULL,
    detector            TEXT    NOT NULL,                 -- 'yunet-2023mar'
    detector_version    TEXT    NOT NULL,                 -- 'cv2-5.0.0'
    detect_max          INTEGER NOT NULL,                 -- 检测时的长边上限（900）
    preview_w           INTEGER NOT NULL,                 -- 还原坐标所依据的宽
    preview_h           INTEGER NOT NULL,
    box_x               REAL    NOT NULL,
    box_y               REAL    NOT NULL,
    box_w               REAL    NOT NULL,
    box_h               REAL    NOT NULL,
    landmarks_json      TEXT,                             -- 15 个浮点（YuNet 原始行）
    det_score           REAL    NOT NULL,                 -- YuNet 置信度
    face_px             REAL    NOT NULL,                 -- min(box_w, box_h)，正面判据之一
    symmetry            REAL,                             -- |鼻-眼中点| / 眼距，越小越正
    eye_ratio           REAL,                             -- 眼距 / 脸宽，越大越清晰
    is_frontal          INTEGER NOT NULL DEFAULT 0,       -- 是否通过正面清晰判据
    created_at          TEXT    NOT NULL,
    -- 同一 asset + 同一检测器 + 同一框位置 = 同一张脸（重复检测是幂等，不是新增行）
    UNIQUE (asset_id, detector, detector_version, box_x, box_y)
);

CREATE INDEX idx_face_asset ON face(asset_id);
CREATE INDEX idx_face_shot ON face(shot_id);
CREATE INDEX idx_face_frontal ON face(is_frontal);

-- ============================================================ 人脸特征
--
-- 与 `embedding` 完全同一套版本化纪律：换模型 = **新增一行**，绝不 UPDATE 覆盖。
-- 这样"用更强的识别模型重跑一遍"永远可与旧结果并存并比对（PRD §9.3 断点 #6）。
CREATE TABLE face_embedding (
    id                  INTEGER PRIMARY KEY,
    face_id             INTEGER NOT NULL REFERENCES face(id) ON DELETE CASCADE,
    model               TEXT    NOT NULL,                 -- 'sface-2021dec'
    model_version       TEXT    NOT NULL,                 -- 'cv2-5.0.0-fp32'
    prep_version        TEXT    NOT NULL,                 -- 'sf-112-align-p1.0'
    dim                 INTEGER NOT NULL,                 -- 128
    normalization       TEXT    NOT NULL,                 -- 'l2'
    vector              BLOB    NOT NULL,                 -- float32 LE，长度必须是 dim*4
    created_at          TEXT    NOT NULL,
    UNIQUE (face_id, model, model_version, prep_version)
);

CREATE INDEX idx_face_embedding_face ON face_embedding(face_id);
CREATE INDEX idx_face_embedding_identity ON face_embedding(model, model_version, prep_version);

-- ============================================================ 人
--
-- `name` 可以为空，这是**核心设计**而不是偷懒：
--   模型只能提出"这里有一簇脸"，**不知道那是谁**。所以它以 `status='proposed'`
--   落地、名字为空，等用户命名。这样界面永远不会出现模型编出来的人名。
-- `uuid` 与 library/collection 同源：跨机迁移时需要稳定身份。
CREATE TABLE person (
    id                  INTEGER PRIMARY KEY,
    uuid                TEXT    NOT NULL UNIQUE,
    name                TEXT,                             -- NULL = 模型提议、尚未命名
    status              TEXT    NOT NULL DEFAULT 'proposed',
    note                TEXT,
    cover_face_id       INTEGER REFERENCES face(id) ON DELETE SET NULL,
    created_at          TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL,
    CHECK (status IN ('proposed', 'named', 'archived'))
);

CREATE INDEX idx_person_status ON person(status, id);

-- ============================================================ 归属（正例）
--
-- `source` 区分"模型猜的"与"用户说的"：界面据此用不同措辞
-- （前者写"这是模型的判断"，后者写"你确认过"）。**这个区分不可省略** ——
-- 少了它，用户无法分辨界面上的归属是不是自己的意思。
CREATE TABLE person_face (
    person_id           INTEGER NOT NULL REFERENCES person(id) ON DELETE CASCADE,
    face_id             INTEGER NOT NULL REFERENCES face(id) ON DELETE CASCADE,
    source              TEXT    NOT NULL,
    confidence          REAL,                             -- 0..1，仅 auto 有意义
    similarity          REAL,                             -- 判定时与 prototype 的余弦（可复算）
    created_at          TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL,
    PRIMARY KEY (person_id, face_id),
    CHECK (source IN ('auto', 'user'))
);

CREATE INDEX idx_person_face_face ON person_face(face_id);
CREATE INDEX idx_person_face_source ON person_face(person_id, source);

-- ============================================================ 否决（反例）
--
-- 单独一张表而不是 `person_face` 上的布尔列：一条记录要么是"属于"要么是"不属于"，
-- 用两张表让"不属于"成为**一等事实**（可计数、可审计），而不是"缺少一行"。
-- 学习时"缺少一行"和"被否决"必须区分开 —— 前者是未知，后者是明确的负标签。
CREATE TABLE person_reject (
    person_id           INTEGER NOT NULL REFERENCES person(id) ON DELETE CASCADE,
    face_id             INTEGER NOT NULL REFERENCES face(id) ON DELETE CASCADE,
    created_at          TEXT    NOT NULL,
    PRIMARY KEY (person_id, face_id)
);

CREATE INDEX idx_person_reject_face ON person_reject(face_id);

-- ============================================================ 学到的原型
--
-- 这是"模型持续学习我的选择"的**唯一落点**，必须如实说清楚它是什么：
--
--   * 它**不是**被微调的神经网络 —— 128 维 SFace 权重一个字节都没变。
--   * 它是**从用户的确认/否决里重新估计出来的原型（均值向量）+ 判定阈值**。
--     每次用户点一次"对/不对"，本行的 pos_count/neg_count/vector/threshold 就更新一次，
--     下一次 `people assign` 用新的原型去判未归类的脸 → 增量结果跟着变。
--   * 之所以不做微调：全库人脸量级（千级）不足以训练，且微调会**破坏可复算性**
--     （同一个人脸向量在不同时刻得到不同分数，"为什么当初判成他"就不可复算了）。
--     原型的每一步都可以用库里的向量重算出来，符合铁律 #4。
--
-- `threshold` 由带标注的样本学出来（正例分位与反例分位之间取最大化间隔的点），
-- 而不是写死 0.363 —— 写死的阈值在换了人/换了相机之后必然失准。
CREATE TABLE person_profile (
    person_id           INTEGER NOT NULL REFERENCES person(id) ON DELETE CASCADE,
    model               TEXT    NOT NULL,
    model_version       TEXT    NOT NULL,
    prep_version        TEXT    NOT NULL,
    dim                 INTEGER NOT NULL,
    normalization       TEXT    NOT NULL,
    vector              BLOB    NOT NULL,                 -- 正例均值，已 L2 归一化
    negative_vector     BLOB,                             -- 反例均值（无否决时为空）
    pos_count           INTEGER NOT NULL DEFAULT 0,
    neg_count           INTEGER NOT NULL DEFAULT 0,
    threshold           REAL,                             -- 学到的接受阈值
    margin              REAL,                             -- 正/反例分布之间的间隔（界面据此说"有多不确定"）
    updated_at          TEXT    NOT NULL,
    PRIMARY KEY (person_id, model, model_version, prep_version)
);

-- ============================================================ 选择过程可追溯
--
-- 与 001 的 `selection_event` 同一纪律（"记录选择过程"是 PRD 明写的）：
-- 用户否决掉的那张脸也要留痕 —— 否则"为什么这个人少了一张"永远查不出来。
CREATE TABLE person_event (
    id                  INTEGER PRIMARY KEY,
    person_id           INTEGER REFERENCES person(id) ON DELETE CASCADE,
    face_id             INTEGER REFERENCES face(id) ON DELETE SET NULL,
    action              TEXT    NOT NULL,
    detail_json         TEXT,
    created_at          TEXT    NOT NULL,
    CHECK (action IN ('discover', 'assign', 'confirm', 'reject', 'rename',
                      'learn', 'merge', 'profile'))
);

CREATE INDEX idx_person_event_person ON person_event(person_id, created_at);
CREATE INDEX idx_person_event_action ON person_event(action, created_at);

-- ============================================================ 语义搜索的人工反馈
--
-- 一行 = "某个词的结果里，用户对某个 Shot 表了态"。
--   * `keep` = 用户把它选进主题（正例）；
--   * `drop` = 用户把它从结果里删掉（反例）。
--
-- **为什么必须落库而不是只改前端状态**：用户的目标是"模型能持续学习我的选择"。
-- 反馈一旦只活在前端，刷新就没了，学习也就无从谈起。
-- UNIQUE(query_id, shot_id) 让"改主意"变成幂等 UPDATE（keep ↔ drop），
-- 而不是在表里堆一堆互相矛盾的行。
CREATE TABLE semantic_feedback (
    id                  INTEGER PRIMARY KEY,
    query_id            INTEGER NOT NULL REFERENCES semantic_query(id) ON DELETE CASCADE,
    shot_id             INTEGER NOT NULL REFERENCES shot(id) ON DELETE CASCADE,
    action              TEXT    NOT NULL,
    created_at          TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL,
    UNIQUE (query_id, shot_id),
    CHECK (action IN ('keep', 'drop'))
);

CREATE INDEX idx_semantic_feedback_query ON semantic_feedback(query_id, action);

-- ============================================================ 学到的重排参数
--
-- 与 `person_profile` 同一套思路：**不改模型权重，改的是检索的打分**。
--
--   score'(x) = cos(x, 文本向量) + alpha * cos(x, 正例均值) - beta * cos(x, 反例均值)
--
-- 三个理由让这个设计成立：
--   1. **服务端不需要 torch**：只用库里已有的图像向量做点积（纯 numpy），
--      所以 `serve` 仍然不装 onnxruntime/torch，「方案 a」架构不被破坏。
--   2. **可复算**：正/负例均值与系数都在库里，任何时刻都能重算出同样的排序。
--   3. **不覆盖历史**：`semantic_query_hit` 保持"模型原始判断"，
--      重排只发生在读取端 —— 用户随时能关掉学习看到原始结果。
CREATE TABLE semantic_tuning (
    query_id            INTEGER PRIMARY KEY REFERENCES semantic_query(id) ON DELETE CASCADE,
    image_model         TEXT    NOT NULL,                 -- 图像向量身份（与 embedding 对齐）
    image_model_version TEXT    NOT NULL,
    image_prep_version  TEXT    NOT NULL,
    dim                 INTEGER NOT NULL,
    pos_vector          BLOB,                             -- 正例均值（L2 归一化）
    neg_vector          BLOB,                             -- 反例均值（L2 归一化）
    pos_count           INTEGER NOT NULL DEFAULT 0,
    neg_count           INTEGER NOT NULL DEFAULT 0,
    alpha               REAL    NOT NULL DEFAULT 0.35,
    beta                REAL    NOT NULL DEFAULT 0.25,
    updated_at          TEXT    NOT NULL
);

-- ============================================================ 主题的来源
--
-- 用户的工作流是"精选 → 按关键词/时间地点建主题"。主题列表将来要能回答
-- "这个主题是怎么来的"（来自某个词？某个人？某次筛选？）。
-- 存 JSON 而不是拆列：来源的形状会随入口增加而变化，而这些值只被整体读写。
-- 形状：{"kind":"query","query_text":"白裙子","feedback":"keep+drop 12/3"}
--      {"kind":"person","person_id":3,"person_name":"妈妈"}
--      {"kind":"find","city":"杭州","date_from":"2025-10-01"}
--      {"kind":"manual"}
ALTER TABLE collection ADD COLUMN source_json TEXT;
