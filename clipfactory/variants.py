#!/usr/bin/env python3
"""
variants.py — рендерит один и тот же участок в нескольких стилях кадрирования,
чтобы выбрать стиль вживую, а не на словах.

Ключевое отличие от reframe.py: здесь кроп СТАТИЧЕН. Позиция считается один раз
по всему участку (или по сцене) и держится. Так делают FrameShift и
Autocrop-vertical: дрожание и паразитный дрейф исключены архитектурно, а не
подавляются сглаживанием.

Использование:
    python3 variants.py src.mp4 --start 8 --dur 10 --out-dir variants/
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import reframe as R  # noqa: E402

OUT_W, OUT_H = 1080, 1920

# безопасные зоны TikTok на холсте 1080x1920 (сводка из разведки)
SAFE_TOP = 250        # сверху перекрыто вкладками
SAFE_BOTTOM = 1460    # ниже — ник, подпись, трек, прогресс
SAFE_RIGHT = 1000     # правее — кнопки лайк/коммент/репост
HOOK_Y = 360          # крупный текст хука: верхняя треть
CAPTION_Y = 1180      # субтитры: центральный пояс


def sh(cmd: list[str], why: str) -> None:
    cp = subprocess.run(cmd, capture_output=True, text=True,
                        stdin=subprocess.DEVNULL)
    if cp.returncode != 0:
        raise SystemExit(f"[{why}] упало:\n{cp.stderr[-1800:]}")


def static_best_x(path: str, crop_w: int, src_w: int) -> dict:
    """
    Одна позиция кропа на весь участок.

    Берём энергию по столбцам (контраст + движение), суммируем её по всем
    кадрам участка и ищем окно шириной crop_w с максимумом суммарной энергии.
    Получается «где по совокупности всего отрезка происходит главное».
    """
    meta = R.probe(path)
    sample_h = int(round(R.SAMPLE_W * meta["height"] / meta["width"]))
    if sample_h % 2:
        sample_h += 1

    frames = R.sample_gray(path, sample_h)
    energy = R.column_energy(frames)          # (n, w)
    total = energy.sum(axis=0)                # энергия по столбцам за весь участок

    scale = src_w / R.SAMPLE_W
    win = max(2, int(round(crop_w / scale)))
    if win >= total.shape[0]:
        return {"x": (src_w - crop_w) // 2, "confidence": 0.0}

    csum = np.concatenate([[0.0], np.cumsum(total)])
    sums = csum[win:] - csum[:-win]
    best_start = int(sums.argmax())
    best_x = int(round(best_start * scale))
    best_x = max(0, min(best_x, src_w - crop_w))

    center_x = (src_w - crop_w) // 2
    # насколько выбор лучше центрального кропа
    cs = int(round(center_x / scale))
    cs = max(0, min(cs, len(sums) - 1))
    gain = float(sums[best_start] / sums[cs]) if sums[cs] > 0 else 1.0

    return {"x": best_x, "center_x": center_x, "gain_vs_center": round(gain, 3)}


def write_hook_ass(path: Path, text: str, dur: float) -> None:
    """Хук крупным текстом в верхней трети. drawtext в сборке нет — только libass."""
    ass = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {OUT_W}
PlayResY: {OUT_H}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Hook,DejaVu Sans,86,&H00FFFFFF,&H0000FFFF,&H00000000,&H90000000,1,0,0,0,100,100,0,0,1,6,2,8,60,60,{HOOK_Y},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,{int(dur//3600)}:{int(dur%3600//60):02d}:{dur%60:05.2f},Hook,,0,0,0,,{text}
"""
    path.write_text(ass, encoding="utf-8")


def render_variants(src: str, start: float, dur: float, out_dir: Path,
                    hook_text: str) -> list[dict]:
    out_dir.mkdir(parents=True, exist_ok=True)

    # вырезаем участок один раз, дальше работаем с ним
    seg = out_dir / "_seg.mp4"
    sh(["ffmpeg", "-nostdin", "-y", "-v", "error", "-ss", f"{start}", "-i", src,
        "-t", f"{dur}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", str(seg)], "рез участка")

    meta = R.probe(str(seg))
    W, H = meta["width"], meta["height"]
    crop_w = int(round(H * 9 / 16))
    if crop_w % 2:
        crop_w += 1

    best = static_best_x(str(seg), crop_w, W)
    cx = best["center_x"]
    bx = best["x"]

    blur_bg = (f"split[bg][fg];"
               f"[bg]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,"
               f"crop={OUT_W}:{OUT_H},boxblur=luma_radius=42:luma_power=3[bgb];"
               f"[fg]scale={OUT_W}:-2:flags=lanczos[fgs];"
               f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2,setsar=1")

    hook = out_dir / "hook.ass"
    write_hook_ass(hook, hook_text, dur)

    variants = [
        ("01_статичный_центр",
         f"crop={crop_w}:{H}:{cx}:0,scale={OUT_W}:{OUT_H}:flags=lanczos,setsar=1",
         "Фиксированный центральный кроп. Ничего не двигается."),

        ("02_статичный_умный",
         f"crop={crop_w}:{H}:{bx}:0,scale={OUT_W}:{OUT_H}:flags=lanczos,setsar=1",
         f"Позиция выбрана по энергии за весь участок (x={bx} вместо центра {cx}), держится неподвижно."),

        ("03_леттербокс_размытый",
         blur_bg,
         "Весь кадр 16:9 целиком, поля залиты размытой копией. Ничего не обрезано."),

        ("04_леттербокс_с_хуком",
         blur_bg + f",ass='{hook}'",
         "То же плюс крупный хук в верхней трети, в безопасной зоне."),

        ("05_умный_плюс_медленный_зум",
         (f"crop={crop_w}:{H}:{bx}:0,"
          f"scale={int(OUT_W*1.14)}:-2:flags=lanczos,"
          f"zoompan=z='1+0.055*on/{max(1,int(dur*30))}':d=1:"
          f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={OUT_W}x{OUT_H},setsar=1"),
         "Умный статичный кроп плюс очень медленное приближение. Движение есть, но осмысленное."),

        ("06_леттербокс_зум_хук",
         blur_bg + f",scale={int(OUT_W*1.06)}:-2,crop={OUT_W}:{OUT_H},ass='{hook}'",
         "Леттербокс, слегка укрупнённый, с хуком."),
    ]

    made = []
    for name, vf, note in variants:
        dst = out_dir / f"{name}.mp4"
        sh(["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", str(seg),
            "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
            "-pix_fmt", "yuv420p", "-c:a", "copy",
            "-movflags", "+faststart", str(dst)], f"вариант {name}")
        made.append({"name": name, "file": str(dst), "note": note})
        print(f"  готов: {name}")

    print(f"\nстатичный умный кроп: x={bx}, центр был бы {cx}, "
          f"выигрыш по энергии {best['gain_vs_center']}x")
    return made


def main() -> None:
    ap = argparse.ArgumentParser(description="Варианты кадрирования одного участка")
    ap.add_argument("source")
    ap.add_argument("--start", type=float, required=True)
    ap.add_argument("--dur", type=float, default=10.0)
    ap.add_argument("--out-dir", default="variants")
    ap.add_argument("--hook", default="ЦЕНА НАКЛЕЕК 1760 ₽")
    args = ap.parse_args()
    render_variants(args.source, args.start, args.dur,
                    Path(args.out_dir), args.hook)


if __name__ == "__main__":
    main()
