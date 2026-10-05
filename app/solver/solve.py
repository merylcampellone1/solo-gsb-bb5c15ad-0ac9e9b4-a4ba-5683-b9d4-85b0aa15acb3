"""Public solve() entry point: search, plan assembly and verification."""
from __future__ import annotations

from fractions import Fraction
from typing import Optional

from .core import (
    DILUENT_NODE,
    RECIPE,
    STOCK,
    Problem,
    SolveFailure,
    SolveResult,
    Well,
    is_aggregate,
    make_pip,
    split_parts,
)
from .engine import Assignment
from .options import generate_options


class SearchLimit(Exception):
    pass


def solve(prob: Problem) -> SolveResult:
    pip = make_pip(prob.pip_min, prob.pip_max, prob.pip_step)

    # ---- structural prechecks ------------------------------------------- #
    pre = _prechecks(prob, pip)
    if pre is not None:
        return SolveResult(ok=False, failure=pre)

    wells = sorted(prob.wells, key=_difficulty)
    assign = Assignment(prob, pip)
    nodes_used = 0
    limit = prob.max_search_nodes

    def dfs(idx: int):
        nonlocal nodes_used
        if idx == len(wells):
            return {}  # success; reason set empty
        nodes_used += 1
        if nodes_used > limit:
            raise SearchLimit()
        well = wells[idx]
        opts = generate_options(prob, pip, well)
        if not opts:
            return {well.id: {"no_fill_option"}}
        all_reasons: dict = {}
        for opt in opts:
            new_records = assign.apply(well, opt)
            evaluated, reasons = assign.evaluate()
            if evaluated is not None:
                sub = dfs(idx + 1)
                if sub is not None and not sub:
                    return {}
                if sub:
                    for wid, codes in sub.items():
                        all_reasons.setdefault(wid, set()).update(codes)
            else:
                for code in reasons:
                    all_reasons.setdefault(well.id, set()).add(code)
            assign.revert(well, new_records)
        if not all_reasons:
            all_reasons[well.id] = {"search_exhausted"}
        return all_reasons

    try:
        reasons = dfs(0)
    except SearchLimit:
        return SolveResult(ok=False, failure=_limit_failure(prob, wells))

    if reasons == {}:
        evaluated, _ = assign.evaluate()
        batches, container_map = evaluated
        plan = _assemble(prob, pip, assign, batches, container_map)
        verification = verify_plan(prob, pip, plan)
        if verification is not None:
            # Internal inconsistency: never hand back an unsound plan.
            raise RuntimeError("solver produced invalid plan: " + verification)
        return SolveResult(ok=True, plan=plan)

    return SolveResult(ok=False, failure=_explain(prob, pip, wells, reasons))


# --------------------------------------------------------------------------- #
# Prechecks / difficulty
# --------------------------------------------------------------------------- #


def _prechecks(prob: Problem, pip) -> Optional[SolveFailure]:
    blocked = []
    for w in prob.wells:
        codes = []
        if w.volume > w.capacity:
            codes.append("well_capacity_exceeded")
        if w.volume % pip.step:
            codes.append("volume_not_step_aligned")
        else:
            m = w.volume // pip.step
            if not is_aggregate(pip, m):
                codes.append("volume_below_pipette_minimum")
        if codes:
            blocked.append({"well": w.id, "constraints": codes})
    global_codes = []
    if prob.containers is not None and not prob.containers:
        # no containers at all is legal (direct dilution); only a blocker if
        # some target cannot be reached directly (handled by later analysis).
        pass
    if blocked or global_codes:
        return SolveFailure(
            blocked=blocked,
            reason="structural",
            detail={"global": global_codes},
        )
    return None


def _difficulty(w: Well) -> tuple:
    # target concentration 0 (pure diluent) is always easiest
    return (w.conc > 0, w.id)


def _limit_failure(prob, wells) -> SolveFailure:
    return SolveFailure(
        blocked=[{"well": w.id, "constraints": ["search_node_limit"]} for w in wells],
        reason="search_limit",
        detail={"max_search_nodes": prob.max_search_nodes},
    )


# --------------------------------------------------------------------------- #
# Failure explanation
# --------------------------------------------------------------------------- #


def _explain(prob: Problem, pip, wells, reasons) -> SolveFailure:
    blocked = []
    for w in wells:
        codes = sorted(reasons.get(w.id, {"search_exhausted"}))
        blocked.append({"well": w.id, "constraints": codes})

    global_constraints = []

    # Exact feasibility bounds given finite stock volumes + free diluent.
    stock_items = sorted(prob.stocks.values(), key=lambda s: s["conc"])
    total_vol = sum(w.volume for w in prob.wells)
    total_solute = sum((w.conc * w.volume for w in prob.wells), Fraction(0))
    stock_vol_total = sum(s["volume"] for s in stock_items)
    diluent = prob.diluent_volume

    # Maximum solute deliverable: use stocks from the highest concentration.
    max_solute = Fraction(0)
    budget = total_vol
    for s in sorted(stock_items, key=lambda s: -s["conc"]):
        take = min(budget, s["volume"])
        max_solute += s["conc"] * take
        budget -= take
        if budget <= 0:
            break
    if total_solute > max_solute:
        global_constraints.append("insufficient_total_solute")

    # Total liquid availability.
    if stock_vol_total + diluent < total_vol:
        global_constraints.append("insufficient_total_volume")

    cmax = stock_items[-1]["conc"] if stock_items else Fraction(0)
    if any(w.conc > cmax for w in prob.wells):
        global_constraints.append("target_above_max_stock_concentration")

    # With no intermediate container every target concentration must equal a
    # stock concentration (pure fill) or 0; mixed direct dilution is allowed
    # between any existing source and diluent, so only values strictly between
    # *no* bracketing pair need flagging.  Direct dilution always brackets via
    # diluent (0) when a stock at/above the target exists.
    if not prob.containers:
        stock_concs = {s["conc"] for s in stock_items}
        for w in prob.wells:
            if w.conc == 0 or w.conc in stock_concs:
                continue
            if not any(sc >= w.conc for sc in stock_concs):
                global_constraints.append(
                    "intermediate_required_but_no_container")
                break

    return SolveFailure(
        blocked=blocked,
        reason="no_feasible_plan",
        detail={"global": sorted(set(global_constraints))},
    )


# --------------------------------------------------------------------------- #
# Plan assembly
# --------------------------------------------------------------------------- #


def _node_label(prob: Problem, node: tuple) -> dict:
    kind, ref = node
    if kind == STOCK:
        return {"kind": "stock", "id": ref}
    if kind == DILUENT_NODE[0]:
        return {"kind": "diluent"}
    if kind == "well":
        return {"kind": "well", "id": ref}
    r = prob.recipes[ref]
    return {"kind": "intermediate", "id": f"I{ref}", "container": None}


def _assemble(prob: Problem, pip, assign: Assignment,
              batches: dict, container_map: dict) -> dict:
    # order recipes by ascending depth (producers before consumers)
    recipe_order = sorted(batches, key=lambda k: (prob.recipes[k].depth, k))

    transfers = []
    step_no = 0

    def emit(src: tuple, dst: tuple, amount_nl: int):
        nonlocal step_no
        for piece in split_parts(pip, amount_nl):
            step_no += 1
            transfers.append({
                "step": step_no,
                "source": _node_label(prob, src),
                "destination": _node_label(prob, dst),
                "volume_nl": piece,
            })

    # 1. produce every intermediate batch
    produced_volumes: dict = {}
    for key in recipe_order:
        r = prob.recipes[key]
        node = (RECIPE, key)
        x = batches[key]
        hi_nl = x * r.frac.numerator // r.frac.denominator
        lo_nl = x - hi_nl
        if hi_nl:
            emit(r.hi, node, hi_nl)
        if lo_nl:
            emit(r.lo, node, lo_nl)
        produced_volumes[key] = x

    # 2. fill every target well (request order)
    for w in prob.wells:
        for node, amount in assign.well_fills[w.id]:
            emit(node, ("well", w.id), amount)

    # patch container references into intermediate labels
    for t in transfers:
        for side in ("source", "destination"):
            lab = t[side]
            if lab["kind"] == "intermediate":
                key = int(lab["id"][1:])
                lab["container"] = f"C{container_map[key]}"

    intermediates = []
    for key in recipe_order:
        r = prob.recipes[key]
        x = batches[key]
        out = sum(a for fills in assign.well_fills.values()
                  for n, a in fills if n == (RECIPE, key))
        out += sum(batches[p] * prob.recipes[p].frac.numerator
                   // prob.recipes[p].frac.denominator
                   for p in recipe_order
                   if prob.recipes[p].hi == (RECIPE, key))
        out += sum(batches[p] - batches[p] * prob.recipes[p].frac.numerator
                   // prob.recipes[p].frac.denominator
                   for p in recipe_order
                   if prob.recipes[p].lo == (RECIPE, key))
        intermediates.append({
            "id": f"I{key}",
            "container": f"C{container_map[key]}",
            "concentration": str(prob.conc((RECIPE, key))),
            "produced_nl": x,
            "remaining_nl": x - out,
            "recipe": {
                "high": _node_label(prob, r.hi),
                "low": _node_label(prob, r.lo),
                "high_fraction": str(r.frac),
            },
        })

    wells_out = []
    for w in prob.wells:
        wells_out.append({
            "id": w.id,
            "final_volume_nl": w.volume,
            "final_concentration": str(w.conc),
        })

    return {
        "transfers": transfers,
        "intermediates": intermediates,
        "wells": wells_out,
    }


# --------------------------------------------------------------------------- #
# Independent verification (rational mass-balance replay)
# --------------------------------------------------------------------------- #


def verify_plan(prob: Problem, pip, plan: dict) -> Optional[str]:
    level: dict = {}          # node -> (volume nl, Fraction concentration*volume)
    for sid, s in prob.stocks.items():
        level[(STOCK, sid)] = [s["volume"], s["conc"] * s["volume"]]
    level[DILUENT_NODE] = [prob.diluent_volume, Fraction(0)]
    for i, cap in enumerate(prob.containers):
        level[("container", i)] = [0, Fraction(0), cap]
    for w in prob.wells:
        level[("well", w.id)] = [0, Fraction(0), w.capacity]

    def resolve(lab):
        if lab["kind"] == "stock":
            return (STOCK, lab["id"])
        if lab["kind"] == "diluent":
            return DILUENT_NODE
        if lab["kind"] == "intermediate":
            return ("container", int(lab["container"][1:]))
        return ("well", lab["id"])

    prev_step = 0
    for t in plan["transfers"]:
        step = t["step"]
        if step != prev_step + 1:
            return f"step numbering gap at {step}"
        prev_step = step
        v = t["volume_nl"]
        if v % pip.step:
            return f"step {step}: volume not step aligned"
        units = v // pip.step
        if not (pip.k0 <= units <= pip.k1):
            return f"step {step}: transfer outside pipette range"
        src = resolve(t["source"])
        dst = resolve(t["destination"])
        if src[0] == "well":
            return f"step {step}: target well used as source"
        s = level[src]
        if s[0] < v:
            return f"step {step}: source overdraft"
        if dst[0] in ("container", "well"):
            if level[dst][0] + v > level[dst][2]:
                return f"step {step}: destination overflow"
        conc = s[1] / s[0] if s[0] else Fraction(0)
        s[0] -= v
        s[1] -= conc * v
        d = level[dst]
        d[0] += v
        d[1] += conc * v

    for w in prob.wells:
        d = level[("well", w.id)]
        if d[0] != w.volume:
            return f"well {w.id}: final volume {d[0]} != {w.volume}"
        final_c = d[1] / d[0] if d[0] else Fraction(0)
        if final_c != w.conc:
            return f"well {w.id}: final concentration {final_c} != {w.conc}"

    # DAG check on intermediate dependencies
    graph = {}
    for im in plan["intermediates"]:
        deps = []
        for side in ("high", "low"):
            parent = im["recipe"][side]
            if parent["kind"] == "intermediate":
                deps.append(parent["id"])
        graph[im["id"]] = deps
    state = {}

    def visit(node, stack):
        st = state.get(node)
        if st == "done":
            return True
        if st == "active":
            return False
        state[node] = "active"
        for dep in graph[node]:
            if not visit(dep, stack + [node]):
                return False
        state[node] = "done"
        return True

    for node in graph:
        if not visit(node, []):
            return f"dependency cycle detected at {node}"

    return None
