#!/usr/bin/env python3
"""
make_docs_images.py — схемы для документации.

Рисует то, что иначе приходится держать в голове: где у TikTok мёртвые зоны,
как устроена двойная раскладка, как выглядят стили субтитров. Схемы, а не
кадры из чужих роликов: спецификация нагляднее одного примера.

    python3 scripts/make_docs_images.py
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).parent.parent
FONTS = ROOT / "fonts"
OUT = ROOT / "docs"

W, H = 1080, 1920
SCALE = 0.42                      # схемы отдаём уменьшенными, читаемости хватает

INK = (24, 26, 32)
PAPER = (248, 249, 251)
LINE = (188, 194, 208)
DANGER = (255, 92, 92)
SAFE = (66, 184, 131)
ACCENT = (255, 217, 61)
BLUE = (74, 144, 255)


def font(name: str, size: int):
    p = FONTS / name
    try:
        f = ImageFont.truetype(str(p), size)
        if name.startswith("Montserrat"):
            try:
                f.set_variation_by_axes([900])
            except Exception:
                pass
        return f
    except Exception:
        return ImageFont.load_default()


def ui(size: int):
    for n in ("Montserrat.ttf", "Oswald.ttf"):
        if (FONTS / n).exists():
            return font(n, size)
    return ImageFont.load_default()


def label(d: ImageDraw.ImageDraw, xy, text, f, fill=INK, anchor="la"):
    d.text(xy, text, font=f, fill=fill, anchor=anchor)


def canvas_map() -> Image.Image:
    """Холст 1080x1920: мёртвые зоны интерфейса и позиции субтитров."""
    im = Image.new("RGB", (W, H), PAPER)
    d = ImageDraw.Draw(im, "RGBA")
    f_big, f_mid, f_sm = ui(54), ui(38), ui(30)

    # мёртвые зоны
    zones = [
        (0, 0, W, 250, "верх перекрыт вкладками — до 250"),
        (0, 1460, W, H, "низ перекрыт: ник, подпись, трек — от 1460"),
        (1000, 250, W, 1460, None),
    ]
    for x0, y0, x1, y1, cap in zones:
        d.rectangle([x0, y0, x1, y1], fill=DANGER + (46,))
        if cap:
            label(d, (28, y0 + 16 if y0 == 0 else y0 + 16), cap, f_sm, DANGER)
    # подпись к правой полосе ставим ВНУТРЬ кадра: за краем она обрезается
    for k, line in enumerate(("кнопки", "лайк", "коммент", "репост")):
        label(d, (988, 690 + k * 38), line, f_sm, DANGER, anchor="ra")
    label(d, (988, 1418), "правая полоса — от 1000", f_sm, DANGER, anchor="ra")

    # безопасная область
    d.rectangle([60, 250, 1000, 1460], outline=SAFE, width=5)
    label(d, (78, 268), "безопасная область 940 x 1210", f_sm, SAFE)

    # полоса видео при леттербоксе 100%
    d.rectangle([0, 656, W, 1264], fill=BLUE + (40,), outline=BLUE, width=4)
    label(d, (28, 672), "полоса видео при леттербоксе 100%", f_mid, BLUE)
    label(d, (28, 712), "656 – 1264", f_sm, BLUE)

    # позиции субтитров
    for y, name, mv in ((600, "над полосой", 1290),
                        (1360, "под полосой", 480),
                        (960, "внутри кадра", 900)):
        d.line([90, y, 990, y], fill=ACCENT, width=6)
        label(d, (100, y - 44), f"{name}   MarginV {mv}", f_mid, (150, 120, 0))

    label(d, (W // 2, 120), "1080 x 1920", f_big, INK, anchor="ma")
    label(d, (W // 2, 186), "мёртвые зоны TikTok и позиции субтитров",
          f_sm, (110, 116, 130), anchor="ma")
    return im


def streamer_layout() -> Image.Image:
    """Двойная раскладка: стык на 45% высоты."""
    im = Image.new("RGB", (W, H), PAPER)
    d = ImageDraw.Draw(im, "RGBA")
    f_big, f_mid, f_sm = ui(54), ui(40), ui(30)

    split = int(H * 0.45)
    d.rectangle([0, 0, W, split], fill=(96, 132, 200, 70), outline=BLUE, width=4)
    d.rectangle([0, split, W, H], fill=(96, 176, 132, 70), outline=SAFE, width=4)

    label(d, (W // 2, split // 2 - 70), "ВЕРХ", f_big, INK, anchor="ma")
    label(d, (W // 2, split // 2 - 6), "говорящий, вебка", f_mid, INK, anchor="ma")
    label(d, (W // 2, split // 2 + 46), "1080 x 864   даёт звук", f_sm,
          (70, 76, 92), anchor="ma")

    ly = split + (H - split) // 2
    label(d, (W // 2, ly - 70), "НИЗ", f_big, INK, anchor="ma")
    label(d, (W // 2, ly - 6), "посторонний исходник", f_mid, INK, anchor="ma")
    label(d, (W // 2, ly + 46), "1080 x 1056   звук не нужен", f_sm,
          (70, 76, 92), anchor="ma")
    label(d, (W // 2, ly + 96), "короче ролика — зациклится", f_sm,
          (70, 76, 92), anchor="ma")

    # стык и субтитры на нём
    d.line([0, split, W, split], fill=INK, width=6)
    label(d, (28, split + 18), "стык 45% высоты = y 864", f_sm, INK)
    d.line([90, split - 34, 990, split - 34], fill=ACCENT, width=6)
    label(d, (100, split - 84), "субтитры на стыке   MarginV 1014", f_mid,
          (150, 120, 0))

    label(d, (W // 2, 90), "раскладка «Стример»", f_big, INK, anchor="ma")
    return im


def caption_styles() -> Image.Image:
    """Четыре стиля субтитров на нейтральном фоне."""
    rows = [
        ("hormozi", "Montserrat.ttf", 76, (255, 255, 255), (255, 217, 61),
         None, 8, "белое + жёлтое активное слово"),
        ("три состояния", "Oswald.ttf", 72, (255, 255, 255), (245, 158, 11),
         (142, 142, 156), 8, "сказанное / текущее / будущее"),
        ("плашка", "Montserrat.ttf", 68, (255, 255, 255), (255, 255, 255),
         None, 4, "активное слово на подложке"),
        ("неон", "Oswald.ttf", 72, (255, 255, 255), (0, 255, 255),
         None, 3, "тонкая цветная обводка со свечением"),
    ]
    rh = 420
    im = Image.new("RGB", (W, rh * len(rows) + 120), (16, 18, 24))
    d = ImageDraw.Draw(im, "RGBA")
    f_ttl, f_note = ui(40), ui(28)
    label(d, (W // 2, 40), "стили субтитров", ui(54), (240, 242, 248), anchor="ma")

    words = ["ЦЕНА", "НАКЛЕЕК", "1760"]
    for i, (name, fname, size, base, active, upcoming, bord, note) in enumerate(rows):
        y0 = 120 + i * rh
        # фон-градиент: слева тёмный, справа светлый — видно, где стиль держится
        for x in range(0, W, 8):
            t = x / W
            g = int(20 + 200 * t)
            d.rectangle([x, y0, x + 8, y0 + rh - 30], fill=(g, g + 4, g + 10))

        label(d, (40, y0 + 24), name, f_ttl, (255, 255, 255))
        label(d, (40, y0 + 74), note, f_note, (190, 196, 210))

        f = font(fname, size)
        # активное — второе слово
        widths = [d.textlength(w, font=f) for w in words]
        space = d.textlength(" ", font=f) or 14
        total = sum(widths) + space * (len(words) - 1)
        x = (W - total) / 2
        ty = y0 + rh - 190
        for j, w in enumerate(words):
            col = active if j == 1 else (upcoming if (upcoming and j > 1) else base)
            if name == "плашка" and j == 1:
                d.rounded_rectangle([x - 14, ty - 10, x + widths[j] + 14, ty + size + 18],
                                    radius=14, fill=(255, 45, 85))
            if name == "неон":
                d.text((x + 3, ty + 3), w, font=f, fill=(0, 90, 90))
            d.text((x, ty), w, font=f, fill=col,
                   stroke_width=bord, stroke_fill=(0, 0, 0))
            x += widths[j] + space
    return im


def main() -> None:
    OUT.mkdir(exist_ok=True)
    for name, im in (("canvas.png", canvas_map()),
                     ("streamer_layout.png", streamer_layout()),
                     ("caption_styles.png", caption_styles())):
        w, h = im.size
        im.resize((int(w * SCALE), int(h * SCALE)), Image.LANCZOS).save(
            OUT / name, optimize=True)
        print(f"  {name}: {int(w * SCALE)}x{int(h * SCALE)}")


if __name__ == "__main__":
    main()
