"""Typo correction for spoken/typed commands (pure text, no robot API).

A word is corrected only when it is not a real English word (offline pyspellchecker list) and it is
one or two edits from a word the planner understands; real words ("hold", "think") are never changed.
Shared by the local planner and the engine's auto-grader so both read a command the same way.
"""
import re

COLOURS = r"red|blue|yellow|orange|green|grey|gray|purple|white|black|pink|brown|silver|gold|violet|cyan"

# every word the planner reacts to: a misspelling of one of these is corrected to it
VOCAB = sorted(set("""
cube cubes box boxes block blocks sphere spheres ball balls cylinder cylinders can cans cone cones
square zone area patch pad mat tile goal finish destination target landing end spot point thing things object objects something item
red blue yellow orange green grey gray purple white black pink brown silver gold violet cyan
bring fetch carry deliver take pick grab lift collect drop release place put move drive head go navigate
turn rotate spin circle orbit loop around round behind approach explore inspect find locate search look spot
stop pause closer toward towards front ahead forward forwards backward backwards back reverse obstacle avoid past
next near into twice thrice three times once slowly slow carefully gently metres meters degrees left right
drink drinks soda beverage juice thirsty tallest smallest biggest largest farthest closest nearest where
""".split()))
# ordinary words that are never "corrected" even if they look like a command word
COMMON = set("""
a an the to me my it its is are was be of in on at by and or then please you u i we us could would should
will can with from that this these those there here up down out off for all one two any some what which
who how do does did get got let see now again also just not no yes very much more less bit little first
then after before over under side corner wall room floor them they he she his her our your their him
""".split())
SHAPE_WORDS = {"cube", "box", "block", "sphere", "ball", "cylinder", "can", "cone"}
NOT_OBJECT = {"thing", "things", "object", "objects", "one", "item", "something", "square", "zone", "area",
              "finish", "destination", "goal", "target", "drop", "end", "landing", "spot", "point", "line", "state",
              "highest", "shortest", "lowest", "furthest", "tiniest", "room", "area",
              "patch", "pad", "mat", "tile", "goal", "room", "floor", "wall", "corner", "side", "left", "right",
              "front", "obstacle", "way", "end", "middle", "centre", "center", "robot", "camera", "same",
              "tallest", "smallest", "biggest", "largest", "farthest", "closest", "nearest", "other", "drink"}


def _dist(a, b):
    """Edit distance counting a swap of two neighbouring letters as one edit ("sqaure", "rde")."""
    d = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) + 1):
        d[i][0] = i
    for j in range(len(b) + 1):
        d[0][j] = j
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            c = 0 if a[i - 1] == b[j - 1] else 1
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + c)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[-1][-1]


_FREQ = None


def _english(w):
    """How common a real English word is (0 = not a word). Offline word list from pyspellchecker;
    without it only COMMON protects ordinary words."""
    global _FREQ
    if _FREQ is None:
        try:
            from spellchecker import SpellChecker
            _FREQ = SpellChecker(distance=1).word_frequency
        except Exception:  # noqa: BLE001
            _FREQ = {}
    try:
        return _FREQ[w] if w in _FREQ else 0
    except Exception:  # noqa: BLE001
        return 0


REAL_WORD = 1000          # a word seen this often in English is spelled on purpose: never "correct" it


COLOUR_WORDS = set(COLOURS.split("|"))
TARGETS = sorted(set(VOCAB) | {"the", "then", "and", "with", "please"})


def _correct(w, slot=None):
    """Closest command word to `w`, or `w` itself.
    slot="shape": the word follows a colour, so only a shape fits (a rare real word like "cub" still counts);
    slot="colour": the word comes just before a shape, so a colour fits there ("read cube" -> red cube)."""
    import difflib
    if len(w) < 3 or w in VOCAB or w in COMMON or not w.isalpha():
        return w
    if w.endswith("s") and w[:-1] in VOCAB:
        return w
    f = _english(w)
    pool = TARGETS
    if slot == "shape" and f < 5000:
        pool = sorted(SHAPE_WORDS | {"square", "zone", "area", "patch", "thing", "object"})
    elif slot == "colour" and len(w) >= 4:
        pool = sorted(COLOUR_WORDS | {"round"})
    elif f >= REAL_WORD:
        return w
    limit = 1 if len(w) <= 5 else 2
    cands = []
    for v in pool:
        if abs(len(v) - len(w)) > limit:
            continue
        k = _dist(w, v)
        if k > limit:
            continue
        if len(w) == 3 and k == 1 and w[0] != v[0] and sorted(w) != sorted(v):
            continue                       # 3-letter words: same first letter or a swap only ("rde" -> red)
        cands.append((k, -difflib.SequenceMatcher(None, w, v).ratio(), v))
    if not cands:
        return w
    cands.sort()
    if len(cands) > 1 and cands[0][:2] == cands[1][:2]:
        return w                           # two words exactly as close ("bloack": black or block?) -> leave it
    return cands[0][2]


def _fix_typos(text):
    """Small typos in any command word ("bloack" -> block, "rde" -> red, "yelow" -> yellow, "brign" -> bring).
    Real English words are left alone ("hold", "think", "make" are not command words misspelt)."""
    toks = re.findall(r"[A-Za-z]+|[^A-Za-z]+", text)
    words = [i for i, t in enumerate(toks) if t.isalpha()]
    low = [t.lower() for t in toks]
    for n, i in enumerate(words):                       # pass 1: words on their own
        low[i] = _correct(low[i])
    for n, i in enumerate(words):                       # pass 2: use the neighbours (colour + shape slots)
        prev = low[words[n - 1]] if n else None
        nxt = low[words[n + 1]] if n + 1 < len(words) else None
        w = toks[i].lower()
        if low[i] != w and low[i] not in ("black", "block"):
            continue
        if prev in COLOUR_WORDS:
            c = _correct(w, "shape")
            if c != w:
                low[i] = c
                continue
        if nxt in SHAPE_WORDS or (nxt and nxt.rstrip("s") in SHAPE_WORDS):
            c = _correct(w, "colour")
            if c != w:
                low[i] = c
    return "".join(low[i] if low[i] != toks[i].lower() else toks[i] for i in range(len(toks)))


# Different people say the same task differently. After typo correction, phrasings are mapped onto
# the words the planner and the grader both use. Order matters (longer phrases first).
PHRASES = [
    (r"\b(i'?m|i am|feeling|so) thirsty\b|\b(i )?(need|want|would like|could use) (a |some )?(drink|soda|beverage|juice)\b",
     "bring me something to drink"),
    (r"\b(get|grab|fetch|hand|give|pass) me (a |an |some |the )?(soda|drink|beverage|juice|pop)\b", "bring me something to drink"),
    (r"\b(get|grab|fetch|hand|give|pass) me\b", "bring me"),
    (r"\b(go and|go|come and) (get|grab|fetch)\b", "fetch"),
    (r"\b(look|drive|wander|go) (all )?around (the room|everywhere)\b|\blook around\b|\bsurvey (the room)?\b"
     r"|\b(scan|search|map|chart) the (whole )?(room|area|place)\b", "explore the room"),
    (r"\btraffic cone\b|\bpylon\b", "cone"),
    (r"\b(orb|globe|marble)s?\b", "sphere"),
    (r"\b(tube|bottle|tin|pipe|column|pillar|soda can|drink can)s?\b", "cylinder"),
    (r"\b(brick|crate|dice|die)s?\b", "cube"),
    (r"\b(loftiest|largest in height)\b", "tallest"),
    (r"\bfurthest\b", "farthest"),
    (r"\b(two|2) times\b", "twice"),
]


def normalise(text):
    t = text
    for pat, rep in PHRASES:
        t = re.sub(pat, rep, t, flags=re.IGNORECASE)
    return t


def fix(text):
    """Typos first, then phrasings: what both the planner and the grader read."""
    return normalise(_fix_typos(text))
