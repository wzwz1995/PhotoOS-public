# PhotoOS

本机单用户的照片管理、精选与主题系统。桌面绿色包见根目录 README；这个包也可以作为 Python 后端 / CLI / Web UI 单独安装。

## 它是什么

- FastAPI + SQLite（WAL）后端，零构建前端（原生 ES module + CSS）
- 数据模型：Library → Session（时段）→ Shot（拍摄组）→ Scene（场景）→ Selection（精选）→ Collection（主题）
- 原片只读：导入、索引、精选、导出都不移动、不改名、不删除原片
- 可选 AI：CLIP 相似照片、OpenCV 人脸、EmbeddingGemma 中文语义搜索；不装也能用基础功能
- 单库实测 21,000+ 张照片可正常索引、浏览、精选
- schema 9，迁移只追加

## 安装

### 绿色包（推荐给最终用户）

从 [GitHub Releases](https://github.com/wzwz1995/PhotoOS-public/releases) 下载
`PhotoOS-<版本>-win64.zip`，解压后双击 `PhotoOS.exe`。不需要 Python / Node，不需要安装程序。

### wheel

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install .\photoos-0.1.0-py3-none-any.whl
$env:PHOTOOS_DATA_ROOT = "$env:USERPROFILE\PhotoOS\data"
.\.venv\Scripts\python.exe -m photoos init
.\.venv\Scripts\python.exe -m photoos serve    # http://127.0.0.1:8760
```

### 源码

在本目录（有 `pyproject.toml`）执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
$env:PHOTOOS_DATA_ROOT = "$env:USERPROFILE\PhotoOS\data"
.\.venv\Scripts\python.exe -m photoos init
.\.venv\Scripts\python.exe -m photoos serve
```

需要 Python 3.12 / Windows 10+（64 位）。

## 数据根

数据根必须显式指定；没有 `photoos.toml` 时不要依赖隐式默认值。

```text
<数据根>\
  photoos.db          SQLite（WAL）
  thumbnails\         缩略图（可重建）
  previews\           预览图（可重建）
  backups\            迁移前自动备份 + 手动备份
  exports\            导出的 zip
  logs\ jobs\ ai_cache\
  config.json
```

- 原片在**你自己的目录**里，PhotoOS 只读。
- PhotoOS 写进照片库根目录的唯一文件是 `.photoos\library.json`（库身份标记）。
- 删除数据库记录不等于删除原片；导出/打包也不改原片。

## 常用命令

```powershell
photoos init                       # 初始化数据根与数据库（幂等）
photoos serve                      # 本机 Web UI
photoos library add <路径> --write-marker
photoos import <源目录> --library <id|名称> --apply
photoos index --library <id|名称> --wait
photoos sessions backfill --library <id|名称> --apply
photoos shots regroup --library <id|名称> --apply
photoos scenes rebuild --library <id|名称> --apply
photoos integrity                  # 完整性/外键/孤立数据检查
photoos backup --label manual      # 在线备份
photoos stats                      # 索引统计
```

## 文档

- 根目录 `README.md`：下载、运行、数据位置
- `docs/20-reference/`：架构、数据模型、任务引擎、AI、前端、运维
- `docs/`：分阶段设计说明（公开快照保留结论与现状）

## 许可证

MIT。见仓库根目录 `LICENSE`。
