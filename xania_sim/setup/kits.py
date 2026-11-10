"""Gear kits: what a staged bot wears, chosen from Explorer's item tables for a (level, class, alignment, race).

A kit is a list of pieces, each a wear slot and an object vnum, plus the school gear the character keeps on in the
slots the kit leaves alone. Named kits (``KITS``):

- ``best``: the best obtainable item per slot by AC + hitroll + damroll (both are armour-class units in the to-hit
  ratio, HitChance.cpp:31-67: ``to_hit_ac -= hitroll`` against the victim's AC), and the best weapon (dice average +
  hitroll + damroll) of a type the class is good with: one it starts with and finds easy (Explorer's class profiles,
  weapons.py ``ClassProfile``: difficulty <= 3);
- ``best-ac`` / ``best-hitdam``: the same judged by AC alone (Explorer's ``ac_kits``) or hitroll + damroll alone
  (``hit_kits``);
- ``shop``: like ``best`` but only items some shopkeeper stocks (sources.py: ``G``/``E`` resets on a keeper);
- ``floor``: the weakest item per slot that still adds something (score > 0), and the weakest such weapon;
- ``bare``: nothing beyond what a new character starts with (act_wiz.cpp do_outfit: banner, vest, shield and the
  class's school weapon, Class.cpp first_weapon_vnum).

Rules from the C++ the choice follows, so every piece can actually go on:
- level: ``wear_obj`` refuses an object above the character's level (act_obj.cpp:137);
- alignment: ``equip_char`` zaps AntiGood/AntiEvil/AntiNeutral items off the matching alignment (handler.cpp:430);
- slot: ``wear`` puts an object in the *first* slot of ``wear_obj``'s order its wear flags allow (act_obj.cpp:148-
  413), and a Light-type object always in the light slot, so each item is considered for that slot only;
- two hands: a two-handed weapon (weapon flag TwoHands, value[4]: Object.cpp:117) and a shield exclude each other
  below body size Large (act_obj.cpp:337, :359);
- strength: a weapon heavier than ``str_app[str].wield`` can't be wielded (act_obj.cpp:355), so weapons are limited
  to what the race's starting strength (+3 on a strength-prime class, MudImpl.cpp:985) can hold;
- weight: everything worn and carried counts against ``can_carry_w`` (handler.cpp:156), and ``give`` refuses what
  would go over (Char.cpp:557), so a kit too heavy swaps pieces for lighter ones, losing the least score per unit
  of weight saved;
- unique: an item with extra flag Unique can't be carried twice, so a two-of slot takes the next best for the other;
- two of each neck, wrist and finger item (Worn.hpp).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from xania_explorer.assess import equipment as E
from xania_explorer.assess import weapons as WP
from xania_explorer.source.character_tables import load_classes, load_pc_races, load_stat_tables

KITS = ("best", "best-ac", "best-hitdam", "shop", "floor", "bare")
ALIGNMENTS = {"g": "good", "n": "neutral", "e": "evil"}
ANTI = {"g": "AntiGood", "n": "AntiNeutral", "e": "AntiEvil"}
# wear_obj's order (act_obj.cpp:148-413): an object goes in the first of these its wear flags allow
WEAR_ORDER = ["finger", "neck", "body", "head", "legs", "feet", "hands", "arms", "about", "waist", "wrist", "shield",
              "wield", "hold", "ears"]
# do_outfit (act_wiz.cpp:128-131) and each class's first_weapon_vnum (Class.cpp:25-67, VnumObjects.hpp:31-37)
SCHOOL_GEAR = {"light": 3716, "body": 3703, "shield": 3704}
SCHOOL_WEAPON = {"mage": 3701, "cleric": 3700, "knight": 3702, "barbarian": 3717}
MAP = 3162  # VnumObjects.hpp Map: every new character carries one (MudImpl.cpp:998)
SEE_INVISIBLE = 3041  # "a yellow potion of see invisible" (midgaard.are): what stage quaffs for invisible pieces
LARGE_SIZES = {"large", "huge", "giant"}  # BodySize >= Large: a two-hander and a shield together
TWO_HANDS = 1 << 5  # WeaponFlag::TwoHands = F
COLOUR = re.compile(r"\|.")


@dataclass(frozen=True)
class Piece:
    slot: str  # equipment.SLOTS name, "wield" or "light"
    vnum: int
    name: str  # short description, colour codes stripped
    keyword: str  # the first keyword: what to give and wear it by
    level: int
    ac: float = 0.0
    hitroll: int = 0
    damroll: int = 0
    average: float = 0.0  # weapons: dice average n * (s + 1) / 2
    two_handed: bool = False
    weapon_type: str = ""
    sources: tuple[str, ...] = ()  # shop, mob, floor, container, code
    invisible: bool = False  # extra flag Invis: the wearer must detect invisible to find it (Char.cpp:352)
    weight: int = 0

    @property
    def command(self) -> str:
        return {"wield": "wield", "hold": "hold"}.get(self.slot, "wear")

    @property
    def score(self) -> float:
        return self.ac + self.hitroll + self.damroll


@dataclass(frozen=True)
class Totals:
    ac: float
    hitroll: int
    damroll: int
    weapon_average: float
    slots: int  # pieces worn, school gear included
    weight: int = 0

    def row(self) -> list:
        return [round(self.ac, 1), self.hitroll, self.damroll, self.weapon_average, self.slots, self.weight]


@dataclass
class Kit:
    name: str
    level: int
    cls: str
    align: str  # g, n or e
    race: str
    pieces: list[Piece]  # to give and wear, armour first, the weapon last
    base: list[Piece]  # school gear that stays on (in the slots the kit doesn't fill)
    notes: list[str] = field(default_factory=list)
    remove: list[Piece] = field(default_factory=list)  # school gear to take off first (the shield, for a two-hander)
    outfit: list[Piece] = field(default_factory=list)  # all the school gear a new character wears

    @property
    def worn(self) -> list[Piece]:
        return self.base + self.pieces

    @property
    def weapon(self) -> Piece | None:
        return next((p for p in self.worn if p.slot == "wield"), None)

    def totals(self) -> Totals:
        w = self.worn
        weapon = self.weapon
        return Totals(sum(p.ac for p in w), sum(p.hitroll for p in w), sum(p.damroll for p in w),
                      weapon.average if weapon else 0.0, len(w), sum(p.weight for p in w))

    def describe(self) -> str:
        lines = [f"{self.name} kit, level {self.level} {ALIGNMENTS[self.align]} {self.race} {self.cls}:"]
        for p in self.worn:
            given = "" if p in self.pieces else "  (school gear)"
            extra = f" avg {p.average:g}{' 2H' if p.two_handed else ''}" if p.slot == "wield" else ""
            lines.append(f"  {p.slot:<7} {p.vnum:>6} {p.name[:38]:<38} L{p.level:<3} ac {p.ac:g} hit {p.hitroll} "
                         f"dam {p.damroll}{extra}{given}")
        lines += [f"  note: {n}" for n in self.notes]
        return "\n".join(lines)


def strip_colour(text: str) -> str:
    return COLOUR.sub("", text)


def natural_slot(item: E.Item) -> str | None:
    """Where ``wear`` would put it (wear_obj's order; Light objects in the light slot)."""
    if item.obj.type_name == "Light":
        return "light"
    return next((s for s in WEAR_ORDER if s in item.slots or (s == "wield" and _wieldable(item))), None)


def _wieldable(item: E.Item) -> bool:
    return "Wield" in E.F.decode(item.obj.wear_flags, E.F.WEAR)


def start_strength(cls: str, race: str, src_dir=None) -> int:
    """The race's starting strength, +3 on a strength-prime class (MudImpl.cpp:985; not for dragons)."""
    r = load_pc_races(src_dir)[race]
    return r.stats[0] + (3 if load_classes(src_dir)[cls].prime_stat == "str" and race != "dragon" else 0)


def wield_limit(cls: str, race: str, src_dir=None) -> int:
    """``str_app[str].wield``: the heaviest weapon the character can wield (act_obj.cpp:355)."""
    return load_stat_tables(src_dir).str_app[start_strength(cls, race, src_dir)][3]


def carry_limit(cls: str, race: str, level: int, src_dir=None) -> int:
    """``can_carry_w`` (handler.cpp:156): ``str_app[str].carry + level * 5 / 2``. Worn gear counts, and ``give``
    refuses what would go over ("<name> can't carry that much weight.", Char.cpp:557)."""
    return load_stat_tables(src_dir).str_app[start_strength(cls, race, src_dir)][2] + level * 5 // 2


def _piece(item: E.Item, slot: str, weapon: WP.Weapon | None = None) -> Piece:
    obj = item.obj
    return Piece(slot, obj.vnum, strip_colour(obj.short_descr), obj.keywords.split()[0], item.level,
                 item.ac if weapon is None else 0.0, item.hitroll, item.damroll,
                 weapon.average if weapon else 0.0, bool(weapon and int(obj.values[4]) & TWO_HANDS),
                 weapon.type if weapon else "", tuple(sorted({s.kind for s in item.sources})),
                 "Invis" in E.F.decode(obj.extra_flags, E.F.OBJ_EXTRA), obj.weight)


def unique(item: E.Item) -> bool:
    """Extra flag Unique: nobody may carry two (the give is refused: "<god> has forbidden <name> from possessing
    more than one ...")."""
    return "Unique" in E.F.decode(item.obj.extra_flags, E.F.OBJ_EXTRA)


def _take(ranked: list[E.Item], count: int) -> list[E.Item]:
    """``count`` pieces for a slot: the first pick again for a two-of slot, unless it's unique (then the next)."""
    out: list[E.Item] = []
    for item in ranked:
        if len(out) == count:
            break
        out += [item] * (1 if unique(item) else count - len(out))
    return out


def weapon_types(profile: WP.ClassProfile) -> set[str]:
    """Weapon types a class starts with and finds easy (knight: sword, dagger, mace; barbarian: axe), else the easy
    ones."""
    both = set(profile.starting) & set(profile.easy)
    return both or set(profile.easy)


def _allowed(item: E.Item, level: int, align: str) -> bool:
    return item.level <= level and ANTI[align] not in item.alignment_flags


class Kits:
    """Builds kits from one world; the item tables are computed once."""

    def __init__(self, world):
        self.world = world
        self.analysis = E.analyse(world)
        self.src_dir = world.src_dir
        self.by_vnum = {i.vnum: i for i in self.analysis.items}
        self.armour: dict[str, list[E.Item]] = {s: [] for s in E.SLOTS}
        for i in self.analysis.got:
            slot = natural_slot(i)
            if slot in self.armour:
                self.armour[slot].append(i)
        self.weapons = [w for w in WP.collect(world) if w.obtainable]
        self.profiles = {p.name: p for p in WP.class_profiles(self.src_dir)}
        self._outfits: dict[str, list[Piece]] = {}

    # -- choosing -------------------------------------------------------------------------------------------------
    def build(self, name: str, level: int, cls: str, align: str = "n", race: str = "human",
              two_handed: bool | None = None) -> Kit:
        """``two_handed``: None lets barbarians take a two-hander when it beats their best one-hander (others keep a
        shield); True/False forces the choice (a Large race wears both)."""
        if name not in KITS:
            raise KeyError(f"no kit named {name!r} (have {', '.join(KITS)})")
        align = align[0].lower()
        kit = Kit(name, level, cls, align, race, [], [])
        if name != "bare":
            score, pick_min, shop = _criteria(name)
            ranked = {slot: self._ranked(self.armour[slot], level, align, score, pick_min, shop) for slot in E.SLOTS}
            for slot, count in E.SLOTS.items():
                kit.pieces += [_piece(item, slot) for item in _take(ranked[slot], count)]
            self._add_weapon(kit, pick_min, shop, two_handed)
            self._fit_weight(kit, ranked, score)
        kit.base = self._school(kit)
        kit.remove = self._school(kit, removed=True)
        kit.outfit = self.outfit(cls)
        return kit

    def weight(self, kit: Kit) -> int:
        """What the worn kit weighs, school gear included."""
        return sum(p.weight for p in kit.pieces + self._school(kit))

    def weight_budget(self, kit: Kit) -> int:
        """``carry_limit`` less room for what the bot carries besides the kit while it dresses: the map, a potion
        of see invisible, and one school item on its way out."""
        spare = [self.by_vnum[v].obj.weight for v in (*SCHOOL_GEAR.values(), *SCHOOL_WEAPON.values())
                 if v in self.by_vnum]
        extra = sum(self.by_vnum[v].obj.weight for v in (MAP, SEE_INVISIBLE) if v in self.by_vnum)
        return carry_limit(kit.cls, kit.race, kit.level, self.src_dir) - extra - max(spare, default=0)

    def _fit_weight(self, kit: Kit, ranked: dict[str, list[E.Item]], score) -> None:
        """Too heavy to carry: swap pieces for lighter ones of the same slot (or leave the slot to school gear),
        each time the swap that loses least score per unit of weight saved."""
        budget, start = self.weight_budget(kit), self.weight(kit)
        swaps = []
        while (now := self.weight(kit)) > budget:
            move = self._lightest_loss(kit, ranked, score)
            if move is None:
                kit.notes.append(f"still {now} weight against a budget of {budget}")
                return
            i, new = move
            old = kit.pieces[i]
            if new is None:
                del kit.pieces[i]
            else:
                kit.pieces[i] = _piece(new, old.slot)
            swaps.append(f"{old.name} -> {new.obj.short_descr if new else '(school gear or nothing)'}")
        if swaps:
            kit.notes.append(f"lightened from {start} to {self.weight(kit)} weight (can carry {budget} with room to "
                             f"dress): " + "; ".join(swaps))

    def _lightest_loss(self, kit: Kit, ranked: dict[str, list[E.Item]], score):
        best, best_ratio = None, None
        for i, p in enumerate(kit.pieces):
            if p.slot == "wield":
                continue
            cur = self.by_vnum[p.vnum]
            taken = {q.vnum for j, q in enumerate(kit.pieces) if j != i}
            lighter = [c for c in ranked[p.slot] if c.obj.weight < cur.obj.weight
                       and not (unique(c) and c.vnum in taken)]
            for new in (lighter[:1] + [None]):
                saving = cur.obj.weight - (new.obj.weight if new else 0)
                loss = score(cur) - (score(new) if new else 0)
                ratio = loss / saving if saving > 0 else None
                if ratio is not None and (best_ratio is None or ratio < best_ratio):
                    best, best_ratio = (i, new), ratio
        return best

    def all(self, level: int, cls: str, align: str = "n", race: str = "human") -> dict[str, Kit]:
        return {n: self.build(n, level, cls, align, race) for n in KITS}

    @staticmethod
    def _ranked(items: list[E.Item], level: int, align: str, score, pick_min: bool, shop: bool) -> list[E.Item]:
        """The candidates for a slot, the one to pick first."""
        pool = [i for i in items if _allowed(i, level, align) and score(i) > 0
                and (not shop or any(s.kind == "shop" for s in i.sources))]
        if pick_min:
            return sorted(pool, key=lambda i: (score(i), i.vnum))
        return sorted(pool, key=lambda i: (-score(i), -(i.ac + i.hitroll + i.damroll), i.vnum))

    def _add_weapon(self, kit: Kit, pick_min: bool, shop: bool, two_handed: bool | None) -> None:
        types = weapon_types(self.profiles[kit.cls])
        limit = wield_limit(kit.cls, kit.race, self.src_dir)
        pool = []
        for w in self.weapons:
            item = self.by_vnum[w.vnum]
            if (w.type in types and _allowed(item, kit.level, kit.align) and w.obj.weight <= limit and w.average > 0
                    and (not shop or w.buyable)):
                pool.append(_piece(item, "wield", w))
        if not pool:
            kit.notes.append(f"no {'shop ' if shop else ''}weapon of {'/'.join(sorted(types))} at level {kit.level}")
            return

        def rank(p: Piece):
            return (p.average + p.hitroll + p.damroll, -p.vnum)

        large = load_pc_races(self.src_dir)[kit.race].size in LARGE_SIZES
        one = [p for p in pool if not p.two_handed]
        two = [p for p in pool if p.two_handed]
        best = min if pick_min else max
        w1 = best(one, key=rank) if one else None
        w2 = best(two, key=rank) if two else None
        if large:
            choice = best([w for w in (w1, w2) if w], key=rank)
        elif two_handed is True or (two_handed is None and kit.cls == "barbarian" and w2 and not pick_min
                                    and (not w1 or rank(w2) > rank(w1))):
            choice = w2 or w1
        else:
            choice = w1 or w2
        if choice.two_handed and not large:
            dropped = [p for p in kit.pieces if p.slot == "shield"]
            kit.pieces = [p for p in kit.pieces if p.slot != "shield"]
            kit.notes.append(f"two-handed {choice.name}: no shield"
                             + (f" (drops {dropped[0].name})" if dropped else ""))
        kit.pieces.append(choice)

    def outfit(self, cls: str) -> list[Piece]:
        """What a new character of ``cls`` wears (act_wiz.cpp do_outfit)."""
        if cls not in self._outfits:
            out = []
            for slot, vnum in [*SCHOOL_GEAR.items(), ("wield", SCHOOL_WEAPON[cls])]:
                if vnum in self.by_vnum:
                    w = next((w for w in self.weapons if w.vnum == vnum), None) if slot == "wield" else None
                    out.append(_piece(self.by_vnum[vnum], slot, w))
            self._outfits[cls] = out
        return list(self._outfits[cls])

    def _school(self, kit: Kit, removed: bool = False) -> list[Piece]:
        """The outfit pieces that stay on, in the slots the kit doesn't fill; with ``removed``, the ones to take off
        first instead (the shield, beside a two-hander, below body size Large)."""
        filled = {p.slot for p in kit.pieces}
        weapon = next((p for p in kit.pieces if p.slot == "wield"), None)
        no_shield = weapon is not None and weapon.two_handed and \
            load_pc_races(self.src_dir)[kit.race].size not in LARGE_SIZES
        off = [p for p in self.outfit(kit.cls) if p.slot not in filled and p.slot == "shield" and no_shield]
        return off if removed else [p for p in self.outfit(kit.cls) if p.slot not in filled and p not in off]


def _criteria(name: str):
    """(score, pick the weakest?, shops only?) for a kit name."""
    if name == "best-ac":
        return (lambda i: i.ac), False, False
    if name == "best-hitdam":
        return (lambda i: i.hitroll + i.damroll), False, False
    return (lambda i: i.ac + i.hitroll + i.damroll), name == "floor", name == "shop"


def totals_table(kits: dict[str, Kit]) -> str:
    """A plain-text table of each kit's on-paper totals."""
    rows = [f"{'kit':<12} {'AC':>6} {'hit':>4} {'dam':>4} {'wpn avg':>7} {'worn':>4} {'weight':>6}  weapon"]
    for name, k in kits.items():
        t = k.totals()
        w = k.weapon
        rows.append(f"{name:<12} {t.ac:>6.1f} {t.hitroll:>4} {t.damroll:>4} {t.weapon_average:>7.1f} {t.slots:>4} "
                    f"{t.weight:>6}  {w.name if w else '-'}")
    return "\n".join(rows)


_KITS: dict[int, Kits] = {}


def kits_for(world) -> Kits:
    """One ``Kits`` per loaded world."""
    if id(world) not in _KITS:
        _KITS[id(world)] = Kits(world)
    return _KITS[id(world)]


def build_kit(world, name: str, level: int, cls: str, align: str = "n", race: str = "human",
              two_handed: bool | None = None) -> Kit:
    return kits_for(world).build(name, level, cls, align, race, two_handed)
