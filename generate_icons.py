"""
生成插件图标（NFC 天线线圈示意图）。
依赖 Pillow（KiCad 自带 Python 已包含）：python generate_icons.py
"""

import os

from PIL import Image, ImageDraw


def draw_antenna(size: int, fg, bg, scale=1.0) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    m = size * 0.06
    d.rounded_rectangle([m, m, size - m, size - m], radius=size * 0.18, fill=bg)
    # 同心方螺旋（简化画法：由外向内画 4 段开口方框）
    pad = size * 0.24 * scale
    lw = max(1, int(round(size * 0.065)))
    x0, y0, x1, y1 = pad, pad, size - pad, size - pad
    n = 3
    for i in range(n):
        col = fg if i % 2 == 0 else fg
        d.line([(x0, y1), (x0, y0), (x1, y0), (x1, y1)], fill=col, width=lw, joint="curve")
        x0 += lw * 1.8
        y0 += lw * 1.8
        x1 -= lw * 1.8
        y1 -= lw * 1.8
    return img


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    plugins = os.path.join(here, "plugins")
    icons = os.path.join(plugins, "icons")
    resources = os.path.join(here, "resources")
    os.makedirs(icons, exist_ok=True)
    os.makedirs(resources, exist_ok=True)

    light_bg = (245, 246, 248, 255)
    dark_bg = (38, 42, 48, 255)
    copper = (200, 89, 43, 255)
    copper_d = (222, 122, 72, 255)

    for name, bg, fg, size in (
        ("icons/icon-light.png", light_bg, copper, 24),
        ("icons/icon-dark.png", dark_bg, copper_d, 24),
        ("icons/icon-64.png", light_bg, copper, 64),
        ("icon.png", light_bg, copper, 24),             # 工具栏按钮
    ):
        target = os.path.join(plugins, *name.split("/"))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        draw_antenna(size, fg, bg).save(target, "PNG")
        print("wrote", target)

    draw_antenna(64, copper, light_bg).save(
        os.path.join(resources, "icon.png"), "PNG")
    print("wrote", os.path.join(resources, "icon.png"))


if __name__ == "__main__":
    main()
