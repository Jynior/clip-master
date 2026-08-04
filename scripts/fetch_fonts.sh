#!/bin/bash
# Montserrat и Anton под SIL Open Font License. В репозитории не лежат,
# чтобы не тащить бинарники — скачиваются сюда.
set -e
DIR="$(cd "$(dirname "$0")/.." && pwd)/fonts"
mkdir -p "$DIR"
curl -sL -o "$DIR/Montserrat.ttf" \
  "https://github.com/google/fonts/raw/main/ofl/montserrat/Montserrat%5Bwght%5D.ttf"
# Oswald вместо Anton: у Anton нет кириллицы вообще, русский текст выходил
# пустыми прямоугольниками. Oswald такой же узкий, но с полной кириллицей.
curl -sL -o "$DIR/Oswald.ttf" \
  "https://github.com/google/fonts/raw/main/ofl/oswald/Oswald%5Bwght%5D.ttf"
echo "Шрифты в $DIR:"
ls -lh "$DIR"
