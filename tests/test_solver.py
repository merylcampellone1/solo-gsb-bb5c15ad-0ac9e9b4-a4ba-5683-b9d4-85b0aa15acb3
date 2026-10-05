"""Solver-level tests (no network / database required)."""
from fractions import Fraction

import pytest

from app.solver import Problem, Well, solve
from app.solver.core import make_pip, split_parts
from app.solver.solve import verify_plan


def prob(wells, containers=(), stocks=(("S", 100, 10_000_000),),
         diluent=10_000_000, pmin=100, pmax=1000, step=50, max_depth=3):
    return Problem(
        stocks={sid: {"conc": Fraction(c), "volume": v}
                for sid, c, v in stocks},
        diluent_volume=diluent,
        wells=[Well(wid, vol, Fraction(c), cap)
               for wid, vol, c, cap in wells],
        containers=list(containers),
        pip_min=pmin, pip_max=pmax, pip_step=step, max_depth=max_depth,
    )


def assert_solvable(p):
    res = solve(p)
    assert res.ok, res.failure
    pip = make_pip(p.pip_min, p.pip_max, p.pip_step)
    assert verify_plan(p, pip, res.plan) is None
    return res.plan


def assert_unsolvable(p):
    res = solve(p)
    assert not res.ok
    return res.failure


def test_direct_dilution_shares():
    plan = assert_solvable(prob([("A1", 1000, 40, 5000)]))
    assert sum(t["volume_nl"] for t in plan["transfers"]) == 1000
    by_kind = {}
    for t in plan["transfers"]:
        by_kind[t["source"]["kind"]] = by_kind.get(t["source"]["kind"], 0) + t["volume_nl"]
    assert by_kind == {"stock": 400, "diluent": 600}


def test_rational_concentration_exact():
    plan = assert_solvable(prob([("A1", 1000, Fraction(25), 5000)]))
    by_kind = {}
    for t in plan["transfers"]:
        by_kind[t["source"]["kind"]] = by_kind.get(t["source"]["kind"], 0) + t["volume_nl"]
    assert by_kind == {"stock": 250, "diluent": 750}


def test_pure_stock_and_pure_diluent():
    plan = assert_solvable(prob([
        ("A1", 1000, 100, 5000),
        ("A2", 1000, 0, 5000),
    ]))
    assert len(plan["intermediates"]) == 0


def test_two_stock_blend():
    plan = assert_solvable(prob(
        [("A1", 1000, 150, 5000)],
        stocks=(("HI", 200, 10 ** 9), ("LO", 50, 10 ** 9)),
        step=50,
    ))
    assert not plan["intermediates"]
    # exact 150 = 2/3 * 200 + 1/3 * 50 => 2000/3 (not integral at 50 nl step),
    # so use an achievable rational tested elsewhere; here just assert replay
    # validation (in assert_solvable) guarantees the exact blend.


def test_serial_dilution_single_container():
    plan = assert_solvable(prob(
        [("A1", 1000, 100, 5000)],
        stocks=(("S", 1000, 10 ** 9),),
        containers=(10_000,), pmin=500, pmax=1000, step=100))
    assert len(plan["intermediates"]) == 1
    assert plan["intermediates"][0]["concentration"] == "100"


def test_forced_two_stage_chain():
    plan = assert_solvable(prob(
        [("A1", 1000, 10, 5000)],
        stocks=(("S", 1000, 10 ** 9),),
        containers=(5000, 5000), pmin=500, pmax=1000, step=100))
    concs = {i["concentration"] for i in plan["intermediates"]}
    # two stages of a 100x dilution from 1000 to 10
    assert len(concs) == 2 and "10" in concs
    # there is a container -> container transfer (DAG, no cycle)
    assert any(t["source"]["kind"] == "intermediate"
               and t["destination"]["kind"] == "intermediate"
               for t in plan["transfers"])


def test_shared_intermediate_multiple_wells():
    plan = assert_solvable(prob(
        [("A1", 1000, 250, 5000), ("A2", 1500, 250, 5000)],
        stocks=(("S", 1000, 10 ** 9),),
        containers=(20_000,), pmin=200, pmax=1000, step=100))
    assert len(plan["intermediates"]) == 1
    assert plan["intermediates"][0]["produced_nl"] >= 2500


def test_large_well_splits_into_transfers():
    plan = assert_solvable(prob([("A1", 4000, Fraction(75, 2), 5000)]))
    assert sum(t["volume_nl"] for t in plan["transfers"]) == 4000
    assert all(100 <= t["volume_nl"] <= 1000 for t in plan["transfers"])


def test_split_parts_always_in_range():
    pip = make_pip(100, 1000, 50)
    for total in range(0, 4001, 50):
        if total == 0:
            continue
        units = total // 50
        if units < 2:
            continue
        parts = split_parts(pip, total)
        assert sum(parts) == total
        assert all(100 <= p <= 1000 for p in parts), (total, parts)


def test_unsolvable_stock_overdraft():
    f = assert_unsolvable(prob(
        [("A1", 1000, 50, 5000)],
        stocks=(("S", 100, 100),)))
    assert "stock_overdraft" in f.blocked[0]["constraints"]
    assert "insufficient_total_solute" in f.detail["global"]


def test_unsolvable_total_volume():
    f = assert_unsolvable(prob(
        [("A1", 1000, 100, 5000)],
        stocks=(("S", 100, 100),), diluent=50))
    assert "insufficient_total_volume" in f.detail["global"]


def test_unsolvable_above_stock():
    f = assert_unsolvable(prob([("A1", 1000, 200, 5000)]))
    assert "target_above_max_stock_concentration" in f.detail["global"]


def test_unsolvable_well_overflow():
    f = assert_unsolvable(prob([("A1", 6000, 100, 5000)]))
    assert "well_capacity_exceeded" in f.blocked[0]["constraints"]


def test_unsolvable_below_pipette_min():
    f = assert_unsolvable(prob(
        [("A1", 200, 100, 5000)], pmin=500, pmax=1000, step=100))
    assert "volume_below_pipette_minimum" in f.blocked[0]["constraints"]


def test_unsolvable_unaligned_volume():
    f = assert_unsolvable(prob(
        [("A1", 777, 50, 5000)], step=100))
    assert "volume_not_step_aligned" in f.blocked[0]["constraints"]


def test_unsolvable_container_shortage():
    f = assert_unsolvable(prob(
        [("A1", 1000, 10, 5000)],
        stocks=(("S", 1000, 10 ** 9),),
        containers=(10_000,), pmin=500, pmax=1000, step=100))
    # 100x dilution needs a two-stage chain but only one container exists
    assert not solve(prob(
        [("A1", 1000, 10, 5000)],
        stocks=(("S", 1000, 10 ** 9),),
        containers=(10_000,), pmin=500, pmax=1000, step=100)).ok


def test_many_wells_achievable():
    concs = [10, 20, 30, 40, 50, 60, 70, 80, 90] * 3
    wells = [(f"W{i:02d}", 1000, concs[i], 5000) for i in range(27)]
    plan = assert_solvable(prob(wells))
    assert len(plan["wells"]) == 27


def test_well_never_source():
    plan = assert_solvable(prob(
        [("A1", 1000, 100, 5000), ("A2", 1000, 100, 5000)],
        containers=(10_000,), stocks=(("S", 1000, 10 ** 9),),
        pmin=200, pmax=1000, step=100))
    assert all(t["source"]["kind"] != "well" for t in plan["transfers"])
