#!/usr/bin/env python3
"""
chunking.py — разбивка слов на строки субтитров по смыслу, а не по счёту.

Проблема, которую это решает: группировка жёстко по N слов склеивает конец одной
фразы с началом следующей. На реальных данных выходило «[с юспом. А]» и
«[трёх наклеек. И]», хотя между фразами были настоящие паузы 300 и 260 мс.

Правила собраны из WhisperX SubtitlesProcessor, гайдов Netflix и BBC, плюс
поправки под русский язык (слова длиннее, свой набор служебных слов):

  1. Точка, вопрос, восклицание — жёсткий разрыв. Конец фразы не склеивается.
  2. Пауза между словами больше порога — жёсткий разрыв.
  3. Лимит по символам (для русского 38, не 42 как в английском) и по ширине.
  4. Строка не кончается служебным словом: предлог, союз, частица уезжают
     в следующую строку вместе со своим словом.
  5. Числа не отрываются от своей единицы: «1760 рублей» одной строкой.
  6. Одиночное слово-сирота приклеивается к предыдущей строке, если влезает.
  7. Слишком короткий показ растягивается до минимума читаемости.
"""
from __future__ import annotations

# Служебные слова: на них строка не заканчивается.
# Союзы взяты из whisperx/conjunctions.py, предлоги и частицы добавлены —
# в исходном списке их нет, а для русского это половина проблемы.
CONJUNCTIONS = {
    "и", "или", "но", "потому", "хотя", "пока", "когда", "где", "как",
    "если", "что", "перед", "после", "несмотря", "таким", "также", "ни",
    "зато", "чтобы", "либо", "однако", "причём", "тоже", "а",
}
PREPOSITIONS = {
    "в", "во", "на", "к", "ко", "с", "со", "у", "о", "об", "обо", "по",
    "из", "за", "до", "при", "от", "ото", "над", "под", "про", "без",
    "для", "через", "между", "среди", "около", "возле", "вокруг", "кроме",
    "против", "сквозь", "ради", "вместо", "внутри",
}
PARTICLES = {"же", "бы", "ли", "не", "ни", "вот", "уж", "аж", "то", "ведь"}
FUNCTION_WORDS = CONJUNCTIONS | PREPOSITIONS | PARTICLES

SENTENCE_END = ".!?…"
# единицы, которые нельзя отрывать от числа
UNITS = {
    "рублей", "рубля", "рубль", "руб", "долларов", "доллара", "доллар",
    "процентов", "процента", "процент", "тысяч", "тысячи", "тысяча",
    "миллионов", "миллиона", "миллион", "штук", "штуки", "раз", "раза",
    "секунд", "секунды", "минут", "минуты", "часов", "часа",
    "место", "месте", "мест",
}

MAX_CHARS = 38          # для русского меньше, чем 42 у Netflix: длиннее словоформы
MIN_CHARS_SPLIT = 20    # не рвём, пока не набрали столько
GAP_HARD = 0.30         # пауза, после которой строка обязательно новая
GAP_SOFT = 0.16         # пауза, на которой рвать предпочтительно
MIN_SHOW = 0.45         # минимальное время показа строки


def _core(word: str) -> str:
    return word.strip(".,!?;:—–\"'()«»").lower()


def _is_number(word: str) -> bool:
    c = _core(word).replace(",", "").replace(".", "")
    return c.isdigit() or (c and c[0].isdigit())


def ends_sentence(word: str) -> bool:
    return word.rstrip("\"'»)").endswith(tuple(SENTENCE_END))


def chunk_words(words: list[dict], max_words: int, width_fn=None,
                max_chars: int = MAX_CHARS) -> list[list[dict]]:
    """
    Слова -> строки. width_fn(текст) -> ширина в px, если нужно ограничение
    по реальной ширине шрифта; иначе считаем только символы.
    """
    if not words:
        return []

    chunks: list[list[dict]] = []
    cur: list[dict] = []

    def line_text(ws: list[dict]) -> str:
        return " ".join(w["word"] for w in ws)

    def too_long(ws: list[dict]) -> bool:
        t = line_text(ws)
        if len(t) > max_chars:
            return True
        if width_fn is not None and width_fn(t.upper()) > 0:
            return False
        return False

    for i, w in enumerate(words):
        cur.append(w)
        nxt = words[i + 1] if i + 1 < len(words) else None

        # 1. конец предложения — закрываем строку всегда
        if ends_sentence(w["word"]):
            chunks.append(cur)
            cur = []
            continue

        if nxt is None:
            break

        gap = nxt["start"] - w["end"]
        text = line_text(cur)

        # 2. заметная пауза — новая строка
        if gap >= GAP_HARD and len(cur) >= 1:
            chunks.append(cur)
            cur = []
            continue

        # набрали лимит по словам или символам
        hit_limit = len(cur) >= max_words or len(text) >= max_chars
        if not hit_limit:
            # 3. мягкая пауза при уже достаточной длине — хороший момент
            if gap >= GAP_SOFT and len(text) >= MIN_CHARS_SPLIT:
                chunks.append(cur)
                cur = []
            continue

        # 4. не заканчиваем строку служебным словом
        if _core(w["word"]) in FUNCTION_WORDS and len(cur) > 1:
            cur.pop()
            chunks.append(cur)
            cur = [w]
            continue

        # 5. число не отрываем от единицы
        if _is_number(w["word"]) and _core(nxt["word"]) in UNITS:
            continue

        chunks.append(cur)
        cur = []

    if cur:
        chunks.append(cur)

    return _fix_orphans(chunks, max_words, max_chars)


def _fix_orphans(chunks: list[list[dict]], max_words: int,
                 max_chars: int) -> list[list[dict]]:
    """
    6. Одиночное слово в строке смотрится как обрывок. Приклеиваем к соседу,
    если это не ломает границу предложения и влезает по длине.
    """
    out: list[list[dict]] = []
    for ch in chunks:
        if (len(ch) == 1 and out
                and not ends_sentence(out[-1][-1]["word"])
                and len(out[-1]) < max_words):
            merged = out[-1] + ch
            if len(" ".join(w["word"] for w in merged)) <= max_chars:
                out[-1] = merged
                continue
        out.append(ch)
    return out


def wrap_chunk(chunk: list[dict], width_fn, max_px: float,
               max_lines: int = 2, upper: bool = True) -> list[list[dict]] | None:
    """
    Разбивает группу на строки по РЕАЛЬНОЙ ширине шрифта.

    Считать символы бессмысленно: при шрифте 88 в 940 px влезает около
    14 символов, а осмысленная группа почти всегда длиннее. Netflix и BBC
    разрешают до двух строк — этим и пользуемся.

    Возвращает список строк или None, если в max_lines не уложилось.
    Разрыв не ставится после служебного слова, если есть альтернатива.
    """
    def px(ws: list[dict]) -> float:
        t = " ".join(w["word"] for w in ws)
        return width_fn(t.upper() if upper else t)

    lines: list[list[dict]] = []
    cur: list[dict] = []
    for w in chunk:
        trial = cur + [w]
        if cur and px(trial) > max_px:
            # не оставляем служебное слово в хвосте строки
            if len(cur) > 1 and _core(cur[-1]["word"]) in FUNCTION_WORDS:
                moved = cur.pop()
                lines.append(cur)
                cur = [moved, w]
            else:
                lines.append(cur)
                cur = [w]
            if len(lines) > max_lines:
                return None
        else:
            cur = trial
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        return None
    # «нижняя пирамида»: вторая строка не короче первой читается лучше
    if len(lines) == 2 and px(lines[0]) < px(lines[1]) * 0.55 and len(lines[1]) > 1:
        cand0 = lines[0] + [lines[1][0]]
        if px(cand0) <= max_px:
            lines = [cand0, lines[1][1:]]
    return [ln for ln in lines if ln]


def fit_to_width(chunks: list[list[dict]], width_fn, max_px: float,
                 max_lines: int = 2, upper: bool = True) -> list[dict]:
    """
    Приводит смысловые группы к тем, что реально влезают в кадр.
    Не влезло в две строки — группа делится пополам по словам и проверяется снова.
    """
    out: list[dict] = []
    queue = list(chunks)
    while queue:
        ch = queue.pop(0)
        lines = wrap_chunk(ch, width_fn, max_px, max_lines, upper)
        if lines is None:
            if len(ch) == 1:
                # одно слово шире кадра — отдаём как есть, сожмётся масштабом
                out.append({"words": ch, "lines": [ch]})
                continue
            mid = len(ch) // 2
            # стараемся не рвать после служебного слова
            if _core(ch[mid - 1]["word"]) in FUNCTION_WORDS and mid > 1:
                mid -= 1
            queue.insert(0, ch[mid:])
            queue.insert(0, ch[:mid])
            continue
        out.append({"words": ch, "lines": lines})
    return out


def chunk_timings(chunks: list[list[dict]], clip_dur: float) -> list[dict]:
    """
    7. Границы показа строки. Строка держится до начала следующей, но не
    короче минимума — иначе мигает.
    """
    res = []
    for i, ch in enumerate(chunks):
        start = ch[0]["start"]
        end = ch[-1]["end"]
        nxt = chunks[i + 1][0]["start"] if i + 1 < len(chunks) else clip_dur
        # тянем до следующей строки, если разрыв небольшой
        if nxt - end <= 0.8:
            end = nxt
        if end - start < MIN_SHOW:
            end = min(start + MIN_SHOW, nxt if nxt > start else clip_dur)
        res.append({"words": ch, "start": round(start, 3), "end": round(end, 3)})
    return res


def report(chunks: list[list[dict]]) -> dict:
    lens = [len(" ".join(w["word"] for w in ch)) for ch in chunks]
    counts = [len(ch) for ch in chunks]
    bad_tail = sum(1 for ch in chunks
                   if _core(ch[-1]["word"]) in FUNCTION_WORDS)
    glued = 0
    for ch in chunks:
        for w in ch[:-1]:
            if ends_sentence(w["word"]):
                glued += 1
    return {
        "строк": len(chunks),
        "слов в строке": f"{min(counts)}–{max(counts)}",
        "символов макс": max(lens) if lens else 0,
        "строк, кончающихся служебным словом": bad_tail,
        "строк со склейкой через точку": glued,
        "одиночных строк": sum(1 for c in counts if c == 1),
    }
