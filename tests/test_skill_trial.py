"""The skill trial's decisions: who learns a skill, what kind it is, and what counts as a finding."""

from xania_sim.trials.skill import ClassRun, _findings, _note, kind_of, learners


def test_learners_are_the_classes_it_belongs_to():
    assert learners("bash") == {"knight": 5, "barbarian": 1}
    assert learners("fireball") == {"mage": 22}  # the others only get it at 60, like cross-class training
    assert learners("no such skill") == {}


def test_kinds():
    assert kind_of("bash", False) == "combat skill"
    assert kind_of("sanctuary", True) == "buff"
    assert kind_of("cure light", True) == "heal"
    assert kind_of("fireball", True) == "spell"
    assert kind_of("hide", False) == "skill"


def test_findings():
    clean = ClassRun("knight", 5, uses=6, successes=4)
    assert _findings(clean, "bash", "repro") == []
    broken = ClassRun("knight", 5, uses=3, silent=1, unknown="Bashing? What's that?",
                      practice_reply="You can't practice that.")
    sigs = {f.signature for f in _findings(broken, "bash", "repro")}
    assert sigs == {"skill:bash:unknown:knight", "skill:bash:practice:knight", "skill:bash:silent"}


def test_messages_keep_distinct_first_lines():
    run = ClassRun("knight", 5)
    _note(run, True, "You slam into the drunk, and send him flying!\n<20hp 100m>")
    _note(run, True, "You slam into the drunk, and send him flying!")
    _note(run, False, "You fall flat on your face!")
    assert run.messages == {"ok": ["You slam into the drunk, and send him flying!"],
                            "failed": ["You fall flat on your face!"]}
    s = ClassRun("mage", 1, uses=4, successes=3, damage_seen=[10, 14], practice_from=1, practice_to=12,
                 percent_before_use=75, percent_after_use=77).summary()
    assert s["success_rate"] == 0.75 and s["damage_seen"] == 12.0 and s["practice"] == "1% -> 12%" and s["use_gain"] == 2
