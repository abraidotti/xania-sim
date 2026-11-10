"""Scanning skills/spells from source text, fingerprints that notice changes, and the shared lore store."""

import pytest

from xania_sim.record.scan import AbilityFacts, function_bodies, normalise, parse_ability_list, skill_entries
from xania_sim.record.store import KNOWN_AFTER, Lore, band_of

TABLE = '''
const struct skill_type skill_table[MAX_SKILL] = {
    {"reserved", nullptr, nullptr, "", "", nullptr, nullptr, 0, Target::Ignore, Position::Type::Standing},
    {
        "magic missile", // it's the first spell
        spell_magic_missile,
        nullptr, "magic missile", "!Magic Missile!", nullptr, nullptr, 0,
        Target::CharOffensive, Position::Type::Fighting, {1, 60}, {1, -1}
    },
    {"second attack", nullptr, nullptr, "", "", nullptr, nullptr, 0, Target::Ignore, Position::Type::Fighting},
};
'''
MAGIC = '''
void spell_magic_missile(int sn, int level, Char *ch, const SpellTarget &spell_target) {
    // don't forget: braces in strings "{" and chars '}' don't count
    static constexpr std::array dam_each = {0, 3, 3, 4, 4, 1'000};
    auto dam = number_range(dam_each[level] / 2, dam_each[level] * 2);
    if (saves_spell(level, victim)) { dam /= 2; }
    damage(ch, victim, dam, sn, DamageType::Energy);
}
void spell_other(int sn) { }
'''


def test_entries_and_function_bodies_are_extracted():
    entries = skill_entries(TABLE)
    assert set(entries) == {"reserved", "magic missile", "second attack"}
    fun, text = entries["magic missile"]
    assert fun == "spell_magic_missile" and "Target::CharOffensive" in text and text.endswith("}")
    assert entries["second attack"][0] == ""
    body = function_bodies({"magic.cpp": MAGIC}, {"spell_magic_missile"})["spell_magic_missile"]
    assert body.startswith("void spell_magic_missile") and body.rstrip().endswith("}") and "spell_other" not in body


def test_fingerprint_text_ignores_comments_and_spacing_only():
    a = normalise("int x = 1;  // note\n  /* old */ return x;")
    assert a == normalise("int x = 1;\nreturn x;")
    assert a != normalise("int x = 2; return x;")


def test_spells_and_skills_lists_parse():
    # SkillsTabulator.cpp: "L{:>2}: " on the first spell at a level, "     " after; two columns per line
    spells = ("L 1: magic missile       15m  75%     chill touch          n/a    \n"
              "L 7: blindness           20m   1%\n"
              "     faerie fire         n/a\n")
    assert parse_ability_list(spells) == {"magic missile": (1, 75), "chill touch": (1, None), "blindness": (7, 1),
                                          "faerie fire": (7, None)}
    skills = "L 1: sword               40%          dagger              40%     \nL 5: second attack        n/a     \n"
    assert parse_ability_list(skills) == {"sword": (1, 40), "dagger": (1, 40), "second attack": (5, None)}


def facts(name, fp, spell=True):
    return AbilityFacts(name, spell, "spell_x" if spell else "", "CharOffensive", "Fighting", 10, 12,
                        {"mage": 1}, fp)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def lore(tmp_path):
    clock = Clock()
    lore = Lore(tmp_path / "lore.db", clock=clock)
    lore.clock_ = clock
    yield lore
    lore.close()


def test_scans_report_new_changed_and_removed(lore):
    d = lore.apply_scan("aaa", False, {"magic missile": facts("magic missile", "f1"), "kick": facts("kick", "k1", False)})
    assert sorted(d.new) == ["kick", "magic missile"] and not d.changed
    assert lore.apply_scan("aaa", False, {"magic missile": facts("magic missile", "f1"),
                                          "kick": facts("kick", "k1", False)}).new == []
    d = lore.apply_scan("bbb", True, {"magic missile": facts("magic missile", "f2")})
    assert d.changed == ["magic missile"] and d.removed == ["kick"] and not d.new
    assert lore.abilities() == ["magic missile"] and lore.facts("kick") is None
    d = lore.apply_scan("ccc", False, {"magic missile": facts("magic missile", "f2"), "kick": facts("kick", "k1", False)})
    assert d.changed == ["kick"]  # it came back: not new, it was seen before


def test_status_follows_observations_on_the_current_code(lore):
    lore.apply_scan("aaa", False, {"magic missile": facts("magic missile", "f1")})
    assert lore.summary("magic missile", 5).status == "unknown"
    for i in range(KNOWN_AFTER):
        lore.observe("magic missile", "Brena", 5, success=i != 0, damage=8.0, target="a rat")
    s = lore.summary("magic missile", 5)
    assert (s.status, s.observations, s.success_rate, s.mean_damage) == ("known", 5, 0.8, 8.0)
    assert lore.summary("magic missile", 25).status == "unknown"  # another level band
    lore.apply_scan("bbb", False, {"magic missile": facts("magic missile", "f2")})  # rebalanced
    assert lore.summary("magic missile", 5).status == "stale"
    with pytest.raises(KeyError):
        lore.observe("fireball", "Brena", 5)


def test_claims_share_the_work_and_expire(lore):
    lore.apply_scan("aaa", False, {"magic missile": facts("magic missile", "f1"), "chill touch": facts("chill touch", "c1")})
    assert lore.claim("magic missile", 3, "Brena")
    assert not lore.claim("magic missile", 8, "Tobble")  # same band, already held
    assert lore.claim("magic missile", 3, "Brena")  # renewing your own claim is fine
    assert lore.claim("magic missile", 15, "Tobble")  # a different band
    assert lore.summary("magic missile", 3).status == "testing" and lore.summary("magic missile", 3).claimed_by == "Brena"
    assert lore.to_learn(["magic missile", "chill touch", "unheard of"], 3) == ["chill touch"]
    lore.clock_.t += 60 * 60  # Brena vanished; the claim expires
    assert lore.claim("magic missile", 3, "Tobble")
    lore.release("magic missile", 3, "Tobble")
    assert lore.summary("magic missile", 3).status == "unknown"
    assert band_of(1) == band_of(10) == 0 and band_of(11) == 1
