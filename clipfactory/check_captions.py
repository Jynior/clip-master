#!/usr/bin/env python3
"""
check_captions.py — проверяет субтитры на ГОТОВОМ mp4, по пикселям.

Зачем по пикселям, а если есть .ass. Потому что .ass — это намерение, а не
результат. Позиция строки на экране зависит от того, как libass разложил текст:
сколько строк вышло, сработал ли перенос, какие метрики у шрифта. Ровно на этом
и проехали: файл субтитров был правильный, а в кадре текст прыгал между двумя
высотами 56 раз за минуту. Проверять надо то, что увидит зритель.

Что ловит:

  1. Плавание позиции. Субтитры должны стоять на одном y. Считается разброс
     верхней границы блока по всем кадрам и число переключений между разными
     положениями. Разброс больше допуска — ошибка.
  2. Заезд под интерфейс TikTok. Ниже SAFE_BOTTOM идут ник, подпись, трек и
     прогресс: текст там просто не читается.
  3. Заезд в верхнюю мёртвую зону и под кнопки справа.
  4. Разрыв по границе видео. Если блок одной частью на картинке, а другой на
     размытой подложке, это выглядит как ошибка вёрстки.

Как отличает субтитры от яркого видео. Не по яркости — её в кадре сколько угодно.
Признак другой: белый пиксель, рядом с которым есть почти чёрный, — это след
обводки 10 px. Проверяется по обеим осям, иначе от буквы остаются только верх и
низ. Одного признака мало: его дают и граница леттербокса, и блики, поэтому поверх
работает второй отбор — зона. Субтитрами считается блок, который держится в кадре
дольше всех; хук и текст внутри самого видео уходят в замечания и на оценку
позиции не влияют. Пока отбора по зоне не было, проверка складывала хук на y=363
с субтитрами на y=1206 и показывала разброс 963 px вместо настоящих 246.

Чего проверка НЕ делает. Не судит, попадает ли подсветка в голос — это к
captions.py и словным таймингам. Не оценивает читаемость на пёстром фоне. И не
считает дефектом низкий охват кадров: короткие показы в одно слово детектор берёт
неохотно, поэтому охват около половины кадров при исправных субтитрах — норма.

    python3 check_captions.py clip.mp4
    python3 check_captions.py clip.mp4 --style hormozi --tolerance 12
    python3 check_captions.py clip.mp4 --json отчёт.json

Код возврата 1, если найдены дефекты, — модуль можно ставить в конец сборки.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys

import numpy as np

OUT_W, OUT_H = 1080, 1920

# те же зоны, что в captions.py и variants.py
SAFE_TOP = 250
SAFE_BOTTOM = 1460
SAFE_RIGHT = 1000
VIDEO_BAND = (656, 1264)      # полоса картинки при леттербоксе 100%

AW, AH = 360, 640             # размер кадра для анализа
SCALE = OUT_H // AH           # = 3

DRIFT_TOLERANCE = 16          # допустимый разброс верха блока, px
MIN_COVERAGE = 0.25           # ниже этой доли кадров с текстом проверять нечего
MIN_BAND_PX = 60              # блок мельче — почти наверняка шум, а не строка текста


def probe_fps(path: str) -> float:
    cp = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=r_frame_rate", "-of", "csv=p=0", path],
        capture_output=True, text=True, stdin=subprocess.DEVNULL)
    try:
        a, b = cp.stdout.strip().split("/")
        return float(a) / float(b)
    except Exception:
        return 30.0


def frames(path: str, fps: float):
    """Кадры RGB как (AH, AW, 3), потоком — видео целиком в память не влезает."""
    p = subprocess.Popen(
        ["ffmpeg", "-nostdin", "-v", "error", "-i", path,
         "-vf", f"fps={fps},scale={AW}:{AH}", "-pix_fmt", "rgb24",
         "-f", "rawvideo", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    n = AW * AH * 3
    try:
        while True:
            buf = p.stdout.read(n)
            if len(buf) < n:
                break
            yield np.frombuffer(buf, np.uint8).reshape(AH, AW, 3)
    finally:
        p.stdout.close()
        p.wait()


def caption_mask(f: np.ndarray) -> np.ndarray:
    """
    Пиксели, похожие на субтитр: белые и с почти чёрным соседом по вертикали.

    Второе условие и делает всю работу — это след обводки. Яркое место в кадре
    его не даёт, поэтому детектор не путает субтитры с содержимым видео.
    """
    a = f.astype(np.int16)
    mx = a.max(axis=2)
    mn = a.min(axis=2)
    white = (mn >= 200) & ((mx - mn) <= 45)
    dark = mx <= 70

    # Тёмный сосед ищется и по строкам, и по столбцам. Только по строкам мало:
    # вертикальная палочка буквы высокая, и её середина оказывается дальше 4
    # строк от обводки — тогда от буквы остаются лишь верх и низ, охват падает
    # до 38% кадров, а замеренная высота блока выходит вдвое меньше настоящей.
    # По столбцам палочка тонкая, поэтому проверка по обеим осям берёт глиф весь.
    near = np.zeros_like(dark)
    for d in range(1, 5):
        near[:-d] |= dark[d:]
        near[d:] |= dark[:-d]
        near[:, :-d] |= dark[:, d:]
        near[:, d:] |= dark[:, :-d]
    return white & near


def blocks(rows: np.ndarray, thresh: int, gap: int = 6) -> list[tuple[int, int]]:
    """Все связные группы строк с текстом, а не только самая заметная."""
    hit = np.where(rows >= thresh)[0]
    if len(hit) == 0:
        return []
    groups, start, prev = [], hit[0], hit[0]
    for y in hit[1:]:
        if y - prev > gap:
            groups.append((int(start), int(prev)))
            start = y
        prev = y
    groups.append((int(start), int(prev)))
    return groups


def measure(path: str, fps: float = 10.0, min_px: int = 4) -> dict:
    """
    Все текстовые блоки в каждом кадре.

    Блоков в кадре бывает несколько: субтитры внизу и хук в верхней трети. Хук
    сделан так же — белый текст с чёрной обводкой, — поэтому по признаку «похоже
    на субтитр» он неотличим. Разделять их надо не признаком, а положением:
    какой блок держится в кадре дольше всех, тот и субтитры. Пока это не было
    сделано, проверка показывала разброс 963 px, потому что складывала хук на
    y=363 с субтитрами на y=1206.
    """
    recs = []
    for i, f in enumerate(frames(path, fps)):
        m = caption_mask(f)
        rows = m.sum(axis=1)
        found = []
        for b in blocks(rows, min_px):
            px = int(rows[b[0]:b[1] + 1].sum())
            # Отсекаем мелочь. Одиночные яркие точки на тёмном фоне проходят
            # признак «белое рядом с чёрным» и без этого фильтра дают тысячи
            # ложных блоков, размывающих поиск зоны субтитров. Строка текста
            # в уменьшенном кадре даёт заметно больше.
            if px < MIN_BAND_PX:
                continue
            cols = m[b[0]:b[1] + 1].sum(axis=0)
            xs = np.where(cols > 0)[0]
            found.append({
                "top": b[0] * SCALE,
                "bottom": (b[1] + 1) * SCALE,
                "px": px,
                "left": int(xs[0]) * SCALE if len(xs) else None,
                "right": int(xs[-1] + 1) * SCALE if len(xs) else None,
            })
        recs.append({"t": round(i / fps, 3), "bands": found})
    return {"fps": fps, "frames": recs}


def dominant_zone(recs: list[dict], window: int = 240) -> tuple[int, int]:
    """
    Зона, где текст держится в наибольшем числе кадров.

    Считаем по числу КАДРОВ, а не по сумме пикселей: хук крупнее субтитров и по
    пикселям легко перевесил бы, хотя живёт всего несколько секунд.
    """
    centers = [(b["top"] + b["bottom"]) // 2
               for r in recs for b in r["bands"]]
    if not centers:
        return (0, OUT_H)
    best, best_n = None, -1
    for c in sorted(set(centers)):
        n = sum(1 for r in recs
                if any(abs((b["top"] + b["bottom"]) // 2 - c) <= window // 2
                       for b in r["bands"]))
        if n > best_n:
            best, best_n = c, n
    return (max(0, best - window // 2), min(OUT_H, best + window // 2))


def verdict(meas: dict, expect_box: tuple[int, int] | None,
            tolerance: int = DRIFT_TOLERANCE) -> dict:
    recs = meas["frames"]
    total = len(recs)

    # выделяем зону субтитров и берём в каждом кадре только её блок
    zone = dominant_zone(recs)
    withtext = []
    other = []
    for r in recs:
        inside = [b for b in r["bands"] if zone[0] <= (b["top"] + b["bottom"]) // 2 <= zone[1]]
        outside = [b for b in r["bands"] if b not in inside]
        other.extend(outside)
        if inside:
            b = max(inside, key=lambda x: x["px"])
            withtext.append({"t": r["t"], **b})
    res: dict = {"frames": total, "with_text": len(withtext),
                 "coverage": round(len(withtext) / max(1, total), 3),
                 "zone": [int(zone[0]), int(zone[1])],
                 "problems": [], "notes": []}
    if not withtext:
        res["problems"].append("субтитры не найдены ни в одном кадре")
        return res

    # прочие текстовые блоки — обычно хук; сообщаем, но в оценку не берём
    if other:
        oc = sorted((b["top"] + b["bottom"]) // 2 for b in other)
        res["other_text_bands"] = {"frames": len(other),
                                   "center_median": int(np.median(oc))}
        res["notes"].append(
            f"вне зоны субтитров найдено текстовых блоков: {len(other)}, "
            f"центр около y={int(np.median(oc))}. Это может быть хук или текст "
            f"внутри самого видео — в оценку позиции субтитров не берётся")
    if res["coverage"] < MIN_COVERAGE:
        res["notes"].append(
            f"текст виден только в {res['coverage']*100:.0f}% кадров — "
            f"проверка по такой выборке слабая")

    top = np.array([r["top"] for r in withtext])
    bot = np.array([r["bottom"] for r in withtext])
    res["top"] = {"min": int(top.min()), "max": int(top.max()),
                  "median": int(np.median(top)), "spread": int(top.max() - top.min())}
    res["bottom"] = {"min": int(bot.min()), "max": int(bot.max()),
                     "median": int(np.median(bot))}

    # 1. плавание. Считаем не только разброс, но и число переключений: разброс
    # может дать один выброс, а прыгающий туда-сюда текст виден именно по ним.
    med = np.median(top)
    far = top[np.abs(top - med) > tolerance]
    lvl = [0 if abs(t - med) <= tolerance else 1 for t in top]
    switches = sum(1 for a, b in zip(lvl, lvl[1:]) if a != b)
    res["off_position_frames"] = int(len(far))
    res["switches"] = int(switches)
    if res["top"]["spread"] > tolerance:
        res["problems"].append(
            f"позиция плавает: верх блока {top.min()}..{top.max()} "
            f"(разброс {res['top']['spread']} px при допуске {tolerance}), "
            f"переключений {switches}")

    # 2 и 3. безопасные зоны
    if bot.max() > SAFE_BOTTOM:
        n = int((bot > SAFE_BOTTOM).sum())
        res["problems"].append(
            f"уходит под интерфейс TikTok: низ до {bot.max()} при границе "
            f"{SAFE_BOTTOM} ({n} кадров)")
    if top.min() < SAFE_TOP:
        res["problems"].append(
            f"заходит в верхнюю мёртвую зону: верх {top.min()} < {SAFE_TOP}")
    rights = [r["right"] for r in withtext if "right" in r]
    if rights and max(rights) > SAFE_RIGHT:
        n = sum(1 for x in rights if x > SAFE_RIGHT)
        res["notes"].append(
            f"правый край доходит до {max(rights)} при границе кнопок "
            f"{SAFE_RIGHT} ({n} кадров) — проверьте кадром, детектор мог "
            f"захватить яркое место видео")

    # 4. разрыв по границе картинки
    straddle = sum(1 for r in withtext
                   if r["top"] < VIDEO_BAND[1] < r["bottom"])
    if straddle:
        res["problems"].append(
            f"блок разрезан границей видео y={VIDEO_BAND[1]}: {straddle} кадров "
            f"частью на картинке, частью на подложке")

    # 5. сверка с расчётом стиля.
    # Сравниваем не «верх в верх». Стиль задаёт КОРОБКУ строки, а замер даёт
    # чернила — сами буквы. Капс без выносных элементов занимает не всю коробку,
    # поэтому чернила обязаны лежать ВНУТРИ неё, а не совпадать с её краем.
    # Требование равенства давало ложную ошибку в 57 px на правильном рендере.
    if expect_box is not None:
        bt, bb = expect_box
        res["expected_box"] = [int(bt), int(bb)]
        slack = tolerance
        if top.min() < bt - slack or bot.max() > bb + slack:
            res["problems"].append(
                f"текст вышел за расчётную коробку стиля {bt}..{bb}: "
                f"замерено {top.min()}..{bot.max()}")

    res["ok"] = not res["problems"]
    return res


def report(res: dict) -> None:
    print(f"кадров {res['frames']}, с субтитрами {res['with_text']} "
          f"({res['coverage']*100:.0f}%)")
    if "top" in res:
        t, b = res["top"], res["bottom"]
        print(f"верх блока:  {t['min']}..{t['max']}  медиана {t['median']}, "
              f"разброс {t['spread']} px")
        print(f"низ блока:   {b['min']}..{b['max']}  медиана {b['median']}")
        print(f"кадров вне позиции: {res['off_position_frames']}, "
              f"переключений: {res['switches']}")
    if "expected_box" in res:
        bt, bb = res["expected_box"]
        print(f"расчётная коробка стиля: {bt}..{bb}")
    for n in res.get("notes", []):
        print(f"  замечание: {n}")
    if res.get("problems"):
        print("\nНЕ ПРОШЛО:")
        for p in res["problems"]:
            print(f"  - {p}")
    else:
        print("\nпроверка пройдена: позиция фиксирована, зоны не нарушены")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Проверка субтитров по готовому mp4")
    ap.add_argument("source")
    ap.add_argument("--fps", type=float, default=10.0,
                    help="частота выборки кадров")
    ap.add_argument("--expect-box", default=None,
                    help="ожидаемая коробка строки как ВЕРХ:НИЗ, "
                         "например 1290:1430; для стиля берётся из captions.py")
    ap.add_argument("--style", default=None,
                    help="взять ожидаемую позицию из этого стиля")
    ap.add_argument("--tolerance", type=int, default=DRIFT_TOLERANCE)
    ap.add_argument("--min-px", type=int, default=4,
                    help="сколько пикселей в строке считать субтитром")
    ap.add_argument("--json", default=None, help="куда сложить полный отчёт")
    args = ap.parse_args()

    expect = None
    if args.expect_box:
        a, b = args.expect_box.split(":")
        expect = (int(a), int(b))
    if expect is None and args.style:
        sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
        import captions as C
        if args.style not in C.STYLES:
            raise SystemExit(f"нет стиля {args.style}; есть: {', '.join(C.STYLES)}")
        expect = C.band_of(C.STYLES[args.style])

    meas = measure(args.source, args.fps, args.min_px)
    res = verdict(meas, expect, args.tolerance)
    report(res)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump({"verdict": res, "measurement": meas}, fh,
                      ensure_ascii=False)
        print(f"\nполный отчёт: {args.json}")

    sys.exit(0 if res.get("ok") else 1)


if __name__ == "__main__":
    main()
