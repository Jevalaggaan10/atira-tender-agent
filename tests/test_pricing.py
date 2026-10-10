"""The pricing calculator against invented golden values (v6 bid rules 1–29).

Every payload here is synthetic (P-1/P-2, M-XYZ, dates 2099). Expected cents are hand-computed oracles written
down next to each case, never values read back from the module.
"""
from __future__ import annotations

import copy
import sys
import unittest
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from jsonschema import validate

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from atira_agent.pricing import (
    PAYLOAD_SCHEMA_DOC,
    PricingError,
    calculate_bid,
    derive_nominal_stroke,
    select_dated,
)

# The platform's public answer schema, verbatim from the challenge docs (public spec, not scenario data).
ANSWER_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "tender_reference": {"type": "string", "maxLength": 100},
        "decision": {"type": "string", "enum": ["bid", "no_bid"]},
        "currency": {"type": "string", "const": "EUR"},
        "discount_percent": {"type": "number", "minimum": 0, "maximum": 100},
        "positions": {
            "maxItems": 100,
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "position": {"type": "string", "maxLength": 20},
                    "material_number": {"type": "string", "maxLength": 50},
                    "quantity": {"type": "integer", "minimum": 0, "maximum": 9007199254740991},
                    "unit_price": {"type": "number", "minimum": 0},
                    "lead_time_weeks": {"type": "integer", "minimum": 0, "maximum": 9007199254740991},
                    "base_unit_price": {"type": "number", "minimum": 0},
                    "surcharge_per_unit": {"type": "number", "minimum": 0},
                    "packaging_per_unit": {"type": "number", "minimum": 0},
                    "rebate_total": {"type": "number", "minimum": 0},
                },
                "required": ["position", "material_number", "quantity", "unit_price", "lead_time_weeks"],
                "additionalProperties": False,
            },
        },
        "total_net": {"type": "number", "minimum": 0},
        "notes": {"type": "string", "maxLength": 4000},
    },
    "required": ["tender_reference", "decision", "currency", "discount_percent", "positions", "total_net"],
    "additionalProperties": False,
}

CENT = Decimal("0.01")


def hu(value) -> Decimal:
    """Independent cent oracle: HALF_UP a hand-written exact Decimal to cents."""
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def mat(**over) -> dict:
    m = {
        "material_number": "M-XYZ-1",
        "attrs": {"bore_mm": 80, "stroke_mm": 300, "certification": "ED"},
        "stock": 24, "lead_time_stock_weeks": 1, "lead_time_production_weeks": 6,
        "price_conditions": [{"valid_from": "2099-01-01", "valid_to": "2099-01-31",
                              "currency": "EUR", "fx_key": None, "price_unit_pieces": 1}],
        "quantity_scales": [{"min_pieces": 1, "price": "120.00"}, {"min_pieces": 10, "price": "108.50"}],
    }
    m.update(over)
    return m


def pos(**over) -> dict:
    p = {
        "position_id": "P-1", "tender_quantity": 10, "tender_unit": "Stück", "pieces_per_set": None,
        "quantity_events": [], "requirement": {"bore_mm": 80, "stroke_mm": 300, "certification": "ED"},
        "material": mat(), "konditionsgruppe": None, "discount_scope": None, "mass_surcharge": None,
        "ordinary_setup_fee": "0.00", "packaging": None, "delivery_commitment": None,
        "project_bonus_eligible": False, "metal_index_code": None,
    }
    p.update(over)
    return p


def pl(**over) -> dict:
    payload = {
        "scenario_date": "2099-01-15", "tender_reference": "T-SYNTH-1",
        "discount": {"percent": "0", "default_scope": "base"},
        "fx_rates": [], "positions": [pos()],
    }
    payload.update(over)
    return payload


def one(r: dict, pid: str = "P-1") -> dict:
    return next(a for a in r["answer"]["positions"] if a["position"] == pid)


class Basics(unittest.TestCase):
    def test_simple_bid_golden(self):
        # 10 pieces -> scale 10 -> base 108.50; setup 35.00 -> unit round(108.50+3.50)=112.00; line 1120.00.
        r = calculate_bid(pl(positions=[pos(ordinary_setup_fee="35.00")]))
        a = r["answer"]
        self.assertEqual((a["decision"], a["discount_percent"], a["total_net"]), ("bid", 0.0, 1120.0))
        p = one(r)
        self.assertEqual(Decimal(str(p["unit_price"])), hu(Decimal("108.50") + Decimal("35.00") / 10))
        self.assertEqual(Decimal(str(p["unit_price"])), Decimal("112.00"))
        self.assertEqual((p["quantity"], p["lead_time_weeks"]), (10, 1))  # stock 24 >= pooled 10
        self.assertEqual(Decimal(str(p["base_unit_price"])), Decimal("108.50"))
        self.assertEqual(Decimal(str(p["packaging_per_unit"])), Decimal("0.00"))
        self.assertEqual(Decimal(str(p["rebate_total"])), Decimal("0.00"))
        self.assertEqual(Decimal(str(p["surcharge_per_unit"])), Decimal("3.50"))  # report: 0 mass + 35/10
        self.assertTrue(all(r["checks"].values()))
        d = r["derived"]["positions"]["P-1"]
        self.assertEqual((d["final_pieces"], d["setup_total"], d["line_total"]), (10, "35.0", "1120.0"))

    def test_total_is_sum_of_rounded_lines(self):
        # P-1: round(108.50 + 10.00/10) = 109.50, line 1095.00; P-2: round(120.00 + 10.00/5) = 122.00, line 610.00.
        p1 = pos(ordinary_setup_fee="10.00")
        p2 = pos(position_id="P-2", tender_quantity=5, ordinary_setup_fee="10.00",
                 material=mat(quantity_scales=[{"min_pieces": 1, "price": "120.00"}]))
        r = calculate_bid(pl(positions=[p1, p2]))
        self.assertEqual(Decimal(str(r["answer"]["total_net"])), Decimal("1705.00"))

    def test_answer_validates_and_floats_are_exact_cents(self):
        r = calculate_bid(pl(positions=[pos(ordinary_setup_fee="35.00",
                                            packaging={"eur_per_carton": "7.00", "pieces_per_carton": 3},
                                            mass_surcharge={"kg_per_piece": "2.5", "eur_per_kg": "1.5"})]))
        validate(r["answer"], ANSWER_SCHEMA)
        self.assertNotIn("notes", r["answer"])
        for a in r["answer"]["positions"]:
            d = r["derived"]["positions"][a["position"]]
            for field, exact in (("unit_price", Decimal(d["unit_price"])),
                                 ("base_unit_price", Decimal(d["base_per_piece"])),
                                 ("rebate_total", Decimal(d["bonus_allocated"]))):
                self.assertEqual(Decimal(str(a[field])), exact, field)
                self.assertEqual(a[field], float(exact), field)
            self.assertEqual(Decimal(str(a["unit_price"])) * a["quantity"], Decimal(d["line_total"]))
        total = sum((Decimal(x["line_total"]) for x in r["derived"]["positions"].values()), Decimal(0))
        self.assertEqual(Decimal(str(r["answer"]["total_net"])), total)

    def test_validation_rejects_bad_payloads(self):
        with self.assertRaises(PricingError) as e:
            calculate_bid({**pl(), "notes_for_me": 1})
        self.assertEqual(e.exception.path, "notes_for_me")
        bad = pl()
        del bad["scenario_date"]
        with self.assertRaises(PricingError) as e:
            calculate_bid(bad)
        self.assertEqual(e.exception.path, "scenario_date")
        with self.assertRaises(PricingError) as e:
            calculate_bid(pl(positions=[pos(ordinary_setup_fee="-1.00")]))
        self.assertEqual(e.exception.path, "positions.0.ordinary_setup_fee")
        with self.assertRaises(PricingError) as e:
            calculate_bid(pl(positions=[]))
        self.assertEqual(e.exception.path, "positions")

    def test_derive_nominal_stroke_rule20(self):
        self.assertEqual(derive_nominal_stroke("300", "2.5"), Decimal("305.0"))
        self.assertEqual(derive_nominal_stroke(299.5, 2.25), Decimal("304.0"))


class Quantities(unittest.TestCase):
    def test_withdrawal_of_a_chain_member_revives_the_change_rule13(self):
        # 11 withdraws 9; 9 was withdrawn, so its withdrawal of 7 never happened: answer 7 stands.
        ev = [{"answer": 7, "quantity": 12}, {"answer": 9, "withdraws": 7}, {"answer": 11, "withdraws": 9}]
        r = calculate_bid(pl(positions=[pos(tender_unit="Satz", pieces_per_set=2, quantity_events=ev)]))
        # quantity 12 is in the position's tender unit (Satz): 12 * 2 pieces
        self.assertEqual(r["derived"]["positions"]["P-1"]["final_pieces"], 24)

    def test_surviving_highest_answer_wins_rule13(self):
        ev = [{"answer": 7, "quantity": 12}, {"answer": 9, "quantity": 5, "withdraws": 7}]
        r = calculate_bid(pl(positions=[pos(quantity_events=ev)]))
        self.assertEqual(r["derived"]["positions"]["P-1"]["final_pieces"], 5)

    def test_all_withdrawn_falls_back_to_tender_quantity(self):
        ev = [{"answer": 7, "quantity": 12}, {"answer": 9, "withdraws": 7}]
        r = calculate_bid(pl(positions=[pos(tender_quantity=8, tender_unit="Satz", pieces_per_set=2,
                                            quantity_events=ev)]))
        self.assertEqual(r["derived"]["positions"]["P-1"]["final_pieces"], 16)  # tender 8 Satz

    def test_mutual_withdrawal_is_ambiguous(self):
        # a withdrawal may list before the answer it names (Q&A page order is not the rule); mutual ones do not
        # resolve and must be rejected as a set.
        ev = [{"answer": 3, "withdraws": 4}, {"answer": 4, "withdraws": 3}]
        with self.assertRaises(PricingError) as e:
            calculate_bid(pl(positions=[pos(quantity_events=ev)]))
        self.assertEqual(e.exception.path, "positions.0.quantity_events")
        self.assertIn("mutual", e.exception.message)

    def test_withdraws_must_name_an_answer_of_this_position(self):
        with self.assertRaises(PricingError) as e:
            calculate_bid(pl(positions=[pos(quantity_events=[{"answer": 5, "withdraws": 9}])]))
        self.assertEqual(e.exception.path, "positions.0.quantity_events.0.withdraws")
        self.assertIn("no answer of this position", e.exception.message)

    def test_withdrawal_may_list_before_the_answer_it_names(self):
        ev = [{"answer": 9, "withdraws": 7}, {"answer": 7, "quantity": 12}]
        r = calculate_bid(pl(positions=[pos(quantity_events=ev)]))
        self.assertEqual(r["derived"]["positions"]["P-1"]["final_pieces"], 10)  # 7 gone, no surviving change

    def test_satz_conversion_rule17(self):
        r = calculate_bid(pl(positions=[pos(tender_quantity=8, tender_unit="Satz", pieces_per_set=2)]))
        self.assertEqual(one(r)["quantity"], 16)

    def test_satz_needs_pieces_per_set(self):
        with self.assertRaises(PricingError) as e:
            calculate_bid(pl(positions=[pos(tender_unit="Satz", pieces_per_set=None)]))
        self.assertEqual(e.exception.path, "positions.0.pieces_per_set")

    def test_latest_dated_change_wins_over_higher_answer(self):
        # Rule 1: the clarification DATED later wins, even if its answer number is older.
        ev = [{"answer": 40, "dated": "2099-01-03", "quantity": 12},
              {"answer": 41, "dated": "2099-01-02", "quantity": 11}]
        r = calculate_bid(pl(positions=[pos(quantity_events=ev)]))
        self.assertEqual(r["derived"]["positions"]["P-1"]["final_pieces"], 12)

    def test_future_dated_event_never_applies(self):
        # rule 21: after scenario_date it does not change the quantity and cannot withdraw either.
        r = calculate_bid(pl(positions=[pos(quantity_events=[
            {"answer": 9, "dated": "2099-02-01", "quantity": 12}])]))
        self.assertEqual(r["derived"]["positions"]["P-1"]["final_pieces"], 10)  # tender
        r2 = calculate_bid(pl(positions=[pos(quantity_events=[
            {"answer": 7, "dated": "2099-01-02", "quantity": 30},
            {"answer": 9, "dated": "2099-02-05", "withdraws": 7}])]))
        self.assertEqual(r2["derived"]["positions"]["P-1"]["final_pieces"], 30)

    def test_undated_change_that_could_be_later_is_not_guessed(self):
        # an undated survivor (answer 9) would win the legacy answer-number order, but a dated survivor
        # could be newer — ambiguous: the calculator asks for the dated field instead of guessing.
        with self.assertRaises(PricingError) as e:
            calculate_bid(pl(positions=[pos(quantity_events=[
                {"answer": 9, "quantity": 8},
                {"answer": 8, "dated": "2099-01-06", "quantity": 9}])]))
        self.assertEqual(e.exception.path, "positions.0.quantity_events.0.dated")
        self.assertIn("rule 1", e.exception.message)

    def test_undated_change_that_cannot_be_later_wins(self):
        # the undated change (answer 9) is later than every dated one and withdraws the dated survivor:
        # tender quantity remains — no ambiguity to raise.
        r = calculate_bid(pl(positions=[pos(quantity_events=[
            {"answer": 7, "dated": "2099-01-02", "quantity": 30},
            {"answer": 9, "withdraws": 7}])]))
        self.assertEqual(r["derived"]["positions"]["P-1"]["final_pieces"], 10)


class MaterialsAndWindows(unittest.TestCase):
    def test_no_bid_when_material_missing_rule8(self):
        r = calculate_bid(pl(positions=[pos(), pos(position_id="P-2", material=None, requirement={})]))
        a = r["answer"]
        validate(a, ANSWER_SCHEMA)
        self.assertEqual((a["decision"], a["discount_percent"], a["total_net"]), ("no_bid", 0.0, 0.0))
        self.assertEqual(a["positions"], [])

    def test_requirement_match_is_format_forgiving_rule2(self):
        req = {"bore_mm": "80.0", "stroke_mm": 300, "certification": " ed ", "ports": ["a", "b"]}
        m = mat(attrs={"bore_mm": 80, "stroke_mm": "300", "certification": "ED", "ports": ["b", "a"]})
        r = calculate_bid(pl(positions=[pos(requirement=req, material=m)]))
        self.assertEqual(r["answer"]["decision"], "bid")

    def test_requirement_mismatch_error_path(self):
        with self.assertRaises(PricingError) as e:
            calculate_bid(pl(positions=[pos(requirement={"bore_mm": 90})]))
        self.assertEqual(e.exception.path, "positions.0.requirement.bore_mm")

    def test_condition_window_inclusive_both_ends(self):
        m = mat(price_conditions=[{"valid_from": "2099-01-01", "valid_to": "2099-01-15",
                                   "currency": "EUR", "fx_key": None, "price_unit_pieces": 1}])
        self.assertEqual(calculate_bid(pl(positions=[pos(material=m)]))["answer"]["decision"], "bid")  # == valid_to
        m2 = mat(price_conditions=[{"valid_from": "2099-01-16", "valid_to": None,
                                    "currency": "EUR", "fx_key": None, "price_unit_pieces": 1}])
        with self.assertRaises(PricingError) as e:
            calculate_bid(pl(positions=[pos(material=m2)]))
        self.assertEqual(e.exception.path, "positions.0.price_conditions")

    def test_exactly_one_valid_condition(self):
        m = mat(price_conditions=[
            {"valid_from": None, "valid_to": None, "currency": "EUR", "fx_key": None, "price_unit_pieces": 1},
            {"valid_from": "2099-01-01", "valid_to": "2099-12-31", "currency": "EUR", "fx_key": None,
             "price_unit_pieces": 1}])
        with self.assertRaises(PricingError) as e:
            calculate_bid(pl(positions=[pos(material=m)]))
        self.assertEqual(e.exception.path, "positions.0.price_conditions")

    def test_scale_boundary_exact_min(self):
        self.assertEqual(calculate_bid(pl(positions=[pos(tender_quantity=10)]))
                         ["derived"]["positions"]["P-1"]["base_per_piece"], "108.5")
        self.assertEqual(calculate_bid(pl(positions=[pos(tender_quantity=9)]))
                         ["derived"]["positions"]["P-1"]["base_per_piece"], "120.0")

    def test_scale_below_every_min(self):
        m = mat(quantity_scales=[{"min_pieces": 10, "price": "108.50"}])
        with self.assertRaises(PricingError) as e:
            calculate_bid(pl(positions=[pos(tender_quantity=3, material=m)]))
        self.assertEqual(e.exception.path, "positions.0.quantity_scales")

    def test_duplicate_scale_is_ambiguous(self):
        m = mat(quantity_scales=[{"min_pieces": 1, "price": "120.00"}, {"min_pieces": 1, "price": "110.00"}])
        with self.assertRaises(PricingError) as e:
            calculate_bid(pl(positions=[pos(material=m)]))
        self.assertIn("duplicate scale", e.exception.message)


class Currency(unittest.TestCase):
    def usd(self, cpu=1):
        return [{"valid_from": None, "valid_to": None, "currency": "USD", "fx_key": "FX-USD",
                 "price_unit_pieces": cpu}]

    def test_price_unit_normalization_then_convert_rule22(self):
        # 11.00 per 10 pieces -> 1.10/piece native; x 0.920 = 1.012 -> HALF_UP -> 1.01.
        m = mat(price_conditions=[{"valid_from": None, "valid_to": None, "currency": "USD",
                                   "fx_key": "FX-USD", "price_unit_pieces": 10}],
                quantity_scales=[{"min_pieces": 1, "price": "11.00"}])
        r = calculate_bid(pl(fx_rates=[{"key": "FX-USD", "eur_per_unit": "0.920", "dated": "2099-01-10"}],
                             positions=[pos(material=m, requirement={})]))
        self.assertEqual(r["derived"]["positions"]["P-1"]["base_per_piece"], "1.01")

    def test_latest_dated_rate_not_last_and_future_never_rule21(self):
        m = mat(price_conditions=[{"valid_from": None, "valid_to": None, "currency": "USD",
                                   "fx_key": "FX-USD", "price_unit_pieces": 1}],
                quantity_scales=[{"min_pieces": 1, "price": "100.00"}])
        r = calculate_bid(pl(fx_rates=[{"key": "FX-USD", "eur_per_unit": "0.80", "dated": "2099-01-05"},
                                       {"key": "FX-USD", "eur_per_unit": "0.50", "dated": "2099-02-01"}],
                             positions=[pos(material=m, requirement={})]))
        self.assertEqual(r["derived"]["positions"]["P-1"]["fx_rate"], "0.8")

    def test_missing_fx_rate_rejected(self):
        m = mat(price_conditions=[{"valid_from": None, "valid_to": None, "currency": "USD",
                                   "fx_key": "FX-GHOST", "price_unit_pieces": 1}])
        with self.assertRaises(PricingError) as e:
            calculate_bid(pl(positions=[pos(material=m, requirement={})]))
        self.assertEqual(e.exception.path, "positions.0.price_conditions.0.fx_key")

    def test_half_up_exactly_at_half_cent(self):
        # exact half-cent: 1.24 / 8 pieces = 0.155 native -> HALF_UP -> 0.16 (banker's would give 0.15).
        m = mat(price_conditions=[{"valid_from": None, "valid_to": None, "currency": "USD",
                                   "fx_key": "FX-USD", "price_unit_pieces": 8}],
                quantity_scales=[{"min_pieces": 1, "price": "1.24"}])
        r = calculate_bid(pl(fx_rates=[{"key": "FX-USD", "eur_per_unit": "1", "dated": "2099-01-01"}],
                             positions=[pos(material=m, requirement={})]))
        self.assertEqual(r["derived"]["positions"]["P-1"]["base_per_piece"], "0.16")
        # exact half after FX: 2.55 / 2 = 1.275 x 0.9 = 1.14750 -> 1.15; exact values stay exact.
        m2 = mat(price_conditions=[{"valid_from": None, "valid_to": None, "currency": "USD",
                                    "fx_key": "FX-USD", "price_unit_pieces": 2}],
                 quantity_scales=[{"min_pieces": 1, "price": "2.55"}])
        r2 = calculate_bid(pl(fx_rates=[{"key": "FX-USD", "eur_per_unit": "0.9", "dated": "2099-01-01"}],
                              positions=[pos(material=m2, requirement={})]))
        self.assertEqual(r2["derived"]["positions"]["P-1"]["base_per_piece"], "1.15")
        m3 = mat(price_conditions=[{"valid_from": None, "valid_to": None, "currency": "USD",
                                    "fx_key": "FX-USD", "price_unit_pieces": 2}],
                 quantity_scales=[{"min_pieces": 1, "price": "2.50"}])
        r3 = calculate_bid(pl(fx_rates=[{"key": "FX-USD", "eur_per_unit": "0.92", "dated": "2099-01-01"}],
                              positions=[pos(material=m3, requirement={})]))
        self.assertEqual(r3["derived"]["positions"]["P-1"]["base_per_piece"], "1.15")  # exactly 1.15, no drift


class DiscountAndScope(unittest.TestCase):
    def test_threshold_inclusive_and_exact_rule16(self):
        # goods = 10 x 100.00 + 8 x 125.00 = 1000.00 + 1000.00 = 2000.00 (setup excluded); threshold 2000.00 met.
        p1 = pos(material=mat(stock=999, quantity_scales=[{"min_pieces": 1, "price": "100.00"}]),
                 ordinary_setup_fee="500.00")
        p2 = pos(position_id="P-2", tender_quantity=8,
                 material=mat(stock=999, quantity_scales=[{"min_pieces": 1, "price": "125.00"}]))
        base = pl(discount={"percent": "10", "threshold_min_net": "2000.00", "default_scope": "base"},
                  positions=[p1, p2])
        r = calculate_bid(copy.deepcopy(base))
        self.assertTrue(r["derived"]["threshold"]["met"])
        self.assertEqual(r["answer"]["discount_percent"], 10.0)
        self.assertEqual(r["derived"]["threshold"]["undiscounted_net"], "2000.0")
        base["discount"]["threshold_min_net"] = "2000.01"
        r2 = calculate_bid(base)
        self.assertFalse(r2["derived"]["threshold"]["met"])
        self.assertEqual(r2["answer"]["discount_percent"], 0.0)  # 0 %, never an older approval

    def test_scopes_and_pre_discount_reports_rule12(self):
        # base 100, mass 2.5 x 2.0 = 5, setup 10, 10 pieces, discount 10 %.
        common = {"material": mat(stock=99, quantity_scales=[{"min_pieces": 1, "price": "100.00"}]),
                  "mass_surcharge": {"kg_per_piece": "2.5", "eur_per_kg": "2.0"},
                  "ordinary_setup_fee": "10.00"}
        p1 = pos(position_id="P-1", discount_scope="base_plus_mass", **common)
        p2 = pos(position_id="P-2", discount_scope="none", **common)
        r = calculate_bid(pl(discount={"percent": "10", "default_scope": "base"}, positions=[p1, p2]))
        a1, a2 = one(r, "P-1"), one(r, "P-2")
        self.assertEqual(Decimal(str(a1["unit_price"])), hu(Decimal(90) + Decimal("4.5") + Decimal(1)))   # 95.50
        self.assertEqual(Decimal(str(a2["unit_price"])), hu(Decimal(100) + Decimal(5) + Decimal(1)))    # 106.00
        self.assertEqual(Decimal(str(a1["base_unit_price"])), Decimal("100.00"))   # reports always pre-discount
        self.assertEqual(Decimal(str(a1["surcharge_per_unit"])), Decimal("6.00"))  # (5 + 10/10) before discount


class Allocation(unittest.TestCase):
    def m50(self, stock):
        return mat(stock=stock, quantity_scales=[{"min_pieces": 1, "price": "50.00"}])

    def test_shared_stock_pooled_exact_rule15(self):
        r = calculate_bid(pl(positions=[pos(tender_quantity=10, material=self.m50(24)),
                                        pos(position_id="P-2", tender_quantity=15, material=self.m50(24))]))
        self.assertEqual([a["lead_time_weeks"] for a in r["answer"]["positions"]], [6, 6])  # 25 > 24: production
        r2 = calculate_bid(pl(positions=[pos(tender_quantity=10, material=self.m50(25)),
                                         pos(position_id="P-2", tender_quantity=15, material=self.m50(25))]))
        self.assertEqual([a["lead_time_weeks"] for a in r2["answer"]["positions"]], [1, 1])  # exact cover: stock

    def test_commitment_boundaries_rule24(self):
        commit = {"material_number": "M-XYZ-1", "ceiling_pieces": 25, "lead_time_weeks": 3}
        ok = calculate_bid(pl(positions=[pos(tender_quantity=10, material=self.m50(1), delivery_commitment=commit),
                                         pos(position_id="P-2", tender_quantity=15, material=self.m50(1))]))
        self.assertEqual([a["lead_time_weeks"] for a in ok["answer"]["positions"]], [3, 6])  # pooled 25 == ceiling
        over = calculate_bid(pl(positions=[pos(tender_quantity=11, material=self.m50(1),
                                               delivery_commitment=commit),
                                           pos(position_id="P-2", tender_quantity=15, material=self.m50(1))]))
        self.assertEqual(one(over)["lead_time_weeks"], 6)  # pooled 26 > 25
        other = calculate_bid(pl(positions=[pos(tender_quantity=10, material=self.m50(1),
                                                delivery_commitment={"material_number": "M-OTHER-9",
                                                                     "ceiling_pieces": 99,
                                                                     "lead_time_weeks": 3})]))
        self.assertEqual(one(other)["lead_time_weeks"], 6)  # material mismatch

    def test_setup_pool_weights_and_ties_ascending_rule25(self):
        # weight = final pieces x allocation mass: 10 x 1 : 10 x 1 : 7 x 1 = 10 : 10 : 7 over a 204.00 fee.
        # exact shares 75.5556 / 75.5556 / 52.8889 -> floors 75.55 / 75.55 / 52.88, 0.02 left;
        # the two equal largest remainders tie -> ascending member keys take the cents: 75.56 / 75.55 / 52.89.
        members = {"P-1": "1.0", "P-2": "1.0", "P-3": "1.0"}
        pool = {"pool": "R1", "fee": "204.00", "members": members, "allocation": "largest_remainder_cents_asc"}
        r = calculate_bid(pl(setup_pools=[pool], positions=[
            pos(tender_quantity=10, material=self.m50(99)),
            pos(position_id="P-2", tender_quantity=10, material=self.m50(99)),
            pos(position_id="P-3", tender_quantity=7, material=self.m50(99))]))
        setup = {pid: Decimal(d["setup_total"]) for pid, d in r["derived"]["positions"].items()}
        self.assertEqual(setup, {"P-1": Decimal("75.56"), "P-2": Decimal("75.55"), "P-3": Decimal("52.89")})
        self.assertEqual(sum(setup.values()), Decimal("204.00"))
        self.assertEqual(Decimal(str(one(r, "P-1")["surcharge_per_unit"])), hu(Decimal("75.56") / 10))  # 7.56

    def test_pool_zero_weights_and_unknown_member(self):
        p = pl(setup_pools=[{"pool": "R1", "fee": "10.00", "members": {"P-1": "0.0"},
                             "allocation": "largest_remainder_cents_asc"}],
               positions=[pos(tender_quantity=5)])
        with self.assertRaises(PricingError) as e:
            calculate_bid(p)
        self.assertEqual(e.exception.path, "setup_pools.0")
        p2 = pl(setup_pools=[{"pool": "R1", "fee": "10.00", "members": {"P-9": "1.0"},
                              "allocation": "largest_remainder_cents_asc"}], positions=[pos()])
        with self.assertRaises(PricingError) as e:
            calculate_bid(p2)
        self.assertEqual(e.exception.path, "setup_pools.0.members.P-9")


class PackagingAndBonus(unittest.TestCase):
    def test_packaging_ceil_and_report_rule23(self):
        # 10 pieces / 3 = 4 cartons x 7.00 = 28.00; report 28/10 = 2.80; unit 10.00 + 2.80 = 12.80; qty stays 10.
        r = calculate_bid(pl(positions=[pos(tender_quantity=10, packaging={"eur_per_carton": "7.00",
                                                                          "pieces_per_carton": 3},
                                            material=mat(stock=99, quantity_scales=[
                                                {"min_pieces": 1, "price": "10.00"}]))]))
        a = one(r)
        d = r["derived"]["positions"]["P-1"]
        self.assertEqual((d["cartons"], d["carton_cost"]), (4, "28.0"))
        self.assertEqual((a["quantity"], Decimal(str(a["packaging_per_unit"]))), (10, Decimal("2.80")))
        self.assertEqual(Decimal(str(a["unit_price"])), Decimal("12.80"))

    def test_zero_packaging_reports_zero_rule18(self):
        r = calculate_bid(pl())
        self.assertEqual(Decimal(str(one(r)["packaging_per_unit"])), Decimal("0.00"))
        self.assertEqual(r["derived"]["positions"]["P-1"]["cartons"], 0)

    def test_bonus_bands_and_allocation_rule26(self):
        # eligible goods 1000.00 + 2000.00 + 3000.00 = 6000.00 (P-4's 500.00 excluded).
        # bands marginal: 1% x 2000.00 + 2% x 4000.00 = 20.00 + 80.00 = 100.00 credit.
        # proportional in cents: 1000/6000 = 16.66|0.667, 2000/6000 = 33.33|0.333, 3000/6000 = 50.00|0 ->
        # the 0.01 leftover goes to the largest remainder P-1 -> 16.67 / 33.33 / 50.00 (sums to 100.00).
        def m(price):
            return mat(stock=999, quantity_scales=[{"min_pieces": 1, "price": price}])
        bonus = {"bands": [{"min_net": "0.00", "max_net": "2000.00", "percent": "1"},
                           {"min_net": "2000.00", "max_net": None, "percent": "2"}],
                 "allocation": "largest_remainder_cents_asc"}
        r = calculate_bid(pl(project_bonus=bonus, positions=[
            pos(tender_quantity=10, material=m("100.00"), project_bonus_eligible=True),
            pos(position_id="P-2", tender_quantity=10, material=m("200.00"), project_bonus_eligible=True),
            pos(position_id="P-3", tender_quantity=10, material=m("300.00"), project_bonus_eligible=True),
            pos(position_id="P-4", tender_quantity=10, material=m("50.00"), project_bonus_eligible=False)]))
        rebates = {a["position"]: Decimal(str(a["rebate_total"])) for a in r["answer"]["positions"]}
        self.assertEqual(rebates, {"P-1": Decimal("16.67"), "P-2": Decimal("33.33"),
                                   "P-3": Decimal("50.00"), "P-4": Decimal("0.00")})
        self.assertEqual(sum(rebates.values()), Decimal("100.00"))

    def test_bonus_credit_half_up_once(self):
        # goods 500.00 x 1.5 % = 7.50 -> credit 7.50 -> sole eligible member 7.50.
        bonus = {"bands": [{"min_net": "0.00", "max_net": None, "percent": "1.5"}],
                 "allocation": "largest_remainder_cents_asc"}
        r = calculate_bid(pl(project_bonus=bonus,
                             positions=[pos(tender_quantity=10, material=mat(
                                 stock=99, quantity_scales=[{"min_pieces": 1, "price": "50.00"}]),
                                 project_bonus_eligible=True)]))
        self.assertEqual(Decimal(str(one(r)["rebate_total"])), Decimal("7.50"))

    def test_bands_must_be_sorted_and_non_overlapping(self):
        bonus = {"bands": [{"min_net": "0.00", "max_net": None, "percent": "1"},
                           {"min_net": "100.00", "max_net": None, "percent": "2"}],
                 "allocation": "largest_remainder_cents_asc"}
        with self.assertRaises(PricingError) as e:
            calculate_bid(pl(project_bonus=bonus, positions=[pos()]))
        self.assertEqual(e.exception.path, "project_bonus.bands.1")


class MetalIndex(unittest.TestCase):
    def msgs(self):
        return [{"code": "STL-X", "rate": "2.00", "valid_from": "2099-01-01", "valid_to": "2099-01-15"},
                {"code": "STL-X", "rate": "4.00", "valid_from": "2099-01-16", "valid_to": "2099-02-01"}]

    def m10(self):
        return mat(stock=99, quantity_scales=[{"min_pieces": 1, "price": "10.00"}])

    def test_dated_index_rate_is_authoritative_for_marked_stale_tariff(self):
        # mass 2.0 kg/piece; supplied eur_per_kg 2.00 is stale — the window-selected rate (2.00 valid today,
        # later-stored 4.00 out of window) must win: surcharge pp = 4.00, unit = round(10.00 + 4.00 + 0/10) = 14.00.
        r = calculate_bid(pl(metal_index={"messages": self.msgs()}, positions=[pos(
            material=self.m10(), metal_index_code="STL-X",
            mass_surcharge={"kg_per_piece": "2.0", "eur_per_kg": "1.00", "index_code": "STL-X", "max_stale_rate": "2.00"})]))
        self.assertEqual(r["derived"]["positions"]["P-1"]["mass_per_piece"], "4.0")  # 2.0 kg x DATED rate 2.00
        self.assertEqual(Decimal(str(one(r)["unit_price"])), Decimal("14.00"))
        self.assertEqual(Decimal(str(one(r)["surcharge_per_unit"])), Decimal("4.00"))

    def test_rate_is_the_dated_windows_rate_not_the_last_listed(self):
        # same tariff, scenario day at the window edge: even if the ERP tariff says 4.00 and it matches the
        # LAST-LISTED rate, the dated (3.15-style) selection wins — here scenario 2099-01-15 gives 2.00.
        r = calculate_bid(pl(metal_index={"messages": self.msgs()}, positions=[pos(
            material=self.m10(), metal_index_code="STL-X",
            mass_surcharge={"kg_per_piece": "2.0", "eur_per_kg": "2.00", "index_code": "STL-X"})]))
        self.assertEqual(r["derived"]["positions"]["P-1"]["mass_per_piece"], "4.0")  # 2.0 kg x 2.00 (not 4.00)

    def test_untouched_rate_mismatch_is_rejected_with_actionable_message(self):
        with self.assertRaises(PricingError) as e:
            calculate_bid(pl(metal_index={"messages": self.msgs()}, positions=[pos(
                material=self.m10(), metal_index_code="STL-X",
                mass_surcharge={"kg_per_piece": "2.0", "eur_per_kg": "3.50", "index_code": "STL-X"})]))
        self.assertEqual(e.exception.path, "positions.0.mass_surcharge.eur_per_kg")
        self.assertIn("dated index rate", e.exception.message)

    def test_index_code_without_position_code_rejected(self):
        with self.assertRaises(PricingError) as e:
            calculate_bid(pl(metal_index={"messages": self.msgs()}, positions=[pos(
                material=self.m10(), mass_surcharge={"kg_per_piece": "2.0", "eur_per_kg": "2.00",
                                                     "index_code": "STL-X"})]))
        self.assertEqual(e.exception.path, "positions.0.mass_surcharge.index_code")

    def test_mass_index_code_mismatching_position_code_rejected(self):
        with self.assertRaises(PricingError) as e:
            calculate_bid(pl(metal_index={"messages": [{"code": "ALU-X", "rate": "9.00",
                                                       "valid_from": "2099-01-01"}]},
                             positions=[pos(material=self.m10(), metal_index_code="STL-X",
                                            mass_surcharge={"kg_per_piece": "2.0", "eur_per_kg": "2.00",
                                                            "index_code": "ALU-X"})]))
        self.assertEqual(e.exception.path, "positions.0.mass_surcharge.index_code")

    def test_index_code_never_bypasses_position_authority(self):
        # bypass attempt: metal_index_code STL-X (dated rate 2.00) set, mass tariff 5.00, index_code omitted —
        # it must NOT silently price on 5.00; the position code's dated rate is the authority and wins over
        # an omitted-code tariff only via an explicit stale mark, otherwise reject.
        with self.assertRaises(PricingError) as e:
            calculate_bid(pl(metal_index={"messages": self.msgs()}, positions=[pos(
                material=self.m10(), metal_index_code="STL-X",
                mass_surcharge={"kg_per_piece": "2.0", "eur_per_kg": "5.00"})]))
        self.assertEqual(e.exception.path, "positions.0.mass_surcharge.eur_per_kg")
        self.assertIn("does not match the dated index rate", e.exception.message)

    def test_report_only_index_never_touches_money(self):
        # matching tariff and code: the dated rate 2.00 is reported in derived; the matching tariff 2.00 prices
        # normally — an index never invents money on its own.
        r = calculate_bid(pl(metal_index={"messages": self.msgs()}, positions=[pos(
            material=self.m10(), metal_index_code="STL-X",
            mass_surcharge={"kg_per_piece": "2.0", "eur_per_kg": "2.00"})]))
        self.assertEqual(r["derived"]["positions"]["P-1"]["metal_index"], {"code": "STL-X", "rate": "2.0"})
        self.assertEqual(r["derived"]["positions"]["P-1"]["mass_per_piece"], "4.0")  # 2.0 kg x 2.00
        self.assertEqual(Decimal(str(one(r)["unit_price"])), Decimal("14.00"))  # 10.00 + 4.00

    def test_mass_index_code_without_position_code_rejected(self):
        with self.assertRaises(PricingError) as e:
            calculate_bid(pl(metal_index={"messages": self.msgs()}, positions=[pos(
                material=self.m10(), mass_surcharge={"kg_per_piece": "2.0", "eur_per_kg": "2.00",
                                                     "index_code": "STL-X"})]))
        self.assertEqual(e.exception.path, "positions.0.mass_surcharge.index_code")

    def test_inclusive_window_not_last_listed_rule28(self):
        r = calculate_bid(pl(metal_index={"messages": self.msgs()},
                             positions=[pos(material=self.m10(), metal_index_code="STL-X")]))
        self.assertEqual(r["derived"]["positions"]["P-1"]["metal_index"], {"code": "STL-X", "rate": "2.0"})
        self.assertEqual(Decimal(str(one(r)["base_unit_price"])), Decimal("10.00"))  # never applied to money

    def test_missing_window_selection_rejected(self):
        with self.assertRaises(PricingError) as e:
            calculate_bid(pl(metal_index={"messages": [{"code": "STL-X", "rate": "3.15",
                                                       "valid_from": "2099-02-01"}]},
                             positions=[pos(material=self.m10(), metal_index_code="STL-X")]))
        self.assertEqual(e.exception.path, "positions.0.metal_index_code")


class FinalFormula(unittest.TestCase):
    def test_rule27_single_round_from_unrounded_components(self):
        # base 10.0045 -> FX-converted 10.00 (the only base round); goods 3 x 10.00 = 30.00; bonus 0.1 % = 0.03.
        # unit = round(10.00 + 0.02*1.66667 + (0.01 setup + 0.01*30.0045/100 bonus credit - 0.03)/3)
        #      = round(10.00 + 0.0333334 + 0.0000345/3) ... computed exactly below = 10.03.
        m = mat(price_conditions=[{"valid_from": None, "valid_to": None, "currency": "USD",
                                   "fx_key": "FX-USD", "price_unit_pieces": 1}],
                quantity_scales=[{"min_pieces": 1, "price": "10.0045"}], stock=99)
        bonus = {"bands": [{"min_net": "0.00", "max_net": None, "percent": "0.1"}],
                 "allocation": "largest_remainder_cents_asc"}
        r = calculate_bid(pl(fx_rates=[{"key": "FX-USD", "eur_per_unit": "1", "dated": "2099-01-01"}],
                             project_bonus=bonus,
                             positions=[pos(tender_quantity=3, material=m,
                                            mass_surcharge={"kg_per_piece": "0.02", "eur_per_kg": "1.66667"},
                                            ordinary_setup_fee="0.01", project_bonus_eligible=True)]))
        a = one(r)
        # hand-computed: goods 3 x 10.00 = 30.00; bonus credit round(30.00 x 0.001) = 0.03;
        # unit = round(10.00 + 0.02 x 1.66667 + (0.01 setup - 0.03 bonus) / 3)
        #      = round(10.0333334 - 0.0066667) = round(10.0266667) -> HALF_UP -> 10.03.
        self.assertEqual(hu(Decimal("10.00") + Decimal("0.02") * Decimal("1.66667")
                            + (Decimal("0.01") - hu(Decimal("30.00") * Decimal("0.001"))) / 3),
                         Decimal("10.03"))
        self.assertEqual(Decimal(str(a["unit_price"])), Decimal("10.03"))
        self.assertEqual(Decimal(str(a["base_unit_price"])), Decimal("10.00"))
        self.assertEqual(Decimal(str(a["rebate_total"])), Decimal("0.03"))
        self.assertEqual(Decimal(str(a["surcharge_per_unit"])),
                         hu(Decimal("0.02") * Decimal("1.66667") + Decimal("0.01") / 3))  # 0.03399 -> 0.03
        self.assertEqual(Decimal(str(a["unit_price"])), Decimal("10.03"))


class SelectDatedAndErrors(unittest.TestCase):
    def test_dated_beats_windowed_and_future_never(self):
        recs = [{"k": "A", "v": "undated"},
                {"k": "A", "v": "window", "valid_from": "2099-01-01", "valid_to": "2099-12-31"},
                {"k": "A", "v": "dated", "dated": "2099-01-05"},
                {"k": "A", "v": "future", "dated": "2099-02-01"}]
        self.assertEqual(select_dated(recs, "k", "A", "2099-01-15")["v"], "dated")

    def test_window_excludes_then_undated_fallback(self):
        recs = [{"k": "A", "v": "undated"}, {"k": "A", "v": "late", "valid_from": "2099-02-01"}]
        self.assertEqual(select_dated(recs, "k", "A", "2099-01-15")["v"], "undated")
        self.assertIsNone(select_dated([{"k": "A", "v": "late", "valid_from": "2099-02-01"}],
                                       "k", "A", "2099-01-15"))

    def test_exact_code_and_latest_dated_wins(self):
        recs = [{"k": "A", "v": 1, "dated": "2099-01-01"}, {"k": "A", "v": 2, "dated": "2099-01-10"},
                {"k": "B", "v": 3, "dated": "2099-01-02"}]
        self.assertEqual(select_dated(recs, "k", "A", "2099-01-15")["v"], 2)
        self.assertIsNone(select_dated(recs, "k", "C", "2099-01-15"))

    def test_conflicting_windowed_rates_are_ambiguous_not_last_listed(self):
        # Rule 21 orders records by date, and a windowed record has none: two windows over the same day that
        # disagree on rate cannot be resolved, so the page order must not decide (rule 28 has the same rule).
        windowed = [{"k": "A", "v": "early", "rate": "1.00", "valid_from": "2099-01-01", "valid_to": "2099-12-31"},
                    {"k": "A", "v": "later", "rate": "9.00", "valid_from": "2099-01-10", "valid_to": "2099-02-01"}]
        with self.assertRaisesRegex(PricingError, "disagree on rate") as e:
            select_dated(windowed, "k", "A", "2099-01-15")
        self.assertEqual(e.exception.path, "records.1")
        self.assertIn("rule 21", str(e.exception))
        # a `dated` field resolves it again: the dated branch still wins over every windowed record
        self.assertEqual(select_dated(windowed + [{"k": "A", "v": "dated", "dated": "2099-01-12"}],
                                       "k", "A", "2099-01-15")["v"], "dated")
        # one window containing the day still selects that rate, not the last listed
        self.assertEqual(select_dated([{"k": "A", "v": "out", "rate": "5.00", "valid_from": "2099-02-01"},
                                       windowed[0]], "k", "A", "2099-01-15")["v"], "early")
        # identical rates are the same answer twice, so a duplicate mail is harmless (last listed)
        self.assertEqual(select_dated([windowed[0], dict(windowed[1], rate="1.00", v="same")],
                                       "k", "A", "2099-01-15")["v"], "same")
        # an FX list compares eur_per_unit too: two windowed rates that disagree are not resolvable by
        # list order (the ambiguity guard), identical rates stay harmless, a dated one resolves them.
        fx = [{"k": "A", "eur_per_unit": "0.90", "valid_to": "2099-01-31"},
              {"k": "A", "eur_per_unit": "0.92", "valid_to": "2099-01-31"}]
        with self.assertRaisesRegex(PricingError, "disagree on eur_per_unit"):
            select_dated(fx, "k", "A", "2099-01-15")
        self.assertEqual(select_dated([dict(fx[0]), dict(fx[1], eur_per_unit="0.90")],
                                       "k", "A", "2099-01-15")["eur_per_unit"], "0.90")
        self.assertEqual(select_dated(fx + [{"k": "A", "eur_per_unit": "0.95", "dated": "2099-01-12"}],
                                       "k", "A", "2099-01-15")["eur_per_unit"], "0.95")
        # a payload with neither rate nor eur_per_unit (a plain windowed lookup) is not compared
        self.assertEqual(select_dated([{"k": "A", "v": 1, "valid_to": "2099-01-31"},
                                       {"k": "A", "v": 2, "valid_to": "2099-01-31"}],
                                       "k", "A", "2099-01-15")["v"], 2)

    def test_ambiguous_fx_windows_reach_bid_as_fx_error(self):
        # A USD condition over two undated FX windows that disagree must surface against fx_rates,
        # not price at the last-listed rate (rule 22 then rule 16/21 authority).
        cond = {"valid_from": "2099-01-01", "valid_to": "2099-01-31", "currency": "USD",
                "fx_key": "SYNTH-FX", "price_unit_pieces": 1}
        scales = [{"min_pieces": 1, "price": "100.00"}]
        fx = [{"key": "SYNTH-FX", "eur_per_unit": "0.90", "valid_to": "2099-12-31"},
              {"key": "SYNTH-FX", "eur_per_unit": "0.92", "valid_to": "2099-12-31"}]
        with self.assertRaises(PricingError) as e:
            calculate_bid(pl(fx_rates=fx, positions=[pos(material=mat(price_conditions=[cond],
                                                                       quantity_scales=scales))]))
        self.assertEqual(e.exception.path, "fx_rates.records.1")
        self.assertIn("disagree on eur_per_unit", str(e.exception))
        # resolving the ambiguity with a dated record prices the bid at the dated rate
        r = calculate_bid(pl(fx_rates=fx + [{"key": "SYNTH-FX", "eur_per_unit": "0.85", "dated": "2099-01-10"}],
                             positions=[pos(material=mat(price_conditions=[cond], quantity_scales=scales))]))
        self.assertEqual(r["answer"]["decision"], "bid")
        # 100.00 USD/piece x 0.85 = 85.00 EUR, exact at the cent boundary (rule 22)
        self.assertEqual(Decimal(str(one(r)["base_unit_price"])), Decimal("85.00"))

    def test_conflicting_windowed_rates_reach_bid_as_position_error(self):
        # the ambiguity must surface against the position, not silently price the wrong index
        msgs = [{"code": "STL-X", "rate": "1.00", "valid_from": "2099-01-01", "valid_to": "2099-12-31"},
                {"code": "STL-X", "rate": "9.00", "valid_from": "2099-01-10", "valid_to": "2099-02-01"}]
        with self.assertRaises(PricingError) as e:
            select_dated(msgs, "code", "STL-X", "2099-01-15")
        self.assertEqual(e.exception.path, "records.1")

    def test_bad_record_or_date_raises(self):
        with self.assertRaises(PricingError):
            select_dated([{"other": 1}], "k", "A", "2099-01-15")
        with self.assertRaises(PricingError):
            select_dated([{"k": "A", "v": 1, "dated": "15.01.2099"}], "k", "A", "2099-01-15")

    def test_error_paths_point_at_the_exact_field(self):
        cases = [
            ({**pl(), "tender_reference": ""}, "tender_reference"),
            (pl(discount={"percent": "101", "default_scope": "base"}), "discount.percent"),
            (pl(discount={"percent": "5", "default_scope": "sideways"}), "discount.default_scope"),
            (pl(fx_rates=[{"key": "K", "eur_per_unit": "oops"}]), "fx_rates.0.eur_per_unit"),
            (pl(setup_pools=[{"pool": "R", "fee": "1.00", "members": {},
                              "allocation": "largest_remainder_cents_asc"}]), "setup_pools.0.members"),
        ]
        for payload, path in cases:
            with self.assertRaises(PricingError, msg=str(payload)) as e:
                calculate_bid(payload)
            self.assertEqual(e.exception.path, path)


class SchemaDoc(unittest.TestCase):
    def test_doc_is_compact_and_carries_the_example(self):
        self.assertLessEqual(PAYLOAD_SCHEMA_DOC.count("\n"), 90)
        self.assertIn("pieces_per_set", PAYLOAD_SCHEMA_DOC)
        self.assertIn("largest_remainder_cents_asc", PAYLOAD_SCHEMA_DOC)
        self.assertIn("M-EXAMPLE-9", PAYLOAD_SCHEMA_DOC)

    def test_the_documented_example_payload_round_trips(self):
        import json
        doc = PAYLOAD_SCHEMA_DOC
        pl = json.loads(doc[doc.index('{\n  "scenario_date"'):])
        r = calculate_bid(pl)
        validate(r["answer"], ANSWER_SCHEMA)
        self.assertEqual(r["answer"]["decision"], "bid")
        self.assertTrue(all(r["checks"].values()))
        # the example's USD condition: 108.50 per 10 pieces x 0.92 = 9.982 -> 9.98 (rule 22 in the doc works)
        self.assertEqual(r["derived"]["positions"]["P-2"]["base_per_piece"], "9.98")


if __name__ == "__main__":
    unittest.main()
