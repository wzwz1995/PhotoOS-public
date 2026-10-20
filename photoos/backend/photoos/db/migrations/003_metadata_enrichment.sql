-- 003: metadata 增加"补充来源"字段，并记录缩略图/预览的合并逻辑所需的字段。
--
-- 背景：RAW 文件的元数据由 LibRaw 提供主体（机型/镜头/曝光），但 GPS、OffsetTime、
-- 机身序列号等只在标准 EXIF 里。因此一个 Asset 的元数据可能由两个解析器共同贡献，
-- 必须分别记录，否则"数据来自哪里"不可追溯（PRD §9.3 对 AI 数据要求版本化，元数据同理）。

ALTER TABLE metadata ADD COLUMN source_enrichment TEXT
    CHECK (source_enrichment IS NULL OR source_enrichment IN ('pillow','exifread','exiftool'));

-- 视频：容器解析出的时长/编码也值得留存（Phase 1 只入库不做内容分析）
ALTER TABLE metadata ADD COLUMN duration_seconds REAL;
ALTER TABLE metadata ADD COLUMN codec TEXT;
