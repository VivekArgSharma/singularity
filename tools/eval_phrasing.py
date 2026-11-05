"""Phrasing bank: many ways people say the same task. Dry-runs the local planner's command reading (no robot
motion: every skill is replaced by a recorder) and the grader's target choice, and checks both.

    .venv\\Scripts\\python tools\\eval_phrasing.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "engine"), str(ROOT / "agent")]
import local_brain as L  # noqa: E402
from grader import resolve_reference, resolve_targets  # noqa: E402
from world import World  # noqa: E402

W = World("level1", 0)
IDS = {"red cube": "o1", "red sphere": "o2", "blue cube": "o3", "blue sphere": "o4", "yellow cylinder": "o5", "orange cone": "o6"}

# (command, expected action, expected object, expected destination: "zone" | object name | None, holding at start)
BANK = [
    # fetch to the square, many verbs and names
    ("Bring the red cube to the green square.", "fetch", "red cube", "zone", False),
    ("bring red cube to green square", "fetch", "red cube", "zone", False),
    ("Fetch the red block and drop it in the green area", "fetch", "red cube", "zone", False),
    ("get the red box onto the green mat", "fetch", "red cube", "zone", False),
    ("carry the red cube over to the green zone please", "fetch", "red cube", "zone", False),
    ("can you take the red cube to the green tile?", "fetch", "red cube", "zone", False),
    ("could you please move the red cube into the green square", "fetch", "red cube", "zone", False),
    ("I want you to deliver the red cube to the goal", "fetch", "red cube", "zone", False),
    ("hey robot, put the red cube on the finish", "fetch", "red cube", "zone", False),
    ("place the red cube at the destination", "fetch", "red cube", "zone", False),
    ("drop the red cube on the green tile", "fetch", "red cube", "zone", False),
    ("leave the red cube in the drop zone", "fetch", "red cube", "zone", False),
    ("go get the red cube and bring it to the green square", "fetch", "red cube", "zone", False),
    ("go and grab the red cube then put it on the green square", "fetch", "red cube", "zone", False),
    ("pick up the red cube and put it on the green tile", "fetch", "red cube", "zone", False),
    ("pick the red cube up and place it in the green square", "fetch", "red cube", "zone", False),
    ("grab the red cube, carry it to the finish line", "fetch", "red cube", "zone", False),
    ("Please bring me the red cube", "fetch", "red cube", "zone", False),
    ("fetch me the blue ball", "fetch", "blue sphere", "zone", False),
    ("bring the blue ball to the green square", "fetch", "blue sphere", "zone", False),
    ("take the blue orb to the green square", "fetch", "blue sphere", "zone", False),
    ("bring the round red thing", "fetch", "red sphere", "zone", False),
    ("bring the red round object to the green square", "fetch", "red sphere", "zone", False),
    ("Bring the orange cone to the green square", "fetch", "orange cone", "zone", False),
    ("bring the traffic cone to the green square", "fetch", "cone", "zone", False),
    ("bring the orange thing to the green square", "fetch", "orange object", "zone", False),
    ("bring the yellow can to the green square", "fetch", "yellow cylinder", "zone", False),
    ("bring the yellow tube to the green square", "fetch", "yellow cylinder", "zone", False),
    ("bring me something to drink", "fetch", "drink can", "zone", False),
    ("I'm thirsty", "fetch", "drink can", "zone", False),
    ("get me a soda", "fetch", "drink can", "zone", False),
    ("Bring the tallest object.", "fetch", "tallest object", "zone", False),
    ("bring the highest thing to the green square", "fetch", "highest object", "zone", False),
    ("bring the biggest object", "fetch", "biggest object", "zone", False),
    ("Bring the cube that is farthest from the green square.", "fetch", "farthest cube", "zone", False),
    ("bring the cube furthest away from the green square", "fetch", "farthest cube", "zone", False),
    ("bring the blue cube to the green sqaure", "fetch", "blue cube", "zone", False),
    ("brng the bleu cub to the gren squar", "fetch", "blue cube", "zone", False),
    # pick only
    ("pick up the orange cone", "pick", "orange cone", None, False),
    ("grab the orange cone", "pick", "orange cone", None, False),
    ("lift the blue sphere", "pick", "blue sphere", None, False),
    ("get the yellow cylinder", "pick", "yellow cylinder", None, False),
    # follow-ups while holding
    ("now put it on the green tile", "deliver_held", None, "zone", True),
    ("take it to the destination", "deliver_held", None, "zone", True),
    ("drop it in the green square", "deliver_held", None, "zone", True),
    ("bring it to the finish", "deliver_held", None, "zone", True),
    ("put it down", "drop", None, None, True),
    ("drop it", "drop", None, None, True),
    ("let go", "drop", None, None, True),
    # next to another object
    ("put the red cube next to the blue sphere", "near", "red cube", "blue sphere", False),
    ("place the red cube near the blue cube", "near", "red cube", "blue cube", False),
    ("take red cube and put it beside the blue cube", "near", "red cube", "blue cube", False),
    ("move the yellow can close to the orange cone", "near", "yellow cylinder", "orange cone", False),
    ("put the blue ball by the red box", "near", "blue sphere", "red cube", False),
    # other skills
    ("Circle around the blue sphere twice.", "orbit", "blue sphere", None, False),
    ("go round the orange cone two times", "orbit", "orange cone", None, False),
    ("drive in a circle around the red cube", "orbit", "red cube", None, False),
    ("go to the green square", "goto_zone", None, "zone", False),
    ("drive to the finish", "goto_zone", None, "zone", False),
    ("go to the blue sphere", "approach", "blue sphere", None, False),
    ("approach the red cube", "approach", "red cube", None, False),
    ("find the yellow cylinder", "find", "yellow cylinder", None, False),
    ("where is the orange cone", "find", "orange cone", None, False),
    ("explore the room", "explore|rotate", None, None, False),
    ("look around the room", "explore|rotate", None, None, False),
    ("map the room", "explore|rotate", None, None, False),
    ("turn 90 degrees to the left", "rotate", None, None, False),
    ("move forward 1 metre", "move", None, None, False),
    # not present -> refused
    ("bring the purple cube to the green square", "refuse", None, None, False),
    ("bring the red pyramid to the green square", "refuse", None, None, False),
]


class Done(Exception):
    pass


class Dry(L.LocalBrain):
    """Command reading only: every robot skill records what it would do and stops."""
    def __init__(self, cmd, holding):
        super().__init__(None, lambda *a, **k: None, cmd, mapping=False)
        self.holding = holding
        self.got = None

    def st(self):
        self.s = {"name": "Locate", "x": -2.4, "y": 0.0, "heading": 0.0, "holding": self.holding, "retries": 0}
        return self.s

    def _rec(self, action, obj=None, dest=None):
        self.got = (action, obj, dest)
        raise Done()

    def see(self):
        return []

    def resolve_superlative(self, spec):
        self._rec("fetch", spec["name"], "zone")

    def pick(self, tgt):
        self._pending = tgt
        self._rec_pick = tgt

    def deliver_zone(self):
        tgt = getattr(self, "_rec_pick", None)
        self._rec("deliver_held" if tgt is None else "fetch", tgt["name"] if tgt else None, "zone")

    def approach(self, tgt, stand):
        if getattr(self, "_rec_pick", None) is not None:
            self._rec("near", self._rec_pick["name"], tgt["name"])
        self._rec("approach", tgt["name"])

    def find(self, tgt, sweeps=9):
        return {"bearing": 0.0}

    def measure(self, tgt):
        self._rec("find", tgt["name"])

    def orbit(self, tgt, loops, sweep=None, finish_facing=False):
        self._rec("orbit", tgt["name"])

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


def dry(cmd, holding):
    import explore
    b = Dry(cmd, holding)
    real_explore = explore.explore_nbv
    explore.explore_nbv = lambda *a, **k: b._rec("explore")
    b.cmap = type("M", (), {"stats": lambda self: {}, "objects": {}})() if False else None
    b.cl = type("C", (), {"turn": lambda self, *a: b._rec("rotate"), "move": lambda self, *a: b._rec("move"),
                          "depth": lambda self: 1.0, "scan": lambda self, n=91: []})()
    try:
        b._run()
        if b.got is None and getattr(b, "_rec_pick", None) is not None:
            b.got = ("pick", b._rec_pick["name"], None)
    except Done:
        if b.got is None and getattr(b, "_rec_pick", None) is not None:
            b.got = ("pick", b._rec_pick["name"], None)
    except L.Fail as e:
        b.got = ("refuse", None, None) if ("Not present" in str(e) or "can't find" in str(e)) else ("not_understood", str(e)[:60], None)
    finally:
        explore.explore_nbv = real_explore
    return b.got


def main():
    bad = 0
    for cmd, act, obj, dest, holding in BANK:
        if "explore" in act:
            pass
        got = dry(cmd, holding)
        ok = got is not None and got[0] in act.split("|") and (obj is None or got[1] == obj) and (dest is None or got[2] == dest)
        gtxt = ""
        if act in ("fetch", "near") and (obj in IDS or obj == "orange object"):
            t = resolve_targets(cmd, W.objects, W.zone_c)
            ok_g = t == {IDS.get(obj, "o6")}
            if act == "near":
                ok_g = ok_g and resolve_reference(cmd, W.objects, W.zone_c) == {IDS[dest]}
            if not ok_g:
                gtxt = f"  GRADER targets={t}"
                ok = False
        bad += 0 if ok else 1
        print(("PASS " if ok else "FAIL ") + f"{cmd!r:62} -> {got}{gtxt}")
    print(f"\n{len(BANK) - bad}/{len(BANK)} understood as intended")
    return bad


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
