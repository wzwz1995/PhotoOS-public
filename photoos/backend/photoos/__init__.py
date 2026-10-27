r"""PhotoOS · 个人摄影管理系统（Phase 1: Photo Library Foundation）

设计依据：`C:\PhotoOS\PHASE1_TECH_PLAN.md`（PRD v3.0 Final 的 Phase 1 实现契约）。

铁律（PRD §2.1 / §14）：
  * 原始照片永不被系统移动、改写或删除；删除数据库记录 ≠ 删除原文件。
  * 数据库内不出现绝对路径、盘符、Windows 用户名 —— 只存 relative_path + Library UUID + 卷指纹。
  * Phase 1 只实现 Phase 1 功能；下一阶段只预留必要接口。
"""

__version__ = "0.2.2"

__all__ = ["__version__"]
