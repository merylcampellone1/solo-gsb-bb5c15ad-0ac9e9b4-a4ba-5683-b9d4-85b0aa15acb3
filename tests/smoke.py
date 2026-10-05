import json
import sys
from fractions import Fraction

sys.path.insert(0, ".")

from app.solver import Problem, Well, solve
from app.solver.core import make_pip
from app.solver.solve import verify_plan


def build(stocks, wells, containers=(), diluent=10_000_000,
          pmin=100, pmax=1000, step=50, max_depth=3):
    return Problem(
        stocks={sid: {"conc": Fraction(c), "volume": v} for sid, c, v in stocks},
        diluent_volume=diluent,
        wells=[Well(wid, vol, Fraction(c), cap) for wid, vol, c, cap in wells],
        containers=list(containers),
        pip_min=pmin, pip_max=pmax, pip_step=step,
        max_depth=max_depth,
    )


def check(res, prob, expect_ok=True):
    pip = make_pip(prob.pip_min, prob.pip_max, prob.pip_step)
    assert res.ok == expect_ok, (res.ok, res.failure)
    if expect_ok:
        err = verify_plan(prob, pip, res.plan)
        assert err is None, err
    return res


def test_direct_dilution():
    prob = build(
        [("S1", 100, 1_000_000)],
        [("A1", 1000, 40, 5000)],
    )
    res = check(solve(prob), prob)
    vols = sorted(t["volume_nl"] for t in res.plan["transfers"])
    assert sum(t["volume_nl"] for t in res.plan["transfers"]) == 1000
    print("direct dilution:",
          [(t["source"]["kind"], t["volume_nl"]) for t in res.plan["transfers"]])


def test_serial_dilution():
    # 1000 uM stock -> 100 uM target (10x) through one 10_000 nl container
    prob = build(
        [("S1", 1000, 1_000_000)],
        [("A1", 1000, 100, 5000)],
        containers=[10_000],
        pmin=500, pmax=1000, step=100,
    )
    res = check(solve(prob), prob)
    assert res.plan["intermediates"], "expected an intermediate"
    print("serial dilution:")
    for im in res.plan["intermediates"]:
        print("  ", im["id"], im["concentration"], im["produced_nl"], "nl in",
              im["container"])
    for t in res.plan["transfers"]:
        print("   ", t["step"], t["source"]["kind"], "->",
              t["destination"]["kind"], t["destination"].get("id", ""),
              t["volume_nl"])


def test_two_step_serial():
    # 1000 -> 10 (100x) across two 10x stages / two containers, min 500
    prob = build(
        [("S1", 1000, 1_000_000)],
        [("A1", 1000, 10, 5000)],
        containers=[10_000, 10_000],
        pmin=500, pmax=1000, step=100,
    )
    res = check(solve(prob), prob)
    print("two-stage serial ok, intermediates:",
          [(im["id"], im["concentration"]) for im in res.plan["intermediates"]])


def test_no_solution_overdraft():
    # stock 100 nl at 100 can supply at most 10000 solute units; a 1000 nl
    # target at 50 needs 50000 units -> infeasible (and stock overdraft).
    prob = build(
        [("S1", 100, 100)],
        [("A1", 1000, 50, 5000)],
        diluent=1_000_000,
    )
    res = solve(prob)
    assert not res.ok
    codes = res.failure.blocked[0]["constraints"]
    assert "stock_overdraft" in codes
    assert "insufficient_total_solute" in res.failure.detail["global"]
    print("overdraft correctly rejected:", res.failure.blocked,
          res.failure.detail)


def test_no_solution_total_volume():
    prob = build(
        [("S1", 100, 100)],
        [("A1", 1000, 100, 5000)],
        diluent=50,
    )
    res = solve(prob)
    assert not res.ok
    assert "insufficient_total_volume" in res.failure.detail["global"]
    print("total-volume shortage rejected")


def test_illegal_volume_below_min():
    # target 200 nl with min transfer 500 -> structurally impossible
    prob = build(
        [("S1", 100, 1_000_000)],
        [("A1", 200, 100, 5000)],
        pmin=500, pmax=1000, step=100,
    )
    res = solve(prob)
    assert not res.ok
    codes = res.failure.blocked[0]["constraints"]
    assert "volume_below_pipette_minimum" in codes
    print("sub-minimum volume rejected:", codes)


def test_well_overflow():
    prob = build(
        [("S1", 100, 1_000_000)],
        [("A1", 6000, 100, 5000)],
    )
    res = solve(prob)
    assert not res.ok
    assert "well_capacity_exceeded" in res.failure.blocked[0]["constraints"]
    print("well overflow rejected")


def test_shared_intermediate_two_wells():
    prob = build(
        [("S1", 1000, 1_000_000)],
        [("A1", 1000, 250, 5000), ("A2", 1500, 250, 5000)],
        containers=[10_000],
        pmin=200, pmax=1000, step=100,
    )
    res = check(solve(prob), prob)
    assert len(res.plan["intermediates"]) == 1
    print("shared intermediate serves 2 wells:",
          res.plan["intermediates"][0]["concentration"])


def test_concentration_above_stock():
    prob = build(
        [("S1", 50, 1_000_000)],
        [("A1", 1000, 100, 5000)],
    )
    res = solve(prob)
    assert not res.ok
    assert "target_above_max_stock_concentration" in res.failure.detail["global"]
    print("above-stock concentration rejected")


if __name__ == "__main__":
    test_direct_dilution()
    test_serial_dilution()
    test_two_step_serial()
    test_shared_intermediate_two_wells()
    test_no_solution_overdraft()
    test_no_solution_total_volume()
    test_illegal_volume_below_min()
    test_well_overflow()
    test_concentration_above_stock()
    print("\nALL SOLVER TESTS PASSED")
