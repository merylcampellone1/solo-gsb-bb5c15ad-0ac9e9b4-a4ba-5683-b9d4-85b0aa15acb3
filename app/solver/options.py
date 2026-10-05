"""Candidate option generation for each target well."""
from __future__ import annotations

from fractions import Fraction

from .core import (
    DILUENT_NODE,
    RECIPE,
    STOCK,
    Option,
    Pip,
    Problem,
    is_aggregate,
    smallest_batch,
)


def existing_nodes(prob: Problem):
    nodes = [DILUENT_NODE]
    for sid in prob.stocks:
        nodes.append((STOCK, sid))
    for key in prob.recipes:
        nodes.append((RECIPE, key))
    return nodes


def _largest_cap(prob: Problem) -> int:
    return max(prob.containers) if prob.containers else 0


def tier1_existing(prob: Problem, pip: Pip, vol_nl: int, conc: Fraction):
    """Fills using nodes that already exist (no new intermediate)."""
    opts = []
    nodes = existing_nodes(prob)
    m = vol_nl // pip.step

    # single source, exact concentration
    for node in nodes:
        if prob.conc(node) == conc and is_aggregate(pip, m):
            opts.append(Option(fills=[(node, vol_nl)], new_specs=[], kind="direct"))

    # two sources bracketing the target concentration
    for hi in nodes:
        ch = prob.conc(hi)
        if ch < conc:
            continue
        for lo in nodes:
            if lo is hi:
                continue
            cl = prob.conc(lo)
            if cl > conc or ch == cl:
                continue
            # u (step units from hi) solves u*ch + (m-u)*cl = m*c
            num = (conc - cl) * m
            den = ch - cl
            if num % den:
                continue
            u = num // den
            if u <= 0 or u >= m:
                continue
            if not is_aggregate(pip, u) or not is_aggregate(pip, m - u):
                continue
            opts.append(Option(
                fills=[(hi, u * pip.step), (lo, (m - u) * pip.step)],
                new_specs=[],
                kind="blend",
            ))
    return _dedupe(opts)


def _new_recipe_option(fills, spec_chain, kind):
    return Option(fills=fills, new_specs=spec_chain, kind=kind)


def tier1_new_recipe(prob: Problem, pip: Pip, vol_nl: int, conc: Fraction):
    """Create exactly one new recipe whose parents already exist."""
    if len(prob.recipes) >= len(prob.containers):
        return []
    cap = _largest_cap(prob)
    if cap <= 0:
        return []
    m = vol_nl // pip.step
    nodes = existing_nodes(prob)
    opts = []

    # (a) well filled entirely by the new recipe whose concentration is conc
    if is_aggregate(pip, m):
        for hi in nodes:
            ch = prob.conc(hi)
            if ch <= conc:
                continue
            for lo in nodes:
                if lo is hi:
                    continue
                cl = prob.conc(lo)
                if cl >= conc:
                    continue
                depth = max(prob.depth(hi), prob.depth(lo)) + 1
                if depth > prob.max_depth:
                    continue
                frac = (conc - cl) / (ch - cl)
                batch = smallest_batch(vol_nl, cap, frac, pip)
                if batch is None:
                    continue
                opts.append(Option(
                    fills=[(("local", 1), vol_nl)],
                    new_specs=[(1, hi, lo, frac)],
                    kind="intermediate_direct",
                ))

    # (b) new recipe + an existing companion bracket the target together
    if m >= 2 * pip.k0 and m <= prob.max_enumeration_units:
        parents = nodes
        companions = nodes
        for comp in companions:
            q = prob.conc(comp)
            for u in range(pip.k0, m - pip.k0 + 1):
                if not is_aggregate(pip, u) or not is_aggregate(pip, m - u):
                    continue
                # required concentration of the new recipe
                rem = m - u
                rc = (conc * m - q * rem) / u
                if rc < 0:
                    continue
                low_comp = q < conc
                high_comp = q > conc
                if not (low_comp or high_comp):
                    continue
                for hi in parents:
                    ch = prob.conc(hi)
                    for lo in parents:
                        if lo is hi:
                            continue
                        cl = prob.conc(lo)
                        if not (ch > rc > cl):
                            continue
                        depth = max(prob.depth(hi), prob.depth(lo)) + 1
                        if depth > prob.max_depth:
                            continue
                        frac = (rc - cl) / (ch - cl)
                        if smallest_batch(u * pip.step, cap, frac, pip) is None:
                            continue
                        opts.append(Option(
                            fills=[(("local", 1), u * pip.step),
                                   (comp, rem * pip.step)],
                            new_specs=[(1, hi, lo, frac)],
                            kind="intermediate_blend",
                        ))
    return _dedupe(opts)


# --------------------------------------------------------------------------- #
# Serial dilution chains (integer factors)
# --------------------------------------------------------------------------- #


def ordered_factorizations(n: int, max_len: int, cap: int = 500):
    """All ordered factorizations of ``n`` into factors >= 2.

    Length 1 is included.  Factor order is significant (serial dilution
    stages are ordered).  Results are capped to keep enumeration bounded.
    """
    results = []

    def rec(rem: int, prefix: list, length_left: int):
        if len(results) >= cap:
            return
        results.append(prefix + [rem])
        if length_left <= 1:
            return
        for f in range(2, rem):
            q = rem // f
            if rem % f == 0 and q >= 2:
                rec(q, prefix + [f], length_left - 1)

    rec(n, [], max_len)
    return results


def _size_chain(pip: Pip, cap: int, top_demand_nl: int, factors):
    """Bottom-up feasibility / batch sizing for an all-diluent chain.

    Returns list (stage order, top last) of (frac, batch_nl, parent_draw_nl)
    or None.
    """
    demand = top_demand_nl
    stages = [None] * len(factors)
    for i in range(len(factors) - 1, -1, -1):
        f = factors[i]
        frac = Fraction(1, f)
        batch = smallest_batch(demand, cap, frac, pip)
        if batch is None:
            return None
        parent_draw = batch // f
        if batch != parent_draw * f:
            return None
        stages[i] = (frac, batch, parent_draw)
        demand = parent_draw
    return stages


def tier2_chains(prob: Problem, pip: Pip, vol_nl: int, conc: Fraction):
    """Serial-dilution chains built from a stock + diluent."""
    if len(prob.recipes) >= len(prob.containers):
        return []
    cap = _largest_cap(prob)
    if cap <= 0:
        return []
    m = vol_nl // pip.step
    opts = []
    remaining_containers = len(prob.containers) - len(prob.recipes)

    for sid, stock in prob.stocks.items():
        ch = stock["conc"]
        if ch <= conc:
            continue

        # (a) single fill: c_top == conc, requiring ch / conc integer
        if is_aggregate(pip, m) and conc > 0:
            ratio = ch / conc
            if ratio.denominator == 1 and 2 <= ratio.numerator <= prob.max_factor:
                F = ratio.numerator
                for factors in ordered_factorizations(F, prob.max_depth):
                    if len(factors) > remaining_containers:
                        continue
                    stages = _size_chain(pip, cap, vol_nl, factors)
                    if stages is None:
                        continue
                    specs = []
                    parent = (STOCK, sid)
                    for i, f in enumerate(factors, start=1):
                        specs.append((i, parent, DILUENT_NODE, Fraction(1, f)))
                        parent = ("local", i)
                    opts.append(Option(
                        fills=[(("local", len(factors)), vol_nl)],
                        new_specs=specs,
                        kind="serial_chain",
                    ))

        # (b) chain top plus an existing companion liquid
        if m >= 2 * pip.k0 and m <= prob.max_enumeration_units:
            nodes = existing_nodes(prob)
            for comp in nodes:
                q = prob.conc(comp)
                if q == conc:
                    continue
                low_comp = q < conc
                high_comp = q > conc
                if not (low_comp or high_comp):
                    continue
                for u in range(pip.k0, m - pip.k0 + 1):
                    if not is_aggregate(pip, u) or not is_aggregate(pip, m - u):
                        continue
                    rem = m - u
                    rc = (conc * m - q * rem) / u
                    if rc < 0 or rc == 0:
                        continue
                    ratio = ch / rc
                    if ratio.denominator != 1 or not (2 <= ratio.numerator <= prob.max_factor):
                        continue
                    F = ratio.numerator
                    fills = [(("local", 0), u * pip.step), (comp, rem * pip.step)]
                    # local key 0 resolved to the top stage after building specs
                    for factors in ordered_factorizations(F, prob.max_depth):
                        if len(factors) > remaining_containers:
                            continue
                        stages = _size_chain(pip, cap, u * pip.step, factors)
                        if stages is None:
                            continue
                        specs = []
                        parent = (STOCK, sid)
                        for i, f in enumerate(factors, start=1):
                            specs.append((i, parent, DILUENT_NODE, Fraction(1, f)))
                            parent = ("local", i)
                        top_key = len(factors)
                        resolved_fills = [((("local", top_key) if s == ("local", 0) else s), a)
                                          for s, a in fills]
                        opts.append(Option(fills=resolved_fills, new_specs=specs,
                                           kind="serial_chain_blend"))
    return _dedupe(opts)


def generate_options(prob: Problem, pip: Pip, well) -> list:
    vol = well.volume
    c = well.conc
    opts = tier1_existing(prob, pip, vol, c)
    opts += tier1_new_recipe(prob, pip, vol, c)
    opts += tier2_chains(prob, pip, vol, c)
    return _dedupe(opts)


def _dedupe(opts):
    seen = set()
    out = []
    for o in opts:
        sig = o.signature()
        if sig not in seen:
            seen.add(sig)
            out.append(o)
    return out
