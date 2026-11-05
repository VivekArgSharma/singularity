"""Command interpreter: whatever English the operator types -> the one standard command the robot runs.

    interpret("can u grab teh red box n put it on the finish") ->
        {"canonical": "Bring the red cube to the green square.", "action": "fetch", "object": "red cube",
         "destination": "the green square", "changed": True, "method": "rules"}

Stages: typo correction -> phrasing normaliser (agent/typos.py) -> intent read by the local planner's own parser
(dry run: no robot motion) -> a canonical sentence. Missions, refinements ("(update: slower)") and anything that
cannot be mapped are passed through unchanged (the planner then explains, e.g. "Not present ...").
Pure text: no robot, no scene data.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import local_brain as L  # noqa: E402


class _Done(Exception):
    pass


class _Dry(L.LocalBrain):
    """The planner's own command reading, with every robot skill replaced by a recorder."""
    def __init__(self, cmd, holding=False):
        super().__init__(None, lambda *a, **k: None, cmd, mapping=False)
        self.holding = holding
        self.got = None
        self._rec_pick = None

    def st(self):
        self.s = {"name": "Locate", "x": 0.0, "y": 0.0, "heading": 0.0, "holding": self.holding, "retries": 0}
        return self.s

    def _rec(self, action, obj=None, dest=None):
        self.got = (action, obj, dest)
        raise _Done()

    def see(self):
        return []

    def resolve_superlative(self, spec):
        self._rec("fetch", spec["name"], "zone")

    def pick(self, tgt):
        self._rec_pick = tgt

    def deliver_zone(self):
        t = self._rec_pick
        self._rec("deliver_held" if t is None else "fetch", t["name"] if t else None, "zone")

    def approach(self, tgt, stand):
        if self._rec_pick is not None:
            self._rec("near", self._rec_pick["name"], tgt["name"])
        self._rec("approach", tgt["name"])

    def find(self, tgt, sweeps=9):
        return {"bearing": 0.0}

    def measure(self, tgt):
        self._rec("find", tgt["name"])

    def orbit(self, tgt, loops, sweep=None, finish_facing=False):
        self._rec("behind" if sweep is not None else "orbit", tgt["name"])

    def inspect(self, tgt, views=4):
        self._rec("inspect", tgt["name"])

    def drop(self):
        self._rec("drop")

    def put_down_here(self):
        pass

    def find_zone(self):
        self._rec("goto_zone", None, "zone")

    def go_point(self, *a, **k):
        self._rec("goto_zone", None, "zone")

    def move(self, d, speed=None):
        self._rec("move")

    def avoid_ahead(self):
        self._rec("avoid")


def dry(cmd, holding=False):
    """(action, object name, destination) as the local planner would read the command."""
    import explore
    b = _Dry(cmd, holding)
    real = explore.explore_nbv
    explore.explore_nbv = lambda *a, **k: b._rec("explore")
    b.cl = type("C", (), {"turn": lambda self, *a: b._rec("rotate"), "move": lambda self, *a: b._rec("move"),
                          "depth": lambda self: 1.0, "scan": lambda self, n=91: []})()
    try:
        b._run()
    except _Done:
        pass
    except L.Fail as e:
        b.got = ("refuse", str(e), None) if ("Not present" in str(e) or "can't find" in str(e)) \
            else ("not_understood", str(e), None)
    finally:
        explore.explore_nbv = real
    if b.got is None and b._rec_pick is not None:
        b.got = ("pick", b._rec_pick["name"], None)
    return b.got, b


def _the(name):
    if name.startswith("drink"):
        return "something to drink"
    m = re.match(r"(farthest|furthest|closest|nearest) (.+)", name)
    if m:                                     # "farthest cube" -> "the cube that is farthest from the green square"
        return f"the {m.group(2)} that is {m.group(1).replace('furthest', 'farthest')} from the green square"
    return "the " + name


def interpret(text, holding=False):
    raw = (text or "").strip()
    out = {"said": raw, "canonical": raw, "action": None, "object": None, "destination": None,
           "changed": False, "method": "rules"}
    low = raw.lower()
    if not raw or low.startswith("mission") or "(update:" in low:
        return out                            # missions and refinements go to the robot exactly as written
    got, b = dry(raw, holding)
    act, obj, dest = got if got else (None, None, None)
    out.update(action=act, object=obj)
    slow = " slowly" if b.speed < 1.0 else ""
    canon = None
    if act == "fetch" and obj:
        thing = _the(obj)
        canon = ("Bring me something to drink." if obj.startswith("drink") else
                 f"Bring {thing}{slow}." if "from the green square" in thing else
                 f"Bring {thing} to the green square{slow}.")
        out["destination"] = "the green square"
    elif act == "near" and obj and dest:
        canon = f"Put the {obj} next to the {dest}{slow}."
        out["destination"] = "next to the " + dest
    elif act == "deliver_held":
        canon = f"Put what you are holding on the green square{slow}."
        out["destination"] = "the green square"
    elif act == "pick" and obj:
        canon = f"Pick up the {obj}{slow}."
    elif act == "drop":
        canon = "Put it down."
    elif act == "orbit" and obj:
        n = b.loops()
        canon = f"Circle around the {obj} {'once' if n == 1 else 'twice' if n == 2 else f'{n} times'}{slow}."
    elif act == "behind" and obj:
        canon = f"Go behind the {obj}."
    elif act == "approach" and obj:
        canon = f"Go to the {obj}{slow}."
    elif act == "find" and obj:
        canon = f"Find the {obj}."
    elif act == "inspect" and obj:
        canon = f"Inspect the {obj}."
    elif act == "goto_zone":
        canon = f"Go to the green square{slow}."
        out["destination"] = "the green square"
    elif act == "explore":
        canon = "Explore the room."
    # rotate / move (numbers), refuse (the planner explains why), not_understood: keep the operator's words
    if canon:
        out["canonical"] = canon
        out["changed"] = canon.rstrip(".").lower() != raw.rstrip(".?!").lower()
    elif act == "not_understood":
        out["note"] = "not understood by the free planner (the Claude agent reads free language)"
    elif act == "refuse":
        out["note"] = got[1][:200]
    return out


if __name__ == "__main__":
    for c in sys.argv[1:] or ["can u grab teh red box n put it on the finish", "I'm thirsty", "go round the cone 2 times",
                              "take red cube and put it beside the blue cube", "now drop it on the green tile",
                              "bring the cube furthest away from the green square", "bring the purple cube"]:
        r = interpret(c, holding=c.startswith("now"))
        print(f"{c!r:58} -> {r['canonical']!r}  [{r['action']}]" + (f"  note: {r.get('note')}" if r.get("note") else ""))
