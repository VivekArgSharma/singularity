"""Grader: every rule in the agreed contract, and the four outcomes.

Run:  .venv\\Scripts\\python tests\\test_grader.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
from grader import grade, resolve_targets  # noqa: E402
from robot import Robot  # noqa: E402
from world import World  # noqa: E402

fails = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({info})" if info and not cond else ""))
    if not cond:
        fails.append(name)


def main():
    w = World("level1", 0)
    o = {x.id: x for x in w.objects}
    t = lambda c: resolve_targets(c, w.objects, w.zone_c)  # noqa: E731
    cases = [
        ("colour + shape", "Bring the red cube to the green square.", {"o1"}),
        ("shape only", "Bring the cone to the green square.", {"o6"}),
        ("colour only (two red things: either counts)", "Bring something red.", {"o1", "o2"}),
        ("round = sphere", "Bring the round red thing.", {"o2"}),
        ("ball = sphere, box = cube", "Fetch the blue ball.", {"o4"}),
        ("can = cylinder", "Bring the can to the green zone.", {"o5"}),
        ("tallest", "Bring the tallest object.", {"o5"}),
        ("smallest: tie between the two equal spheres, both count", "Bring the smallest object.", {"o2", "o4"}),
        ("biggest: cube 0.0270 vs can 0.0271 m^3 is a near-tie, both count", "Bring the biggest thing.", {"o1", "o5"}),
        ("farthest from the zone", "Bring the object farthest from the green square.", {"o1"}),
        ("closest to the zone", "Bring the object closest to the green square.", {"o4"}),
        ("destination words are not object words", "Bring the blue cube to the green square.", {"o3"}),
    ]
    for name, cmd, want in cases:
        got = t(cmd)
        check(f"{name}: {cmd!r}", got == want, got)
    check("indirect command -> manual Y/N (no automatic rule)", t("Bring me something to drink.") is None)
    check("object that is not in the room -> nothing counts as right",
          t("Bring the white block to the green block.") == set() and
          grade(o["o1"], True, t("Bring the white block to the green block.")) == "fail_wrong_object")
    check("'green block / box' is the drop zone, not an object", t("Bring the red block to the green box.") == {"o1"})

    # the local planner reads commands the same way (and never substitutes)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
    import local_brain as L
    parse = lambda c: L.LocalBrain(None, lambda *a, **k: None, c, mapping=False)  # noqa: E731
    b = parse("bring the white block to the green bloack")
    check("typo 'bloack' -> 'block'; green block = zone", "green block" in b.text and bool(L.ZONE.search(b.text)), b.text)
    check("unknown colour kept as a constraint (not dropped)", b.objects() == [{"color": "white", "shape": "cube", "name": "white cube"}],
          b.objects())
    b.map = {("red", "cube"): ((0, 0), 0.15)}
    try:
        b._check_exists(b.objects())
        refused = False
    except L.Fail as e:
        refused = "white cube" in str(e)
    check("planner refuses to fetch something it cannot see, instead of substituting", refused)
    check("'sqaure' -> 'square'", "green square" in parse("Bring the red cube to the green sqaure.").text)
    check("real words are never 'corrected' ('round' stays round -> red sphere)",
          parse("Bring the round red thing to the green square.").objects() ==
          [{"color": "red", "shape": "sphere", "name": "red sphere"}])
    check("'around' still means orbit", parse("Circle around the blue sphere twice.").objects()[0]["shape"] == "sphere")
    check("unparseable -> manual", t("Do the thing.") is None)

    # typos: any small misspelling of a command word is understood; real English words are left alone
    import typos as TY
    for bad, good in (("sqaure", "square"), ("rde", "red"), ("yelow", "yellow"), ("brign", "bring"), ("cbue", "cube"),
                      ("spehre", "sphere"), ("gren", "green"), ("bleu", "blue"), ("fecth", "fetch"),
                      ("cilinder", "cylinder"), ("twise", "twice"), ("arround", "around"), ("circel", "circle"),
                      ("orang", "orange"), ("pruple", "purple"), ("whte", "white"), ("teh", "the")):
        check(f"typo '{bad}' -> '{good}'", TY.fix(bad) == good, TY.fix(bad))
    check("real words never changed (hold/think/make/book/call/fast/found)",
          TY.fix("hold think make book call fast found") == "hold think make book call fast found")
    check("context: 'red cub' -> red cube, 'read cube' -> red cube",
          TY.fix("the red cub") == "the red cube" and TY.fix("the read cube") == "the red cube")
    check("whole typo sentence -> right objects",
          parse("brign teh rde cbue to teh gren sqaure").objects() == [{"color": "red", "shape": "cube", "name": "red cube"}])
    check("grader reads typos the same way", t("brign teh rde cbue to teh gren sqaure") == {"o1"})
    check("'can you ...' is not a drink can", t("Can you bring the red cube to the green square?") == {"o1"} and
          parse("can you bring the red cube to the green square").objects()[0]["name"] == "red cube")

    # "near X": the object before "near" is the one moved; it must end up next to X
    import grader as G2
    c = "pick up the blue cube and place it near the red cube"
    check("near: target is the blue cube, reference the red cube",
          t(c) == {"o3"} and G2.resolve_reference(c, w.objects, w.zone_c) == {"o1"})
    bc, rc = o["o3"], o["o1"]
    bx, by = bc.x, bc.y
    bc.x, bc.y = rc.x + rc.r + bc.r + 0.2, rc.y
    check("near: 20 cm from the red cube -> pass", G2.grade_near(bc, [rc], {"o3"}) == "pass")
    bc.x = rc.x + 1.2
    check("near: 1 m away -> fail_not_reached", G2.grade_near(bc, [rc], {"o3"}) == "fail_not_reached")
    check("near: wrong object -> fail_wrong_object", G2.grade_near(rc, [o["o4"]], {"o3"}) == "fail_wrong_object")
    bc.x, bc.y = bx, by
    check("zone commands unaffected by the near rule", G2.resolve_reference("Bring the red cube to the green square.", w.objects, w.zone_c) is None)

    # other names for the drop square are understood, never refused
    for c in ("Bring the red cube to the destination.", "take the red cube to the finish line", "put the red cube on the green tile",
              "bring the red cube to the goal", "move the red cube to the drop zone", "Bring the red cube to the finish state",
              "bring the red cube to the tile"):
        b = parse(c)
        try:
            b._check_exists(b.objects())
            ok = True
        except L.Fail:
            ok = False
        check(f"destination word: '{c}'", ok and b.objects()[0]["name"] == "red cube" and bool(L.ZONE.search(b.text))
              and t(c) == {"o1"}, (b.objects(), t(c)))

    # phrasing bank: many ways testers may say the same task (tools/eval_phrasing.py)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
    import eval_phrasing as EP
    for cmd, act, obj_, dest, holding in EP.BANK:
        got = EP.dry(cmd, holding)
        check(f"phrasing: {cmd!r}", got is not None and got[0] in act.split("|") and (obj_ is None or got[1] == obj_)
              and (dest is None or got[2] == dest), got)

    # command interpreter: free English -> one standard command that reads back as the same intent
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
    import interpret as IP
    for cmd, act, obj_, dest, holding in EP.BANK:
        r = IP.interpret(cmd, holding)
        if r["action"] in ("fetch", "near", "pick", "orbit", "approach", "find", "deliver_held", "goto_zone", "drop"):
            back, _ = IP.dry(r["canonical"], holding)
            check(f"interpreter: {cmd!r} -> {r['canonical']!r} reads the same", back[:2] == (r["action"], r["object"]), back)
    check("interpreter: missions pass through unchanged",
          IP.interpret("Mission maze: explore the maze and stop on the magenta exit tile.")["changed"] is False)

    # not present: never substitute, say so
    check("grader: unknown shape -> nothing matches", t("Bring the red pyramid to the green square.") == set())
    for cmd, why in (("Bring the red pyramid to the green square.", "red pyramid"), ("Fetch the star.", "star"),
                     ("Bring the green cube.", "green cube"), ("Bring the purple sphere.", "purple sphere")):
        b = parse(cmd)
        b.map = {("red", "cube"): ((0, 0), 0.15)}
        try:
            b._check_exists(b.objects())
            msg = ""
        except L.Fail as e:
            msg = str(e)
        check(f"'{cmd}' -> Not present ({why})", msg.startswith("Not present") and why in msg, msg)

    # the four outcomes (+ manual)
    targets = t("Bring the red cube to the green square.")
    check("right object in zone -> pass", grade(o["o1"], True, targets) == "pass")
    check("wrong object in zone -> fail_wrong_object", grade(o["o2"], True, targets) == "fail_wrong_object")
    check("right object outside zone -> fail_not_reached", grade(o["o1"], False, targets) == "fail_not_reached")
    check("nothing delivered -> fail_not_reached", grade(None, False, targets) == "fail_not_reached")
    check("indirect command delivered -> needs_manual", grade(o["o5"], True, None) == "needs_manual")
    r = Robot("level1", 0)
    r.set_command("Bring me something to drink.", "g")
    r.outcome = "needs_manual"
    check("manual verdict Y -> pass", r.verdict(True) == "pass")
    r.outcome = "needs_manual"
    check("manual verdict N -> fail_wrong_object", r.verdict(False) == "fail_wrong_object")
    r.outcome = "pass"
    check("verdict cannot overwrite an automatic grade", r.verdict(False) == "pass")
    r = Robot("level1", 0)
    r.set_command("x", "g")
    for _ in range(4):
        r._enter_blocked()
    check("4th Blocked -> fail_collision", r.outcome == "fail_collision" and r.state == "Done", (r.outcome, r.state))
    r = Robot("level1", 0)
    r.set_command("x", "g")
    r.give_up()
    check("give up -> fail_not_reached", r.outcome == "fail_not_reached")
    # 2 s without progress -> Blocked (wheels turning but the robot not getting anywhere)
    import robot as robot_mod
    r = Robot("level1", 0)
    r.set_command("x", "g")
    real = robot_mod.forward
    robot_mod.forward = lambda h: (0.0, 0.0)          # no progress, and nothing to collide with
    try:
        gen, steps = r.move(3.0), 0
        try:
            while True:
                next(gen)
                steps += 1
        except StopIteration as st:
            res = st.value
    finally:
        robot_mod.forward = real
    check("no progress for 2 s -> collided + Blocked", res["collided"] is True and r.state == "Blocked" and r.retries == 1,
          (res, r.state, r.retries))
    check("... detected after 2 s (+0.1 s stop, +0.1 m back-off)", abs(steps * robot_mod.DT - 2.2) < 0.05, steps * robot_mod.DT)
    print(f"\n{len(fails)} failed")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
