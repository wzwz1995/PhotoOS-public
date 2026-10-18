"""生成桌面壳的图标（不引外网字体 / 不引图标库）。

一个光圈：金色叶片 + 皇家蓝的中孔 —— 与 PhotoOS 的三色基因一致
（金 = 你的选择，蓝 = 你的操作），缩到 16px 仍能认出是"镜头"。

用法：python tools/make_icons.py
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent.parent
OUT = HERE / "assets"

NAVY = (10, 36, 99, 255)        # 皇家蓝 #0A2463
GOLD = (244, 211, 94, 255)      # 复古金 #F4D35E
STAGE = (7, 9, 15, 255)         # --ink-stage #07090f
PAPER = (244, 241, 234, 255)


def rounded(size: int, radius: int, fill) -> Image.Image:
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=fill)
    return image


def aperture(size: int, blades: int = 6) -> Image.Image:
    """六叶片光圈：外环 + 叶片之间的缺口 + 中孔。"""
    scale = 8  # 超采样后缩小，边缘不毛
    big = size * scale
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    cx = cy = big / 2
    outer = big * 0.40
    inner = big * 0.135
    ring = big * 0.055

    # 外环
    draw.ellipse((cx - outer, cy - outer, cx + outer, cy + outer), outline=GOLD, width=int(ring))

    # 叶片：从外环内缘切向中孔的三角形，留出六道缝
    for index in range(blades):
        start = (360 / blades) * index + 8
        end = start + (360 / blades) - 16
        blade = [
            (cx + outer * 0.94 * math.cos(math.radians(start)),
             cy + outer * 0.94 * math.sin(math.radians(start))),
            (cx + outer * 0.94 * math.cos(math.radians(end)),
             cy + outer * 0.94 * math.sin(math.radians(end))),
            (cx + inner * 1.55 * math.cos(math.radians((start + end) / 2 + 38)),
             cy + inner * 1.55 * math.sin(math.radians((start + end) / 2 + 38))),
        ]
        draw.polygon(blade, fill=GOLD)

    # 中孔（皇家蓝）
    draw.ellipse((cx - inner, cy - inner, cx + inner, cy + inner), fill=NAVY)

    return image.resize((size, size), Image.LANCZOS)


def compose(size: int, radius_ratio: float = 0.22) -> Image.Image:
    canvas = rounded(size, int(size * radius_ratio), STAGE)
    # 一层极淡的皇家蓝径向感：用同心圆近似（不值得为图标引 numpy）
    glow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(glow)
    steps = 14
    for step in range(steps, 0, -1):
        alpha = int(10 * (1 - step / steps))
        if alpha <= 0:
            continue
        pad = size * 0.5 * step / steps
        gdraw.ellipse((pad * 0.35, pad * 0.35, size - pad * 0.35, size - pad * 0.35),
                      fill=(*NAVY[:3], alpha))
    canvas = Image.alpha_composite(canvas, glow)
    canvas = Image.alpha_composite(canvas, aperture(size))
    return canvas


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    compose(256).save(OUT / "icon.png")
    compose(64).save(OUT / "icon-64.png")
    compose(32, radius_ratio=0.28).save(OUT / "tray.png")
    # Windows 任务栏/资源管理器喜欢 .ico（多尺寸）
    compose(256).save(OUT / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("wrote:", ", ".join(p.name for p in sorted(OUT.iterdir()) if p.is_file()))


if __name__ == "__main__":
    main()
