#!/usr/bin/env python3
"""
make_docs_images.py — схемы для документации в формате SVG.

Рисует то, что иначе приходится держать в голове: где у TikTok мёртвые зоны,
как устроена двойная раскладка, как выглядят стили субтитров. Схемы, а не кадры
из чужих роликов: спецификация нагляднее одного примера.

Почему SVG, а не PNG. Схемы состоят из прямоугольников, линий и подписей —
для такого растр только вредит: он мылится на зуме и весит в разы больше.
Плюс SVG — текстовый файл, поэтому он уходит в git как код и не ломается при
передаче через API (PNG пришлось бы гнать base64, а на этом легко получить
двойную кодировку и битую картинку в README).

Одна тонкость. Внутри SVG шрифт рисует браузер читателя, а не мы, поэтому ширина
надписей у всех разная. Значит геометрию, зависящую от ширины текста, вычислять
нельзя вообще: слова верстает рендерер через tspan, а мы задаём только цвета.
Раньше здесь была подложка под активным словом, которой требовались точные
границы слова, — вместе со стилем «плашка» она убрана.

Проверять схемы cairosvg нельзя: он игнорирует text-anchor у tspan и показывает
слова наехавшими друг на друга там, где браузер верстает их правильно. Смотреть
надо в браузере — им же смотрит и читатель на гитхабе.

    python3 scripts/make_docs_images.py
"""
from __future__ import annotations

from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).parent.parent
OUT = ROOT / "docs"

W, H = 1080, 1920
SCALE = 0.42                      # во что схема отрисуется по умолчанию в README

INK = "#181a20"
PAPER = "#f8f9fb"
DANGER = "#ff5c5c"
SAFE = "#42b883"
ACCENT = "#ffd93d"
BLUE = "#4a90ff"
MUTED = "#6e7480"

# Шрифтовые стеки: сначала наши шрифты (если стоят в системе), потом надёжные
# запасные. sans-serif в конце гарантирует кириллицу где угодно.
UI = "Montserrat, 'Helvetica Neue', Helvetica, Arial, sans-serif"
COND = "Oswald, 'Arial Narrow', 'Helvetica Neue', Arial, sans-serif"

# Средняя ширина глифа в долях кегля — нужна только чтобы посчитать textLength.
# Дальше рендерер подгоняет текст под эту ширину, так что точность здесь не
# критична: важно лишь, чтобы пропорции слов были похожи на правду.
EM_UI = 0.62
EM_COND = 0.50


def txt(x: float, y: float, s: str, size: int, fill: str = INK,
        anchor: str = "start", family: str = UI, weight: int = 700,
        length: float | None = None, stroke: str | None = None,
        sw: float = 0.0) -> str:
    """
    Одна надпись. anchor: start | middle | end.

    Обводка (stroke) рисуется тем же элементом, а не второй копией текста снизу:
    paint-order="stroke" велит рендереру положить штрих ПОД заливку. Копия текста
    дала бы двойной набор глифов и рассинхрон при переносе шрифта.
    """
    a = f' text-anchor="{anchor}"' if anchor != "start" else ""
    tl = (f' textLength="{length:.0f}" lengthAdjust="spacingAndGlyphs"'
          if length else "")
    so = (f' stroke="{stroke}" stroke-width="{sw:.1f}" stroke-linejoin="round"'
          f' paint-order="stroke"' if stroke else "")
    return (f'<text x="{x:.0f}" y="{y:.0f}" font-family="{family}" '
            f'font-size="{size}" font-weight="{weight}" fill="{fill}"'
            f'{a}{tl}{so}>{escape(s)}</text>')


def rect(x: float, y: float, w: float, h: float, fill: str = "none",
         stroke: str | None = None, sw: int = 0, op: float = 1.0,
         r: int = 0) -> str:
    st = f' stroke="{stroke}" stroke-width="{sw}"' if stroke else ""
    rr = f' rx="{r}"' if r else ""
    return (f'<rect x="{x:.0f}" y="{y:.0f}" width="{w:.0f}" height="{h:.0f}" '
            f'fill="{fill}" fill-opacity="{op}"{st}{rr}/>')


def line(x1: float, y1: float, x2: float, y2: float,
         stroke: str, sw: int = 6) -> str:
    return (f'<line x1="{x1:.0f}" y1="{y1:.0f}" x2="{x2:.0f}" y2="{y2:.0f}" '
            f'stroke="{stroke}" stroke-width="{sw}"/>')


def svg(width: int, height: int, body: list[str], bg: str) -> str:
    vw, vh = int(width * SCALE), int(height * SCALE)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'viewBox="0 0 {width} {height}" width="{vw}" height="{vh}" '
            f'role="img">\n'
            f'{rect(0, 0, width, height, bg)}\n' +
            "\n".join(body) + "\n</svg>\n")


def canvas_map() -> str:
    """Холст 1080x1920: мёртвые зоны интерфейса и позиции субтитров."""
    b: list[str] = []

    # мёртвые зоны интерфейса
    b.append(rect(0, 0, W, 250, DANGER, op=0.18))
    b.append(txt(28, 52, "верх перекрыт вкладками — до 250", 30, DANGER))
    b.append(rect(0, 1460, W, H - 1460, DANGER, op=0.18))
    b.append(txt(28, 1496, "низ перекрыт: ник, подпись, трек — от 1460", 30, DANGER))
    b.append(rect(1000, 250, W - 1000, 1210, DANGER, op=0.18))

    # подписи правой полосы ставим ВНУТРЬ кадра: за краем они обрежутся
    for k, s in enumerate(("кнопки", "лайк", "коммент", "репост")):
        b.append(txt(988, 716 + k * 38, s, 30, DANGER, anchor="end"))
    b.append(txt(988, 1444, "правая полоса — от 1000", 30, DANGER, anchor="end"))

    # безопасная область
    b.append(rect(60, 250, 940, 1210, "none", SAFE, 5))
    b.append(txt(78, 300, "безопасная область 940 x 1210", 30, SAFE))

    # полоса видео при леттербоксе 100%
    b.append(rect(0, 656, W, 608, BLUE, BLUE, 4, op=0.16))
    b.append(txt(28, 706, "полоса видео при леттербоксе 100%", 38, BLUE))
    b.append(txt(28, 746, "656 – 1264", 30, BLUE))

    # позиции субтитров
    for y, name, mv in ((600, "над полосой", 1290),
                        (1360, "под полосой", 480),
                        (960, "внутри кадра", 900)):
        b.append(line(90, y, 990, y, ACCENT))
        b.append(txt(100, y - 20, f"{name}   MarginV {mv}", 38, "#8a6f00"))

    b.append(txt(W / 2, 140, "1080 x 1920", 54, INK, anchor="middle", weight=800))
    b.append(txt(W / 2, 200, "мёртвые зоны TikTok и позиции субтитров",
                 30, MUTED, anchor="middle"))
    return svg(W, H, b, PAPER)


def streamer_layout() -> str:
    """Двойная раскладка: стык на 45% высоты."""
    b: list[str] = []
    split = int(H * 0.45)

    b.append(rect(0, 0, W, split, "#6084c8", BLUE, 4, op=0.28))
    b.append(rect(0, split, W, H - split, "#60b084", SAFE, 4, op=0.28))

    mid = split // 2
    b.append(txt(W / 2, mid - 40, "ВЕРХ", 54, INK, anchor="middle", weight=800))
    b.append(txt(W / 2, mid + 26, "говорящий, вебка", 40, INK, anchor="middle"))
    b.append(txt(W / 2, mid + 80, "1080 x 864   даёт звук", 30, "#464c5c",
                 anchor="middle"))

    ly = split + (H - split) // 2
    b.append(txt(W / 2, ly - 40, "НИЗ", 54, INK, anchor="middle", weight=800))
    b.append(txt(W / 2, ly + 26, "посторонний исходник", 40, INK, anchor="middle"))
    b.append(txt(W / 2, ly + 80, "1080 x 1056   звук не нужен", 30, "#464c5c",
                 anchor="middle"))
    b.append(txt(W / 2, ly + 130, "короче ролика — зациклится", 30, "#464c5c",
                 anchor="middle"))

    # стык и субтитры на нём
    b.append(line(0, split, W, split, INK))
    b.append(txt(28, split + 50, "стык 45% высоты = y 864", 30, INK))
    b.append(line(90, split - 34, 990, split - 34, ACCENT))
    b.append(txt(100, split - 54, "субтитры на стыке   MarginV 1014", 38, "#8a6f00"))

    b.append(txt(W / 2, 110, "раскладка «Стример»", 54, INK,
                 anchor="middle", weight=800))
    return svg(W, H, b, PAPER)


def caption_styles() -> str:
    """Два стиля субтитров: как каждый выглядит в кадре."""
    rows = [
        ("hormozi", UI, EM_UI, 76, "#ffffff", "#ffd93d", None,
         "белое + жёлтое активное слово"),
        ("три состояния", COND, EM_COND, 72, "#ffffff", "#f59e0b", "#8e8e9c",
         "сказанное / текущее / будущее"),
    ]
    words = ["ЦЕНА ", "НАКЛЕЕК ", "1760"]
    rh, top = 420, 120
    total_h = top + rh * len(rows)

    b: list[str] = []
    # мягкий градиент фона строки: слева темно, справа светло — видно,
    # держится ли стиль на любом фоне
    b.append('<defs><linearGradient id="g" x1="0" x2="1" y1="0" y2="0">'
             '<stop offset="0" stop-color="#141620"/>'
             '<stop offset="1" stop-color="#dcdee6"/></linearGradient>'
             '</defs>')
    b.append(txt(W / 2, 62, "стили субтитров", 54, "#f0f2f8",
                 anchor="middle", weight=800))

    for i, (name, fam, em, size, base, active, upcoming, note) in enumerate(rows):
        y0 = top + i * rh
        b.append(rect(0, y0, W, rh - 30, "url(#g)"))
        b.append(txt(40, y0 + 60, name, 40, "#ffffff"))
        b.append(txt(40, y0 + 104, note, 28, "#bec4d2"))
        b.append(txt(40, y0 + 142, "одна строка, низ на y=1420 — позиция не меняется",
                     24, "#8b93a6"))

        # Слова верстает сам рендерер: одна надпись по центру, слова внутри —
        # tspan со своим цветом. Мы не считаем ширины, поэтому строка не
        # разъезжается ни на каком наборе шрифтов.
        ty = y0 + rh - 130
        bord = 8          # обводка одинаковая: она и держит читаемость

        spans = []
        for j, w in enumerate(words):
            col = active if j == 1 else (upcoming if (upcoming and j > 1) else base)
            spans.append(f'<tspan fill="{col}">{escape(w)}</tspan>')

        el = (f'<text x="{W / 2:.0f}" y="{ty:.0f}" xml:space="preserve" '
              f'text-anchor="middle" font-family="{fam}" font-size="{size}" '
              f'font-weight="800" fill="{base}" stroke="#000000" '
              f'stroke-width="{bord}" stroke-linejoin="round" '
              f'paint-order="stroke">' + "".join(spans) + '</text>')
        b.append(el)

    return svg(W, total_h, b, "#101218")


def main() -> None:
    OUT.mkdir(exist_ok=True)
    for name, body in (("canvas.svg", canvas_map()),
                       ("streamer_layout.svg", streamer_layout()),
                       ("caption_styles.svg", caption_styles())):
        p = OUT / name
        p.write_text(body, encoding="utf-8")
        print(f"  {name}: {len(body.encode('utf-8')) / 1024:.1f} КБ")


if __name__ == "__main__":
    main()
