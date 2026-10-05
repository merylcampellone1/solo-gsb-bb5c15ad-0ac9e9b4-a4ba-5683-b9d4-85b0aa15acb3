"""Exact microplate liquid-planning solver.

Volumes are integer nanoliters everywhere.  Concentrations are ``fractions.Fraction``.

The solver builds a directed acyclic blending graph:

* source nodes  - stocks and the diluent (no incoming transfers)
* recipe nodes  - intermediate liquids held in finite-capacity containers,
                  each produced once by mixing a "high" parent and a "low"
                  parent in an exact rational ratio
* well nodes    - target wells (sinks, never used as a source)

Every edge volume is an integer number of pipette step units and must be
expressible as a sum of individual transfers in [pip_min, pip_max].  A
depth-first search assigns each well one or two source fills; whenever no
existing liquid can fill a well, candidate recipe (serial-dilution) nodes are
synthesized and their shared batch size is grown bottom-up so that every
descending transfer stays integral, transferable and within container
capacity.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Optional


# --------------------------------------------------------------------------- #
# Nodes / recipes
# --------------------------------------------------------------------------- #

# node tuples: ('stock', id), ('d', None), ('recipe', global_key)
STOCK = "stock"
DILUENT = "d"
RECIPE = "recipe"


def stock_node(stock_id: str) -> tuple:
    return (STOCK, stock_id)


DILUENT_NODE = (DILUENT, None)


@dataclass
class Recipe:
    key: int
    hi: tuple
    lo: tuple
    frac: Fraction  # fraction of hi in the blend
    depth: int


@dataclass
class Well:
    id: str
    volume: int
    conc: Fraction
    capacity: int


@dataclass
class Problem:
    stocks: dict               # id -> {"conc": Fraction, "volume": int}
    diluent_volume: int
    wells: list[Well]
    containers: list[int]      # capacities in nl, each holds at most one recipe
    pip_min: int
    pip_max: int
    pip_step: int
    max_depth: int = 3
    max_search_nodes: int = 200_000
    max_enumeration_units: int = 20_000
    max_factor: int = 200_000

    # populated during solving
    recipes: dict = field(default_factory=dict)
    _recipe_counter: int = 0
    _dedup: dict = field(default_factory=dict)
    _conc_cache: dict = field(default_factory=dict)

    def conc(self, node: tuple) -> Fraction:
        kind = node[0]
        if kind == STOCK:
            return self.stocks[node[1]]["conc"]
        if kind == DILUENT:
            return Fraction(0)
        cached = self._conc_cache.get(node)
        if cached is not None:
            return cached
        r = self.recipes[node[1]]
        v = self.conc(r.hi) * r.frac + self.conc(r.lo) * (1 - r.frac)
        self._conc_cache[node] = v
        return v

    def depth(self, node: tuple) -> int:
        kind = node[0]
        if kind in (STOCK, DILUENT):
            return 0
        return self.recipes[node[1]].depth

    def get_or_create_recipe(self, hi: tuple, lo: tuple, frac: Fraction) -> tuple:
        sig = (hi, lo, frac)
        existing = self._dedup.get(sig)
        if existing is not None:
            return existing, False
        self._recipe_counter += 1
        key = self._recipe_counter
        self.recipes[key] = Recipe(
            key=key,
            hi=hi,
            lo=lo,
            frac=frac,
            depth=max(self.depth(hi), self.depth(lo)) + 1,
        )
        self._dedup[sig] = key
        return key, True

    def remove_recipe(self, key: int, sig: tuple) -> None:
        self._conc_cache.pop((RECIPE, key), None)
        del self.recipes[key]
        del self._dedup[sig]
        self._recipe_counter -= 1


@dataclass
class Option:
    """One candidate way of filling a single well.

    fills: (node_spec, amount_nl); node specs are either resolved node tuples
           or ("local", local_key) references into new_specs.
    new_specs: creation order (parents first) of (local_key, hi_spec, lo_spec,
               Fraction).  Node specs may themselves reference earlier locals.
    """

    fills: list
    new_specs: list
    kind: str

    def signature(self) -> tuple:
        return (tuple(self.fills), tuple(self.new_specs), self.kind)


# --------------------------------------------------------------------------- #
# Transfer feasibility primitives
# --------------------------------------------------------------------------- #


def _ceil(a: int, b: int) -> int:
    return -((-a) // b)


@dataclass
class Pip:
    k0: int           # minimum positive transfer, in step units
    k1: int           # maximum transfer, in step units
    step: int


def make_pip(pmin: int, pmax: int, step: int) -> Pip:
    k0 = _ceil(pmin, step)
    k1 = pmax // step
    if k0 > k1:
        raise ValueError("pipette range contains no step-aligned volume")
    return Pip(k0=k0, k1=k1, step=step)


def min_feasible_aggregate(pip: Pip, m: int) -> int:
    """Smallest feasible pipette-total (in step units) >= m (m > 0)."""
    if m <= 0:
        return 0
    t = _ceil(m, pip.k1)
    low = t * pip.k0
    return max(m, low)


def is_aggregate(pip: Pip, m: int, cap_units: Optional[int] = None) -> bool:
    if m == 0:
        return True
    if m < pip.k0:
        return False
    if cap_units is not None and m > cap_units:
        return False
    return min_feasible_aggregate(pip, m) == m


def split_parts(pip: Pip, total_nl: int) -> list[int]:
    """Split an edge total into individual transfer volumes (nl)."""
    assert total_nl % pip.step == 0
    m = total_nl // pip.step
    parts: list[int] = []
    while m > 0:
        take = min(pip.k1, m - (pip.k0 if m > pip.k1 else 0))
        if take < pip.k0:
            take = m
        parts.append(take * pip.step)
        m -= take
    return parts


def smallest_batch(
    demand_nl: int,
    cap_nl: int,
    frac: Fraction,
    pip: Pip,
    min_batch_nl: int = 0,
) -> Optional[int]:
    """Smallest feasible production batch volume (nl).

    Let the batch volume be ``x`` and ``n = x / step`` (step units).

    * ``x >= max(demand_nl, min_batch_nl)`` and ``x <= cap_nl``;
    * both parent shares ``x*frac`` and ``x*(1-frac)`` are integer nl,
      multiples of the pipette step, and feasible pipette aggregates
      (i.e. splittable into transfers in ``[k0*step, k1*step]``).

    Returns the batch volume in nl, or None when no such batch fits.
    """
    if demand_nl <= 0:
        return 0
    if frac == 0:
        # no high-parent draw: the whole batch comes from the low parent
        n = _ceil(max(demand_nl, min_batch_nl, pip.k0 * pip.step), pip.step)
        if n * pip.step > cap_nl or not is_aggregate(pip, n):
            return None
        return n * pip.step
    a, b = frac.numerator, frac.denominator
    n_min = _ceil(max(demand_nl, min_batch_nl), pip.step)
    # n must be a multiple of b so both shares are integral step counts
    n = ((n_min + b - 1) // b) * b
    guard = 0
    while n * pip.step <= cap_nl:
        guard += 1
        if guard > 10_000_000:
            return None
        hi_units = n * a // b
        lo_units = n - hi_units
        if is_aggregate(pip, hi_units) and (
            lo_units == 0 or is_aggregate(pip, lo_units)
        ):
            return n * pip.step
        n += b
    return None


# --------------------------------------------------------------------------- #
# Solution
# --------------------------------------------------------------------------- #


@dataclass
class SolveFailure:
    blocked: list
    reason: str
    detail: dict = field(default_factory=dict)


@dataclass
class SolveResult:
    ok: bool
    # success payload
    plan: Optional[dict] = None
    # failure payload
    failure: Optional["SolveFailure"] = None
