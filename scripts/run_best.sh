#!/bin/bash
# Лучшие моменты по смысловому разбору + баннер MUSORDROP
set -e
cd /agent/workspace/tiktok/sandbox
CF=/agent/workspace/tiktok/clipfactory
WM="delogo=x=922:y=621:w=317:h=57"
mkdir -p best

# name start dur  (границы из story.py, закрываются на конкретике)
while read -u 3 NAME START DUR; do
  echo "=========== $NAME: $START +$DUR с ==========="
  ffmpeg -nostdin -y -v error -ss "$START" -i cs_src.mp4 -t "$DUR" \
    -vf "$WM" -c:v libx264 -preset veryfast -crf 18 -pix_fmt yuv420p \
    -c:a aac -b:a 192k -ar 48000 "best/src_$NAME.mp4" < /dev/null
  echo "  рез + снятие знака"

  ffmpeg -nostdin -y -v error -i "best/src_$NAME.mp4" -vn -ac 1 -ar 16000 \
    "best/a_$NAME.wav" < /dev/null
  python3 /tmp/wt2.py "best/a_$NAME.wav" "best/w_$NAME.json" small hot 2>&1 | tail -1

  python3 "$CF/captions.py" --words "best/w_$NAME.json" --clip-dur "$DUR" \
    --style hormozi --margin-v 480 --font-size 88 --outline 10 \
    --max-words 3 --out "best/cap_$NAME.ass" < /dev/null | tail -1

  # баннер по центру ролика
  BAT=$(python3 -c "print(round(($DUR-6.0)/2,1))")
  python3 "$CF/rhythm.py" "best/src_$NAME.mp4" --dur "$DUR" \
    --captions "best/cap_$NAME.ass" --out "best/BEST_$NAME.mp4" \
    --banner banner_src.mp4 --banner-at "$BAT" --banner-pos upper \
    --duck -15 --banner-gain -2 --draft < /dev/null
done 3< best_list.txt

echo "=== ГОТОВО ==="
ls -lh best/BEST_*.mp4
