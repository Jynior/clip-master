#!/usr/bin/env python3
"""
build.py — полный конвейер: исходник -> готовые вертикальные нарезки под TikTok.

Шаги на каждую часть:
  1. рез по плану (естественные границы из plan.py)
  2. вертикальный рефрейм 9:16 с ведением субъекта (reframe.py)
  3. баннер поверх на N секунд в заданной точке, с плавным появлением
  4. двухпроходная нормализация громкости EBU R128 до -14 LUFS
  5. экспорт под TikTok (H.264 high, yuv420p, +faststart, AAC 48 кГц)
  6. QC: разрешение, длительность, громкость, чёрные кадры

Использование:
    python3 build.py source.mp4 --parts 3 --banner banner.png --out-dir out/
    python3 build.py source.mp4 --parts 3 --no-banner --draft
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import banner as bannerer       # noqa: E402
import plan as planner          # noqa: E402
import reframe as reframer      # noqa: E402

TARGET_LUFS = -14.0
TRUE_PEAK = -1.0
LRA = 11.0
OUT_W, OUT_H = 1080, 1920


def sh(cmd: list[str], why: str) -> subprocess.CompletedProcess:
    # stdin наглухо: иначе ffmpeg уходит в интерактивный режим команд и съедает
    # стандартный ввод вызывающего скрипта (например, список нарезок в while read)
    cp = subprocess.run(cmd, capture_output=True, text=True,
                        stdin=subprocess.DEVNULL)
    if cp.returncode != 0:
        raise SystemExit(f"[{why}] ffmpeg упал:\n{cp.stderr[-2000:]}")
    return cp


def cut_segment(src: str, start: float, dur: float, dst: Path, draft: bool) -> None:
    """Точный рез с перекодированием — иначе первый кадр съедет до опорного."""
    preset = "veryfast" if draft else "medium"
    crf = "23" if draft else "18"
    sh(["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.3f}", "-i", src,
        "-t", f"{dur:.3f}", "-c:v", "libx264", "-preset", preset, "-crf", crf,
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-avoid_negative_ts", "make_zero", str(dst)], "рез")


def measure_loudness(path: Path) -> dict | None:
    """Первый проход loudnorm — измерение."""
    cp = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
         "-af", f"loudnorm=I={TARGET_LUFS}:TP={TRUE_PEAK}:LRA={LRA}:print_format=json",
         "-f", "null", "-"], capture_output=True, text=True)
    m = re.findall(r"\{[^{}]*\"input_i\"[^{}]*\}", cp.stderr, re.S)
    if not m:
        return None
    try:
        return json.loads(m[-1])
    except json.JSONDecodeError:
        return None


def banner_video_chains(banner: Path, ban_meta: dict, seg_dur: float,
                        at: float | None, pos: str, width_frac: float,
                        key: str, similarity: float, blend: float,
                        despill: bool, fade: float) -> tuple[float, float, str, str]:
    """
    Видеобаннер с хромакеем: возвращает (t0, t1, видеоцепочка, ключ выхода).
    Длительность берётся из самого баннера — обрезать анимацию нельзя,
    в ней сюжет заканчивается логотипом.
    """
    hold = ban_meta["duration"]
    t0 = (seg_dur - hold) / 2 if at is None else at
    t0 = max(0.0, min(t0, max(0.0, seg_dur - hold)))
    t1 = t0 + hold

    bw = int(OUT_W * width_frac)
    if bw % 2:
        bw += 1
    ypos = {"center": "(H-h)/2", "upper": "H*0.34-h/2",
            "top": "H*0.14", "bottom": "H*0.72"}.get(pos, "(H-h)/2")

    chain = bannerer.key_chain(key, similarity, blend, despill)
    if fade > 0:
        chain += (f",fade=t=in:st=0:d={fade}:alpha=1"
                  f",fade=t=out:st={max(0.0, hold - fade):.3f}:d={fade}:alpha=1")
    chain += f",scale={bw}:-2:flags=lanczos,setpts=PTS-STARTPTS+{t0}/TB"

    fc = (f"[1:v]{chain}[bn];"
          f"[vid][bn]overlay=x=(W-w)/2:y={ypos}:"
          f"enable='between(t,{t0:.3f},{t1:.3f})':eof_action=pass[vout]")
    return t0, t1, fc, "[vout]"


def render(seg: Path, chain_reframe: str, banner: Path | None,
           ban_meta: dict | None, seg_dur: float, loud: dict | None,
           dst: Path, draft: bool, banner_at: float | None, banner_pos: str,
           banner_width: float, key: str, similarity: float, blend: float,
           despill: bool, fade: float, duck_db: float,
           banner_gain_db: float, has_audio: bool) -> None:
    vchain = chain_reframe or f"scale={OUT_W}:{OUT_H}:flags=lanczos,setsar=1"

    parts: list[str] = []
    b_in: list[str] = []
    t0 = t1 = None

    if banner is not None and ban_meta is not None:
        b_in = ["-i", str(banner)]
        t0, t1, bfc, vmap = banner_video_chains(
            banner, ban_meta, seg_dur, banner_at, banner_pos, banner_width,
            key, similarity, blend, despill, fade)
        parts.append(f"[0:v]{vchain}[vid]")
        parts.append(bfc)
    else:
        parts.append(f"[0:v]{vchain}[vout]")
        vmap = "[vout]"

    # Громкость выравниваем на основной дорожке ДО подмешивания баннера —
    # иначе loudnorm пересчитает уровень с учётом рулетки и просадит голос.
    amap = None
    if has_audio:
        if loud:
            ln = (f"loudnorm=I={TARGET_LUFS}:TP={TRUE_PEAK}:LRA={LRA}:"
                  f"measured_I={loud['input_i']}:measured_TP={loud['input_tp']}:"
                  f"measured_LRA={loud['input_lra']}:"
                  f"measured_thresh={loud['input_thresh']}:"
                  f"offset={loud.get('target_offset', '0.0')}:linear=true")
        else:
            ln = f"loudnorm=I={TARGET_LUFS}:TP={TRUE_PEAK}:LRA={LRA}"

        if banner is not None and ban_meta is not None and ban_meta["has_audio"]:
            parts.append(f"[0:a]{ln},volume=enable='between(t,{t0:.3f},{t1:.3f})':"
                         f"volume={duck_db}dB[mainA]")
            parts.append(f"[1:a]volume={banner_gain_db}dB,"
                         f"adelay={int(t0 * 1000)}|{int(t0 * 1000)},"
                         f"apad,atrim=0:{t1 + 0.5:.3f},asetpts=PTS-STARTPTS[banA]")
            parts.append("[mainA][banA]amix=inputs=2:duration=first:"
                         f"dropout_transition=0:normalize=0,"
                         f"alimiter=limit={bannerer.PEAK_CEILING}:attack=5:"
                         "release=60:level=disabled[aout]")
        else:
            parts.append(f"[0:a]{ln}[aout]")
        amap = "[aout]"

    preset = "veryfast" if draft else "slow"
    crf = "23" if draft else "20"

    cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(seg)] + b_in + [
        "-filter_complex", ";".join(parts), "-map", vmap]
    if amap:
        cmd += ["-map", amap]
    cmd += ["-c:v", "libx264", "-preset", preset, "-crf", crf,
            "-profile:v", "high", "-level", "4.1", "-pix_fmt", "yuv420p",
            "-g", "60", "-keyint_min", "60", "-sc_threshold", "0",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
            "-movflags", "+faststart", str(dst)]
    sh(cmd, "финальный рендер")
    if t0 is not None:
        print(f"  баннер: {t0:.2f}–{t1:.2f} с")


def qc(path: Path, expect_dur: float) -> dict:
    meta = planner.probe(str(path))
    checks, fails = {}, []

    checks["resolution"] = f"{meta['width']}x{meta['height']}"
    if (meta["width"], meta["height"]) != (OUT_W, OUT_H):
        fails.append(f"разрешение {checks['resolution']}, ожидалось {OUT_W}x{OUT_H}")

    checks["duration"] = round(meta["duration"], 2)
    if abs(meta["duration"] - expect_dur) > 1.0:
        fails.append(f"длительность {meta['duration']:.2f} с вместо {expect_dur:.2f} с")

    if not (3 <= meta["duration"] <= 600):
        fails.append(f"длительность {meta['duration']:.1f} с вне лимитов TikTok")

    loud = measure_loudness(path)
    if loud:
        li = float(loud["input_i"])
        tp = float(loud["input_tp"])
        checks["lufs"] = round(li, 2)
        checks["true_peak_db"] = round(tp, 2)
        if abs(li - TARGET_LUFS) > 1.5:
            fails.append(f"громкость {li:.1f} LUFS, цель {TARGET_LUFS}")
        if tp > -0.5:
            fails.append(f"истинный пик {tp:.1f} dBTP — риск клиппинга")
    else:
        checks["lufs"] = None

    cp = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
         "-vf", "blackdetect=d=0.5:pic_th=0.98", "-f", "null", "-"],
        capture_output=True, text=True)
    blacks = re.findall(r"black_start:([\d.]+) black_end:([\d.]+)", cp.stderr)
    checks["black_spans"] = len(blacks)
    if blacks:
        fails.append(f"чёрные участки: {len(blacks)} шт")

    checks["size_mb"] = round(path.stat().st_size / 1e6, 2)
    return {"file": path.name, "checks": checks,
            "verdict": "PASS" if not fails else "FAIL", "fails": fails}


def main() -> None:
    ap = argparse.ArgumentParser(description="Исходник -> вертикальные нарезки под TikTok")
    ap.add_argument("source")
    ap.add_argument("--parts", type=int, default=3)
    ap.add_argument("--target", type=float, default=None,
                    help="целевая длина части в секундах (вместо --parts)")
    ap.add_argument("--tolerance", type=float, default=12.0)
    ap.add_argument("--out-dir", default="out")
    ap.add_argument("--banner", default=None, help="PNG с альфой")
    ap.add_argument("--no-banner", action="store_true")
    ap.add_argument("--banner-at", type=float, default=None,
                    help="секунда начала баннера внутри части (по умолчанию середина)")
    ap.add_argument("--banner-pos",
                    choices=["center", "upper", "top", "bottom"], default="upper")
    ap.add_argument("--banner-key", default=bannerer.DEFAULT_KEY)
    ap.add_argument("--banner-similarity", type=float,
                    default=bannerer.DEFAULT_SIMILARITY)
    ap.add_argument("--banner-blend", type=float, default=bannerer.DEFAULT_BLEND)
    ap.add_argument("--no-despill", action="store_true")
    ap.add_argument("--banner-fade", type=float, default=0.12)
    ap.add_argument("--duck", type=float, default=bannerer.DEFAULT_DUCK_DB)
    ap.add_argument("--banner-gain", type=float,
                    default=bannerer.DEFAULT_BANNER_GAIN)
    ap.add_argument("--banner-width", type=float, default=0.82,
                    help="доля ширины кадра")
    ap.add_argument("--no-reframe", action="store_true")
    ap.add_argument("--draft", action="store_true", help="быстрый черновик")
    ap.add_argument("--keep-temp", action="store_true")
    args = ap.parse_args()

    src = Path(args.source)
    if not src.exists():
        raise SystemExit(f"Нет файла: {src}")
    banner = None if args.no_banner else (Path(args.banner) if args.banner else None)
    if banner and not banner.exists():
        raise SystemExit(f"Нет баннера: {banner}")
    ban_meta = bannerer.probe(str(banner)) if banner else None
    if ban_meta:
        print(f"Баннер: {ban_meta['width']}x{ban_meta['height']}, "
              f"{ban_meta['duration']:.2f} с, "
              f"звук: {'есть' if ban_meta['has_audio'] else 'нет'}")

    out_dir = Path(args.out_dir)
    tmp = out_dir / "_tmp"
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp.mkdir(exist_ok=True)

    t_start = time.time()
    print(f"Анализ {src.name} …")
    p = planner.build_plan(str(src), args.parts, None, 20.0, 180.0,
                           args.target, args.tolerance)
    (out_dir / "plan.json").write_text(
        json.dumps(p, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"  {p['source']['width']}x{p['source']['height']}, "
          f"{p['source']['duration']:.1f} с, {p['source']['fps']} fps")
    print(f"  тишин: {p['analysis']['silences_found']}, "
          f"смен сцен: {p['analysis']['scene_cuts_found']}")
    for s in p["segments"]:
        print(f"  часть {s['index']}: {s['start']:.2f}–{s['end']:.2f} "
              f"({s['duration']:.1f} с, границы: {s['cut_in']}/{s['cut_out']})")
    for w in p["warnings"]:
        print(f"  ! {w}")

    reports = []
    for seg in p["segments"]:
        i = seg["index"]
        print(f"\n— часть {i}/{args.parts} —")
        raw = tmp / f"seg{i}.mp4"
        cut_segment(str(src), seg["start"], seg["duration"], raw, args.draft)
        print("  рез готов")

        chain = ""
        if not args.no_reframe:
            r = reframer.compute(str(raw))
            if r["needed"]:
                traj = tmp / f"seg{i}.traj.txt"
                reframer.write_sendcmd(r, traj)
                chain = reframer.filter_chain(r, traj, OUT_W, OUT_H)
                st = r["stats"]
                print(f"  рефрейм: кроп {r['crop_w']}px, ход камеры "
                      f"{st['total_travel_px']} px, x {st['x_min']}..{st['x_max']}")
            else:
                print(f"  рефрейм пропущен: {r.get('reason')}")

        loud = measure_loudness(raw)
        if loud:
            print(f"  громкость исходной части: {float(loud['input_i']):.1f} LUFS")

        dst = out_dir / f"{src.stem}_part{i}.mp4"
        render(raw, chain, banner, ban_meta, seg["duration"], loud, dst,
               args.draft, args.banner_at, args.banner_pos, args.banner_width,
               args.banner_key, args.banner_similarity, args.banner_blend,
               not args.no_despill, args.banner_fade, args.duck,
               args.banner_gain, p["source"]["has_audio"])
        print(f"  рендер: {dst.name}")

        rep = qc(dst, seg["duration"])
        reports.append(rep)
        mark = "OK" if rep["verdict"] == "PASS" else "ПРОВАЛ"
        print(f"  QC {mark}: {rep['checks']}")
        for f in rep["fails"]:
            print(f"    ! {f}")

    (out_dir / "qc.json").write_text(
        json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")
    if not args.keep_temp:
        shutil.rmtree(tmp, ignore_errors=True)

    ok = sum(1 for r in reports if r["verdict"] == "PASS")
    print(f"\nГотово за {time.time() - t_start:.0f} с: {ok}/{len(reports)} прошли QC")
    print(f"Файлы: {out_dir.resolve()}")


if __name__ == "__main__":
    main()
