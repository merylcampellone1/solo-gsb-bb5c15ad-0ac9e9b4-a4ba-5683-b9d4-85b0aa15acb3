"""Search engine: depth-first assignment state and feasibility evaluation."""
from __future__ import annotations

from functools import lru_cache

from .core import (
    DILUENT_NODE,
    RECIPE,
    STOCK,
    Option,
    Pip,
    Problem,
    Well,
    smallest_batch,
)


# --------------------------------------------------------------------------- #
# Container assignment
# --------------------------------------------------------------------------- #


def _match(batches, order, container_caps, sorted_indices):
    """Exact feasibility matching of recipes (in ``order``) to containers.

    Containers are tried smallest-first; feasibility is decided with a
    memoized recursive bipartite matching over remaining container masks.
    Returns recipe_key -> container_index, or None if no assignment exists.
    """
    n = len(order)
    m = len(container_caps)
    if n > m:
        return None

    @lru_cache(maxsize=None)
    def feasible(idx, used_mask):
        if idx == n:
            return True
        key = order[idx]
        need = batches[key]
        for ci in sorted_indices:
            bit = 1 << ci
            if used_mask & bit:
                continue
            if container_caps[ci] < need:
                continue
            if feasible(idx + 1, used_mask | bit):
                return True
        return False

    if not feasible(0, 0):
        return None

    # reconstruct a tight-fitting assignment
    assignment = {}
    mask = 0
    for idx, key in enumerate(order):
        need = batches[key]
        for ci in sorted_indices:
            bit = 1 << ci
            if mask & bit or container_caps[ci] < need:
                continue
            if feasible(idx + 1, mask | bit):
                assignment[key] = ci
                mask |= bit
                break
    return assignment


class Assignment:
    """Mutable state carried through the depth-first search."""

    def __init__(self, prob: Problem, pip: Pip):
        self.prob = prob
        self.pip = pip
        self.well_fills: dict = {}          # well_id -> [(node, amount_nl)]
        self.demand: dict = {}             # recipe key -> nl demanded
        self.stock_used: dict = {sid: 0 for sid in prob.stocks}
        self.diluent_used: int = 0
        # created by current prefix (for rollback): key -> signature
        self.created: list = []

    # ---- spec resolution -------------------------------------------------- #

    def resolve(self, spec, locals_map):
        if spec[0] == "local":
            return (RECIPE, locals_map[spec[1]])
        return spec

    # ---- apply / revert --------------------------------------------------- #

    def apply(self, well: Well, opt: Option):
        locals_map = {}
        new_records = []
        for local_key, hi_spec, lo_spec, frac in opt.new_specs:
            hi = self.resolve(hi_spec, locals_map)
            lo = self.resolve(lo_spec, locals_map)
            key, created = self.prob.get_or_create_recipe(hi, lo, frac)
            locals_map[local_key] = key
            if created:
                new_records.append((key, (hi, lo, frac)))
        fills = [(self.resolve(node_spec, locals_map), amount)
                 for node_spec, amount in opt.fills]
        self.well_fills[well.id] = fills
        for node, amount in fills:
            self._count(node, amount)
        return new_records

    def _count(self, node: tuple, amount: int) -> None:
        kind = node[0]
        if kind == STOCK:
            self.stock_used[node[1]] += amount
        elif node[0] == "d":
            self.diluent_used += amount
        else:
            self.demand[node[1]] = self.demand.get(node[1], 0) + amount

    def revert(self, well: Well, new_records) -> None:
        fills = self.well_fills.pop(well.id)
        for node, amount in fills:
            kind = node[0]
            if kind == STOCK:
                self.stock_used[node[1]] -= amount
            elif kind == DILUENT_NODE[0]:
                self.diluent_used -= amount
            else:
                self.demand[node[1]] -= amount
                if self.demand[node[1]] == 0:
                    del self.demand[node[1]]
        for key, sig in reversed(new_records):
            self.prob.remove_recipe(key, sig)

    # ---- feasibility ------------------------------------------------------ #

    def evaluate(self):
        """Compute bottom-up batches; return (batches, assignment, reasons).

        Reasons is a dict of short failure codes encountered (for diagnostics).
        Returns (None, reasons) on failure.
        """
        prob = self.prob
        demand = dict(self.demand)
        stock_used = dict(self.stock_used)
        diluent_used = self.diluent_used
        batches: dict = {}
        reasons = set()

        # Process deepest demanded recipe first; producing it pushes demand
        # onto its (shallower) parents, which must be picked up dynamically.
        processed = set()
        while len(processed) < len(demand):
            key = max(
                (k for k in demand if k not in processed),
                key=lambda k: (prob.recipes[k].depth, k),
            )
            processed.add(key)
            r = prob.recipes[key]
            d = demand[key]
            frac = r.frac
            cap = max(prob.containers) if prob.containers else 0
            if cap <= 0:
                reasons.add("no_container")
                return None, reasons
            x = smallest_batch(d, cap, frac, self.pip)
            if x is None:
                reasons.add("batch_capacity")
                return None, reasons
            batches[key] = x
            hi_nl = x * frac.numerator // frac.denominator
            lo_nl = x - hi_nl
            for parent, amount in ((r.hi, hi_nl), (r.lo, lo_nl)):
                if amount == 0:
                    continue
                kind = parent[0]
                if kind == STOCK:
                    stock_used[parent[1]] += amount
                elif kind == DILUENT_NODE[0]:
                    diluent_used += amount
                else:
                    demand[parent[1]] = demand.get(parent[1], 0) + amount

        # container assignment: one container per recipe.  Exact feasibility
        # via memoized bipartite matching (containers are few); prefer the
        # tightest fitting container among feasible assignments.
        order = sorted(batches, key=lambda k: (-batches[k], k))
        caps = sorted(range(len(prob.containers)),
                      key=lambda i: prob.containers[i])
        assignment = _match(batches, order, prob.containers, caps)
        if assignment is None:
            reasons.add("container_shortage")
            return None, reasons

        # source budgets
        for sid, used in stock_used.items():
            if used > prob.stocks[sid]["volume"]:
                reasons.add("stock_overdraft")
                return None, reasons
        if diluent_used > prob.diluent_volume:
            reasons.add("diluent_overdraft")
            return None, reasons

        return (batches, assignment), reasons
