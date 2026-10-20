# 品牌字体（本地化）

本目录的 woff2 由 `venv/vendor_fonts.py` 从 Google Fonts 拉取，
**只取 latin 子集**；中文不下载 —— 本机已装 `Noto Serif SC` / `Noto Sans SC`，
系统字体栈直接命中。

为什么不用 CDN：PhotoOS 是本地优先应用，断点 #7 要验「离线仍可用」；
运行时向第三方发请求与这个定位相悖。

## 许可

| 字体 | 许可 |
| --- | --- |
| Playfair Display | SIL Open Font License 1.1 |
| Inter | SIL Open Font License 1.1 |
| JetBrains Mono | SIL Open Font License 1.1 |

三者均为开源可商用字体，允许随应用一起分发。

## 来源与校验（重新拉取后可复核）

| 文件 | 字重 | 字节 | sha256 | 来源 |
| --- | --- | ---: | --- | --- |
| `PlayfairDisplay-latin-italic.woff2` | [400] | 21,884 | `eabce94d4a69e439…` | `https://fonts.gstatic.com/s/playfairdisplay/v40/nuFRD-vYSZviVYUb_rj3ij…` |
| `PlayfairDisplay-latin.woff2` | [400, 500, 700, 900] | 38,404 | `e0c764a8e9e1cce9…` | `https://fonts.gstatic.com/s/playfairdisplay/v40/nuFiD-vYSZviVYUb_rj3ij…` |
| `Inter-latin.woff2` | [300, 400, 500, 600, 700] | 48,256 | `3100e775e8616cd2…` | `https://fonts.gstatic.com/s/inter/v20/UcC73FwrK3iLTeHuS_nVMrMxCp50SjIa…` |
| `JetBrainsMono-latin.woff2` | [400, 500, 700] | 31,432 | `83c005d49d8a6a50…` | `https://fonts.gstatic.com/s/jetbrainsmono/v24/tDbv2o-flEEny0FZhsfKu5WU…` |

