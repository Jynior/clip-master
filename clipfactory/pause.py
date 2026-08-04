#!/usr/bin/env python3
"""
pause.py — вставляет остановку основного видео под рекламный баннер.

Раньше баннер шёл поверх идущего видео, а основной звук приглушался. Это не то:
зритель продолжает следить за сюжетом и баннер проскакивает мимо. Здесь основное
видео замирает на стоп-кадре, баннер проходит целиком, и только потом сюжет
продолжается с того же места. Ничего не теряется.

Как сделано. В исходник вставляется стоп-кадр длиной ровно в баннер, со тишиной
на этом участке. Дальше по конвейеру идёт уже удлинённый исходник — поэтому и
словные тайминги, и субтитры считаются от него, и никакой сдвижки таймингов
руками не нужно. Баннер потом накладывается ровно в окно паузы, со своим звуком.

Использование:
    python3 pause.py src.mp4 --at 29.2 --hold 6.0 --out src_paused.mp4
    python3 pause.py src.mp4 --at 29.2 --banner banner.mp4 --out src_paused.mp4
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


def probe(path: str) -> dict:
    cp = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", path],
        capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if cp.returncode != 0:
        raise SystemExit(f"ffprobe упал на {path}:\n{cp.stderr}")
    d = json.loads(cp.stdout)
    v = next(s for s in d["streams"] if s["codec_type"] == "video")
    a = next((s for s in d["streams"] if s["codec_type"] == "audio"), None)
    fps = 30.0
    if v.get("avg_frame_rate", "0/0") not in ("0/0", ""):
        num, _, den = v["avg_frame_rate"].partition("/")
        if float(den or 1):
            fps = float(num) / float(den or 1)
    return {
        "duration": float(d["format"]["duration"]),
        "width": int(v["width"]), "height": int(v["height"]),
        "fps": round(fps, 3),
        "has_audio": a is not None,
        "sample_rate": int(a["sample_rate"]) if a else 48000,
    }


def sh(cmd: list[str], why: str) -> None:
    cp = subprocess.run(cmd, capture_output=True, text=True,
                        stdin=subprocess.DEVNULL)
    if cp.returncode != 0:
        raise SystemExit(f"[{why}] упало:\n{cp.stderr[-1800:]}")


def insert_pause(src: str, at: float, hold: float, out: str,
                 draft: bool = False) -> dict:
    meta = probe(src)
    if at <= 0 or at >= meta["duration"]:
        raise SystemExit(f"Точка паузы {at} вне ролика (0..{meta['duration']:.1f})")

    tmp = Path(out).parent / f"_pause_{Path(out).stem}"
    tmp.mkdir(parents=True, exist_ok=True)
    preset = "veryfast" if draft else "medium"
    crf = "20" if draft else "18"
    sr = meta["sample_rate"]

    vargs = ["-c:v", "libx264", "-preset", preset, "-crf", crf,
             "-pix_fmt", "yuv420p", "-r", str(meta["fps"])]
    aargs = ["-c:a", "aac", "-b:a", "192k", "-ar", str(sr), "-ac", "2"]

    # 1. до точки паузы
    part_a = tmp / "a.mp4"
    sh(["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", src, "-t", f"{at:.3f}"]
       + vargs + aargs + [str(part_a)], "часть до паузы")

    # 2. стоп-кадр ровно в точке паузы, с тишиной
    still = tmp / "still.png"
    sh(["ffmpeg", "-nostdin", "-y", "-v", "error", "-ss", f"{at:.3f}",
        "-i", src, "-frames:v", "1", str(still)], "стоп-кадр")
    part_p = tmp / "p.mp4"
    sh(["ffmpeg", "-nostdin", "-y", "-v", "error",
        "-loop", "1", "-framerate", str(meta["fps"]), "-i", str(still),
        "-f", "lavfi", "-i", f"anullsrc=channel_layout=stereo:sample_rate={sr}",
        "-t", f"{hold:.3f}",
        "-vf", f"scale={meta['width']}:{meta['height']},setsar=1"]
       + vargs + aargs + [str(part_p)], "пауза")

    # 3. после точки паузы
    part_b = tmp / "b.mp4"
    sh(["ffmpeg", "-nostdin", "-y", "-v", "error", "-ss", f"{at:.3f}", "-i", src]
       + vargs + aargs + [str(part_b)], "часть после паузы")

    lst = tmp / "list.txt"
    lst.write_text("".join(f"file '{p.resolve()}'\n"
                           for p in (part_a, part_p, part_b)), encoding="utf-8")
    sh(["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "concat",
        "-safe", "0", "-i", str(lst)] + vargs + aargs
       + ["-movflags", "+faststart", out], "сборка")

    for p in (part_a, part_p, part_b, still, lst):
        p.unlink(missing_ok=True)
    tmp.rmdir()

    res = probe(out)
    return {
        "source_duration": round(meta["duration"], 2),
        "out_duration": round(res["duration"], 2),
        "pause_from": round(at, 2),
        "pause_to": round(at + hold, 2),
        "hold": round(hold, 2),
        "grew_by": round(res["duration"] - meta["duration"], 2),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Остановка видео под баннер")
    ap.add_argument("source")
    ap.add_argument("--at", type=float, required=True,
                    help="секунда, на которой видео замирает")
    ap.add_argument("--hold", type=float, default=None,
                    help="длина паузы; по умолчанию берётся из --banner")
    ap.add_argument("--banner", default=None,
                    help="видео баннера — длина паузы берётся из него")
    ap.add_argument("--out", required=True)
    ap.add_argument("--draft", action="store_true")
    args = ap.parse_args()

    hold = args.hold
    if hold is None:
        if not args.banner:
            raise SystemExit("Нужен либо --hold, либо --banner.")
        hold = probe(args.banner)["duration"]

    r = insert_pause(args.source, args.at, hold, args.out, args.draft)
    print(f"{args.out}")
    print(f"  было {r['source_duration']:.2f} с -> стало {r['out_duration']:.2f} с "
          f"(+{r['grew_by']:.2f})")
    print(f"  видео стоит с {r['pause_from']:.2f} по {r['pause_to']:.2f} с, "
          f"на этом участке тишина")


if __name__ == "__main__":
    main()
