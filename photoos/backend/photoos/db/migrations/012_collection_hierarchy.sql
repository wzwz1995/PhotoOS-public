-- PhotoOS 主题上下级关系 (012)
--
-- 目标：主题可以有父子关系（用户需求：世界树之巅 → 奥尔梅克 / 阿兹特克）。
-- 语义（用户确认）：
--   * 父主题**本身仍是主题**，可以放自己的照片（collection_item 不受影响）。
--   * 子主题在父主题详情里作为「统合目录」展示：只显示封面、可以点开进入子主题。
--   * 层级**任意深**（子主题还能再建子主题）。
--
-- 删除策略：ON DELETE SET NULL —— 删除父主题时，它的子主题**升为顶层**，
-- 而不是被级联删除（铁律 #5：删一个主题不该连带吞掉用户的其它主题）。
-- 防环（主题不能成为自己的后代）由仓库层 `CollectionRepo` 负责 ——
-- 自引用外键只保证 parent_id 指向存在的主题，不保证无环。

ALTER TABLE collection ADD COLUMN parent_id INTEGER REFERENCES collection(id) ON DELETE SET NULL;

-- 子主题查询（父主题详情里的「子主题」区块）与主题树的构建都要按 parent_id 反查
CREATE INDEX idx_collection_parent ON collection(parent_id);
