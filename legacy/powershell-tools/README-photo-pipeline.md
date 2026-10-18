# 照片/视频自动归类流水线 · 使用说明

把新照片（相机卡、手机导出、下载目录）丢进来，一条命令完成：**读元数据 → GPS 反查地级市 → 按「城市 / 连续时段」归位 → 内容级查重 → 出报告**。

- 图形界面：`C:\Photos工具\照片归类工具.cmd`（桌面也有快捷方式）
- 命令行入口：`photo-pipeline.ps1`（一行快捷方式：`import-photos.cmd`）
- 全部文件都在 `C:\Photos工具\`（自带全部脚本，不依赖别的盘）

## 0. 目录约定
- `C:\Photos工具\` —— 工具本体（双击 `照片归类工具.cmd` 即可），自带全部脚本与配置
- `C:\Photos\` / `C:\Videos\` —— 归类后的库
- `C:\Photos\_归类清单\` —— 日志、缓存、索引、回滚脚本、隔离区
- 上面这些路径**写在 `C:\Photos工具\photo-tool.config.json` 里**，界面点「设置」就能改；脚本本身没有写死路径（命令行参数优先级最高）

## 0.1 图形界面（推荐日常使用）

双击 `C:\Photos工具\照片归类工具.cmd` 或桌面的「照片归类工具」：

1. 把素材目录粘进输入框（也可以直接把文件夹**拖进窗口**）
2. **先不勾**「实际执行」→ 点「开始处理」= 试运行，只看计划、不动任何文件
3. 看过日志没问题，再勾上「实际执行」跑一次，文件才真正归位

界面上的按钮：`▶ 开始处理`、`■ 停止`（中断后可回滚）、`浏览...`、`打开照片库`、`打开隔离区`、`打开运行记录`。
选项：时段阈值（默认 12 小时）、跳过查重、只处理前 N 个（试跑用）。

界面本质是调用下面的命令行流水线，输出**实时**打进窗口日志；同一个日志文件也会留在 `C:\Photos\_归类清单\gui-run-*.log`。

自测（不需要人点）：`photo-tool-gui.ps1 -SelfTest -TestSource <目录> -TestExecute`，成功输出 `SELFTEST: OK`。

## 1. 一句话用法

```powershell
# 先看计划（不移动任何文件）
powershell -ExecutionPolicy Bypass -File C:\Photos工具\photo-pipeline.ps1 -Source "E:\新照片"

# 确认后执行
powershell -ExecutionPolicy Bypass -File C:\Photos工具\photo-pipeline.ps1 -Source "E:\新照片" -Execute

# 或双击式快捷入口
C:\Photos工具\import-photos.cmd "E:\新照片" -Execute
```

多个来源一起导入：`-Source "E:\卡1","E:\手机导出"`。

完整参数：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `-Source` | 必填 | 新照片/视频所在目录，可多个 |
| `-PhotoRoot` / `-VideoRoot` | `C:\Photos` / `C:\Videos` | 库根目录 |
| `-WorkRoot` | `C:\Photos\_归类清单` | 缓存、日志、运行报告的家目录 |
| `-GapHours` | `12` | 相邻两张间隔超过多少小时就切成新时段（跨天连续拍摄不拆） |
| `-Execute` | 关 | 不加＝dry run；加上才真正移动 |
| `-SkipDedupe` / `-SkipGeocode` | 关 | 跳过查重 / 跳过反查 |
| `-MaxFiles` | 0（不限） | 只处理前 N 个文件，用于试跑 |

## 2. 归类规则

- 目录：`C:\Photos\<地级市>\<时段>\`、`C:\Videos\<地级市>\<时段>\`（视频独立目录，命名规则相同）
- 时段名：同一天 `2025-10-12_0905-1616`；跨天 `2025-10-24_2349_至_10-25_1807`
- 城市：EXIF GPS → 0.05° 网格 → 反查地级市（结果缓存在 `geo-cache.csv`，命中即不再联网）
- 无 GPS 时依次兜底：① 同时段内其他照片有 GPS → 整段继承；② 前后相邻时段（≤24h）城市一致 → 继承；③ 都不行 → `未知地点`
- 时间：照片用 EXIF `DateTimeOriginal`（带亚秒与时区）；视频用容器 `mvhd`（UTC，按 +08:00 折算）；都没有则退回文件时间

## 3. 查重规则（只认字节）

1. 先按体积分组（体积唯一的文件天然不重复）
2. 比「首尾 256KB + 体积」指纹（`dup-cache.csv` 缓存）
3. 指纹相同的才做**全文件 SHA256** —— 只有字节完全相同才判为重复

所以：NEF 与同名 JPG 不是重复；连拍两张不是重复；同名但内容不同的照片（本库有约一半这种情况）也不是重复。

**跨文件夹的重复**默认只报告不移动（`Role=duplicate-other-folder`），避免让某个城市少内容；加 `-AllowCrossFolderDedupe` 才会一起隔离。

隔离时如果目标位置已经有同名文件（上一轮查重留下的），**不会覆盖**，而是自动加 `_q2`、`_q3` 后缀并存。

## 3.5 增量复用（关键机制）

库索引 `library-index.csv` 记录「库里每个文件属于哪个城市 / 哪个时段」。每次导入时：

- **并入已有时段**：新照片若与库内某时段的时间距离 ≤ `GapHours`（同城市），直接放进那个已存在的文件夹，不会另开一个内容重叠的新文件夹
- **继承城市**：新照片自己没 GPS 时，可以靠"并入库内某个已定位时段"来获得城市（`CityHow=inherited-library`），比单看这一批照片可靠得多
- **跨批次查重**：因为重复照片会并到同一个文件夹，查重就能判为"同文件夹副本"并隔离；否则会被当成"跨文件夹"而不动
- **索引自动维护**：每次运行后追加新文件、剔除已隔离/已消失的文件

索引缺失时（全新库）流水线会自动扫描库内文件建立一次，之后都是增量。

## 4. 安全保证

- 默认 dry run；只有 `-Execute` 才动手
- 同盘移动（rename），不复制、不重写、**全程零删除**
- 每个文件移动后立刻校验「目标存在 + 字节数一致」，不一致就移回并中止
- 同名冲突绝不覆盖：自动加 `__2`、`__3` 后缀
- 重复副本不是删除，而是移到 `C:\Photos\_归类清单\_重复文件（确认后可删除）\`（镜像原路径）
- 每次运行都产出：清单、计划、移动日志、查重日志、回滚脚本、运行报告

## 5. 产物在哪

```
C:\Photos\_归类清单\
├── geo-cache.csv          # 坐标网格 → 地级市（跨运行累积，可放心删，删了会重新联网查）
├── dup-cache.csv          # 路径+大小+时间 → 指纹/哈希（跨运行累积，加速查重）
├── library-index.csv      # 库里每个文件的城市/时段（增量合并的关键，别删）
├── _重复文件（确认后可删除）\   # 隔离的重复副本，确认后整个目录删掉即释放空间
└── runs\<时间戳>\
    ├── inventory.csv      # 本次扫描的元数据全清单
    ├── plan-*.csv         # 归类计划（来源 → 目标）
    ├── move-journal-*.csv # 实际移动记录
    ├── restore-*.ps1      # 归类回滚
    ├── duplicate-plan-*.csv
    ├── dedupe-journal-*.csv
    ├── undo-dedupe-*.ps1  # 查重回滚
    └── run-report.md      # 本次运行报告
```

## 6. 常见场景

| 场景 | 命令 |
| --- | --- |
| 相机卡导入 | `import-photos.cmd "E:\DCIM"` → 确认后 `-Execute` |
| 只补跑查重（不导入） | `find-duplicates.ps1 -Roots C:\Photos,C:\Videos`（加 `-Quarantine` 才隔离） |
| 导入但跳过查重 | `photo-pipeline.ps1 -Source "E:\新照片" -SkipDedupe -Execute` |
| 把已知城市补进"未知地点" | 手工移动即可；下次查重会自动识别重复 |
| 释放隔离区空间 | `Remove-Item 'C:\Photos\_归类清单\_重复文件（确认后可删除）' -Recurse -Force`（不可恢复） |
| 反悔某次归类 | 跑该次 `runs\<时间戳>\restore-*.ps1` |
| 反悔某次查重 | 跑该次 `runs\<时间戳>\undo-dedupe-*.ps1` |

## 7. 中断了怎么办

流水线每一步都独立且可重入：

- 扫描/反查中断：重跑即可，缓存会接上
- 归类中断：已移动的文件不会重复处理（脚本会跳过库内文件）；重跑会为剩余文件生成新计划
- 查重中断：重跑会复用哈希缓存
- 任何移动失败：源文件留在原地未动，错误写进 journal 的 `Result` 列

校验方法（任何时候都能跑）：

```powershell
# 库里还有没有重复
powershell -ExecutionPolicy Bypass -File C:\Photos工具\find-duplicates.ps1 -Roots C:\Photos,C:\Videos
# 某次运行的移动是否全部到位
Import-Csv 'C:\Photos\_归类清单\runs\<时间戳>\move-journal-*.csv' | Where-Object Result -ne 'ok'
```

## 8. 改脚本之后必做：自检

```powershell
powershell -ExecutionPolicy Bypass -File C:\Photos工具\check-tools.ps1
```

它会做两件事：把目录里每个 `.ps1` 重新编码成 **UTF-8 with BOM**（PowerShell 5.1 会把无 BOM 的当 GBK 读，中文会被解析坏，甚至整份脚本报语法错），再逐个解析报语法错误。

这一步不是可选项：某次改完 `photo-pipeline.ps1` 丢了 BOM，脚本直接跑不起来，而界面上只表现为"任务快速结束"。GUI 也自带了校验——`-SelfTest` 现在要求后台日志里出现 `PIPELINE-STATUS: OK` 且退出码为 0 才算通过。

## 9. 相机 / 手机（MTP 便携设备）

相机用 USB 直连时（例如 Nikon Z 5）会以 **MTP/PTP 便携设备**出现，路径长这样：

```
此电脑\Z 5\可移动存储 10001\DCIM\101NCZ_5
```

这不是文件系统路径（没有盘符），所以 `Test-Path` / `Get-ChildItem` 天生读不到——**不是工具坏了**。处理办法：

- **界面里**：点「找相机」按钮，自动扫描出设备和 DCIM 目录并填进输入框（相机要先开机、USB 模式选 MTP/PTP），然后正常点「开始处理」。
- **命令行里**：把设备路径直接当 `-Source` 传给流水线即可，它会先只读复制到暂存目录再归类：

```powershell
powershell -ExecutionPolicy Bypass -File photo-pipeline.ps1 -Source "此电脑\Z 5\可移动存储 10001\DCIM\101NCZ_5" -Execute
# 单独用（只复制，不归类）：
powershell -ExecutionPolicy Bypass -File import-from-camera.ps1 -Path "此电脑\Z 5\可移动存储 10001\DCIM\101NCZ_5"
powershell -ExecutionPolicy Bypass -File import-from-camera.ps1 -DeviceName "Z 5" -SubPath "DCIM\101NCZ_5" -List   # 只看清单
```

机制与安全：

- 用 `Shell.Application` 遍历设备命名空间，逐文件 `CopyHere` 复制（默认逐文件，进度可见、失败只影响单个文件；`-BulkFolderCopy` 可换成整目录复制）
- 复制到暂存目录 `C:\CameraImport\<时间戳>\`，每个文件校验"落地存在 + 大小与设备显示值相差不超过 2MB + 非 0 字节"，缺失的自动重试最多 4 轮
- 产出 `_camera-import-manifest.csv`（相对路径 / 设备显示大小 / 本地字节数 / 状态）
- **设备只读**：相机和存储卡上的文件不会被改动、更不会被删除
- 归类真实执行后，照片会从暂存**移入**库里，暂存只剩清单 CSV
- 设备上的 `FolderItem.Size` 恒为 0（MTP 不暴露精确字节数），所以校验用的是"数量 + 显示尺寸 + 非空"，不是精确 SHA256

**更快的办法**：把存储卡拿出来用读卡器，卡会直接变成盘符（如 `E:\DCIM\101NCZ_5`），走普通路径，速度也更快、还能用全量哈希校验。

## 10. 依赖与限制

- 只用 PowerShell（Windows PowerShell 5.1 即可）+ .NET，不装 exiftool / Python
- 反查城市走免 key 的 BigDataCloud（备选 Nominatim），需要联网；只对**新出现的坐标网格**发起请求
- 带中文的 `.ps1` 必须保存为 **UTF-8 with BOM**，否则 PowerShell 5.1 会按 GBK 解析
- 视频未解析容器内的 GPS（`©xyz`），城市按同时段照片继承
- 未覆盖 HEIC/HEIF 元数据（本库暂无样本）
- 大库首次全量查重较慢（本库 1.75 万文件约 45 分钟），之后靠缓存基本秒级
