-- PhotoOS Phase 4 补丁（009）：人脸的人工判定 —— "这张脸不是人"
--
-- 铁律一致性：
--   * #1 原片永不被动：只建一张表，**不涉及任何文件操作**。
--   * #2 库内不出现身份性路径：只引用 `face(id)`。
--   * #3 检测是事实、归属是判断：这里存的是**用户对人脸检测结果的判断**，
--     与 `face`（检测事实）分开 —— 用户说"这不是人"并不改变"检测器在这里检出过一张脸"。
--   * #4 派生数据版本化：本表不是派生数据，是人工输入，故不参与重算、不被覆盖。
--   * #5 破坏性操作二次确认：判定可以随时撤销（删掉这一行即可）。
--   * #6 不扩大范围：只加"不是人"这一种判定，不做标签体系。
--
-- ## 为什么需要它
--
-- 检测器只认"这一块像不像脸"。于是海报上的脸、雕像、电视里的画面、漫画、反光的
-- 模糊团块都会被检出来。聚类时它们会把几个不相干的人粘成一簇（用户的原话是
-- "有的分组里啥都有"），而现有的两个动作都表达不了"这压根不是一个人"：
--   * 「不是他」是**相对某个人**的（`person_reject`），换个人还会再提；
--   * 「解散候选集」是**相对某一簇**的（`person.status='archived'`），
--     那些脸仍然留在池子里等着下次被重新聚在一起。
--
-- ## 为什么是独立表而不是 `face` 上加一列
--
-- `face` 是**检测器的事实**："这个位置有一张脸，置信度多少"。
-- 加一列 `is_person` 会把人的判断混进事实表，于是"重算检测"这类操作
-- 就有了一个无法回答的问题：重算之后用户的判断还算不算？
-- 分开之后语义清楚：**脸还在，只是用户说它不是人**。
-- 撤销就是删这一行（`CREATE TABLE` 无触发器，删除天然可逆）。
--
-- 注意：文件内不得出现独立的事务控制语句（迁移器自己负责事务）。
CREATE TABLE face_verdict (
    -- 一张脸一条判定：`face_id` 直接做主键。
    -- 将来若出现第二种判定（例如"这张脸太糊、别参与学习"），
    -- 那是**另一个关注点**：要么加一个 `kind` 列并改主键，要么另建一张表 ——
    -- 不要在 `verdict` 这个 CHECK 里顺手塞第二个值，那会让"删一行 = 撤销"失效。
    face_id     INTEGER PRIMARY KEY REFERENCES face(id) ON DELETE CASCADE,
    verdict     TEXT    NOT NULL CHECK (verdict IN ('not_person')),
    note        TEXT,
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL
);

-- 按判定查（"我标过哪些不是人"）：主键已经是 face_id，但要按**时间倒序**列出来，
-- 于是这个索引按 created_at 建，避免每次展示都全表排序。
CREATE INDEX idx_face_verdict_created ON face_verdict(created_at DESC);

-- ============================================================ 事件词汇表扩容
--
-- 009 新增的四个动作都要在 `person_event` 里留痕（"选择过程可追溯"是 PRD 明写的），
-- 而 008 把 action 限死在一个 CHECK 白名单里 —— 于是必须扩这个白名单。
--
-- ⚠️ SQLite **不能** `ALTER TABLE ... DROP CONSTRAINT`：改 CHECK 只能重建表。
--    重建三步（建新表 → 拷数据 → 换名），索引也要跟着重建。
--    008 已经应用且 checksum 已记账，**不许改 008**，所以这件事只能在这里做。
--
-- 新增的四个动作名（成对，方向写在 person_id 里）：
--   * `dissolve` / `restore` —— 解散候选集 / 放回来；
--   * `move_in`  / `move_out` —— 一脸搬进某人名下 / 从某人名下搬走；
--   * `not_person` / `person_again` —— 标成"不是人" / 撤销这个判断。
CREATE TABLE person_event_new (
    id                  INTEGER PRIMARY KEY,
    person_id           INTEGER REFERENCES person(id) ON DELETE CASCADE,
    face_id             INTEGER REFERENCES face(id) ON DELETE SET NULL,
    action              TEXT    NOT NULL,
    detail_json         TEXT,
    created_at          TEXT    NOT NULL,
    CHECK (action IN ('discover', 'assign', 'confirm', 'reject', 'rename',
                      'learn', 'merge', 'profile',
                      'dissolve', 'restore', 'move_in', 'move_out',
                      'not_person', 'person_again'))
);

INSERT INTO person_event_new (id, person_id, face_id, action, detail_json, created_at)
     SELECT id, person_id, face_id, action, detail_json, created_at FROM person_event;

DROP TABLE person_event;

ALTER TABLE person_event_new RENAME TO person_event;

CREATE INDEX idx_person_event_person ON person_event(person_id, created_at);
CREATE INDEX idx_person_event_action ON person_event(action, created_at);
