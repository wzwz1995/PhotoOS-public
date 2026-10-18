"""统一媒体处理适配层（PRD §8 LOCKED）。

```
Asset → File Type Detection → Decoder Adapter → Preview / Thumbnail
```

纪律：
  * 业务层**不得**直接依赖 Windows 系统图片查看器或系统编解码器。
  * RAW 走 rawpy/LibRaw，HEIC 走内置 libheif，视频走随包 ffmpeg —— 换机后环境变化不影响。
  * 统一入口只有 `media.adapter.decode_capabilities / extract_metadata / generate_previews`。
  * 单文件失败抛 `MediaError` 子类，由 Job 层记录并重试，**不阻塞整批**（PRD §11.1）。
"""

from __future__ import annotations

from .adapter import (
    DecodeCapability,
    MediaInfo,
    Previews,
    decode_capabilities,
    extract_metadata,
    generate_previews,
    generate_previews_from_bytes,
    load_image,
)
from .detect import DetectedType, detect_type, detect_type_from_bytes
from .video import VideoInfo, extract_cover_frame, ffmpeg_path, probe_video

__all__ = [
    "DecodeCapability",
    "DetectedType",
    "MediaInfo",
    "Previews",
    "VideoInfo",
    "decode_capabilities",
    "detect_type",
    "detect_type_from_bytes",
    "extract_cover_frame",
    "extract_metadata",
    "ffmpeg_path",
    "generate_previews",
    "generate_previews_from_bytes",
    "load_image",
    "probe_video",
]
