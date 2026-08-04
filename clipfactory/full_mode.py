#!/usr/bin/env python3
"""
full_mode.py — последовательная нарезка всего ролика на части по минуте
с вырезанием рекламных вставок и логичной сшивкой соседних частей.

Отличие от режима нарезки. Там мы искали лучшие моменты и остальное выбрасывали.
Здесь ролик разбирается целиком с первой секунды: семиминутное видео становится
семью частями, каждая со своими субтитрами, для последовательной заливки.

Как решается вопрос рекламы. Реклама автора — не просто «плохой участок», а
естественная граница. Поэтому:

  1. Находятся рекламные вставки (по маркерам из story.py) и склеиваются
     в непрерывные интервалы.
  2. Эти интервалы становятся ЖЁСТКИМИ границами частей. Часть кончается
     на закрытии мысли перед рекламой, следующая начинается на входе после неё.
     Так стык читается как продолжение, а не как обрыв посреди фразы.
  3. Внутри каждого чистого участка идёт обычное деление по целевой длине
     с посадкой на закрытие мысли.
  4. Если чистый участок короче минимума, он приклеивается к соседнему —
     тогда часть собирается из двух кусков источника через склейку, и это
     помечается в плане отдельно, потому что склейка внутри части заметна.

Использование:
    python3 full_mode.py --transcript t.json --total 445 --target 60
    python3 full_mode.py --transcript t.json --total 445 --json > plan.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import story as S  # noqa: E402


def sponsor_spans(sents: list[dict], merge_gap: float = 12.0,
                  pad: float = 0.4) -> list[dict]:
    """
    Рекламные интервалы. Отдельные помеченные предложения склеиваются, если
    между ними меньше merge_gap — внутри интеграции автор обычно вставляет
    обычные фразы, и по одной их разрывать нельзя.
    """
    hits = [s for s in sents if s.get("sponsor")]
    if not hits:
        return []
    spans: list[dict] = []
    cur = {"start": hits[0]["start"], "end": hits[0]["end"], "n": 1}
    for s in hits[1:]:
        if s["start"] - cur["end"] <= merge_gap:
            cur["end"] = s["end"]
            cur["n"] += 1
        else:
            spans.append(cur)
            cur = {"start": s["start"], "end": s["end"], "n": 1}
    spans.append(cur)

    # расширяем до границ предложений и добавляем небольшой запас
    for sp in spans:
        before = [x for x in sents if x["end"] <= sp["start"] + 0.01]
        after = [x for x in sents if x["start"] >= sp["end"] - 0.01]
        if before:
            sp["start"] = max(0.0, before[-1]["end"] - pad)
        if after:
            sp["end"] = after[0]["start"] + pad
        sp["duration"] = round(sp["end"] - sp["start"], 2)
        sp["start"] = round(sp["start"], 2)
        sp["end"] = round(sp["end"], 2)
    return spans


def clean_regions(total: float, spans: list[dict],
                  min_len: float = 4.0) -> list[tuple[float, float]]:
    """Участки источника без рекламы."""
    regions: list[tuple[float, float]] = []
    cursor = 0.0
    for sp in spans:
        if sp["start"] - cursor >= min_len:
            regions.append((cursor, sp["start"]))
        cursor = max(cursor, sp["end"])
    if total - cursor >= min_len:
        regions.append((cursor, total))
    return regions


def split_region(sents: list[dict], a: float, b: float, target: float,
                 min_dur: float, max_dur: float) -> list[dict]:
    """Делит чистый участок на части по целевой длине, садясь на закрытия мысли."""
    parts: list[dict] = []
    cursor = a
    while b - cursor > 1.0:
        left = b - cursor
        if left <= max_dur:
            # Последняя часть участка. Хвост нельзя отдавать как есть: если
            # автор успел открыть новую тему перед рекламой и не закрыл её
            # (реклама его перебила), часть оборвётся на полуслове. Подрезаем
            # до последнего настоящего закрытия.
            end = b
            # Берём и предложения, ПЕРЕСЕКАЮЩИЕ границу: граница рекламы почти
            # всегда попадает внутрь фразы, и если смотреть только целиком
            # уместившиеся, незакрытая тема на хвосте останется незамеченной.
            tail = [s for s in sents if s["end"] > cursor and s["start"] < b]
            if tail and tail[-1]["open_after"] > 0:
                closes = [s for s in tail
                          if s["open_after"] == 0 and s["cut_strength"] > 0
                          and s["end"] - cursor >= min_dur * 0.6]
                if closes:
                    end = closes[-1]["end"]
            dropped = round(b - end, 2)
            parts.append({"ranges": [(round(cursor, 2), round(end, 2))],
                          "duration": round(end - cursor, 2),
                          "quality": ("конец участка" if dropped < 0.5
                                      else f"хвост подрезан на {dropped:.1f} с"),
                          "trimmed": dropped,
                          "spliced": False})
            break
        cut = S.pick_cut(sents, cursor, target, min_dur, min(max_dur, left))
        end = min(cut["end"], b)
        if end <= cursor + 1.0:
            end = min(cursor + target, b)
        parts.append({"ranges": [(round(cursor, 2), round(end, 2))],
                      "duration": round(end - cursor, 2),
                      "quality": cut["quality"],
                      "last_sentence": cut.get("last_sentence", ""),
                      "spliced": False})
        cursor = end
    return parts


def build_plan(sents: list[dict], total: float, target: float = 60.0,
               min_dur: float = 40.0, max_dur: float = 95.0,
               min_part: float = 20.0) -> dict:
    spans = sponsor_spans(sents)
    regions = clean_regions(total, spans)

    parts: list[dict] = []
    for a, b in regions:
        parts.extend(split_region(sents, a, b, target, min_dur, max_dur))

    # слишком короткие части приклеиваем к соседу — это и есть склейка через рекламу
    merged: list[dict] = []
    for p in parts:
        if p["duration"] < min_part and merged:
            prev = merged[-1]
            prev["ranges"] = prev["ranges"] + p["ranges"]
            prev["duration"] = round(prev["duration"] + p["duration"], 2)
            prev["spliced"] = True
            prev["quality"] = "склейка через рекламу"
        else:
            merged.append(p)

    for i, p in enumerate(merged, 1):
        p["index"] = i
        p["source_ranges"] = [[r[0], r[1]] for r in p.pop("ranges")]

    return {
        "total": round(total, 2),
        "target": target,
        "sponsor_spans": spans,
        "sponsor_total": round(sum(s["duration"] for s in spans), 2),
        "clean_regions": [[round(a, 2), round(b, 2)] for a, b in regions],
        "parts": merged,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Режим фулл: весь ролик по частям")
    ap.add_argument("--transcript", required=True)
    ap.add_argument("--total", type=float, required=True)
    ap.add_argument("--target", type=float, default=60.0)
    ap.add_argument("--min", dest="min_dur", type=float, default=40.0)
    ap.add_argument("--max", dest="max_dur", type=float, default=95.0)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    d = json.load(open(args.transcript, encoding="utf-8"))
    sents = S.analyse(d.get("segments") or [], args.total)
    plan = build_plan(sents, args.total, args.target, args.min_dur, args.max_dur)

    if args.json:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return

    print(f"источник {plan['total']:.0f} с, целевая часть {plan['target']:.0f} с")
    if plan["sponsor_spans"]:
        print(f"\nрекламные вставки ({plan['sponsor_total']:.0f} с всего):")
        for sp in plan["sponsor_spans"]:
            print(f"   {sp['start']:>6.1f} – {sp['end']:>6.1f}  ({sp['duration']:>4.1f} с, "
                  f"фраз {sp['n']}) — вырезается")
    else:
        print("\nрекламных вставок не найдено")
    print(f"\nчистого материала: "
          f"{plan['total'] - plan['sponsor_total']:.0f} с в "
          f"{len(plan['clean_regions'])} участках")
    print(f"\nчастей: {len(plan['parts'])}")
    for p in plan["parts"]:
        rng = " + ".join(f"{a:.1f}–{b:.1f}" for a, b in p["source_ranges"])
        mark = "  [СКЛЕЙКА]" if p["spliced"] else ""
        print(f"   часть {p['index']}: {p['duration']:>5.1f} с   {rng}{mark}")
        print(f"             {p['quality']}")
        if p.get("last_sentence"):
            print(f"             выход: «{p['last_sentence'][:70]}»")


if __name__ == "__main__":
    main()
