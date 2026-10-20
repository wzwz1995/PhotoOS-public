-- PhotoOS Phase 1 核心 schema (001)
--
-- 铁律（PRD §5.2 / §14.5）：
--   * 本库中永远不出现绝对路径、盘符、Windows 用户名或用户目录。
--   * Asset 的位置身份 = library(uuid) + volume(卷指纹) + relative_path。
--   * 原片永不被系统移动/改写/删除；删除数据库记录 != 删除原文件。
--
-- 设计要点：
--   * asset 是索引的事实单位，**每个被扫描到的文件必有一条记录**；
--     shot 分组是可重算派生物，分组失败绝不丢 asset（PRD §9.1）。
--   * 同一文件的多份物理拷贝 = 1 个 asset + N 个 asset_instance，
--     既不产生重复 Asset，也不隐藏用户真实存在的文件。
--   * 所有 AI/派生数据带版本号（preview.generator_version），升级后可精确重算。

PRAGMA foreign_keys = ON;

-- ============================================================ Library / Volume

CREATE TABLE library (
    id                  INTEGER PRIMARY KEY,
    uuid                TEXT    NOT NULL UNIQUE,          -- 稳定身份，跨盘符/跨机器不变
    name                TEXT    NOT NULL,
    root_path_hint      TEXT,                             -- 运行时提示，非身份；可为空/过期
    marker_uuid         TEXT,                             -- <root>/.photoos/library.json 中的 uuid
    status              TEXT    NOT NULL DEFAULT 'OFFLINE'
                        CHECK (status IN ('ONLINE','OFFLINE','MISSING','UNBOUND','SCANNING')),
    created_at          TEXT    NOT NULL,
    last_seen_at        TEXT,
    notes               TEXT
);

CREATE TABLE volume (
    id                  INTEGER PRIMARY KEY,
    library_id          INTEGER REFERENCES library(id) ON DELETE SET NULL,
    kind                TEXT    NOT NULL DEFAULT 'unknown'
                        CHECK (kind IN ('fixed','removable','network','cdrom','ram','unknown')),
    serial              INTEGER,                          -- Windows 卷序列号（GetVolumeInformationW）
    fingerprint         TEXT,                             -- 相对路径+size+mtime 抽样摘要
    fs_type             TEXT,                             -- NTFS / exFAT / ...
    label               TEXT,
    mount_hint          TEXT,                             -- 最近一次盘符，仅作提示
    status              TEXT    NOT NULL DEFAULT 'UNKNOWN'
                        CHECK (status IN ('ONLINE','OFFLINE','UNKNOWN')),
    first_seen_at       TEXT    NOT NULL,
    last_seen_at        TEXT,
    UNIQUE (serial, label)
);

CREATE INDEX idx_volume_library ON volume(library_id);

-- ============================================================ Session / Scene

-- Phase 1 只建表并用确定性规则生成；人工编辑属 Phase 2（source 字段区分且人工结果不被覆盖）。
CREATE TABLE session (
    id                  INTEGER PRIMARY KEY,
    name                TEXT    NOT NULL,
    captured_from       TEXT,                             -- 本地日期时间（naive text）
    captured_to         TEXT,
    source              TEXT    NOT NULL DEFAULT 'auto' CHECK (source IN ('auto','user')),
    confidence          REAL,
    city_hint           TEXT,                             -- 来自目录一级（城市）
    period_hint         TEXT,                             -- 来自目录二级（时段文件夹名）
    group_revision      INTEGER NOT NULL DEFAULT 1,       -- 重算代数，便于审计
    created_at          TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL
);

CREATE TABLE scene (
    id                  INTEGER PRIMARY KEY,
    session_id          INTEGER REFERENCES session(id) ON DELETE SET NULL,
    name                TEXT    NOT NULL,
    captured_from       TEXT,
    captured_to         TEXT,
    dir_hint            TEXT,                             -- relative dir，作为可重算线索
    source              TEXT    NOT NULL DEFAULT 'auto' CHECK (source IN ('auto','user')),
    confidence          REAL,
    group_revision      INTEGER NOT NULL DEFAULT 1,
    created_at          TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL
);

CREATE INDEX idx_scene_session ON scene(session_id);

-- ============================================================ Shot

CREATE TABLE shot (
    id                  INTEGER PRIMARY KEY,
    scene_id            INTEGER REFERENCES scene(id) ON DELETE SET NULL,
    captured_at         TEXT,                             -- 本地拍摄时间（排序/分组主键）
    capture_date_local  TEXT,                             -- YYYY-MM-DD，便于索引与筛选
    primary_asset_id    INTEGER,                          -- 见 FK 说明：SQLite 允许前向引用，删除时置空
    camera_key          TEXT,                             -- make|model，仅作分组线索，不建 Device 实体（PRD §4.3）
    group_source        TEXT    NOT NULL DEFAULT 'auto' CHECK (group_source IN ('auto','user')),
    group_confidence    REAL,
    group_revision      INTEGER NOT NULL DEFAULT 1,
    created_at          TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL
);

CREATE INDEX idx_shot_scene    ON shot(scene_id);
CREATE INDEX idx_shot_captured ON shot(captured_at);
CREATE INDEX idx_shot_date     ON shot(capture_date_local);

-- ============================================================ Asset

CREATE TABLE asset (
    id                  INTEGER PRIMARY KEY,
    shot_id             INTEGER REFERENCES shot(id) ON DELETE SET NULL,
    library_id          INTEGER NOT NULL REFERENCES library(id) ON DELETE CASCADE,
    relative_path       TEXT    NOT NULL,                 -- 相对 Library 根，反斜杠分隔，不含盘符
    content_hash        TEXT,                             -- BLAKE3 十六进制小写；NULL = 尚未计算
    hash_algo           TEXT,                             -- 'blake3' | 'sha256'（随行存储，便于未来切换）
    size                INTEGER,
    mtime_ns            INTEGER,
    mime                TEXT,
    file_type           TEXT    NOT NULL DEFAULT 'other'
                        CHECK (file_type IN ('raw','jpeg','heic','png','tiff','webp','video','other')),
    status              TEXT    NOT NULL DEFAULT 'PENDING'
                        CHECK (status IN ('PENDING','OK','MISSING','UNREADABLE','QUARANTINED','SKIPPED')),
    status_reason       TEXT,
    first_seen_at       TEXT    NOT NULL,
    last_verified_at    TEXT,
    UNIQUE (library_id, relative_path)
);

CREATE INDEX idx_asset_shot        ON asset(shot_id);
CREATE INDEX idx_asset_library     ON asset(library_id);
CREATE INDEX idx_asset_hash        ON asset(content_hash);
CREATE INDEX idx_asset_status      ON asset(status);
CREATE INDEX idx_asset_type        ON asset(file_type);

-- 同一内容的多个物理副本。UNIQUE(volume_id, relative_path) 让重复扫描天然幂等。
CREATE TABLE asset_instance (
    id                  INTEGER PRIMARY KEY,
    asset_id            INTEGER NOT NULL REFERENCES asset(id) ON DELETE CASCADE,
    volume_id           INTEGER NOT NULL REFERENCES volume(id) ON DELETE CASCADE,
    relative_path       TEXT    NOT NULL,
    size                INTEGER,
    mtime_ns            INTEGER,
    file_id             TEXT,                             -- Windows file index（同卷内改名/移动可识别）
    in_library          INTEGER NOT NULL DEFAULT 0,       -- 1 = 由 PhotoOS 复制入库
    first_seen_at       TEXT    NOT NULL,
    last_seen_at        TEXT,
    UNIQUE (volume_id, relative_path)
);

CREATE INDEX idx_instance_asset ON asset_instance(asset_id);

-- ============================================================ Metadata

CREATE TABLE metadata (
    asset_id            INTEGER PRIMARY KEY REFERENCES asset(id) ON DELETE CASCADE,
    camera_make         TEXT,
    camera_model        TEXT,
    lens_make           TEXT,
    lens_model          TEXT,
    focal_length        REAL,
    focal_length_35mm   REAL,
    aperture            REAL,
    shutter_speed       TEXT,                             -- 保留 1/250 形式；数值另存 exposure_time
    exposure_time       REAL,
    iso                 INTEGER,
    exposure_comp       REAL,
    metering_mode       TEXT,
    flash               TEXT,
    wb                  TEXT,
    orientation         INTEGER,
    width               INTEGER,
    height              INTEGER,
    capture_at          TEXT,                             -- 本地拍摄时间，'YYYY-MM-DD HH:MM:SS'
    capture_at_utc      TEXT,                             -- 仅在时区已知时写入
    capture_date_local  TEXT,
    tz_offset           TEXT,                             -- '+08:00' | NULL
    tz_source           TEXT CHECK (tz_source IN ('exif-offset','container-utc+assumed','unknown','file-mtime')),
    subsec              TEXT,
    gps_lat             REAL,
    gps_lon             REAL,
    gps_alt             REAL,
    device_serial       TEXT,
    software            TEXT,
    source              TEXT    NOT NULL DEFAULT 'none'
                        CHECK (source IN ('libraw','pillow','exifread','exiftool','ffmpeg','none')),
    raw_json            TEXT,                             -- 原始标签快照，便于未来重解析
    extracted_at        TEXT
);

CREATE INDEX idx_meta_capture  ON metadata(capture_at);
CREATE INDEX idx_meta_date     ON metadata(capture_date_local);
CREATE INDEX idx_meta_camera   ON metadata(camera_make, camera_model);
CREATE INDEX idx_meta_lens     ON metadata(lens_model);
CREATE INDEX idx_meta_iso      ON metadata(iso);
CREATE INDEX idx_meta_gps      ON metadata(gps_lat, gps_lon);

-- ============================================================ Preview

CREATE TABLE preview (
    id                  INTEGER PRIMARY KEY,
    asset_id            INTEGER NOT NULL REFERENCES asset(id) ON DELETE CASCADE,
    kind                TEXT    NOT NULL CHECK (kind IN ('thumb','preview','video_cover')),
    path                TEXT    NOT NULL,                 -- 相对 data_root，不含盘符
    width               INTEGER,
    height              INTEGER,
    bytes               INTEGER,
    generator_version   TEXT    NOT NULL,                 -- 升级后可精确重算，不动原片
    created_at          TEXT    NOT NULL,
    UNIQUE (asset_id, kind)
);

-- ============================================================ Job（本地异步队列）

CREATE TABLE job (
    id                  INTEGER PRIMARY KEY,
    type                TEXT    NOT NULL,                 -- scan|hash|metadata|preview|verify|migrate|backup
    status              TEXT    NOT NULL DEFAULT 'PENDING'
                        CHECK (status IN ('PENDING','PROCESSING','COMPLETED','FAILED','RETRY','CANCELLED','PAUSED')),
    scope_json          TEXT,                             -- 任务参数（library_id / 路径等）
    progress_done       INTEGER NOT NULL DEFAULT 0,
    progress_total      INTEGER NOT NULL DEFAULT 0,
    progress_note       TEXT,
    error               TEXT,
    retry_count         INTEGER NOT NULL DEFAULT 0,
    max_retries         INTEGER NOT NULL DEFAULT 3,
    priority            INTEGER NOT NULL DEFAULT 100,
    created_at          TEXT    NOT NULL,
    started_at          TEXT,
    finished_at         TEXT,
    heartbeat_at        TEXT
);

CREATE INDEX idx_job_status ON job(status, priority, id);

-- 断点续跑的单位：每条待处理项独立记录状态与尝试次数（PRD §11.2 导入/缩略图要求）。
CREATE TABLE job_item (
    id                  INTEGER PRIMARY KEY,
    job_id              INTEGER NOT NULL REFERENCES job(id) ON DELETE CASCADE,
    ref_type            TEXT    NOT NULL,                 -- 'asset' | 'path' | 'library'
    ref_id              INTEGER,
    ref_key             TEXT,                             -- 无 asset_id 时用相对路径等稳定键
    state               TEXT    NOT NULL DEFAULT 'PENDING'
                        CHECK (state IN ('PENDING','PROCESSING','DONE','FAILED','SKIPPED')),
    attempts            INTEGER NOT NULL DEFAULT 0,
    last_error          TEXT,
    updated_at          TEXT    NOT NULL,
    UNIQUE (job_id, ref_type, ref_key)
);

CREATE INDEX idx_job_item_job ON job_item(job_id, state);

-- ============================================================ 审计与迁移

CREATE TABLE app_event (
    id                  INTEGER PRIMARY KEY,
    at                  TEXT    NOT NULL,
    kind                TEXT    NOT NULL,                 -- scan|rebind|migrate|integrity|config|safety
    library_id          INTEGER REFERENCES library(id) ON DELETE SET NULL,
    payload_json        TEXT
);

CREATE INDEX idx_event_kind ON app_event(kind, at);

-- 注意：`schema_migrations` 账本表由迁移器自己负责创建（migrator._ensure_ledger），
-- 不属于任何迁移，因此这里不建 —— 保证「自建库」与「升级库」行为完全一致。

-- ============================================================ Phase 2 预留（仅建表，不建逻辑）

-- PRD §9.2 数据模型最低要求。Phase 1 不写入、不提供接口，仅避免 Phase 2 做数据迁移。
-- Phase 2 才允许出现 UNREVIEWED/CANDIDATE/SELECTED/FINAL 状态机与理由字段。
CREATE TABLE selection (
    shot_id             INTEGER PRIMARY KEY REFERENCES shot(id) ON DELETE CASCADE,
    status              TEXT    NOT NULL DEFAULT 'UNREVIEWED'
                        CHECK (status IN ('UNREVIEWED','CANDIDATE','SELECTED','FINAL')),
    reason              TEXT,
    updated_at          TEXT    NOT NULL
);

CREATE TABLE selection_event (
    id                  INTEGER PRIMARY KEY,
    shot_id             INTEGER NOT NULL REFERENCES shot(id) ON DELETE CASCADE,
    from_status         TEXT,
    to_status           TEXT    NOT NULL,
    reason              TEXT,
    created_at          TEXT    NOT NULL
);

CREATE INDEX idx_sel_event_shot ON selection_event(shot_id, created_at);
