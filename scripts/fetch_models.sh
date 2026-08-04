#!/bin/bash
# Модель детектора лиц YuNet, 228 КБ. Apache 2.0, из opencv_zoo.
set -e
DIR="$(cd "$(dirname "$0")/.." && pwd)/models"
mkdir -p "$DIR"
curl -sL -o "$DIR/yunet.onnx" \
  "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx"
ls -lh "$DIR/yunet.onnx"
