#!/bin/bash
# Montserrat и Anton под SIL Open Font License. В репозитории не лежат,
# чтобы не тащить бинарники — скачиваются сюда.
set -e
DIR="$(cd "$(dirname "$0")/.." && pwd)/fonts"
mkdir -p "$DIR"
curl -sL -o "$DIR/Montserrat.ttf" \
  "https://github.com/google/fonts/raw/main/ofl/montserrat/Montserrat%5Bwght%5D.ttf"
curl -sL -o "$DIR/Anton.ttf" \
  "https://github.com/google/fonts/raw/main/ofl/anton/Anton-Regular.ttf"
echo "Шрифты в $DIR:"
ls -lh "$DIR"
