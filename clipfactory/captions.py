#!/usr/bin/env python3
"""
captions.py — караоке-субтитры в стиле TikTok через libass.

Точных таймингов по словам транскрипция не даёт, а сегменты приходят с точностью
до секунды. Поэтому слова привязываются к РЕАЛЬНОЙ речи: по звуку считается
энергия в окнах 20 мс, находятся непрерывные речевые отрезки, и слова
раскладываются внутри них пропорционально длине. Так подсветка попадает в голос,
а не плывёт от накопленной ошибки.

Два стиля, оба с подсветкой звучащего слова: Hormozi (белый текст, активное слово
жёлтым) и три состояния (сказанное белое, текущее янтарное, будущее серое).
Раньше было четыре — «плашка» и «неон» убраны, причины в разделе ниже.

Позиция субтитров фиксирована: один показ = одна строка, поэтому строка стоит на
одном и том же y от начала до конца клипа. Проверяется отдельным модулем
check_captions.py по готовому mp4, а не на глаз.

Использование:
    python3 captions.py --audio clip.mp3 --segments segs.json --style hormozi --out cap.ass
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

import numpy as np

OUT_W, OUT_H = 1080, 1920

# ASS задаёт цвет как &HAABBGGRR& — порядок байт обратный привычному RGB
def rgb_to_ass(hex_rgb: str, alpha: str = "00") -> str:
    h = hex_rgb.lstrip("#")
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{alpha}{b}{g}{r}&".upper()


# Безопасные зоны TikTok — те же числа, что в variants.py. Держим их и здесь,
# потому что прежний дефолт margin_v 380 сажал низ текста на y=1540, то есть ПОД
# ник, подпись и прогресс. Проверять это должен код, а не память оператора.
SAFE_BOTTOM = 1460         # ниже — интерфейс TikTok
VIDEO_BAND_BOTTOM = 1264   # ниже — размытая подложка, картинки там уже нет

# Показ всегда в ОДНУ строку. Это не косметика, а способ прибить позицию гвоздями.
# При двух строках блок прижат к низу и растёт ВВЕРХ, поэтому двухстрочный показ
# уезжает на высоту строки. На реальном клипе замерено: два положения, 1170 и
# 1265, разница 95 px, 56 переключений за 66 секунд — это и есть «субтитры
# плавают туда-сюда». Одна строка делает позицию единственной по построению:
# не сглаживает дрожание, а исключает его.
MAX_LINES = 1

STYLES = {
    "hormozi": {
        "font": "Montserrat", "size": 100, "bold": -1,
        "outline": 10, "shadow": 0, "margin_v": 500,
        "base": "#FFFFFF", "active": "#FFD93D", "upcoming": None,
        "words_per_chunk": 3, "upper": True,
        "note": "Белый текст, активное слово жёлтым. Рабочая лошадка.",
        "looks": "Плотный жирный шрифт, всё капсом, белое с толстой чёрной "
                 "обводкой. Слово, которое звучит сейчас, вспыхивает жёлтым. "
                 "Обводка держит текст читаемым на любом фоне.",
        "fits": "Разборы, топы, обзоры, всё где важен смысл речи. Самый "
                "узнаваемый стиль в лентах, поэтому и самый безопасный выбор.",
    },
    "threestate": {
        "font": "Oswald", "size": 94, "bold": 0,
        "outline": 10, "shadow": 0, "margin_v": 500,
        "base": "#FFFFFF", "active": "#F59E0B", "upcoming": "#8E8E9C",
        "words_per_chunk": 3, "upper": True,
        "note": "Три состояния: сказанное, текущее и будущее разными цветами.",
        "looks": "Уже произнесённые слова белые, текущее янтарное, ещё не "
                 "сказанные приглушённо-серые. Видно, сколько осталось до "
                 "конца фразы.",
        "fits": "Длинные фразы и плотную речь: серый цвет будущих слов не "
                "даёт дочитать вперёд голоса и подсказывает ритм.",
    },
}

# Убранные стили и причины. Оставлено в коде намеренно: чтобы через месяц никто
# не «вернул полезную фичу», не зная, чем она кончилась.
#
# «плашка» (активное слово на розовой подложке). Подложке нужно знать точные
# пиксельные границы слова, а их приходилось считать самим — libass рисует текст
# своими метриками, и они не совпадают с измеренными. Отсюда тянулась череда
# дефектов: подложка уезжала при переносе строки, систематический сдвиг влево
# из-за ведущего пробела в словах, торчащий из-под подложки край буквы. Вдобавок
# подложка достаётся и служебным словам: «НЕ», «ИЗ», «И» получают широкий розовый
# квадрат с непропорциональными полями и читаются как сбой рендера. Стиль требует
# пиксельной точности там, где её негде взять.
#
# «неон» (тонкая голубая обводка со свечением). Обводка 3 px против 10 у рабочих
# стилей не держит контраст на светлом кадре — текст исчезает. Стиль, который
# нельзя применять к половине материала, не годится в набор по умолчанию.
#
# Оба вернуть можно из истории git, вместе с этим объяснением.
REMOVED_STYLES = {
    "pill": "нужна пиксельная точность границ слова, которой негде взять",
    "neon": "обводка 3 px не держит контраст на светлом кадре",
}


def speech_runs(audio: str, hop_ms: int = 20,
                merge_gap_ms: int = 160, min_run_ms: int = 120) -> list[tuple[float, float]]:
    """Непрерывные отрезки речи по энергии сигнала."""
    cp = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", audio, "-ac", "1", "-ar", "16000",
         "-f", "s16le", "-"], capture_output=True, stdin=subprocess.DEVNULL)
    if cp.returncode != 0:
        raise SystemExit(f"не смог прочитать звук:\n{cp.stderr.decode()[-600:]}")
    x = np.frombuffer(cp.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    sr = 16000
    hop = int(sr * hop_ms / 1000)
    n = len(x) // hop
    if n == 0:
        return []
    rms = np.sqrt((x[: n * hop].reshape(n, hop) ** 2).mean(axis=1) + 1e-12)
    db = 20 * np.log10(rms + 1e-12)

    # порог: между полом шума и типичным уровнем речи
    floor = np.percentile(db, 15)
    speech = np.percentile(db, 75)
    thr = floor + 0.42 * (speech - floor)
    mask = db > thr

    runs: list[tuple[float, float]] = []
    i = 0
    while i < n:
        if mask[i]:
            j = i
            while j < n and mask[j]:
                j += 1
            runs.append((i * hop_ms / 1000, j * hop_ms / 1000))
            i = j
        else:
            i += 1

    # склеиваем близкие, выкидываем слишком короткие
    merged: list[list[float]] = []
    for s, e in runs:
        if merged and s - merged[-1][1] <= merge_gap_ms / 1000:
            merged[-1][1] = e
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged if (e - s) * 1000 >= min_run_ms]


def parse_ts(ts: str) -> float:
    parts = [float(p) for p in ts.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0.0)
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def words_with_times(segments: list[dict], runs: list[tuple[float, float]],
                     clip_start: float, clip_dur: float) -> list[dict]:
    """
    Слова с таймингами. Сегмент даёт текст и грубое начало; речевые отрезки
    дают точные границы. Внутри отрезка слова делятся по длине.
    """
    # сегменты, попадающие в окно клипа, в координатах клипа
    segs = []
    for s in segments:
        t = parse_ts(s["timestamp"]) - clip_start
        if -2.0 <= t <= clip_dur:
            segs.append({"t": max(0.0, t), "text": s["text"]})
    if not segs:
        return []
    segs.sort(key=lambda s: s["t"])
    for i, s in enumerate(segs):
        s["end"] = segs[i + 1]["t"] if i + 1 < len(segs) else clip_dur

    out: list[dict] = []
    for seg in segs:
        toks = [w for w in re.findall(r"[^\s]+", seg["text"]) if w.strip()]
        if not toks:
            continue
        # речевые отрезки внутри сегмента
        inner = [(max(s, seg["t"]), min(e, seg["end"]))
                 for s, e in runs if e > seg["t"] and s < seg["end"]]
        inner = [(s, e) for s, e in inner if e > s]
        if not inner:
            inner = [(seg["t"], seg["end"])]

        total_speech = sum(e - s for s, e in inner)
        total_chars = sum(len(w) for w in toks) or 1
        ti = 0
        cur_s, cur_e = inner[0]
        cursor = cur_s
        for w in toks:
            share = (len(w) / total_chars) * total_speech
            # если слово не влезает в текущий отрезок — переходим к следующему
            while cursor + share > cur_e + 1e-6 and ti + 1 < len(inner):
                ti += 1
                cur_s, cur_e = inner[ti]
                cursor = cur_s
            w_end = min(cursor + share, cur_e)
            if w_end <= cursor:
                w_end = cursor + 0.08
            out.append({"word": w, "start": round(cursor, 3), "end": round(w_end, 3)})
            cursor = w_end
    return out


FONT_FILES = {
    "Montserrat": "Montserrat.ttf",
    # Anton заменён на Oswald: у Anton НЕТ кириллицы вообще (0 из 30 заглавных),
    # и стили на нём рисовали пустые прямоугольники вместо русских букв.
    # Oswald такой же узкий (208 px против 210 у Anton на «НАКЛЕЕК» при 60px),
    # но с полной кириллицей.
    "Oswald": "Oswald.ttf",
    "Anton": "Oswald.ttf",
}
SAFE_TEXT_W = OUT_W - 2 * 70          # поля по 70 px с каждой стороны
FONTS_DIR = Path(__file__).parent.parent / "fonts"


def _font(style: dict):
    """Шрифт для измерения ширины. Без измерения строки вылезают за кадр."""
    from PIL import ImageFont
    fp = FONTS_DIR / FONT_FILES.get(style["font"], "Montserrat.ttf")
    try:
        f = ImageFont.truetype(str(fp), style["size"])
        # у переменного Montserrat выставляем тяжёлое начертание
        if style["font"] == "Montserrat":
            try:
                f.set_variation_by_axes([900])
            except Exception:
                pass
        return f
    except Exception:
        return None


def cyrillic_ok(font) -> bool:
    """
    Есть ли в шрифте кириллица. Проверяется отрисовкой, а не метрикой: ширину
    PIL возвращает и для отсутствующих глифов, поэтому замер ширины ничего не
    доказывает — на этом и проехал Anton, рисовавший пустые прямоугольники.
    """
    if font is None:
        return True
    try:
        from PIL import Image, ImageDraw
        import numpy as np

        def render(ch: str):
            im = Image.new("L", (100, 100), 0)
            ImageDraw.Draw(im).text((5, 5), ch, font=font, fill=255)
            return np.asarray(im)

        tofu = render("\uffff")
        return not any(np.array_equal(render(c), tofu) for c in "АБВГЯЮЭ")
    except Exception:
        return True


def text_width(font, s: str) -> float:
    if font is None:
        return len(s) * 0.55 * 100      # грубая оценка, если шрифт не открылся
    box = font.getbbox(s)
    return box[2] - box[0]


def fit_chunks(words: list[dict], style: dict) -> list[list[dict]]:
    """
    Группы слов, гарантированно влезающие в безопасную ширину.

    Русские слова длиннее английских, поэтому жёсткое «3 слова в группе» рвёт
    кадр. Здесь размер группы определяется измеренной шириной строки: набираем
    слова, пока строка влезает, но не больше words_per_chunk.
    """
    font = _font(style)
    limit = style.get("words_per_chunk", 3)
    out: list[list[dict]] = []
    cur: list[dict] = []
    for w in words:
        trial = cur + [w]
        line = " ".join((x["word"].upper() if style["upper"] else x["word"])
                        for x in trial)
        if cur and (text_width(font, line) > SAFE_TEXT_W or len(trial) > limit):
            out.append(cur)
            cur = [w]
        else:
            cur = trial
    if cur:
        out.append(cur)
    return out


def fmt_time(t: float) -> str:
    t = max(0.0, t)
    h = int(t // 3600); m = int(t % 3600 // 60); s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def apply_fixes(words: list[dict]) -> tuple[list[dict], list[tuple[str, str]]]:
    """
    Пост-правка устойчивых ослышек по словарю ниши.

    Подсказки распознавателю (hotwords) убирают большинство ошибок, но часть
    терминов всё равно проскакивает. Здесь заменяется только то, что попало в
    словарь — регистр окончания слова сохраняется, знаки препинания не трогаем.
    """
    try:
        from glossary import FIXES
    except Exception:
        return words, []
    changed: list[tuple[str, str]] = []
    out = []
    for w in words:
        raw = w["word"]
        core = raw.strip(".,!?;:—–\"'()")
        tail = raw[len(core):] if raw.startswith(core) else ""
        key = core.lower()
        if key in FIXES:
            new = FIXES[key]
            if core != new:
                changed.append((core, new))
            w = dict(w, word=new + tail)
        out.append(w)
    return out, changed


def band_of(style: dict) -> tuple[int, int]:
    """
    Где окажется строка при заданном margin_v: (верх, низ) в пикселях кадра.

    Выравнивание 2 в ASS означает «прижать к низу», а margin_v — расстояние от
    низа КАДРА до низа строки. Высота строки берётся как 1.2 кегля (обычная
    метрика для этих шрифтов) плюс обводка с обеих сторон.
    """
    bottom = OUT_H - style["margin_v"] + int(style["outline"])
    top = OUT_H - style["margin_v"] - int(style["size"] * 1.2) - int(style["outline"])
    return top, bottom


def build_ass(words: list[dict], style_name: str,
              margin_v: int | None = None,
              font_size: int | None = None,
              outline: float | None = None,
              shadow: float | None = None,
              max_words: int | None = None) -> str:
    st = dict(STYLES[style_name])
    if margin_v is not None:
        st["margin_v"] = margin_v
    if font_size is not None:
        st["size"] = font_size
    if outline is not None:
        st["outline"] = outline
    if shadow is not None:
        st["shadow"] = shadow
    if max_words is not None:
        st["words_per_chunk"] = max_words

    # Предупреждаем СРАЗУ, до рендера: заезд под интерфейс TikTok иначе
    # обнаруживается только на залитом ролике. Именно так и прожил дефолт 380,
    # сажавший низ текста на y=1540 при границе интерфейса 1460.
    top, bottom = band_of(st)
    if bottom > SAFE_BOTTOM:
        print(f"ВНИМАНИЕ: при margin_v={st['margin_v']} низ текста уходит на "
              f"y={bottom}, а интерфейс TikTok начинается с {SAFE_BOTTOM}. "
              f"Ник, подпись и прогресс перекроют субтитры. "
              f"Нужен margin_v не меньше {OUT_H - SAFE_BOTTOM + int(st['outline'])}.",
              file=sys.stderr)
    base = rgb_to_ass(st["base"])
    active = rgb_to_ass(st["active"])
    upcoming = rgb_to_ass(st["upcoming"]) if st.get("upcoming") else base
    outline_col = rgb_to_ass(st.get("outline_colour", "#000000"))

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {OUT_W}
PlayResY: {OUT_H}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{st['font']},{st['size']},{base},{active},{outline_col},&H64000000&,{st['bold']},0,0,0,100,100,0,0,1,{st['outline']},{st['shadow']},2,70,70,{st['margin_v']},1
"""
    events = ["\n[Events]",
              "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]

    font = _font(st)
    if not cyrillic_ok(font):
        print(f"ВНИМАНИЕ: в шрифте {st['font']} нет кириллицы — "
              f"русский текст выйдет пустыми прямоугольниками.",
              file=sys.stderr)
    # Смысловая разбивка: по паузам, точкам и служебным словам. Дальше показ
    # приводится к ОДНОЙ строке: что не влезло, делится на два показа, а не
    # переносится вниз. Перенос — это и есть причина плавания позиции.
    import chunking as CH
    groups = CH.chunk_words(words, max_words=st["words_per_chunk"],
                            width_fn=lambda t: text_width(font, t), max_chars=60)
    shows = CH.fit_to_width(groups, lambda t: text_width(font, t),
                            SAFE_TEXT_W, max_lines=MAX_LINES, upper=st["upper"])
    chunks = [s["words"] for s in shows]

    for ch in chunks:
        # одиночное слово шире кадра разбить нельзя — сжимаем строку по ширине.
        # \fscy не трогаем: он менял бы высоту строки, а с ней и позицию.
        text = " ".join((w["word"].upper() if st["upper"] else w["word"])
                        for w in ch)
        width = text_width(font, text)
        fit = ""
        if width > SAFE_TEXT_W:
            sc = max(55, int(100 * SAFE_TEXT_W / width))
            fit = f"\\fscx{sc}"

        for k, w in enumerate(ch):
            seg = []
            for idx, ww in enumerate(ch):
                txt = ww["word"].upper() if st["upper"] else ww["word"]
                if idx < k:
                    seg.append(f"{{\\c{base}}}{txt}")
                elif idx == k:
                    seg.append(f"{{\\c{active}}}{txt}")
                else:
                    seg.append(f"{{\\c{upcoming}}}{txt}")
            line = (f"{{{fit}}}" if fit else "") + " ".join(seg)
            s, e = fmt_time(w["start"]), fmt_time(w["end"])
            events.append(f"Dialogue: 0,{s},{e},Cap,,0,0,0,,{line}")

    return header + "\n".join(events) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description="Караоке-субтитры для вертикальных нарезок")
    ap.add_argument("--words", default=None,
                    help="JSON с реальными таймингами слов (faster-whisper). "
                         "Если задан, эвристика не используется вообще.")
    ap.add_argument("--audio", default=None, help="звук клипа (для привязки к речи)")
    ap.add_argument("--segments", default=None, help="JSON транскрипта")
    ap.add_argument("--clip-start", type=float, default=0.0,
                    help="начало клипа в координатах исходника")
    ap.add_argument("--clip-dur", type=float, default=None)
    ap.add_argument("--style", choices=list(STYLES), default="hormozi")
    ap.add_argument("--margin-v", type=int, default=None,
                    help="отступ снизу до низа текста, px (перебивает стиль)")
    ap.add_argument("--font-size", type=int, default=None,
                    help="размер шрифта, px (перебивает стиль)")
    ap.add_argument("--outline", type=float, default=None,
                    help="толщина обводки, px; 0 — без обводки")
    ap.add_argument("--shadow", type=float, default=None,
                    help="смещение тени, px")
    ap.add_argument("--max-words", type=int, default=None,
                    help="максимум слов на показ (2-3 держат синхрон с голосом)")
    ap.add_argument("--no-fixes", action="store_true",
                    help="не применять словарь ослышек")
    ap.add_argument("--out", default=None)
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--styles", action="store_true",
                    help="показать, за что отвечает каждый стиль, и выйти")
    args = ap.parse_args()

    if args.styles:
        for name, st in STYLES.items():
            print(f"\n{name}  —  {st['note']}")
            print(f"  вид:      {st['looks']}")
            print(f"  подходит: {st['fits']}")
            print(f"  шрифт {st['font']} {st['size']} px, обводка "
                  f"{st['outline']} px, слов на показ {st['words_per_chunk']}")
            print(f"  цвета: базовый {st['base']}, активный {st['active']}"
                  + (f", будущий {st['upcoming']}" if st.get('upcoming') else ""))
            print(f"  позиция:  низ строки y={OUT_H - st['margin_v']}, "
                  f"одна строка, не смещается за клип")
        if REMOVED_STYLES:
            print("\nубраны:")
            for name, why in REMOVED_STYLES.items():
                print(f"  {name} — {why}")
        return

    # --styles показывает справку и выходит, поэтому обязательность проверяем
    # здесь, а не в argparse: иначе за справкой пришлось бы передавать рендерные
    # параметры, которых для неё нет.
    missing = [n for n, v in (("--clip-dur", args.clip_dur), ("--out", args.out))
               if v is None]
    if missing:
        raise SystemExit(f"Не хватает аргументов: {', '.join(missing)}")

    runs: list[tuple[float, float]] = []
    if args.words:
        # реальные тайминги из распознавания — точность порядка 50 мс,
        # эвристика по длине слова здесь не нужна и только портит
        wd = json.load(open(args.words, encoding="utf-8"))
        words = [w for w in wd["words"] if w["start"] < args.clip_dur]
        source = "распознавание (реальные тайминги)"
    else:
        if not (args.audio and args.segments):
            raise SystemExit("Нужен либо --words, либо пара --audio и --segments.")
        d = json.load(open(args.segments, encoding="utf-8"))
        segments = d.get("segments") or []
        runs = speech_runs(args.audio)
        words = words_with_times(segments, runs, args.clip_start, args.clip_dur)
        source = "эвристика по длине слова (приблизительно)"
    if not words:
        raise SystemExit("В окне клипа не нашлось слов — проверь clip-start и clip-dur.")

    fixed: list[tuple[str, str]] = []
    if not args.no_fixes:
        words, fixed = apply_fixes(words)

    Path(args.out).write_text(
        build_ass(words, args.style, args.margin_v, args.font_size,
                  args.outline, args.shadow, args.max_words),
        encoding="utf-8")

    if args.report:
        print(f"стиль: {args.style} — {STYLES[args.style]['note']}")
        print(f"источник таймингов: {source}")
        if runs:
            speech_total = sum(e - s for s, e in runs)
            print(f"речевых отрезков: {len(runs)}, речи {speech_total:.1f} с "
                  f"из {args.clip_dur:.1f} с ({speech_total/args.clip_dur*100:.0f}%)")
        print(f"слов размечено: {len(words)}")
        if fixed:
            print(f"исправлено по словарю: {len(fixed)}")
            for a, b in fixed[:8]:
                print(f"   {a} -> {b}")
        print("первые слова:")
        for w in words[:6]:
            print(f"   {w['start']:6.2f}–{w['end']:6.2f}  {w['word']}")
    print(f"ASS записан: {args.out}")


if __name__ == "__main__":
    main()
