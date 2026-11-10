"""Findings from crash reports and xania's BUG lines, their grouping, and the dispatch site built from them."""

import json

import pytest

from xania_sim.report import dispatch as D
from xania_sim.record.findings import Journal, bug_line_findings, crash_finding, project_frames
from xania_sim.record.store import Lore

# From a real hunt (trimmed): autoloot's "get all corpse" reading past the end of "all"
AUTOLOOT = """2026-11-09 04:30:13Z :: TICK
/usr/include/c++/15/string_view:260: constexpr ... operator[](size_type) const ...: Assertion '__pos < this->_M_len' failed.

Program received signal SIGABRT, Aborted.
__pthread_kill_implementation (threadid=<optimized out>, signo=6, no_tid=0) at ./nptl/pthread_kill.c:44

=== xania-sim: backtrace ===
#0  __pthread_kill_implementation (threadid=<optimized out>, signo=6, no_tid=0) at ./nptl/pthread_kill.c:44
#5  0x00007ffff74ae716 in std::__glibcxx_assert_fail(char const*, int, char const*, char const*) () from /usr/lib/x86_64-linux-gnu/libstdc++.so.6
#6  0x00005555570563fd in std::basic_string_view<char, std::char_traits<char> >::operator[] (this=0x7bfff4c0da20, __pos=3) at /usr/include/c++/15/string_view:260
#7  0x00005555579322dd in do_get (ch=0x7d5ff5e23380, args=...) at /home/alessandro/xania/src/act_obj.cpp:707
#8  0x00005555573d3ca5 in loot_and_sacrifice_corpse (looter=0x7d5ff5e23380, victim=0x7d5ff5c08580, victim_room_vnum=3713) at /home/alessandro/xania/src/fight.cpp:565
#9  0x00005555573d9630 in damage (ch=0x7d5ff5e23380, victim=0x7d5ff5c08580, raw_damage=1, atk_type=std::variant [index 0] = {...}, damage_type=DamageType::Slash) at /home/alessandro/xania/src/fight.cpp:813
#15 0x00005555570218e4 in MudImpl::game_loop (this=0x7d7ff5be0080) at /home/alessandro/xania/src/MudImpl.cpp:332
"""
GET_ALL = AUTOLOOT.replace("act_obj.cpp:707", "act_obj.cpp:648").replace(
    "loot_and_sacrifice_corpse (looter=0x7d5ff5e23380, victim=0x7d5ff5c08580, victim_room_vnum=3713) at "
    "/home/alessandro/xania/src/fight.cpp:565", "std::__invoke_impl<void> (__f=...) at /usr/include/c++/15/bits/invoke.h:63")


def test_backtraces_are_reduced_to_xanias_own_frames():
    frames = project_frames(AUTOLOOT)
    assert [(f.func, f.file, f.line) for f in frames[:3]] == [
        ("do_get", "act_obj.cpp", 707), ("loot_and_sacrifice_corpse", "fight.cpp", 565), ("damage", "fight.cpp", 813)]
    f = crash_finding(AUTOLOOT, "python -m xania_sim hunt")
    assert f.signature == "crash:SIGABRT:act_obj.cpp:do_get" and f.severity == "high"
    assert f.title == "xania crashed (SIGABRT, failed assertion) in do_get (act_obj.cpp)"
    assert "Assertion '__pos < this->_M_len' failed" in f.summary and "loot_and_sacrifice_corpse" in f.summary


def test_two_triggers_of_one_faulty_function_group_together():
    assert crash_finding(GET_ALL).signature == crash_finding(AUTOLOOT).signature
    assert "act_obj.cpp:648" in crash_finding(GET_ALL).summary


def test_bug_lines_group_by_message_ignoring_numbers():
    log = ("x :: [*****] BUG: Reset_room: 'M': bad vnum 1234.\ny :: [*****] BUG: Reset_room: 'M': bad vnum 99.\n"
           "z :: [*****] BUG: Nanny: bad d->state() 7.\n")
    found = bug_line_findings(log)
    assert len(found) == 2 and found[0].signature == "bug:Reset_room: 'M': bad vnum #."
    assert len(found[0].evidence["lines"]) == 2 and found[0].severity == "medium"


@pytest.fixture
def journal(tmp_path):
    lore = Lore(tmp_path / "lore.db")
    yield Journal(lore.db)
    lore.close()


def test_runs_record_and_group_findings(journal):
    r1 = journal.start_run("hunt", "Hunt: angry cave beast", "abc123")
    assert journal.record(r1, crash_finding(AUTOLOOT, "hunt"), "abc123")
    journal.finish_run(r1, fights=4, crashed=True)
    r2 = journal.start_run("tests", "Live scenarios", "def456")
    assert not journal.record(r2, crash_finding(GET_ALL, "pytest"), "def456")
    [f] = journal.findings()
    assert (f["count"], f["first_commit"], f["last_commit"], f["status"], f["repro"]) == (2, "abc123", "def456", "new", "hunt")
    assert [o["run"] for o in journal.occurrences(f["signature"])] == [r2, r1]
    sig = journal.find("do_get")
    journal.set_status(sig, "known")
    journal.set_link(sig, "https://github.com/abraidotti/xania/issues/1")
    assert journal.findings()[0]["status"] == "known"
    with pytest.raises(ValueError):
        journal.set_status(sig, "fixed?")


def test_the_dispatch_site_is_built_and_escaped(journal, tmp_path):
    run = journal.start_run("hunt", "Hunt: <script>", "abc123")
    journal.record(run, crash_finding(AUTOLOOT, "python -m xania_sim hunt"), "abc123")
    journal.finish_run(run, fights=4)
    site = D.build(journal, tmp_path / "site")
    index = (site / "index.html").read_text(encoding="utf-8")
    assert "do_get (act_obj.cpp)" in index and "<script>" not in index and "&lt;script&gt;" in index
    [page] = list((site / "findings").glob("*.html"))
    text = page.read_text(encoding="utf-8")
    assert "python -m xania_sim hunt" in text and "loot_and_sacrifice_corpse" in text and "Backtrace" in text
    data = json.loads((site / "dispatch.json").read_text(encoding="utf-8"))
    assert data["findings"][0]["count"] == 1 and data["runs"][0]["summary"] == {"fights": 4}


def test_an_address_sanitizer_report_becomes_a_finding():
    from pathlib import Path

    from xania_sim.record.findings import asan_stacks, crash_finding

    report = (Path(__file__).parent / "asan_report.txt").read_text(encoding="utf-8")  # a real one, 2026-11-09
    f = crash_finding(report, "python -m pytest scenarios")
    assert f.signature == "crash:asan-heap-use-after-free:Descriptor.hpp:Descriptor::is_switched"
    assert f.title == "xania crashed (AddressSanitizer: heap-use-after-free) in Descriptor::is_switched (Descriptor.hpp)"
    access, freed, allocated = asan_stacks(report)
    assert [fr.func for fr in access[:4]] == ["Descriptor::is_switched", "Char::get_trust", "Char::is_immortal",
                                              "char_update"]
    assert freed[0].func == "DescriptorList::reap_closed" and allocated[0].func == "DescriptorList::create"
