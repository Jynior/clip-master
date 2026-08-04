#!/usr/bin/env python3
"""
streamer.py — вертикальная двойная раскладка: сверху говорящий, снизу исходник.

Формат замерен по реальному ролику из ленты, а не придуман:
  стык панелей на 45% высоты,
  сверху вебка стримера (1080x864 при холсте 1080x1920),
  снизу посторонний исходник (1080x1056),
  субтитры по центру, центр строки примерно на 44.5% высоты —
  то есть текст сидит на стыке, в нижней кромке верхней панели.

Звук берётся у стримера: снизу идёт только картинка, её дорожка не нужна и
только мешала бы речи. Если исходник снизу короче ролика — он зацикливается.

Использование:
    python3 streamer.py --top webcam.mp4 --bottom gameplay.mp4 --out out.mp4
    python3 streamer.py --top webcam.mp4 --bottom gameplay.mp4 \\
        --captions cap.ass --dur 45 --split 0.45 --out out.mp4
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

OUT_W, OUT_H = 1080, 1920
SPLIT = 0.45              # доля высоты, на которой стык панелей
FONTS = Path(__file__).parent.parent / "fonts"

# Позиция субтитров для этой раскладки: центр строки на стыке.
# MarginV считается от низа кадра до низа текста.
CAPTION_MARGIN_V = 1014


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
    return {
        "duration": float(d["format"]["duration"]),
        "width": int(v["width"]), "height": int(v["height"]),
        "has_audio": a is not None,
    }


def panel_chain(label_in: str, label_out: str, w: int, h: int,
                focus_x: float = 0.5, focus_y: float = 0.5) -> str:
    """
    Вписывает произвольный кадр в панель w x h без полей.

    Масштабируем с запасом и обрезаем — так панель заполняется целиком.
    focus_x и focus_y задают, какую точку исходника держать в центре: для
    вебки это найденное лицо, а не геометрический центр кадра.
    """
    fx = max(0.0, min(1.0, focus_x))
    fy = max(0.0, min(1.0, focus_y))
    return (f"[{label_in}]scale={w}:{h}:force_original_aspect_ratio=increase,"
            f"crop={w}:{h}:'(iw-{w})*{fx:.3f}':'(ih-{h})*{fy:.3f}',"
            f"setsar=1[{label_out}]")


def build(top: str, bottom: str, out: str, dur: float | None,
          captions: str | None, split: float, draft: bool,
          focus_x: float, focus_y: float,
          banner: str | None = None, banner_at: float | None = None) -> dict:
    mt, mb = probe(top), probe(bottom)
    total = dur if dur is not None else mt["duration"]

    top_h = int(OUT_H * split) // 2 * 2
    bot_h = OUT_H - top_h

    parts = [
        panel_chain("0:v", "tp", OUT_W, top_h, focus_x, focus_y),
        panel_chain("1:v", "bt", OUT_W, bot_h, 0.5, 0.5),
        # склеиваем панели вертикально
        "[tp][bt]vstack=inputs=2[stack]",
    ]
    vlab = "[stack]"

    inputs = ["-i", top]
    # исходник снизу зацикливаем, если он короче
    if mb["duration"] < total - 0.05:
        inputs = ["-i", top, "-stream_loop", "-1", "-i", bottom]
    else:
        inputs = ["-i", top, "-i", bottom]

    if banner:
        mban = probe(banner)
        t0 = (total - mban["duration"]) / 2 if banner_at is None else banner_at
        t1 = t0 + mban["duration"]
        inputs += ["-i", banner]
        bw = int(OUT_W * 0.88) // 2 * 2
        parts.append(
            f"[2:v]format=rgba,colorkey=0x00FF01:0.16:0.06,"
            f"geq=r='r(X,Y)':g='if(gt(g(X,Y),(r(X,Y)+b(X,Y))/2),"
            f"g(X,Y)-1.0*(g(X,Y)-(r(X,Y)+b(X,Y))/2),g(X,Y))':"
            f"b='b(X,Y)':a='alpha(X,Y)',"
            f"fade=t=in:st=0:d=0.12:alpha=1,"
            f"fade=t=out:st={max(0.0, mban['duration'] - 0.12):.3f}:d=0.12:alpha=1,"
            f"scale={bw}:-2:flags=lanczos,setpts=PTS-STARTPTS+{t0}/TB[bn]")
        parts.append(f"{vlab}[bn]overlay=x=(W-w)/2:y=(H-h)/2:"
                     f"enable='between(t,{t0:.3f},{t1:.3f})':eof_action=pass[vb]")
        vlab = "[vb]"
    else:
        t0 = t1 = None

    if captions:
        parts.append(f"{vlab}subtitles={captions}:fontsdir={FONTS}[vout]")
    else:
        parts.append(f"{vlab}null[vout]")

    cmd = ["ffmpeg", "-nostdin", "-y", "-v", "error"] + inputs + [
        "-filter_complex", ";".join(parts),
        "-map", "[vout]", "-map", "0:a?",
        "-af", "loudnorm=I=-14:TP=-1.5:LRA=11",
        "-t", f"{total:.3f}",
        "-c:v", "libx264", "-preset", "veryfast" if draft else "slow",
        "-crf", "23" if draft else "20", "-profile:v", "high",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart", out]
    cp = subprocess.run(cmd, capture_output=True, text=True,
                        stdin=subprocess.DEVNULL)
    if cp.returncode != 0:
        raise SystemExit(f"Сборка раскладки упала:\n{cp.stderr[-2200:]}")

    return {
        "out": out,
        "duration": round(total, 2),
        "top_panel": f"{OUT_W}x{top_h}",
        "bottom_panel": f"{OUT_W}x{bot_h}",
        "split_at": f"{split * 100:.0f}% = y {top_h}",
        "bottom_looped": mb["duration"] < total - 0.05,
        "banner": None if t0 is None else f"{t0:.2f}–{t1:.2f}",
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Раскладка «стример»: вебка + исходник")
    ap.add_argument("--top", required=True, help="видео со стримером (даёт звук)")
    ap.add_argument("--bottom", required=True, help="исходник снизу (без звука)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--dur", type=float, default=None)
    ap.add_argument("--captions", default=None)
    ap.add_argument("--split", type=float, default=SPLIT,
                    help="доля высоты для стыка панелей (замер по ленте: 0.45)")
    ap.add_argument("--focus-x", type=float, default=0.5,
                    help="какую точку вебки держать в центре по ширине")
    ap.add_argument("--focus-y", type=float, default=0.5)
    ap.add_argument("--banner", default=None)
    ap.add_argument("--banner-at", type=float, default=None)
    ap.add_argument("--draft", action="store_true")
    ap.add_argument("--auto-focus", action="store_true",
                    help="взять центр лица из detect.py")
    args = ap.parse_args()

    fx, fy = args.focus_x, args.focus_y
    if args.auto_focus:
        sys.path.insert(0, str(Path(__file__).parent))
        import detect
        d = detect.analyse(args.top)
        if d.get("face_center"):
            fx, fy = d["face_center"]["x"], d["face_center"]["y"]
            print(f"лицо найдено: {fx * 100:.0f}% ширины, {fy * 100:.0f}% высоты "
                  f"— кадрирую по нему")
        else:
            print("лицо не найдено, кадрирую по центру")

    r = build(args.top, args.bottom, args.out, args.dur, args.captions,
              args.split, args.draft, fx, fy, args.banner, args.banner_at)
    print(f"{r['out']}: {r['duration']} с")
    print(f"  верх {r['top_panel']}, низ {r['bottom_panel']}, стык на {r['split_at']}")
    if r["bottom_looped"]:
        print("  исходник снизу короче ролика — зациклен")
    if r["banner"]:
        print(f"  баннер: {r['banner']} с")


if __name__ == "__main__":
    main()
