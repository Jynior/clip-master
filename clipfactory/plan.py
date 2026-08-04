#!/usr/bin/env python3
"""
plan.py — анализ исходника и поиск точек реза.

Не режет вслепую по хронометражу: ищет тишину (границы фраз) и смены сцен,
затем сажает каждый рез на ближайшую естественную границу.

Использование:
    python3 plan.py source.mp4 --parts 3 --out plan.json
    python3 plan.py source.mp4 --parts 3 --min-part 25 --max-part 90
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL)


def probe(path: str) -> dict:
    """Метаданные исходника через ffprobe."""
    cp = run([
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", path,
    ])
    if cp.returncode != 0:
        raise SystemExit(f"ffprobe не смог прочитать {path}:\n{cp.stderr}")
    d = json.loads(cp.stdout)
    v = next((s for s in d["streams"] if s["codec_type"] == "video"), None)
    a = next((s for s in d["streams"] if s["codec_type"] == "audio"), None)
    if v is None:
        raise SystemExit("В файле нет видеодорожки.")
    fps = 30.0
    if v.get("avg_frame_rate", "0/0") not in ("0/0", ""):
        num, _, den = v["avg_frame_rate"].partition("/")
        if float(den or 1):
            fps = float(num) / float(den or 1)
    return {
        "path": path,
        "duration": float(d["format"]["duration"]),
        "width": int(v["width"]),
        "height": int(v["height"]),
        "fps": round(fps, 3),
        "vcodec": v.get("codec_name"),
        "acodec": a.get("codec_name") if a else None,
        "has_audio": a is not None,
    }


def find_silences(path: str, noise_db: int = -32, min_dur: float = 0.30) -> list[dict]:
    """Интервалы тишины — кандидаты на рез между фразами."""
    cp = run([
        "ffmpeg", "-hide_banner", "-nostats", "-i", path,
        "-af", f"silencedetect=noise={noise_db}dB:d={min_dur}",
        "-f", "null", "-",
    ])
    log = cp.stderr
    starts = [float(m) for m in re.findall(r"silence_start:\s*(-?[\d.]+)", log)]
    ends = [float(m) for m in re.findall(r"silence_end:\s*([\d.]+)", log)]
    out = []
    for i, s in enumerate(starts):
        e = ends[i] if i < len(ends) else None
        if e is None or e <= s:
            continue
        out.append({"start": round(s, 3), "end": round(e, 3),
                    "mid": round((s + e) / 2, 3), "len": round(e - s, 3)})
    return out


def mean_volume(path: str) -> float | None:
    """Средний уровень дорожки — от него отсчитываем порог тишины."""
    cp = run(["ffmpeg", "-hide_banner", "-nostats", "-i", path,
              "-af", "volumedetect", "-f", "null", "-"])
    m = re.search(r"mean_volume:\s*(-?[\d.]+) dB", cp.stderr)
    return float(m.group(1)) if m else None


def adaptive_silences(path: str, dur: float,
                      min_dur: float = 0.30) -> tuple[list[dict], dict]:
    """
    Фиксированный порог тишины не работает на материале с музыкальной
    подложкой: под ней «тишины» просто нет, детектор возвращает пусто, и резы
    падают на жёсткий хронометраж. Поэтому идём лестницей относительно
    среднего уровня дорожки, пока не наберём разумную плотность пауз.

    Нужная плотность — примерно одна пауза на 25 с материала, но не меньше 4.
    """
    mv = mean_volume(path)
    need = max(4, int(dur / 25))
    tried: list[dict] = []

    if mv is None:
        offsets = [None]
        ladder = [-32.0, -26.0, -22.0, -18.0]
    else:
        ladder = [round(mv + off, 1) for off in (-10, -6, -3, -1.5, 0)]

    best: list[dict] = []
    best_thr = None
    for thr in ladder:
        found = find_silences(path, int(round(thr)), min_dur)
        tried.append({"threshold_db": thr, "found": len(found)})
        if len(found) > len(best):
            best, best_thr = found, thr
        if len(found) >= need:
            best, best_thr = found, thr
            break

    return best, {
        "mean_volume_db": mv,
        "threshold_used_db": best_thr,
        "needed": need,
        "ladder": tried,
    }


def find_scene_cuts(path: str, threshold: float = 0.35) -> list[float]:
    """Смены сцен — вторые по приоритету кандидаты на рез."""
    cp = run([
        "ffmpeg", "-hide_banner", "-nostats", "-i", path,
        "-filter_complex", f"select='gt(scene,{threshold})',metadata=print:file=-",
        "-an", "-f", "null", "-",
    ])
    times = [float(m) for m in re.findall(r"pts_time:([\d.]+)", cp.stdout + cp.stderr)]
    return sorted(set(round(t, 3) for t in times))


def pick_boundary(ideal: float, silences: list[dict], scenes: list[float],
                  window: float, taken: list[float], min_gap: float = 4.0) -> dict:
    """
    Выбирает ближайшую к идеалу естественную границу.
    Приоритет: середина тишины > смена сцены > жёсткий рез по хронометражу.
    """
    cands: list[tuple[float, float, str]] = []  # (score, time, kind)

    for s in silences:
        t = s["mid"]
        dist = abs(t - ideal)
        if dist > window:
            continue
        # длинная тишина = более уверенная граница фразы
        bonus = min(s["len"], 1.5) * 0.6
        cands.append((dist - bonus, t, "silence"))

    for t in scenes:
        dist = abs(t - ideal)
        if dist > window:
            continue
        cands.append((dist + 1.2, t, "scene"))  # штраф: сцена слабее тишины

    cands.sort(key=lambda c: c[0])
    for score, t, kind in cands:
        if all(abs(t - prev) >= min_gap for prev in taken):
            return {"time": round(t, 3), "kind": kind,
                    "drift": round(t - ideal, 2)}
    return {"time": round(ideal, 3), "kind": "hard", "drift": 0.0}


def plan_by_target(dur: float, target: float, tolerance: float,
                   silences: list[dict], scenes: list[float],
                   min_part: float) -> list[dict]:
    """
    Нарезка под целевую длину части, а не на N равных кусков.
    Идём вперёд от начала: следующая граница ищется около start+target,
    в пределах tolerance садится на ближайшую тишину или смену сцены.
    Хвост короче min_part приклеивается к предыдущей части.
    """
    boundaries: list[dict] = []
    taken: list[float] = []
    cursor = 0.0
    while dur - cursor > target + tolerance:
        ideal = cursor + target
        b = pick_boundary(ideal, silences, scenes, tolerance, taken)
        if b["time"] <= cursor + min_part:
            b = {"time": round(ideal, 3), "kind": "hard", "drift": 0.0}
        boundaries.append(b)
        taken.append(b["time"])
        cursor = b["time"]
    # хвост слишком короткий — убираем последнюю границу, он вольётся в предыдущую часть
    if boundaries and dur - boundaries[-1]["time"] < min_part:
        boundaries.pop()
    return boundaries


def build_plan(path: str, parts: int, window: float | None,
               min_part: float, max_part: float,
               target: float | None = None,
               tolerance: float = 12.0) -> dict:
    meta = probe(path)
    dur = meta["duration"]

    sil_info: dict = {}
    if meta["has_audio"]:
        silences, sil_info = adaptive_silences(path, dur)
    else:
        silences = []
    scenes = find_scene_cuts(path)

    if target is not None:
        boundaries = plan_by_target(dur, target, tolerance, silences,
                                    scenes, min_part)
        window = tolerance
        mode = f"по целевой длине {target:.0f} с (допуск ±{tolerance:.0f} с)"
    else:
        if window is None:
            # окно поиска — четверть средней части, но не больше 20 с
            window = min(20.0, (dur / parts) * 0.25)
        boundaries = []
        taken: list[float] = []
        for i in range(1, parts):
            ideal = dur * i / parts
            b = pick_boundary(ideal, silences, scenes, window, taken)
            boundaries.append(b)
            taken.append(b["time"])
        mode = f"на {parts} равных частей"

    edges = [0.0] + [b["time"] for b in boundaries] + [dur]
    n_parts = len(edges) - 1
    segments = []
    for i in range(n_parts):
        s, e = edges[i], edges[i + 1]
        segments.append({
            "index": i + 1,
            "start": round(s, 3),
            "end": round(e, 3),
            "duration": round(e - s, 3),
            "cut_in": boundaries[i - 1]["kind"] if i > 0 else "source_start",
            "cut_out": boundaries[i]["kind"] if i < n_parts - 1 else "source_end",
        })

    warnings = []
    for seg in segments:
        if seg["duration"] < min_part:
            warnings.append(
                f"часть {seg['index']}: {seg['duration']:.1f} с — короче минимума {min_part:.0f} с")
        if seg["duration"] > max_part:
            warnings.append(
                f"часть {seg['index']}: {seg['duration']:.1f} с — длиннее максимума {max_part:.0f} с")
    if not silences and meta["has_audio"]:
        warnings.append("тишина не найдена — резы сели на смены сцен или на жёсткий хронометраж")

    return {
        "source": meta,
        "analysis": {
            "mode": mode,
            "silences_found": len(silences),
            "silence_detection": sil_info,
            "scene_cuts_found": len(scenes),
            "search_window_sec": round(window, 2),
        },
        "segments": segments,
        "boundaries": boundaries,
        "warnings": warnings,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Анализ исходника и поиск точек реза")
    ap.add_argument("source")
    ap.add_argument("--parts", type=int, default=3)
    ap.add_argument("--target", type=float, default=None,
                    help="целевая длина части в секундах (вместо --parts)")
    ap.add_argument("--tolerance", type=float, default=12.0,
                    help="допуск поиска границы при --target")
    ap.add_argument("--window", type=float, default=None,
                    help="± секунд поиска естественной границы вокруг идеала")
    ap.add_argument("--min-part", type=float, default=20.0)
    ap.add_argument("--max-part", type=float, default=180.0)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    if not Path(args.source).exists():
        raise SystemExit(f"Нет файла: {args.source}")

    plan = build_plan(args.source, args.parts, args.window,
                      args.min_part, args.max_part,
                      args.target, args.tolerance)
    text = json.dumps(plan, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"План записан: {args.out}\n")
    print(text)
    if plan["warnings"]:
        print("\nПРЕДУПРЕЖДЕНИЯ:", file=sys.stderr)
        for w in plan["warnings"]:
            print("  -", w, file=sys.stderr)


if __name__ == "__main__":
    main()
