# PhotoOS · 个人摄影管理

把散落在硬盘和各种存储卡里的照片，整理成**一次拍摄 → 一个场景 → 一组一张**的可回看结构，
最后只留下值得留的那几张。

本机运行、不联网、**不改动你的原片**。

---

## 下载

绿色包发布在 [GitHub Releases](https://github.com/wzwz1995/PhotoOS-public/releases)。

- 文件：`PhotoOS-<版本>-win64.zip`
- 解压后双击 `PhotoOS.exe`；不需要 Python / Node，不需要安装程序
- 每个 Release 正文带 SHA256；下载后先校验
- 未签名 exe，Windows 可能提示「未知发布者」；选择「更多信息 → 仍要运行」

## 它解决什么

- **照片越攒越乱**：按拍摄活动自动分组（城市 + 时间跨度），不用手动建文件夹
- **两万张里挑不出十张**：连拍与相似构图自动归组，一组只做一个决定，按 `Space` 一路划过
- **"我记得拍过一张日落"**：以图搜图 / 相似照片（可选 AI 组件）
- **换盘、换电脑**：库身份靠卷序列号与标记文件识别，不靠盘符

## 主要功能

| 功能 | 说明 |
|---|---|
| 导入 | 从相机目录/存储卡复制归位，按时间与 GPS 落到 `<城市>\<时段>\`；原片只读 |
| 手机导入 | 收件箱（把手机照片复制进一个文件夹，复制稳定后导入）或 ADB（可选，批量拉取到收件箱）；手机上的文件只读 |
| 索引 | 21,000+ 张实测可跑；元数据、哈希、缩略图与预览本地生成 |
| 分组 | 时段（Session）→ 拍摄组（Shot）→ 场景（Scene），三层都是可重建的派生数据 |
| 精选 | 每组选 1–3 张，记录保留"为什么"；重建分组不会丢精选 |
| 主题 | 把跨场景的照片编成一个主题（如"婚礼"），可导出清单 |
| 废片清理 | 详情里标记废片，攒够后统一移入 Windows 回收站（可从回收站还原）；命中精选/主题的照片自动跳过 |
| 换盘 | 卷序列号 + `.photoos` 标记文件识别，换盘符不用重新导入 |
| AI（可选） | CLIP 视觉嵌入（相似照片）、OpenCV 人脸（自动认人）；按需下载，不装也能用全部基础功能 |

## 系统要求

- Windows 10 1809+ / Windows 11（64 位）
- 不需要安装 Python：绿色包内置运行时
- 磁盘：程序约 620 MB；每 1 万张照片的缩略图/预览约 1.5 GB

## 运行（绿色包）

1. 从 [Releases](https://github.com/wzwz1995/PhotoOS-public/releases) 下载 `PhotoOS-<版本>-win64.zip`
2. 解压到任意可写目录（不要放 `Program Files`）
3. 双击 `PhotoOS.exe`
4. 首次启动会建一个空库；在 **设置** 里可切换数据目录

> 未签名程序，首次运行 Windows 可能提示"已保护你的电脑"，
> 点「更多信息 → 仍要运行」。

## 数据放在哪

| 内容 | 位置 |
|---|---|
| 数据库、缩略图、备份 | 绿色包默认 `%APPDATA%\PhotoOS\data`（首次启动可选择）；wheel 安装默认 `%LOCALAPPDATA%\PhotoOS\data` |
| 照片原片 | 你自己的目录，**只读**，不被移动或改名 |
| 库标记 | 照片库根目录下 `.photoos\library.json`（唯一写入原片目录的文件） |

## AI 组件（可选）

默认都不安装。在 **设置 → AI 组件** 里按需下载，支持自定义镜像源：

| 组件 | 体积 | 解锁 |
|---|---|---|
| ONNX Runtime (CPU) | 15 MB | 推理运行时 |
| CLIP 视觉塔 | 335 MB | 相似照片 / 以图搜图 |
| OpenCV + 人脸模型 | 92 MB | 人脸检测与识别 |
| ONNX Runtime (GPU) | 231 MB | 同上，走 NVIDIA GPU（需本机 CUDA） |

中文语义搜索需要 4.3 GB 的 EmbeddingGemma，属于外挂组件，不提供一键安装。

## 隐私

- 全程本机运行，不上传任何照片、路径或统计
- 没有账号系统，不需要联网激活
- 唯一的网络行为：你主动点击"下载 AI 组件"时

## 从源码运行

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e photoos
$env:PHOTOOS_DATA_ROOT = "$env:USERPROFILE\PhotoOS\data"
.\.venv\Scripts\python.exe -m photoos init
.\.venv\Scripts\python.exe -m photoos serve    # http://127.0.0.1:8760
```

要自己构建桌面绿色包：`desktop\build-portable.ps1`（需要 Node/pnpm；会下载 embeddable Python 与依赖）。

架构、数据模型、任务引擎等见 `photoos/docs/20-reference/`。

## 状态

早期可用版本。已实现并验证：导入、索引、分组、精选、主题、换盘、备份、AI 组件按需下载。
尚未覆盖：多用户、云同步、移动端。

## 许可证

MIT，见 [LICENSE](LICENSE)。
