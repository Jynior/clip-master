#!/usr/bin/env python3
"""
banner.py — вставка анимированного баннера с хромакеем поверх вертикального ролика.

Три вещи, на которых обычно палится наложение зелёнки:
  1. Кеинг по одному порогу оставляет рваный край — нужен мягкий переход (blend).
  2. После кеинга по контуру остаётся зелёная кайма: зелёный фон подсвечивал
     края объекта. Без деспилла баннер выглядит наклеенным.
  3. Звук баннера просто подмешивают — и он спорит с голосом. Нужно приглушать
     основную дорожку под баннером.

Использование:
    python3 banner.py --probe banner.mp4
    python3 banner.py --base video.mp4 --banner banner.mp4 --at 12.0 --out out.mp4
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

# зелёный фон присланного баннера: RGB(0,255,1)
DEFAULT_KEY = "0x00FF01"
DEFAULT_SIMILARITY = 0.16   # ширина захвата оттенка
DEFAULT_BLEND = 0.06        # мягкость края
# Приглушение основной дорожки под баннером.
# Было -7 дБ, пользователь попросил тише ещё на 60% по амплитуде:
# множитель 0.4 -> 20*log10(0.4) = -7.96 дБ, итого около -15 дБ.
DEFAULT_DUCK_DB = -15.0
DEFAULT_BANNER_GAIN = -2.0  # баннер смастерен горячо (-14 LUFS), чуть ослабляем
# Потолок лимитера -2 dBFS, а не -1: alimiter ограничивает пик по сэмплам,
# а истинный пик между сэмплами бывает выше, и на -1 он выскакивал до -0.7 dBTP.
PEAK_CEILING = 0.794


def probe(path: str) -> dict:
    cp = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", path],
        capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if cp.returncode != 0:
        raise SystemExit(f"ffprobe упал на {path}:\n{cp.stderr}")
    d = json.loads(cp.stdout)
    v = next((s for s in d["streams"] if s["codec_type"] == "video"), None)
    a = next((s for s in d["streams"] if s["codec_type"] == "audio"), None)
    if v is None:
        raise SystemExit(f"В {path} нет видеодорожки.")
    return {
        "duration": float(d["format"]["duration"]),
        "width": int(v["width"]),
        "height": int(v["height"]),
        "fps": v.get("avg_frame_rate", "?"),
        "has_audio": a is not None,
        "pix_fmt": v.get("pix_fmt"),
        "has_alpha": v.get("pix_fmt") in ("yuva420p", "rgba", "yuva444p", "argb"),
    }


def key_chain(key: str, similarity: float, blend: float, despill: bool,
              despill_strength: float = 1.0) -> str:
    """
    Цепочка кеинга. colorkey сравнивает по RGB — для ровной синтетической
    зелёнки это точнее, чем chromakey по цветности.

    Деспилл: там, где зелёный выше среднего между красным и синим, прижимаем его
    к этому среднему. Зелёная кайма по контуру уходит, а собственные зелёные
    детали внутри карточки почти не страдают, потому что у них красный и синий
    не такие низкие.
    """
    parts = [
        "format=rgba",
        f"colorkey={key}:{similarity}:{blend}",
    ]
    if despill:
        k = max(0.0, min(1.0, despill_strength))
        parts.append(
            "geq="
            f"r='r(X,Y)':"
            f"g='if(gt(g(X,Y),(r(X,Y)+b(X,Y))/2),"
            f"g(X,Y)-{k}*(g(X,Y)-(r(X,Y)+b(X,Y))/2),g(X,Y))':"
            f"b='b(X,Y)':"
            f"a='alpha(X,Y)'"
        )
    return ",".join(parts)


def build_filter(base: dict, ban: dict, at: float, width_frac: float,
                 pos: str, key: str, similarity: float, blend: float,
                 despill: bool, fade: float, duck_db: float,
                 banner_gain_db: float = DEFAULT_BANNER_GAIN) -> tuple[str, str, str]:
    """Возвращает (filter_complex, video_label, audio_label)."""
    bw = int(base["width"] * width_frac)
    if bw % 2:
        bw += 1
    t0, t1 = at, at + ban["duration"]

    ypos = {
        "center": "(H-h)/2",
        "upper": "H*0.34-h/2",
        "top": "H*0.14",
        "bottom": "H*0.72",
    }.get(pos, "(H-h)/2")

    chain = key_chain(key, similarity, blend, despill)
    # анимация стартует с пустого кадра, поэтому проявление очень короткое —
    # иначе смажется первый кадр рулетки
    if fade > 0:
        chain += (f",fade=t=in:st=0:d={fade}:alpha=1"
                  f",fade=t=out:st={max(0.0, ban['duration'] - fade):.3f}:d={fade}:alpha=1")
    chain += f",scale={bw}:-2:flags=lanczos,setpts=PTS-STARTPTS+{t0}/TB"

    fc = [f"[1:v]{chain}[bn]",
          f"[0:v][bn]overlay=x=(W-w)/2:y={ypos}:"
          f"enable='between(t,{t0:.3f},{t1:.3f})':eof_action=pass[vout]"]

    if ban["has_audio"] and base["has_audio"]:
        # баннер приходит смастеренным под -14 LUFS: без ослабления сумма
        # с основной дорожкой упирается в потолок
        fc.append(f"[1:a]volume={banner_gain_db}dB,"
                  f"adelay={int(t0 * 1000)}|{int(t0 * 1000)},"
                  f"apad,atrim=0:{t1 + 0.5:.3f},asetpts=PTS-STARTPTS[ba]")
        # основную дорожку приглушаем строго в окне баннера
        fc.append(f"[0:a]volume=enable='between(t,{t0:.3f},{t1:.3f})':"
                  f"volume={duck_db}dB[bs]")
        # лимитер держит истинный пик под -1 dBTP: без него сумма клиппит
        fc.append("[bs][ba]amix=inputs=2:duration=first:dropout_transition=0:"
                  f"normalize=0,alimiter=limit={PEAK_CEILING}:attack=5:"
                  "release=60:level=disabled[aout]")
        alabel = "[aout]"
    elif base["has_audio"]:
        fc.append("[0:a]anull[aout]")
        alabel = "[aout]"
    else:
        alabel = ""

    return ";".join(fc), "[vout]", alabel


def render(base_path: str, banner_path: str, at: float, out: str,
           width_frac: float, pos: str, key: str, similarity: float,
           blend: float, despill: bool, fade: float, duck_db: float,
           draft: bool, banner_gain_db: float = DEFAULT_BANNER_GAIN) -> None:
    base = probe(base_path)
    ban = probe(banner_path)

    if at + ban["duration"] > base["duration"]:
        at = max(0.0, base["duration"] - ban["duration"])
        print(f"  баннер не помещался — сдвинут на {at:.2f} с", file=sys.stderr)

    fc, vlab, alab = build_filter(base, ban, at, width_frac, pos, key,
                                  similarity, blend, despill, fade, duck_db,
                                  banner_gain_db)

    cmd = ["ffmpeg", "-y", "-v", "error", "-i", base_path, "-i", banner_path,
           "-filter_complex", fc, "-map", vlab]
    if alab:
        cmd += ["-map", alab]
    cmd += ["-c:v", "libx264", "-preset", "veryfast" if draft else "slow",
            "-crf", "23" if draft else "20", "-pix_fmt", "yuv420p",
            "-profile:v", "high", "-level", "4.1",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
            "-movflags", "+faststart", out]

    cp = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if cp.returncode != 0:
        raise SystemExit(f"Рендер баннера упал:\n{cp.stderr[-2500:]}")
    print(f"Готово: {out}  (баннер {at:.2f}–{at + ban['duration']:.2f} с)")


def main() -> None:
    ap = argparse.ArgumentParser(description="Наложение баннера с хромакеем")
    ap.add_argument("--probe", help="показать параметры файла и выйти")
    ap.add_argument("--base")
    ap.add_argument("--banner")
    ap.add_argument("--at", type=float, default=None,
                    help="секунда начала (по умолчанию середина базового ролика)")
    ap.add_argument("--out", default="with_banner.mp4")
    ap.add_argument("--width", type=float, default=0.88, help="доля ширины кадра")
    ap.add_argument("--pos", choices=["center", "upper", "top", "bottom"],
                    default="center")
    ap.add_argument("--key", default=DEFAULT_KEY)
    ap.add_argument("--similarity", type=float, default=DEFAULT_SIMILARITY)
    ap.add_argument("--blend", type=float, default=DEFAULT_BLEND)
    ap.add_argument("--no-despill", action="store_true")
    ap.add_argument("--fade", type=float, default=0.12)
    ap.add_argument("--duck", type=float, default=DEFAULT_DUCK_DB,
                    help="приглушение основной дорожки под баннером, дБ")
    ap.add_argument("--banner-gain", type=float, default=DEFAULT_BANNER_GAIN,
                    help="ослабление звука баннера, дБ")
    ap.add_argument("--draft", action="store_true")
    args = ap.parse_args()

    if args.probe:
        print(json.dumps(probe(args.probe), ensure_ascii=False, indent=2))
        return
    if not (args.base and args.banner):
        raise SystemExit("Нужны --base и --banner (или --probe).")

    at = args.at
    if at is None:
        b, n = probe(args.base), probe(args.banner)
        at = max(0.0, (b["duration"] - n["duration"]) / 2)

    render(args.base, args.banner, at, args.out, args.width, args.pos,
           args.key, args.similarity, args.blend, not args.no_despill,
           args.fade, args.duck, args.draft, args.banner_gain)


if __name__ == "__main__":
    main()
