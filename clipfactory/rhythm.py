#!/usr/bin/env python3
"""
rhythm.py — финальная сборка ролика в зафиксированном стиле.

Стиль: леттербокс 100% ширины с размытым фоном, субтитры Hormozi над полосой
видео, склейки каждые ~3.5 с.

«Склейка» здесь — не удаление материала, а смена кадрирования: чередуются
широкий план (весь кадр в леттербоксе) и приближённый (статичный умный кроп).
Переключения сажаются на речевые паузы, поэтому воспринимаются как монтаж, а не
как случайный рывок. Обе версии считаются одним проходом ffmpeg и переключаются
через enable у overlay — звук при этом не режется и швов не даёт.

Использование:
    python3 rhythm.py src.mp4 --dur 30 --captions cap.ass --out out.mp4
"""
from __future__ import annotations

import argparse
import re
import subprocess

import numpy as np
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import banner as bannerer  # noqa: E402
import captions as C   # noqa: E402
import reframe as R    # noqa: E402
import variants as V   # noqa: E402

OUT_W, OUT_H = 1080, 1920
FONTS = Path(__file__).parent.parent / "fonts"


def detect_bars(path: str, dur: float) -> str | None:
    """
    Собственные чёрные поля исходника.

    Записи экрана часто уже летербоксированы. Если такой кадр вписать в
    вертикаль с размытым фоном, поля наложатся на поля: размытие чёрного даёт
    чёрное, и сверху-снизу вылезают широкие чёрные полосы. Поэтому сначала
    срезаем то, что автор уже добавил, и работаем с реальной картинкой.

    Возвращает строку crop=... или None, если полей нет.
    """
    probes = [dur * f for f in (0.15, 0.35, 0.55, 0.75)]
    found: list[tuple[int, int, int, int]] = []
    for t in probes:
        cp = subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "info", "-ss", f"{t:.2f}", "-i", path,
             "-t", "1.2", "-vf", "cropdetect=limit=24:round=2:reset=0",
             "-f", "null", "-"],
            capture_output=True, text=True, stdin=subprocess.DEVNULL)
        for m in re.finditer(r"crop=(\d+):(\d+):(\d+):(\d+)", cp.stderr):
            found.append(tuple(int(g) for g in m.groups()))
    if not found:
        return None
    # берём самый ЩЕДРЫЙ кроп из найденных: лучше оставить лишнее,
    # чем срезать картинку на кадре, который случайно оказался тёмным
    w = max(f[0] for f in found)
    h = max(f[1] for f in found)
    meta = R.probe(path)
    if w >= meta["width"] - 4 and h >= meta["height"] - 4:
        return None
    x = (meta["width"] - w) // 2
    y = (meta["height"] - h) // 2
    return f"crop={w}:{h}:{x}:{y}"


def letterboxed_map(path: str, fps: float = 2.0) -> tuple[np.ndarray, float]:
    """
    Для каждого момента — летербоксирован ли исходный кадр сам по себе.

    Приближение на таком кадре и есть источник «чёрных полос»: они вылезают
    на контрасте с остальным роликом, где картинка занимает весь кадр.
    """
    cp = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-i", path,
         "-vf", f"fps={fps},scale=160:90", "-pix_fmt", "gray",
         "-f", "rawvideo", "-"],
        capture_output=True, stdin=subprocess.DEVNULL)
    buf = np.frombuffer(cp.stdout, dtype=np.uint8)
    n = len(buf) // (160 * 90)
    if n == 0:
        return np.zeros(0, dtype=bool), fps
    fr = buf[: n * 160 * 90].reshape(n, 90, 160).astype(np.float32)
    top = fr[:, :11].mean(axis=(1, 2))
    bot = fr[:, -11:].mean(axis=(1, 2))
    return (top < 16) & (bot < 24), fps


def block_is_letterboxed(mask: np.ndarray, fps: float,
                         a: float, b: float, thresh: float = 0.25) -> bool:
    """Блок считаем летербоксированным, если такова заметная часть его кадров."""
    if mask.size == 0:
        return False
    i0, i1 = int(a * fps), max(int(b * fps), int(a * fps) + 1)
    seg = mask[i0:i1]
    return bool(seg.size and seg.mean() >= thresh)


def blocks(dur: float, runs: list[tuple[float, float]],
           target: float = 3.5, snap: float = 1.2) -> list[tuple[float, float]]:
    """Границы блоков: каждые ~target секунд, посаженные на ближайшую паузу."""
    pauses = []
    for i in range(len(runs) - 1):
        pauses.append((runs[i][1] + runs[i + 1][0]) / 2)

    edges = [0.0]
    while dur - edges[-1] > target * 1.5:
        ideal = edges[-1] + target
        near = [p for p in pauses if abs(p - ideal) <= snap and p > edges[-1] + 1.2]
        edges.append(min(near, key=lambda p: abs(p - ideal)) if near else ideal)
    edges.append(dur)
    return [(edges[i], edges[i + 1]) for i in range(len(edges) - 1)]


def build(src: str, dur: float, cap_ass: str | None, out: str,
          punch_first: bool, target: float, draft: bool,
          banner: str | None = None, banner_at: float | None = None,
          banner_pos: str = "upper", banner_width: float = 0.88,
          duck_db: float = bannerer.DEFAULT_DUCK_DB,
          banner_gain_db: float = bannerer.DEFAULT_BANNER_GAIN,
          motion: bool = True, trim_bars: bool = False,
          fill: bool = False, every_nth: int = 2) -> None:
    seg = Path(out).with_suffix(".seg.mp4")
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", src,
                    "-t", f"{dur}", "-c:v", "libx264", "-preset", "veryfast",
                    "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-b:a", "192k", "-ar", "48000", str(seg)],
                   check=True, stdin=subprocess.DEVNULL)

    wav = Path(out).with_suffix(".wav")
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", str(seg),
                    "-vn", "-ac", "1", "-ar", "16000", str(wav)],
                   check=True, stdin=subprocess.DEVNULL)

    runs = C.speech_runs(str(wav))
    bl = blocks(dur, runs, target)

    meta = R.probe(str(seg))
    crop_w = int(round(meta["height"] * 9 / 16))
    if crop_w % 2:
        crop_w += 1
    best = V.static_best_x(str(seg), crop_w, meta["width"])

    # Срезаем чёрные поля, которые автор уже добавил сам. Без этого размытие
    # чёрного даёт чёрное, и в кадре появляются широкие чёрные полосы.
    bars = detect_bars(str(seg), dur) if trim_bars else None
    pre = f"{bars}," if bars else ""

    wide = ("split[bgw][fgw];"
            f"[bgw]{pre}scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,"
            f"crop={OUT_W}:{OUT_H},boxblur=luma_radius=42:luma_power=3[bgwb];"
            f"[fgw]{pre}scale={OUT_W}:-2:flags=lanczos[fgws];"
            f"[bgwb][fgws]overlay=(W-w)/2:(H-h)/2,setsar=1")

    # окно приближения: 68% кадра, сохраняя 16:9, по центру найденной активности
    zw = int(meta["width"] * 0.68) // 2 * 2
    zh = int(meta["height"] * 0.68) // 2 * 2
    zx = max(0, min(best["x"] + crop_w // 2 - zw // 2, meta["width"] - zw))
    zy = (meta["height"] - zh) // 2
    punch = ("split[bgp][fgp];"
             f"[bgp]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,"
             f"crop={OUT_W}:{OUT_H},boxblur=luma_radius=42:luma_power=3[bgpb];"
             f"[fgp]crop={zw}:{zh}:{zx}:{zy},scale={OUT_W}:-2:flags=lanczos[fgps];"
             f"[bgpb][fgps]overlay=(W-w)/2:(H-h)/2,setsar=1")

    # По умолчанию кадр статичен: одна позиция на весь ролик, никаких зумов.
    # Смена планов включается только явным флагом.
    # Заполняющий кадр: одна статичная позиция кропа на весь ролик, картинка
    # занимает весь экран. Полей нет ни при каком содержимом источника —
    # именно из-за них леттербокс и даёт чёрные полосы на тёмных кадрах.
    fill_chain = (f"{pre}crop={crop_w}:{meta['height']}:{best['x']}:0,"
                  f"scale={OUT_W}:{OUT_H}:flags=lanczos,setsar=1")

    skipped_dark = 0
    if fill:
        on = []
        fc = f"[0:v]{fill_chain}[vv]"
    elif motion:
        # Чередование планов через блок — конфигурация, на которой собран
        # FULL_p1: статичная полоса видео, приближение внутри неё.
        on = [b for i, b in enumerate(bl) if (i % 2 == 0) == punch_first]
        enable = "+".join(f"between(t,{s:.3f},{e:.3f})" for s, e in on) or "0"
        fc = (f"[0:v]split=2[a][b];[a]{wide}[wide];[b]{punch}[punch];"
              f"[wide][punch]overlay=0:0:enable='{enable}'[vv]")
    else:
        on = []
        fc = f"[0:v]{wide}[vv]"

    # Баннер накладывается ПОСЛЕ смены планов, но ДО субтитров: субтитры должны
    # оставаться поверх всего, иначе баннер их перекроет.
    b_in: list[str] = []
    ban_meta = None
    t0 = t1 = None
    if banner:
        ban_meta = bannerer.probe(banner)
        hold = ban_meta["duration"]
        t0 = (dur - hold) / 2 if banner_at is None else banner_at
        t0 = max(0.0, min(t0, max(0.0, dur - hold)))
        t1 = t0 + hold
        b_in = ["-i", banner]
        bw = int(OUT_W * banner_width) // 2 * 2
        ypos = {"center": "(H-h)/2", "upper": "H*0.34-h/2",
                "top": "H*0.14", "bottom": "H*0.72"}.get(banner_pos, "(H-h)/2")
        key = bannerer.key_chain(bannerer.DEFAULT_KEY,
                                 bannerer.DEFAULT_SIMILARITY,
                                 bannerer.DEFAULT_BLEND, True)
        key += (",fade=t=in:st=0:d=0.12:alpha=1"
                f",fade=t=out:st={max(0.0, hold - 0.12):.3f}:d=0.12:alpha=1")
        key += f",scale={bw}:-2:flags=lanczos,setpts=PTS-STARTPTS+{t0}/TB"
        fc += (f";[1:v]{key}[bn];[vv][bn]overlay=x=(W-w)/2:y={ypos}:"
               f"enable='between(t,{t0:.3f},{t1:.3f})':eof_action=pass[vb]")
        vlab = "[vb]"
    else:
        vlab = "[vv]"

    if cap_ass:
        fc += f";{vlab}subtitles={cap_ass}:fontsdir={FONTS}[vout]"
    else:
        fc += f";{vlab}null[vout]"

    # Звук: выравниваем громкость основной дорожки, приглушаем её под баннером,
    # подмешиваем звук баннера и держим пик лимитером.
    if banner and ban_meta and ban_meta["has_audio"]:
        fc += (f";[0:a]loudnorm=I=-14:TP=-1.5:LRA=11,"
               f"volume=enable='between(t,{t0:.3f},{t1:.3f})':volume={duck_db}dB[mainA]"
               f";[1:a]volume={banner_gain_db}dB,"
               f"adelay={int(t0 * 1000)}|{int(t0 * 1000)},apad,"
               f"atrim=0:{t1 + 0.5:.3f},asetpts=PTS-STARTPTS[banA]"
               f";[mainA][banA]amix=inputs=2:duration=first:dropout_transition=0:"
               f"normalize=0,alimiter=limit={bannerer.PEAK_CEILING}:attack=5:"
               f"release=60:level=disabled[aout]")
        amap = "[aout]"
        afilter: list[str] = []
    else:
        amap = "0:a"
        afilter = ["-af", "loudnorm=I=-14:TP=-1.5:LRA=11"]

    cmd = ["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", str(seg)] + b_in + [
        "-filter_complex", fc, "-map", "[vout]", "-map", amap] + afilter + [
        "-c:v", "libx264", "-preset", "veryfast" if draft else "slow",
        "-crf", "23" if draft else "20", "-profile:v", "high",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
        "-ar", "48000", "-movflags", "+faststart", out]
    cp = subprocess.run(cmd, capture_output=True, text=True,
                        stdin=subprocess.DEVNULL)
    if cp.returncode != 0:
        raise SystemExit(f"рендер упал:\n{cp.stderr[-2000:]}")

    seg.unlink(missing_ok=True)
    wav.unlink(missing_ok=True)

    if fill:
        print(f"{out}: {dur:.0f} с, заполняющий кадр, статика, "
              f"кроп x={best['x']} на весь ролик")
    elif motion:
        print(f"{out}: {dur:.0f} с, блоков {len(bl)}, приближённых {len(on)}, "
              f"кроп x={best['x']}")
        print("  границы: " + ", ".join(f"{s:.1f}" for s, _ in bl[1:]))
    else:
        print(f"{out}: {dur:.0f} с, леттербокс, статика, движения нет")
    if bars:
        print(f"  срезаны собственные поля источника: {bars}")
    if t0 is not None:
        print(f"  баннер: {t0:.1f}–{t1:.1f} с, основной звук приглушён "
              f"на {abs(duck_db):.0f} дБ")


def main() -> None:
    ap = argparse.ArgumentParser(description="Финальная сборка со склейками")
    ap.add_argument("source")
    ap.add_argument("--dur", type=float, required=True)
    ap.add_argument("--captions", default=None)
    ap.add_argument("--out", required=True)
    ap.add_argument("--target", type=float, default=3.5)
    ap.add_argument("--punch-first", action="store_true")
    ap.add_argument("--draft", action="store_true")
    ap.add_argument("--banner", default=None, help="видео баннера с хромакеем")
    ap.add_argument("--banner-at", type=float, default=None,
                    help="секунда показа баннера (по умолчанию середина)")
    ap.add_argument("--banner-pos",
                    choices=["center", "upper", "top", "bottom"], default="upper")
    ap.add_argument("--banner-width", type=float, default=0.88)
    ap.add_argument("--duck", type=float, default=bannerer.DEFAULT_DUCK_DB,
                    help="приглушение основной дорожки под баннером, дБ")
    ap.add_argument("--banner-gain", type=float,
                    default=bannerer.DEFAULT_BANNER_GAIN)
    ap.add_argument("--no-motion", action="store_true",
                    help="полностью статичный кадр, без приближений")
    ap.add_argument("--every-nth", type=int, default=3,
                    help="приближение на каждом N-м блоке; больше = реже")
    ap.add_argument("--keep-bars", action="store_true",
                    help="не срезать собственные чёрные поля источника")
    ap.add_argument("--fill", action="store_true",
                    help="заполняющий кадр без леттербокса: полей не бывает вовсе")
    args = ap.parse_args()
    build(args.source, args.dur, args.captions, args.out,
          args.punch_first, args.target, args.draft,
          args.banner, args.banner_at, args.banner_pos,
          args.banner_width, args.duck, args.banner_gain,
          not args.no_motion, not args.keep_bars, args.fill,
          args.every_nth)


if __name__ == "__main__":
    main()
