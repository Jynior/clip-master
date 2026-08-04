#!/usr/bin/env python3
"""
detect.py — определяет тип материала по картинке: есть ли говорящий человек.

Зачем. Формат нарезки зависит от того, что в кадре. Если это стример или любой
говорящий человек — правильная раскладка вертикальная двойная: сверху вебка,
снизу посторонний исходник. Если это геймплей или запись экрана — раскладка
обычная, на весь кадр.

Спрашивать это у пользователя каждый раз необязательно: лицо в кадре
определяется надёжно. Детектор YuNet из OpenCV, нейросетевой, модель 228 КБ.

Использование:
    python3 detect.py video.mp4
    python3 detect.py video.mp4 --json
"""
from __future__ import annotations

import argparse
import io
import json
import subprocess
from pathlib import Path

import numpy as np

MODEL = Path(__file__).parent.parent / "models" / "yunet.onnx"
SAMPLE_FPS = 0.5          # раз в две секунды достаточно
DET_W, DET_H = 320, 320
CONF = 0.6

# Порог: лицо должно быть в кадре стабильно, а не мелькнуть на превью
FACE_TIME_MIN = 0.35      # доля кадров с лицом
FACE_AREA_MIN = 0.004     # доля площади кадра — отсекает лица на заднем плане


def _frames(path: str, fps: float = SAMPLE_FPS) -> tuple[np.ndarray, int, int]:
    """Кадры видео как массив (n, h, w, 3) в BGR."""
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", path],
        capture_output=True, text=True, stdin=subprocess.DEVNULL)
    w, h = (int(x) for x in probe.stdout.strip().split(",")[:2])
    # уменьшаем до разумного, детектору хватает
    sw = 480
    sh = int(round(h * sw / w))
    if sh % 2:
        sh += 1
    cp = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-i", path,
         "-vf", f"fps={fps},scale={sw}:{sh}", "-pix_fmt", "bgr24",
         "-f", "rawvideo", "-"],
        capture_output=True, stdin=subprocess.DEVNULL)
    buf = np.frombuffer(cp.stdout, dtype=np.uint8)
    px = sw * sh * 3
    n = len(buf) // px
    if n == 0:
        return np.zeros((0, sh, sw, 3), dtype=np.uint8), sw, sh
    return buf[: n * px].reshape(n, sh, sw, 3), sw, sh


def analyse(path: str) -> dict:
    import cv2

    frames, sw, sh = _frames(path)
    if frames.shape[0] == 0:
        return {"error": "не удалось прочитать кадры"}

    det = cv2.FaceDetectorYN_create(str(MODEL), "", (sw, sh), CONF, 0.3, 5000)
    det.setInputSize((sw, sh))

    hits = 0
    boxes: list[tuple[float, float, float, float]] = []
    for fr in frames:
        _, faces = det.detect(np.ascontiguousarray(fr))
        if faces is None or len(faces) == 0:
            continue
        # берём самое крупное лицо в кадре
        best = max(faces, key=lambda f: f[2] * f[3])
        x, y, bw, bh = best[:4]
        area = (bw * bh) / (sw * sh)
        if area < FACE_AREA_MIN:
            continue
        hits += 1
        boxes.append((x / sw, y / sh, bw / sw, bh / sh))

    n = frames.shape[0]
    share = hits / n if n else 0.0

    res: dict = {
        "frames_checked": n,
        "frames_with_face": hits,
        "face_time_share": round(share, 3),
    }

    if boxes:
        arr = np.array(boxes)
        cx = float((arr[:, 0] + arr[:, 2] / 2).mean())
        cy = float((arr[:, 1] + arr[:, 3] / 2).mean())
        res["face_center"] = {"x": round(cx, 3), "y": round(cy, 3)}
        res["face_area_mean"] = round(float((arr[:, 2] * arr[:, 3]).mean()), 4)
        # разброс положения: у вебки лицо стоит на месте, в записи игры прыгает
        res["face_jitter"] = round(float(arr[:, 0].std() + arr[:, 1].std()), 3)

    if share >= FACE_TIME_MIN:
        res["kind"] = "talking_head"
        res["recommendation"] = (
            "Говорящий человек в кадре. Подходит раскладка «стример»: "
            "сверху вебка, снизу посторонний исходник.")
    elif share > 0.05:
        res["kind"] = "mixed"
        res["recommendation"] = (
            "Лицо появляется, но не постоянно. Скорее всего лицо есть только "
            "в части ролика — стоит спросить у пользователя.")
    else:
        res["kind"] = "no_face"
        res["recommendation"] = (
            "Лица нет. Это геймплей, запись экрана или съёмка без людей — "
            "обычная раскладка на весь кадр.")
    return res


def main() -> None:
    ap = argparse.ArgumentParser(description="Определение типа материала по картинке")
    ap.add_argument("source")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    r = analyse(args.source)
    if args.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return
    if "error" in r:
        raise SystemExit(r["error"])
    print(f"кадров проверено: {r['frames_checked']}, с лицом: {r['frames_with_face']} "
          f"({r['face_time_share'] * 100:.0f}%)")
    if "face_center" in r:
        c = r["face_center"]
        print(f"лицо в среднем на {c['x'] * 100:.0f}% ширины, "
              f"{c['y'] * 100:.0f}% высоты, площадь {r['face_area_mean'] * 100:.1f}% кадра")
        print(f"разброс положения: {r['face_jitter']}")
    print(f"тип: {r['kind']}")
    print(r["recommendation"])


if __name__ == "__main__":
    main()
