#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 FloatTrans 的图标 app.ico（需要 Pillow，仅打包时用一次）。"""

import os

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
SIZE = 512
OUT = os.path.join(HERE, "app.ico")

C1 = (109, 168, 255)   # 亮蓝
C2 = (54, 96, 200)     # 深蓝


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def make(size):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))

    # 渐变背景
    grad = Image.new("RGBA", (size, size))
    gd = ImageDraw.Draw(grad)
    for y in range(size):
        gd.line([(0, y), (size, y)], fill=lerp(C1, C2, y / (size - 1)) + (255,))

    # 圆角遮罩
    mask = Image.new("L", (size, size), 0)
    md = ImageDraw.Draw(mask)
    r = int(size * 0.24)
    md.rounded_rectangle([0, 0, size - 1, size - 1], radius=r, fill=255)

    img.paste(grad, (0, 0), mask)

    # 顶部高光
    hi = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    hd = ImageDraw.Draw(hi)
    hd.rounded_rectangle([int(size * 0.04), int(size * 0.04),
                          size - int(size * 0.04), int(size * 0.5)],
                         radius=int(size * 0.2), fill=(255, 255, 255, 26))
    img = Image.alpha_composite(img, hi)

    # “译”字
    d = ImageDraw.Draw(img)
    font = None
    for path in (r"C:\Windows\Fonts\msyhbd.ttc",
                 r"C:\Windows\Fonts\msyh.ttc",
                 r"C:\Windows\Fonts\simhei.ttf"):
        if os.path.exists(path):
            try:
                font = ImageFont.truetype(path, int(size * 0.58))
                break
            except Exception:
                pass
    if font is None:
        font = ImageFont.load_default()

    text = "译"
    box = d.textbbox((0, 0), text, font=font)
    w, h = box[2] - box[0], box[3] - box[1]
    d.text(((size - w) / 2 - box[0], (size - h) / 2 - box[1] - size * 0.01),
           text, font=font, fill=(255, 255, 255, 255))
    return img


def main():
    base = make(SIZE)
    sizes = [256, 128, 64, 48, 32, 24, 16]
    frames = [base.resize((s, s), Image.LANCZOS) for s in sizes]
    frames[0].save(OUT, format="ICO",
                   sizes=[(s, s) for s in sizes], append_images=frames[1:])
    base.resize((256, 256), Image.LANCZOS).save(os.path.join(HERE, "icon_preview.png"))
    print("written:", OUT)


if __name__ == "__main__":
    main()
