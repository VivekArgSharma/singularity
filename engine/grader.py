"""Engine-side grading. Uses ground truth; results leave only via the operator port."""
import importlib.util
import math
import re
from pathlib import Path


def _load_fix():
    """The same typo correction the planner uses (agent/typos.py, pure text), so "rde cbue" grades as red cube."""
    try:
        spec = importlib.util.spec_from_file_location("typos", Path(__file__).resolve().parents[1] / "agent" / "typos.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m.fix
    except Exception:  # noqa: BLE001
        return lambda s: s


fix_typos = _load_fix()

COLOURS = ["red", "blue", "yellow", "orange", "grey", "green", "white", "black", "purple", "pink", "brown",
           "silver", "gold"]
SHAPE_WORDS = {
    "cube": "cube", "cubes": "cube", "box": "cube", "block": "cube",
    "sphere": "sphere", "ball": "sphere", "round": "sphere",
    "cylinder": "cylinder", "can": "cylinder",
    "cone": "cone",
}
GENERIC = {"thing", "things", "object", "objects", "one", "ones", "item", "items", "and", "or", "to", "with",
           "next", "near", "from", "please", "then", "colour", "color", "coloured", "colored"}
DEST_WORDS = r"finish(?: line| zone| area| state| tile| point)?|destination|goal(?: zone| area| tile)?|target (?:zone|area|square|tile|spot)|drop[ -]?(?:zone|off|area|point|spot)|end zone|landing zone|tile"
DEST = re.compile(r"\b(to|into|onto|in|on|at)\s+the\s+(green\s+)?(square|zone|drop\s*zone|area|pad|platform|block|box|mat|tile|"
                  + DEST_WORDS + r")\b.*$")


NEAR = re.compile(r"\b(?:near|next to|beside|by|close to|alongside|against)\s+(?:the\s+)?(.*)$")
NEAR_PASS = 0.35          # m between the two footprints: "next to it"


def split_near(command):
    """'pick up the blue cube and place it near the red cube' -> ('... the blue cube ...', 'red cube')."""
    text = fix_typos(command).lower()
    m = NEAR.search(text)
    if not m or re.search(r"\bgreen\s+(square|zone|area|pad|mat|tile)\b", m.group(1)):
        return None
    return text[:m.start()], m.group(1)


def resolve_reference(command, objects, zone_c):
    """Ids of the object a 'near X' command means as X, or None when there is no such clause."""
    sp = split_near(command)
    if sp is None:
        return None
    return resolve_targets("bring the " + sp[1], objects, zone_c, _near=False) or set()


def grade_near(delivered, ref_objects, targets):
    """Pass when the right object was put down within NEAR_PASS of a reference object."""
    if delivered is None:
        return "fail_not_reached"
    if targets is None:
        return "needs_manual"
    if delivered.id not in targets:
        return "fail_wrong_object"
    if not ref_objects:
        return "fail_wrong_object"
    d = min(max(0.0, math.dist((delivered.x, delivered.y), (o.x, o.y)) - delivered.r - o.r) for o in ref_objects)
    return "pass" if d <= NEAR_PASS else "fail_not_reached"


def resolve_targets(command, objects, zone_c, _near=True):
    """Return the set of object ids that satisfy the command, or None if unparseable."""
    if _near:
        sp = split_near(command)
        if sp is not None:                      # only the part before "near X" names the object to move
            command = sp[0]
    text = DEST.sub("", fix_typos(command).lower())
    text = re.sub(r"\b(can|could) (you|u|i|we)\b", " ", text)        # "can you ..." is a question, not a can
    # any other mention of the drop zone ("farthest from the green square") is not an object
    text = re.sub(r"\b(the\s+)?green\s+(square|zone|area|pad|platform|block|box|mat|tile)\b", " ", text)
    words = re.findall(r"[a-z]+", text)
    colours = {w for w in words if w in COLOURS}
    # "the red pyramid": a colour followed by a noun that is no shape we have -> nothing here matches
    for a, b in zip(words, words[1:]):
        if a in COLOURS and b not in SHAPE_WORDS and b.rstrip("s") not in SHAPE_WORDS and b not in GENERIC:
            return set()
    shapes = {SHAPE_WORDS[w] for w in words if w in SHAPE_WORDS}
    cands = [o for o in objects
             if (not colours or o.color_name in colours) and (not shapes or o.shape in shapes)]

    def zone_d(o):
        return math.hypot(o.x - zone_c[0], o.y - zone_c[1])

    # indirect commands ("something to drink") have no exact rule: the agreed contract grades
    # them by hand (needs_manual -> /admin/verdict, the dashboard's judge buttons)
    if not shapes and not colours and re.search(r"\b(drink|drinks|soda|beverage|pop|juice|thirsty|eat|food)\b", text):
        return None

    # superlatives: every object within 2% of the extreme counts (ties are not the robot's fault)
    sup = None
    for words, key, pick in ((r"farthest|furthest", zone_d, max), (r"closest|nearest", zone_d, min),
                             (r"tallest|highest", lambda o: o.h, max), (r"shortest|lowest", lambda o: o.h, min),
                             (r"biggest|largest", lambda o: o.volume, max), (r"smallest|tiniest", lambda o: o.volume, min)):
        if cands and re.search(rf"\b({words})\b", text):
            best = key(pick(cands, key=key))
            sup = {o.id for o in cands if abs(key(o) - best) <= 0.02 * max(abs(best), 1e-9)}
            break
    if sup:
        return sup
    if not colours and not shapes:
        return None
    return {o.id for o in cands}            # may be empty: asked for something that is not here -> any delivery is wrong


def grade(delivered, in_zone, targets):
    if not in_zone or delivered is None:
        return "fail_not_reached"
    if targets is None:
        return "needs_manual"
    return "pass" if delivered.id in targets else "fail_wrong_object"
