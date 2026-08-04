#!/usr/bin/env python3
"""
reframe.py — вертикальный рефрейм 16:9 -> 9:16 с ведением субъекта.

Центральный кроп режет головы и теряет то, ради чего смотрят. Здесь окно кропа
едет за субъектом: по каждому столбцу кадра считается энергия движения и
контраста, окно ставится туда, где её больше, траектория сглаживается, чтобы
кадр не дёргался.

Выдаёт sendcmd-файл (crop x во времени) + готовые фильтры для ffmpeg.

Использование:
    python3 reframe.py in.mp4 --out-cmds traj.txt --print-filter
    python3 reframe.py in.mp4 --render out.mp4          # сразу отрендерить
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import numpy as np

SAMPLE_W = 320          # ширина анализируемого кадра
SAMPLE_FPS = 4.0        # частота выборки траектории
SMOOTH_SEC = 2.5        # окно сглаживания
DEADZONE_FRAC = 0.06    # не двигаться, пока смещение меньше доли ширины кропа
MAX_SPEED_FRAC = 0.28   # максимум смещения за секунду, в долях ширины кропа


def probe(path: str) -> dict:
    cp = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-print_format", "json",
         "-show_entries", "stream=width,height,avg_frame_rate:format=duration", path],
        capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if cp.returncode != 0:
        raise SystemExit(f"ffprobe упал на {path}:\n{cp.stderr}")
    d = json.loads(cp.stdout)
    st = d["streams"][0]
    return {
        "width": int(st["width"]),
        "height": int(st["height"]),
        "duration": float(d["format"]["duration"]),
    }


def sample_gray(path: str, sample_h: int) -> np.ndarray:
    """Читает видео как серые кадры SAMPLE_W x sample_h -> массив (n, h, w)."""
    cp = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path,
         "-vf", f"fps={SAMPLE_FPS},scale={SAMPLE_W}:{sample_h}",
         "-pix_fmt", "gray", "-f", "rawvideo", "-"],
        capture_output=True, stdin=subprocess.DEVNULL)
    if cp.returncode != 0:
        raise SystemExit(f"ffmpeg не смог выбрать кадры:\n{cp.stderr.decode()[-800:]}")
    buf = np.frombuffer(cp.stdout, dtype=np.uint8)
    frame_px = SAMPLE_W * sample_h
    n = len(buf) // frame_px
    if n == 0:
        raise SystemExit("Не получено ни одного кадра для анализа.")
    return buf[: n * frame_px].reshape(n, sample_h, SAMPLE_W).astype(np.float32)


def column_energy(frames: np.ndarray) -> np.ndarray:
    """Энергия по столбцам: контраст (градиент) + движение (кадровая разница)."""
    # пространственный градиент по X
    gx = np.abs(np.diff(frames, axis=2))                    # (n, h, w-1)
    contrast = np.zeros((frames.shape[0], frames.shape[2]), dtype=np.float32)
    contrast[:, :-1] = gx.sum(axis=1)

    # временная разница
    motion = np.zeros_like(contrast)
    if frames.shape[0] > 1:
        dt = np.abs(np.diff(frames, axis=0))                # (n-1, h, w)
        motion[1:] = dt.sum(axis=1)
        motion[0] = motion[1]

    def norm(a: np.ndarray) -> np.ndarray:
        m = a.max()
        return a / m if m > 0 else a

    # движение важнее: субъект обычно шевелится, фон — нет
    return 0.65 * norm(motion) + 0.35 * norm(contrast)


def best_centers(energy: np.ndarray, win_w: float) -> np.ndarray:
    """Для каждого кадра — центр окна ширины win_w с максимумом энергии."""
    w = energy.shape[1]
    win = max(2, int(round(win_w)))
    if win >= w:
        return np.full(energy.shape[0], w / 2.0, dtype=np.float32)
    # префиксные суммы -> сумма в окне за O(1)
    csum = np.concatenate([np.zeros((energy.shape[0], 1), dtype=np.float32),
                           np.cumsum(energy, axis=1)], axis=1)
    sums = csum[:, win:] - csum[:, :-win]                   # (n, w-win+1)
    starts = sums.argmax(axis=1).astype(np.float32)
    return starts + win / 2.0


def smooth_trajectory(centers: np.ndarray, win_w: float) -> np.ndarray:
    """Сглаживание + мёртвая зона + ограничение скорости — чтобы кадр не рыскал."""
    n = len(centers)
    k = max(1, int(round(SMOOTH_SEC * SAMPLE_FPS)))
    if k % 2 == 0:
        k += 1
    pad = k // 2
    padded = np.pad(centers, pad, mode="edge")
    kernel = np.ones(k, dtype=np.float32) / k
    sm = np.convolve(padded, kernel, mode="valid")[:n]

    deadzone = DEADZONE_FRAC * win_w
    max_step = (MAX_SPEED_FRAC * win_w) / SAMPLE_FPS

    out = np.empty(n, dtype=np.float32)
    cur = float(sm[0])
    for i in range(n):
        target = float(sm[i])
        delta = target - cur
        if abs(delta) > deadzone:
            step = delta - (deadzone if delta > 0 else -deadzone)
            step = max(-max_step, min(max_step, step))
            cur += step
        out[i] = cur
    return out


def compute(path: str, ar_w: int = 9, ar_h: int = 16) -> dict:
    meta = probe(path)
    W, H = meta["width"], meta["height"]

    crop_w = int(round(H * ar_w / ar_h))
    if crop_w % 2:
        crop_w += 1
    if crop_w >= W:
        return {"needed": False, "meta": meta, "crop_w": W, "crop_h": H,
                "reason": "исходник уже не шире целевого кадра"}

    sample_h = int(round(SAMPLE_W * H / W))
    if sample_h % 2:
        sample_h += 1

    frames = sample_gray(path, sample_h)
    energy = column_energy(frames)

    scale = W / SAMPLE_W
    win_w_sample = crop_w / scale

    centers = best_centers(energy, win_w_sample)
    centers = smooth_trajectory(centers, win_w_sample)

    # sample-координаты -> координаты источника, x = левый край кропа
    xs = centers * scale - crop_w / 2.0
    xs = np.clip(xs, 0, W - crop_w)

    times = np.arange(len(xs)) / SAMPLE_FPS
    keys = [{"t": round(float(t), 3), "x": int(round(float(x)))}
            for t, x in zip(times, xs)]

    travel = float(np.abs(np.diff(xs)).sum()) if len(xs) > 1 else 0.0
    return {
        "needed": True,
        "meta": meta,
        "crop_w": crop_w,
        "crop_h": H,
        "keys": keys,
        "stats": {
            "samples": len(keys),
            "x_min": int(xs.min()),
            "x_max": int(xs.max()),
            "x_mean": int(xs.mean()),
            "total_travel_px": int(travel),
            "static_center_x": int((W - crop_w) / 2),
        },
    }


def write_sendcmd(result: dict, path: Path) -> int:
    """sendcmd-файл: crop x меняется во времени. Пишем только реальные изменения."""
    lines, last = [], None
    for k in result["keys"]:
        if last is None or k["x"] != last:
            lines.append(f"{k['t']:.3f} crop x {k['x']};")
            last = k["x"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(lines)


def filter_chain(result: dict, cmds_path: Path, out_w: int = 1080, out_h: int = 1920) -> str:
    cw, ch = result["crop_w"], result["crop_h"]
    x0 = result["keys"][0]["x"] if result.get("keys") else int((result["meta"]["width"] - cw) / 2)
    return (
        f"sendcmd=f='{cmds_path}',"
        f"crop=w={cw}:h={ch}:x={x0}:y=0,"
        f"scale={out_w}:{out_h}:flags=lanczos,setsar=1"
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="Вертикальный рефрейм с ведением субъекта")
    ap.add_argument("source")
    ap.add_argument("--out-cmds", default=None, help="куда записать sendcmd-траекторию")
    ap.add_argument("--render", default=None, help="сразу отрендерить в этот файл")
    ap.add_argument("--print-filter", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    res = compute(args.source)
    if not res["needed"]:
        print(f"Рефрейм не нужен: {res['reason']}", file=sys.stderr)
        return

    cmds = Path(args.out_cmds or (Path(args.source).with_suffix("").as_posix() + ".traj.txt"))
    n = write_sendcmd(res, cmds)
    chain = filter_chain(res, cmds)

    if args.json:
        print(json.dumps({k: v for k, v in res.items() if k != "keys"},
                         ensure_ascii=False, indent=2))
    else:
        s = res["stats"]
        print(f"Кроп {res['crop_w']}x{res['crop_h']} из "
              f"{res['meta']['width']}x{res['meta']['height']}")
        print(f"Точек траектории: {s['samples']}, команд записано: {n}")
        print(f"x: {s['x_min']}..{s['x_max']} (среднее {s['x_mean']}, "
              f"статичный центр был бы {s['static_center_x']})")
        print(f"Суммарный ход камеры: {s['total_travel_px']} px")
        print(f"Траектория: {cmds}")

    if args.print_filter:
        print(chain)

    if args.render:
        cmd = ["ffmpeg", "-y", "-v", "error", "-i", args.source,
               "-vf", chain, "-c:v", "libx264", "-preset", "medium", "-crf", "20",
               "-pix_fmt", "yuv420p", "-c:a", "copy", args.render]
        cp = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL)
        if cp.returncode != 0:
            raise SystemExit(f"Рендер упал:\n{cp.stderr[-1500:]}")
        print(f"Отрендерено: {args.render}")


if __name__ == "__main__":
    main()
