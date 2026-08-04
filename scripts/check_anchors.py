#!/usr/bin/env python3
"""
check_anchors.py — проверяет, что внутренние ссылки README ведут в существующие
заголовки.

Зачем отдельный скрипт. Содержание большое, и ссылка ломается молча: гитхаб
просто ничего не прокручивает, ошибки не показывает. Один раз так уже проехали
две ссылки — заголовок был в кавычках «Стример», а ссылка без них.

Правила гитхаба воспроизведены точно (github-slugger): вниз по регистру, выкинуть
знаки пунктуации и типографские знаки, пробелы заменить на дефисы. Важно, что
кавычки «» и знак × НЕ выкидываются — они остаются частью якоря, и именно на
этом ломались ссылки.

    python3 scripts/check_anchors.py            # код возврата 1, если есть битые
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# набор символов, который github-slugger удаляет: типографские блоки
# U+2000-206F и U+2E00-2E7F плюс ASCII-пунктуация
DROP = re.compile(
    "[ -⁯⸀-⹿\\\\'!\"#$%&()*+,./:;<=>?@\\[\\]^`{|}~]"
)


def slug(heading: str) -> str:
    """Заголовок -> якорь по правилам гитхаба."""
    s = heading.strip().lower()
    s = s.replace("`", "").replace("*", "")   # разметка в якорь не попадает
    return DROP.sub("", s).replace(" ", "-")


def check(path: Path) -> int:
    text = path.read_text(encoding="utf-8")

    # заголовки только вне блоков кода: в ``` могут быть строки с #
    lines, in_code, heads = text.splitlines(), False, {}
    for ln in lines:
        if ln.lstrip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        m = re.match(r"^(#{1,6})\s+(.+?)\s*$", ln)
        if m:
            heads[slug(m.group(2))] = m.group(2)

    links = re.findall(r"\]\(#([^)]+)\)", text)
    bad = [l for l in links if l not in heads]

    print(f"{path.name}: заголовков {len(heads)}, "
          f"внутренних ссылок {len(links)}, битых {len(bad)}")
    for b in dict.fromkeys(bad):
        print(f"  битая ссылка: #{b}")
        # подсказываем ближайший по началу заголовок
        near = [h for h in heads if h[:12] == b[:12]]
        for n in near:
            print(f"      возможно имелся в виду: #{n}")
    return 1 if bad else 0


if __name__ == "__main__":
    root = Path(__file__).parent.parent
    files = [Path(a) for a in sys.argv[1:]] or [root / "README.md"]
    sys.exit(max(check(f) for f in files))
