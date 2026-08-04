#!/usr/bin/env python3
"""
story.py — разбор смысловой структуры ролика и поиск резов, которые не рвут мысль.

Зачем. Рез по секундомеру убивает ролик: нарезка на ровно 60 секунд закончилась
на «а вот на первом месте меня порадовало…» — то есть выбросила ровно ту
развязку, ради которой ролик и смотрят. Правильный порядок обратный: сначала
смысловая единица, потом длина. Целевая длина — ориентир, а не рамка.

Как считается.
  1. Текст режется на предложения, каждому ставится время из транскрипта.
  2. У каждой границы предложения считается сила как точки закрытия: длина
     паузы после неё плюс признак того, что следующее предложение открывает
     новый блок.
  3. Блоки распознаются по маркерам-открывашкам («на пятом месте», «по итогу»,
     «и наконец»). Блок считается закрытым, когда после открывашки прозвучало
     хотя бы одно завершённое предложение с конкретикой — цифрой или оценкой.
  4. Рез ищется в диапазоне от минимума до максимума и садится на сильнейшую
     точку закрытия. Если внутри диапазона блок остаётся открытым — участок
     помечается как обрывающий развязку.

Использование:
    python3 story.py --transcript t.json --target 60 --min 45 --max 120
    python3 story.py --transcript t.json --target 60 --at 371 --json
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

# Маркеры, открывающие новый смысловой блок.
OPENERS = [
    r"\bна\s+(перв|втор|трет|четверт|пят|шест|седьм|восьм|девят|десят)\w*\s+месте\b",
    r"\b(перв|втор|трет)\w*\s+место\b",
    r"\bи\s+наконец\b",
    r"\bпо\s+итогу\b",
    r"\bв\s+итоге\b",
    r"\bначнём\b|\bначнем\b",
    r"\bтеперь\s+перейд\w+\b",
    r"\bа\s+вот\b",
    r"\bследом\b",
    r"\bдалее\b",
]
# Признаки того, что мысль доведена: конкретика, цифра, вывод.
PAYOFF = [
    r"\d",
    r"\bрубл\w+\b", r"\bдоллар\w+\b", r"\bпроцент\w+\b",
    r"\bитог\w*\b", r"\bприбыл\w+\b", r"\bпереплат\w+\b",
    r"\bстоимость\b", r"\bоценивается\b", r"\bсоставля\w+\b",
]
CLOSERS = [
    r"\bпоэтому\b", r"\bтак\s+что\b", r"\bв\s+общем\b", r"\bпо\s+итогу\b",
]
# Рекламные вставки автора. Их нельзя ни резать по ним, ни включать в нарезку:
# это оплаченная интеграция чужого сервиса, и поверх неё бессмысленно ставить
# свой баннер — зритель услышит один промокод и увидит другой бренд.
# Сильные признаки: срабатывают сами по себе, ошибиться почти невозможно.
SPONSOR_STRONG = [
    r"\bпромокод\w*\b", r"\bспонсор\w*\b", r"\bреклам\w+\b",
    r"\bбонус\s+на\s+перв\w+\s+депозит\b", r"\bрегистрируйс\w+\b",
    r"\bвыгодно\s+купить\s+или\s+продать\b", r"\bскидк\w+\s+по\s+код\w+\b",
]
# Слабые признаки: сами по себе НЕ доказательство. «По ссылке в описании» может
# вести и на собственный розыгрыш автора — на этом детектор один раз уже ошибся
# и вырезал подсчёт прибыли. Поэтому слабый признак учитывается только рядом
# с сильным.
SPONSOR_WEAK = [
    r"\bпо\s+ссылке\s+в\s+описании\b", r"\bссылка\s+в\s+описании\b",
    r"\bверификаци\w+\b", r"\bмоментальн\w+\s+вывод\w*\b",
    r"\bсайт\w*\b", r"\bбольшой\s+ассортимент\b",
    r"\bспособ\w*\s+вывода\b", r"\bбаланс\s+сайта\b", r"\bскидк\w+\b",
    r"\bкриптовалют\w+\b", r"\bсбп\b", r"\bрекоменду\w+\b",
]
# Расширение считаем по ВРЕМЕНИ и по цепочке: дальность отсчитывается от
# ПОСЛЕДНЕГО попадания, а не от якоря. Иначе хвост интеграции отваливается —
# 30 секунд от «промокода» заканчивались раньше, чем сама реклама.
SPONSOR_GAP = 10.0        # допустимый провал без признаков внутри блока
SPONSOR_MAX_SPAN = 90.0   # предохранитель: дальше блок не растём
# Служебные концовки канала. Заканчивать нарезку на них нельзя: это не развязка,
# а прощание и просьбы о лайках — зритель уходит без причины остаться.
HOUSEKEEPING = [
    r"\bпока-пока\b", r"\bпока\s+пока\b", r"\bдо\s+встречи\b",
    r"\bхорошего\s+дня\b", r"\bприятного\s+вечера\b", r"\bвсем\s+пока\b",
    r"\bс\s+вами\s+был\b", r"\bподпис\w+\b", r"\bставьте\s+лайк\w*\b",
    r"\bжмите\s+колокольчик\b", r"\bпишите\s+в\s+комментар\w+\b",
    r"\bне\s+забудь\w*\s+подпис\w+\b", r"\bкакие\s+скины\s+мне\s+закупить\b",
]


def parse_ts(ts: str) -> float:
    p = [float(x) for x in ts.split(":")]
    while len(p) < 3:
        p.insert(0, 0.0)
    return p[0] * 3600 + p[1] * 60 + p[2]


def sentences(segments: list[dict], total: float) -> list[dict]:
    """Предложения с временем. Внутри сегмента время делим по длине текста."""
    out: list[dict] = []
    for i, seg in enumerate(segments):
        t0 = parse_ts(seg["timestamp"])
        t1 = parse_ts(segments[i + 1]["timestamp"]) if i + 1 < len(segments) else total
        parts = [p.strip() for p in re.split(r"(?<=[.!?…])\s+", seg["text"]) if p.strip()]
        if not parts:
            continue
        chars = sum(len(p) for p in parts) or 1
        cur = t0
        for p in parts:
            share = (t1 - t0) * len(p) / chars
            out.append({"start": round(cur, 2), "end": round(cur + share, 2), "text": p})
            cur += share
    return out


def _has(patterns: list[str], text: str) -> bool:
    low = text.lower()
    return any(re.search(p, low) for p in patterns)


def _resolve_sponsor(sents: list[dict]) -> None:
    """
    Сильный признак — реклама сразу. Слабый — только если сильный есть рядом.
    Плюс заливка провалов: если между двумя рекламными предложениями меньше
    SPONSOR_NEAR предложений, середина тоже реклама. Внутри интеграции автор
    говорит обычными словами, и по одной фразе её разрывать нельзя.
    """
    n = len(sents)
    for s in sents:
        s["sponsor"] = bool(s["sponsor_strong"])

    anchors = [i for i, s in enumerate(sents) if s["sponsor_strong"]]

    for a in anchors:
        # вперёд: тянемся, пока признаки продолжают появляться
        last_hit = sents[a]["end"]
        for j in range(a + 1, n):
            s = sents[j]
            if s["start"] - sents[a]["end"] > SPONSOR_MAX_SPAN:
                break
            if s["start"] - last_hit > SPONSOR_GAP:
                break
            if s["sponsor_weak"] or s["sponsor_strong"]:
                for k in range(a + 1, j + 1):
                    sents[k]["sponsor"] = True
                last_hit = s["end"]
        # назад: то же в обратную сторону
        last_hit = sents[a]["start"]
        for j in range(a - 1, -1, -1):
            s = sents[j]
            if sents[a]["start"] - s["end"] > SPONSOR_MAX_SPAN:
                break
            if last_hit - s["end"] > SPONSOR_GAP:
                break
            if s["sponsor_weak"] or s["sponsor_strong"]:
                for k in range(j, a):
                    sents[k]["sponsor"] = True
                last_hit = s["start"]


def analyse(segments: list[dict], total: float) -> list[dict]:
    """Каждому предложению — роль в структуре и сила как точки закрытия."""
    sents = sentences(segments, total)

    # Проход 1: маркеры. Реклама разрешается отдельно, потому что слабый признак
    # зависит от соседей и его нельзя решить внутри одного предложения.
    for i, s in enumerate(sents):
        s["opens"] = _has(OPENERS, s["text"])
        s["payoff"] = _has(PAYOFF, s["text"])
        s["closes_phrase"] = _has(CLOSERS, s["text"])
        s["housekeeping"] = _has(HOUSEKEEPING, s["text"])
        s["sponsor_strong"] = _has(SPONSOR_STRONG, s["text"])
        s["sponsor_weak"] = _has(SPONSOR_WEAK, s["text"])
        nxt = sents[i + 1] if i + 1 < len(sents) else None
        s["gap_after"] = round(nxt["start"] - s["end"], 2) if nxt else 2.0

    _resolve_sponsor(sents)

    # Проход 2: структура блоков и сила точек реза
    open_depth = 0
    for i, s in enumerate(sents):
        nxt = sents[i + 1] if i + 1 < len(sents) else None

        if s["opens"]:
            open_depth += 1
        # блок закрывается, когда после открывашки прозвучала конкретика
        if open_depth > 0 and s["payoff"] and not s["opens"]:
            open_depth = max(0, open_depth - 1)
        s["open_after"] = open_depth

        # сила точки закрытия
        strength = 0.0
        if s["text"].rstrip("\"'»)").endswith((".", "!", "?", "…")):
            strength += 1.0
        strength += min(s["gap_after"], 1.0) * 1.5
        if s["payoff"]:
            strength += 0.8
        if s["closes_phrase"]:
            strength += 0.6
        if nxt is not None and nxt.get("opens"):
            strength += 1.4          # дальше начинается новая тема — идеальный рез
        if s["open_after"] > 0:
            strength -= 2.5          # блок не закрыт: резать здесь значит оборвать
        if s["housekeeping"]:
            strength -= 3.0          # прощание и просьбы о лайках — не развязка
        if s["sponsor"]:
            strength -= 4.0          # чужая интеграция: и не финал, и не наш бренд
        if nxt is not None and nxt.get("housekeeping"):
            strength += 0.7          # зато перед прощанием обычно стоит вывод
        s["cut_strength"] = round(strength, 2)
    return sents


def pick_cut(sents: list[dict], start: float, target: float,
             min_dur: float, max_dur: float) -> dict:
    """Точка выхода для куска, начинающегося в start."""
    cands = [s for s in sents
             if min_dur <= s["end"] - start <= max_dur]
    if not cands:
        # В диапазоне нет ни одной границы предложения — значит рез придётся
        # ставить посреди фразы. Честно помечаем это, а не прячем.
        tail = [s for s in sents if s["end"] > start]
        end = round(min(tail[-1]["end"] if tail else start + target,
                        start + max_dur), 2)
        cut_in = next((s for s in sents if s["start"] <= end <= s["end"]), None)
        return {"end": end, "duration": round(end - start, 2),
                "quality": "рвёт фразу", "strength": 0.0, "open_block": True,
                "last_sentence": (cut_in or {}).get("text", "")[:90],
                "drift_from_target": round(end - start - target, 2),
                "note": "в диапазоне нет границ предложений, рез попадёт внутрь фразы"}

    # ближе к цели — лучше, но сила закрытия важнее
    def score(s):
        drift = abs((s["end"] - start) - target)
        return s["cut_strength"] - drift / 30.0

    best = max(cands, key=score)
    open_block = best["open_after"] > 0
    if open_block:
        # ищем ближайшую точку, где блок всё-таки закрыт, даже за целевой длиной
        closed = [s for s in cands if s["open_after"] == 0]
        if closed:
            best = max(closed, key=score)
            open_block = False

    dur = best["end"] - start
    # интеграция внутри куска так же плоха, как на его конце
    inside = [s for s in sents if s["start"] >= start - 0.01 and s["end"] <= best["end"] + 0.01]
    sponsor_hit = [s for s in inside if s.get("sponsor")]
    if open_block:
        quality = "обрывает развязку"
    elif best["cut_strength"] >= 3.0:
        quality = "чистое закрытие"
    elif best["cut_strength"] >= 1.8:
        quality = "приемлемо"
    else:
        quality = "слабое закрытие"

    if sponsor_hit:
        quality = "внутри чужая интеграция"

    return {
        "end": round(best["end"], 2),
        "duration": round(dur, 2),
        "quality": quality,
        "strength": round(best["cut_strength"] - (4.0 if sponsor_hit else 0.0), 2),
        "open_block": open_block,
        "sponsor_inside": round(sum(s["end"] - s["start"] for s in sponsor_hit), 1),
        "last_sentence": best["text"][:90],
        "drift_from_target": round(dur - target, 2),
    }


def suggest(sents: list[dict], target: float, min_dur: float, max_dur: float,
            starts: list[float] | None = None) -> list[dict]:
    """Куски от каждой открывашки — обычно там и сидят сильные входы."""
    if starts is None:
        starts = [s["start"] for s in sents if s["opens"]]
        if not starts:
            starts = [0.0]
    res = []
    for st in starts:
        cut = pick_cut(sents, st, target, min_dur, max_dur)
        entry = next((s for s in sents if abs(s["start"] - st) < 0.01), None)
        res.append({"start": round(st, 2), "hook": (entry or {}).get("text", "")[:80],
                    **cut})
    return res


def main() -> None:
    ap = argparse.ArgumentParser(description="Смысловые резы вместо резов по секундомеру")
    ap.add_argument("--transcript", required=True)
    ap.add_argument("--total", type=float, default=None, help="длина исходника, с")
    ap.add_argument("--target", type=float, default=60.0)
    ap.add_argument("--min", dest="min_dur", type=float, default=45.0)
    ap.add_argument("--max", dest="max_dur", type=float, default=120.0)
    ap.add_argument("--at", type=float, default=None, help="проверить один вход")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    d = json.load(open(args.transcript, encoding="utf-8"))
    segs = d.get("segments") or []
    total = args.total or (parse_ts(segs[-1]["timestamp"]) + 20 if segs else 0)
    sents = analyse(segs, total)

    if args.at is not None:
        r = pick_cut(sents, args.at, args.target, args.min_dur, args.max_dur)
        print(json.dumps(r, ensure_ascii=False, indent=2) if args.json
              else f"вход {args.at:.0f} с -> выход {r['end']:.0f} с "
                   f"({r['duration']:.0f} с), {r['quality']}\n  «{r.get('last_sentence','')}»")
        return

    res = suggest(sents, args.target, args.min_dur, args.max_dur)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return
    print(f"целевая длина {args.target:.0f} с, диапазон {args.min_dur:.0f}–{args.max_dur:.0f}\n")
    for r in sorted(res, key=lambda x: -x["strength"])[:10]:
        print(f"{r['start']:>7.1f} -> {r['end']:>7.1f}  ({r['duration']:>5.1f} с)  "
              f"{r['quality']:<18} сила {r['strength']:>5.2f}")
        print(f"         вход: «{r['hook']}»")
        print(f"         выход: «{r.get('last_sentence','')}»")


if __name__ == "__main__":
    main()
