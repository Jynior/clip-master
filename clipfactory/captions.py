#!/usr/bin/env python3
"""
captions.py — караоке-субтитры в стиле TikTok через libass.

Точных таймингов по словам транскрипция не даёт, а сегменты приходят с точностью
до секунды. Поэтому слова привязываются к РЕАЛЬНОЙ речи: по звуку считается
энергия в окнах 20 мс, находятся непрерывные речевые отрезки, и слова
раскладываются внутри них пропорционально длине. Так подсветка попадает в голос,
а не плывёт от накопленной ошибки.

Стили взяты по замерам того, что реально используют: Hormozi (белый + жёлтое
активное слово), три состояния (Submagic) и «пилюля» с плашкой.

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


STYLES = {
    "hormozi": {
        "font": "Montserrat", "size": 100, "bold": -1,
        "outline": 10, "shadow": 0, "margin_v": 380,
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
        "font": "Anton", "size": 94, "bold": 0,
        "outline": 10, "shadow": 0, "margin_v": 380,
        "base": "#FFFFFF", "active": "#F59E0B", "upcoming": "#8E8E9C",
        "words_per_chunk": 3, "upper": True,
        "note": "Три состояния: сказанное, текущее и будущее разными цветами.",
        "looks": "Уже произнесённые слова белые, текущее янтарное, ещё не "
                 "сказанные приглушённо-серые. Видно, сколько осталось до "
                 "конца фразы.",
        "fits": "Длинные фразы и плотную речь: серый цвет будущих слов не "
                "даёт дочитать вперёд голоса и подсказывает ритм.",
    },
    "pill": {
        "font": "Montserrat", "size": 88, "bold": -1,
        "outline": 4, "shadow": 0, "margin_v": 380,
        "base": "#FFFFFF", "active": "#FFFFFF", "upcoming": None,
        "pill_bg": "#FF2D55",
        "words_per_chunk": 3, "upper": True,
        "note": "Активное слово на цветной плашке.",
        "looks": "Текст белый без яркой подсветки, зато под звучащим словом "
                 "едет цветной прямоугольник. Акцент даёт не цвет буквы, а "
                 "подложка — заметнее на пёстром фоне.",
        "fits": "Яркий насыщенный кадр, где жёлтая буква теряется: геймплей, "
                "светлые сцены. Плашка гарантирует контраст.",
    },
    "neon": {
        "font": "Anton", "size": 94, "bold": 0,
        "outline": 3, "shadow": 3, "margin_v": 350,
        "base": "#FFFFFF", "active": "#00FFFF", "upcoming": None,
        "outline_colour": "#00FFFF",
        "words_per_chunk": 2, "upper": True,
        "note": "Неоновое свечение, активное слово голубое.",
        "looks": "Тонкая цветная обводка с тенью вместо толстой чёрной, "
                 "активное слово голубое. По два слова за показ вместо трёх — "
                 "текст занимает меньше кадра.",
        "fits": "Тёмный и ночной материал, гейминг. На светлом фоне не "
                "использовать: тонкая обводка не держит контраст.",
    },
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
    "Anton": "Anton.ttf",
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


def word_positions(chunk: list[dict], style: dict) -> list[tuple[float, float]]:
    """Абсолютные x-границы каждого слова в отцентрованной строке — для плашки."""
    font = _font(style)
    up = style["upper"]
    words = [(w["word"].upper() if up else w["word"]) for w in chunk]
    space = text_width(font, " ") or 10
    widths = [text_width(font, w) for w in words]
    total = sum(widths) + space * (len(words) - 1)
    x = (OUT_W - total) / 2
    spans = []
    for wd in widths:
        spans.append((x, x + wd))
        x += wd + space
    return spans


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
    if style_name == "pill":
        pill = rgb_to_ass(st["pill_bg"])
        header += (f"Style: Pill,{st['font']},{st['size']},{base},{base},{pill},"
                   f"{pill},{st['bold']},0,0,0,100,100,0,0,3,14,0,2,70,70,"
                   f"{st['margin_v']},1\n")

    events = ["\n[Events]",
              "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]

    font = _font(st)
    # Смысловая разбивка: по паузам, точкам и служебным словам, затем перенос
    # по реальной ширине шрифта максимум на две строки (стандарт Netflix/BBC).
    import chunking as CH
    groups = CH.chunk_words(words, max_words=st["words_per_chunk"],
                            width_fn=lambda t: text_width(font, t), max_chars=60)
    shows = CH.fit_to_width(groups, lambda t: text_width(font, t),
                            SAFE_TEXT_W, max_lines=2, upper=st["upper"])
    chunks = [s["words"] for s in shows]
    lines_of = {id(s["words"]): s["lines"] for s in shows}
    y_line = OUT_H - st["margin_v"]      # базовая линия строки для \pos

    for ch in chunks:
        cur_lines = lines_of.get(id(ch)) or [ch]
        # одиночное длинное слово разбить нельзя — сжимаем строку по ширине
        widest = max(
            text_width(font, " ".join(
                (w["word"].upper() if st["upper"] else w["word"]) for w in ln))
            for ln in cur_lines)
        fit = ""
        if widest > SAFE_TEXT_W:
            sc = max(55, int(100 * SAFE_TEXT_W / widest))
            fit = f"\\fscx{sc}\\fscy{sc}"

        spans = word_positions(ch, st) if style_name == "pill" else None
        for k, w in enumerate(ch):
            # подсветка держится сквозь перенос: строки склеиваются через \N
            rendered = []
            idx = 0
            for ln in cur_lines:
                seg = []
                for ww in ln:
                    txt = ww["word"].upper() if st["upper"] else ww["word"]
                    if idx < k:
                        seg.append(f"{{\\c{base}}}{txt}")
                    elif idx == k:
                        seg.append(f"{{\\c{active}}}{txt}")
                    else:
                        seg.append(f"{{\\c{upcoming}}}{txt}")
                    idx += 1
                rendered.append(" ".join(seg))
            line = (f"{{{fit}}}" if fit else "") + "\\N".join(rendered)
            s, e = fmt_time(w["start"]), fmt_time(w["end"])
            if style_name == "pill" and spans:
                # плашка ставится по измеренной середине активного слова,
                # иначе она уезжает в центр строки
                x0, x1 = spans[k]
                cx = (x0 + x1) / 2
                act = ch[k]["word"].upper() if st["upper"] else ch[k]["word"]
                events.append(
                    f"Dialogue: 0,{s},{e},Pill,,0,0,0,,"
                    f"{{\\an5\\pos({cx:.0f},{y_line - st['size'] * 0.36:.0f})}}{act}")
                events.append(f"Dialogue: 1,{s},{e},Cap,,0,0,0,,{line}")
            else:
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
    ap.add_argument("--clip-dur", type=float, required=True)
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
    ap.add_argument("--out", required=True)
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
                  + (f", будущий {st['upcoming']}" if st.get('upcoming') else "")
                  + (f", плашка {st['pill_bg']}" if st.get('pill_bg') else ""))
        return

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
