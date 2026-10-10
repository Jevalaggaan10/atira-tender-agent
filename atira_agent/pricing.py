"""Pure commercial calculator for the RFQ bid (v6 bid rules 1–29).

Facts arrive only inside the payload (the runtime gathers them model-directed); this module only calculates.
Money is `decimal.Decimal` end to end; a result crosses the boundary as cent strings (derived) and as floats of
exact cent values (answer). Half-up cent rounding happens ONLY at: the converted scale price after FX, the
aggregate project bonus credit, the final unit price (once, from unrounded per-piece components) and nowhere else.
Breakdown fields are reports, never inputs to the final formula (rule 18/27). No I/O, no clock, no network.
"""
from __future__ import annotations

import re
from datetime import date
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_SCOPES = ("base", "base_plus_mass", "none")
_ALLOC = "largest_remainder_cents_asc"
_UNITS = {"stück": 1, "stueck": 1, "stk": 1, "satz": None}  # Satz converts via pieces_per_set


class PricingError(ValueError):
    """A payload or derivation problem: `.message` says what, `.path` says where ("positions.2.quantity_scales")."""

    def __init__(self, message: str, path: str | None = None):
        super().__init__(f"{path}: {message}" if path else message)
        self.message = message
        self.path = path


def _sub(path: str | None, part: str | int | None) -> str:
    if part is None:
        return path or ""
    return f"{path}.{part}" if path else str(part)


def _wrap(exc: PricingError, path: str | None) -> PricingError:
    if path is None or exc.path is None:
        return exc if exc.path or path is None else PricingError(exc.message, path)
    return PricingError(exc.message, f"{path}.{exc.path}")


def _check_keys(obj: dict, path: str, required: tuple = (), optional: tuple = ()) -> None:
    if not isinstance(obj, dict):
        raise PricingError("expected an object", path)
    for key in required:
        if key not in obj:
            raise PricingError(f"missing required key {key!r}", _sub(path, key))
    for key in obj:
        if key not in required and key not in optional:
            raise PricingError(f"unknown key {key!r} (allowed: {', '.join(sorted(required + optional))})",
                               _sub(path, key))


def _dec(value, path: str, *, nonneg: bool = True, name: str = "money") -> Decimal:
    if isinstance(value, bool):
        raise PricingError(f"{name} must be a number, not a boolean", path)
    try:
        d = value if isinstance(value, Decimal) else Decimal(str(value))
    except (ArithmeticError, ValueError):
        raise PricingError(f"{name} must be int/float/decimal-string, got {value!r}", path) from None
    if not d.is_finite():
        raise PricingError(f"{name} must be finite, got {value!r}", path)
    if nonneg and d < 0:
        raise PricingError(f"{name} must not be negative, got {d}", path)
    return d


def _int(value, path: str, *, minimum: int = 0, name: str = "integer") -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PricingError(f"{name} must be an integer, got {value!r}", path)
    if value < minimum:
        raise PricingError(f"{name} must be >= {minimum}, got {value}", path)
    return value


def _str(value, path: str, *, max_len: int | None = None, name: str = "text") -> str:
    if not isinstance(value, str) or not value.strip():
        raise PricingError(f"{name} must be a non-empty string, got {value!r}", path)
    if max_len is not None and len(value) > max_len:
        raise PricingError(f"{name} must be at most {max_len} characters, got {len(value)}", path)
    return value


def _iso(value, path: str, *, name: str = "date") -> date:
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        raise PricingError(f"{name} must be a calendar date as YYYY-MM-DD, got {value!r}", path)
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise PricingError(f"{name} is not a real calendar date: {value!r}", path) from None


def _opt_iso(value, path: str, *, name: str = "date") -> date | None:
    return None if value is None else _iso(value, path, name=name)


def _money(value, path: str, *, name: str = "money") -> Decimal:
    return _dec(value, path, nonneg=True, name=name)


def _cent(d: Decimal) -> Decimal:
    return d.quantize(CENT, rounding=ROUND_HALF_UP)


def _norm(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    if "." not in text:
        text += ".0"
    return text


def _attr_key(v):
    if v is None:
        return "none"
    if isinstance(v, bool):
        return f"bool:{v}"
    if isinstance(v, (int, float, Decimal)):
        try:
            return f"num:{_dec(v, '', name='attr').normalize()}"
        except PricingError:
            return f"str:{str(v).strip().casefold()}"
    if isinstance(v, str):
        try:
            return f"num:{Decimal(v.strip()).normalize()}"
        except (ArithmeticError, ValueError):
            return f"str:{v.strip().casefold()}"
    if isinstance(v, (list, tuple, set, frozenset)):
        return "list:" + ",".join(sorted(_attr_key(x) for x in v))
    return f"other:{v!r}"


def _requirement_match(path: str, requirement: dict, attrs: dict) -> None:
    for key in sorted(requirement):
        want = requirement[key]
        if key not in attrs:
            raise PricingError(
                f"requirement attribute {key!r} = {want!r} does not match the material: attr missing",
                _sub(path, f"requirement.{key}"))
        got = attrs[key]
        if _attr_key(want) != _attr_key(got):
            raise PricingError(
                f"requirement attribute {key!r} = {want!r} does not match material attrs {key!r} = {got!r} "
                "(select the material that matches exactly, or no_bid if none does)",
                _sub(path, f"requirement.{key}"))


def _window_ok(path: str, rec: dict, scenario: date, *, require_window: bool = False) -> bool:
    vfrom = rec.get("valid_from")
    vto = rec.get("valid_to")
    if require_window and vfrom is None and vto is None:
        return False
    lo = _opt_iso(vfrom, _sub(path, "valid_from")) if vfrom is not None else None
    hi = _opt_iso(vto, _sub(path, "valid_to")) if vto is not None else None
    if lo is not None and scenario < lo:
        return False
    return not (hi is not None and scenario > hi)


def select_dated(records: list[dict], code_key: str, code: str, scenario_date: str) -> dict | None:
    """    Latest record with records[code_key] == code (EXACT) valid on scenario_date.

    Inclusive [valid_from, valid_to] windows must contain scenario_date; records dated after scenario_date never
    apply. A record carrying `dated` wins over an undated one; among dated, the latest `dated` (then list order).
    Records without any date or window are eligible only when no dated/windowed record matches (last one listed).
    Two windowed records covering the day that disagree on their applicable value (`rate` or, for an FX list,
    `eur_per_unit`) raise: neither is dated, so rule 21 cannot order them. Equal values are the same answer
    twice: last listed. Records carrying neither field (a plain windowed lookup) are never compared.
    """
    scenario = _iso(scenario_date, "scenario_date")
    if not isinstance(records, list):
        raise PricingError("records must be a list", "records")
    code = _str(code, "code", name="code")
    dated: list[tuple[date, int, dict]] = []
    windowed: list[tuple[int, dict]] = []
    undated: list[tuple[int, dict]] = []
    for i, rec in enumerate(records):
        path = f"records.{i}"
        if not isinstance(rec, dict):
            raise PricingError("record must be an object", path)
        if code_key not in rec:
            raise PricingError(f"record lacks the key {code_key!r}", path)
        if rec[code_key] != code:
            continue
        d = _opt_iso(rec.get("dated"), _sub(path, "dated"))
        if d is not None and d > scenario:
            continue  # future-dated records never apply (rule 21)
        if not _window_ok(path, rec, scenario):
            continue
        if d is not None:
            dated.append((d, i, rec))
        elif rec.get("valid_from") is not None or rec.get("valid_to") is not None:
            windowed.append((i, rec))
        else:
            undated.append((i, rec))
    if dated:
        return max(dated, key=lambda t: (t[0], t[1]))[2]
    if windowed:
        # Rule 21 resolves a key from the LATEST record no later than the scenario date, and a windowed record
        # carries no date to be latest by: only its window says it applies, not when it superseded the others.
        # So two windowed records that both contain scenario_date cannot be ordered by rule 21 or 28 — taking
        # the last listed would decide by Q&A page order. The compared value is `rate`, or `eur_per_unit` for
        # an FX list; identical values are the same answer twice and stay harmless (last listed). A record
        # carrying neither field is not compared (a plain windowed lookup has no value to disagree on).
        if len(windowed) > 1:
            field = "rate" if any(r.get("rate") is not None for _, r in windowed) else "eur_per_unit"
            values = [(i, rec.get(field)) for i, rec in windowed]
            distinct = {_attr_key(r) for _, r in values if r is not None}
            if len(distinct) > 1:
                listed = ", ".join(f"{field}={r!r} at records.{i}" for i, r in values)
                raise PricingError(
                    f"{len(windowed)} windowed records for {code_key}={code!r} cover {scenario.isoformat()} and "
                    f"disagree on {field} ({listed}) — none carries a `dated` field, so rule 21 cannot say which "
                    "supersedes the other: date the governing record, or keep only the one whose window applies",
                    _sub("records", windowed[-1][0]))
        return windowed[-1][1]
    return undated[-1][1] if undated else None


def derive_nominal_stroke(stroke_mm, per_end_reserve_mm) -> Decimal:
    """Rule 20: nominal stroke = usable stroke + twice the per-end reserve (applied after scoped amendments)."""
    stroke = _dec(stroke_mm, "stroke_mm", name="stroke_mm")
    reserve = _dec(per_end_reserve_mm, "per_end_reserve_mm", name="per_end_reserve_mm")
    return stroke + 2 * reserve


def _allocate_cents(total: Decimal, weights: list[tuple[str, Decimal]], path: str) -> list[Decimal]:
    """Floor the exact cent amount by weight, hand out leftover cents by largest remainder, ties by ascending key."""
    if not weights:
        raise PricingError("no members to allocate to", path)
    total_weight = sum((w for _, w in weights), Decimal(0))
    if total_weight <= 0:
        raise PricingError("allocation weights sum to zero — cannot allocate the fee", path)
    exact = [total * w / total_weight for _, w in weights]
    floors = [e.quantize(CENT, rounding=ROUND_DOWN) for e in exact]
    leftover = int(((total - sum(floors)) / CENT).to_integral_value(rounding=ROUND_HALF_UP))
    order = sorted(range(len(weights)), key=lambda i: (-(exact[i] - floors[i]), weights[i][0]))
    out = list(floors)
    for i in order[:leftover]:
        out[i] += CENT
    return out


def _resolve_position_quantity(path: str, pos: dict, scenario: date) -> int:
    tender_qty = _int(pos["tender_quantity"], _sub(path, "tender_quantity"), name="tender_quantity")
    tender_unit = _str(pos["tender_unit"], _sub(path, "tender_unit"), name="tender_unit")
    pps = pos.get("pieces_per_set")
    if pps is not None:
        pps = _int(pps, _sub(path, "pieces_per_set"), minimum=1, name="pieces_per_set")

    def to_pieces(qty: int, unit: str, where: str) -> int:
        key = unit.strip().casefold()
        if key == "satz":
            if pps is None:
                raise PricingError("unit 'Satz' needs pieces_per_set for this position", where)
            return qty * pps
        factor = _UNITS.get(key)
        if factor is None:
            raise PricingError(f"unknown unit {unit!r} (use Stück or Satz, normalized)", where)
        return qty * factor

    units = [tender_unit] + [e.get("unit") for e in pos.get("quantity_events") or []
                             if isinstance(e, dict) and e.get("unit") is not None]
    if any(str(u).strip().casefold() == "satz" for u in units) and pps is None:
        raise PricingError("unit 'Satz' needs pieces_per_set for this position", _sub(path, "pieces_per_set"))

    # Rule 13 withdrawals are validated over the whole event list first: a number named by `withdraws`
    # must be an answer of this position (possibly listed later — reading the Q&A page in order is not a
    # chronology rule); only mutual/cyclic withdrawals are unresolvable. Withdrawn answers never contribute
    # (rule 13): neither a quantity change nor their withdrawal.
    events = pos.get("quantity_events") or []
    if not isinstance(events, list):
        raise PricingError("quantity_events must be a list", _sub(path, "quantity_events"))
    parsed = []
    seen: dict[int, int] = {}
    for i, ev in enumerate(events):
        epath = _sub(path, f"quantity_events.{i}")
        _check_keys(ev, epath, required=("answer",), optional=("dated", "withdraws", "quantity", "unit"))
        answer = _int(ev["answer"], _sub(epath, "answer"), name="answer")
        if answer in seen:
            raise PricingError(f"answer number {answer} already used at events.{seen[answer]}", _sub(epath, "answer"))
        seen[answer] = i
    for i, ev in enumerate(events):
        epath = _sub(path, f"quantity_events.{i}")
        answer = _int(ev["answer"], _sub(epath, "answer"), name="answer")
        dated = _opt_iso(ev.get("dated"), _sub(epath, "dated"))
        withdraws = ev.get("withdraws")
        if withdraws is not None:
            withdraws = _int(withdraws, _sub(epath, "withdraws"), name="withdraws")
            if withdraws == answer:
                raise PricingError("an answer cannot withdraw itself", _sub(epath, "withdraws"))
            if withdraws not in seen:
                raise PricingError(
                    f"withdraws={withdraws} names no answer of this position — include the withdrawn "
                    "answer's event here too (rule 13)", _sub(epath, "withdraws"))
        quantity = ev.get("quantity")
        if quantity is not None:
            quantity = _int(quantity, _sub(epath, "quantity"), minimum=1, name="quantity")
        unit = ev.get("unit")
        if unit is not None:
            unit = _str(unit, _sub(epath, "unit"), name="unit")
        if quantity is None and withdraws is None:
            raise PricingError("event carries neither a quantity nor a withdraws reference", epath)
        parsed.append({"answer": answer, "index": i, "dated": dated, "withdraws": withdraws,
                       "quantity": quantity, "unit": unit})

    # A future-dated clarification never applies yet (rule 21) — and cannot withdraw anything either.
    active = [e for e in parsed if e["dated"] is None or e["dated"] <= scenario]
    withdrawn: set[int] = set()
    for _ in range(len(active) + 1):
        nxt = {e["withdraws"] for e in active if e["withdraws"] is not None and e["answer"] not in withdrawn}
        if nxt == withdrawn:
            break
        withdrawn = nxt
    else:
        raise PricingError("withdrawal chain has no stable resolution (mutual withdrawal)",
                           _sub(path, "quantity_events"))
    surviving = [e for e in active if e["answer"] not in withdrawn and e["quantity"] is not None]
    winner = None
    if surviving:
        if any(e["dated"] is not None for e in surviving):
            # Rule 1: the LATEST DATED clarification that changes the position wins. Undated survivors are
            # unknown-age and cannot claim recency: if one would win the legacy answer-number order, ask the
            # model to date it rather than guess.
            top = max(surviving, key=lambda e: (e["dated"] or date.min, e["answer"], e["index"]))
            legacy = max(surviving, key=lambda e: (e["answer"], e["index"]))
            if legacy["dated"] is None and legacy is not top:
                raise PricingError(
                    f"surviving change answer={legacy['answer']} is undated while a dated clarification "
                    f"(dated={top['dated'].isoformat()}) may be later — give every Q&A change a dated field "
                    "(rule 1)", _sub(path, f"quantity_events.{legacy['index']}.dated"))
            winner = top
        else:
            # Legacy ordering, documented and only used when EVERY surviving change is undated:
            # highest answer number wins (later clarification typically carries the higher number).
            winner = max(surviving, key=lambda e: (e["answer"], e["index"]))
    if winner is None:
        return to_pieces(tender_qty, tender_unit, _sub(path, "tender_unit"))
    unit = winner["unit"] if winner["unit"] is not None else tender_unit
    return to_pieces(winner["quantity"], unit, _sub(path, "quantity_events"))


def _parse_material(path: str, material: dict) -> dict:
    _check_keys(material, path,
                required=("material_number", "attrs", "stock", "lead_time_stock_weeks",
                          "lead_time_production_weeks", "price_conditions", "quantity_scales"))
    number = _str(material["material_number"], _sub(path, "material_number"), max_len=50, name="material_number")
    if not isinstance(material["attrs"], dict):
        raise PricingError("attrs must be an object", _sub(path, "attrs"))
    out = {
        "material_number": number,
        "attrs": material["attrs"],
        "stock": _int(material["stock"], _sub(path, "stock"), name="stock"),
        "stock_weeks": _int(material["lead_time_stock_weeks"], _sub(path, "lead_time_stock_weeks"), name="lead_time"),
        "production_weeks": _int(material["lead_time_production_weeks"], _sub(path, "lead_time_production_weeks"),
                                 name="lead_time"),
    }
    conds = material["price_conditions"]
    if not isinstance(conds, list):
        raise PricingError("price_conditions must be a list", _sub(path, "price_conditions"))
    parsed_conds = []
    for i, c in enumerate(conds):
        cpath = _sub(path, f"price_conditions.{i}")
        _check_keys(c, cpath, required=("currency", "price_unit_pieces"),
                    optional=("valid_from", "valid_to", "fx_key"))
        currency = _str(c["currency"], _sub(cpath, "currency"), max_len=8, name="currency").strip().upper()
        cpu = _int(c["price_unit_pieces"], _sub(cpath, "price_unit_pieces"), minimum=1, name="price_unit_pieces")
        fx_key = c.get("fx_key")
        if fx_key is not None:
            fx_key = _str(fx_key, _sub(cpath, "fx_key"), name="fx_key")
        parsed_conds.append({
            "currency": currency, "cpu": cpu, "fx_key": fx_key,
            "valid_from": c.get("valid_from"), "valid_to": c.get("valid_to"),
        })
    out["conditions"] = parsed_conds
    scales = material["quantity_scales"]
    if not isinstance(scales, list) or not scales:
        raise PricingError("quantity_scales must be a non-empty list", _sub(path, "quantity_scales"))
    parsed_scales = []
    mins: dict[int, int] = {}
    for i, s in enumerate(scales):
        spath = _sub(path, f"quantity_scales.{i}")
        _check_keys(s, spath, required=("min_pieces", "price"))
        minp = _int(s["min_pieces"], _sub(spath, "min_pieces"), minimum=1, name="min_pieces")
        if minp in mins:
            raise PricingError(f"duplicate scale min_pieces={minp} (already at scales.{mins[minp]}) — ambiguous",
                               _sub(spath, "min_pieces"))
        mins[minp] = i
        parsed_scales.append({"min_pieces": minp, "price": _money(s["price"], _sub(spath, "price"),
                                                                  name="scale price")})
    parsed_scales.sort(key=lambda s: s["min_pieces"])
    out["scales"] = parsed_scales
    return out


def _parse_position(path: str, pos: dict, scenario: date) -> dict:
    _check_keys(pos, path,
                required=("position_id", "tender_quantity", "tender_unit", "quantity_events", "requirement",
                          "material", "konditionsgruppe", "discount_scope", "mass_surcharge",
                          "ordinary_setup_fee", "packaging", "delivery_commitment", "project_bonus_eligible"),
                optional=("pieces_per_set", "metal_index_code"))
    pid = _str(pos["position_id"], _sub(path, "position_id"), max_len=20, name="position_id")
    requirement = pos["requirement"] or {}
    if not isinstance(requirement, dict):
        raise PricingError("requirement must be an object or null", _sub(path, "requirement"))
    material = pos["material"]
    if material is not None:
        material = _parse_material(_sub(path, "material"), material)
    scope = pos["discount_scope"]
    if scope is not None and scope not in _SCOPES:
        raise PricingError(f"discount_scope must be one of {'|'.join(_SCOPES)} or null, got {scope!r}",
                           _sub(path, "discount_scope"))
    kg = pos["mass_surcharge"]
    if kg is not None:
        kpath = _sub(path, "mass_surcharge")
        _check_keys(kg, kpath, required=("kg_per_piece", "eur_per_kg"), optional=("index_code", "max_stale_rate"))
        kg = {"kg": _money(kg["kg_per_piece"], _sub(kpath, "kg_per_piece"), name="kg_per_piece"),
              "eur": _money(kg["eur_per_kg"], _sub(kpath, "eur_per_kg"), name="eur_per_kg"),
              "index_code": kg.get("index_code"), "max_stale": kg.get("max_stale_rate")}
    pack = pos["packaging"]
    if pack is not None:
        ppath = _sub(path, "packaging")
        _check_keys(pack, ppath, required=("eur_per_carton", "pieces_per_carton"))
        pack = {"cost": _money(pack["eur_per_carton"], _sub(ppath, "eur_per_carton"), name="eur_per_carton"),
                "per_carton": _int(pack["pieces_per_carton"], _sub(ppath, "pieces_per_carton"), minimum=1,
                                   name="pieces_per_carton")}
    commit = pos["delivery_commitment"]
    if commit is not None:
        cpath = _sub(path, "delivery_commitment")
        _check_keys(commit, cpath, required=("material_number", "ceiling_pieces", "lead_time_weeks"))
        commit = {"material": _str(commit["material_number"], _sub(cpath, "material_number"), max_len=50),
                  "ceiling": _int(commit["ceiling_pieces"], _sub(cpath, "ceiling_pieces"), name="ceiling_pieces"),
                  "weeks": _int(commit["lead_time_weeks"], _sub(cpath, "lead_time_weeks"), name="lead_time")}
    metal = pos.get("metal_index_code")
    if metal is not None:
        metal = _str(metal, _sub(path, "metal_index_code"), name="metal_index_code")
    if not isinstance(pos["project_bonus_eligible"], bool):
        raise PricingError("project_bonus_eligible must be true or false", _sub(path, "project_bonus_eligible"))
    return {
        "pid": pid, "requirement": requirement, "material": material,
        "final_pieces": _resolve_position_quantity(path, pos, scenario),
        "scope": scope, "mass": kg, "pack": pack, "commit": commit, "metal_code": metal,
        "setup": _money(pos["ordinary_setup_fee"], _sub(path, "ordinary_setup_fee"), name="ordinary_setup_fee"),
        "eligible": pos["project_bonus_eligible"],
    }


def _parse_bands(path: str, bonus) -> list[dict] | None:
    if bonus is None:
        return None
    _check_keys(bonus, path, required=("bands", "allocation"))
    if bonus["allocation"] != _ALLOC:
        raise PricingError(f"allocation must be {_ALLOC!r}", _sub(path, "allocation"))
    bands = bonus["bands"]
    if not isinstance(bands, list) or not bands:
        raise PricingError("bands must be a non-empty list", _sub(path, "bands"))
    out = []
    for i, b in enumerate(bands):
        bpath = _sub(path, f"bands.{i}")
        _check_keys(b, bpath, required=("min_net", "percent"), optional=("max_net",))
        lo = _money(b["min_net"], _sub(bpath, "min_net"), name="min_net")
        hi = None if b.get("max_net") is None else _money(b["max_net"], _sub(bpath, "max_net"), name="max_net")
        if hi is not None and hi <= lo:
            raise PricingError("max_net must exceed min_net (max exclusive)", _sub(bpath, "max_net"))
        if out and (out[-1]["max"] is None or lo < out[-1]["min"]
                    or (out[-1]["max"] is not None and lo < out[-1]["max"])):
            raise PricingError("bands must be ascending and non-overlapping; only the last may be unbounded", bpath)
        out.append({"min": lo, "max": hi, "pct": _dec(b["percent"], _sub(bpath, "percent"), name="percent")})
    return out


def calculate_bid(payload: dict) -> dict:
    """The one correct bid for the facts in `payload`: {"answer", "derived", "checks"} — see docs/INTERFACES.md."""
    if not isinstance(payload, dict):
        raise PricingError("payload must be an object", None)
    _check_keys(payload, None,
                required=("scenario_date", "tender_reference", "discount", "fx_rates", "positions"),
                optional=("metal_index", "project_bonus", "setup_pools"))
    scenario = _iso(payload["scenario_date"], "scenario_date")
    reference = _str(payload["tender_reference"], "tender_reference", max_len=100, name="tender_reference")
    if not isinstance(payload["positions"], list) or not payload["positions"]:
        raise PricingError("positions must be a non-empty list (with no positions, no_bid is moot)", "positions")
    if len(payload["positions"]) > 100:
        raise PricingError("at most 100 positions", "positions")

    discount = payload["discount"]
    _check_keys(discount, "discount", required=("percent",), optional=("threshold_min_net", "default_scope"))
    approved = _dec(discount["percent"], "discount.percent", name="discount.percent")
    if not 0 <= approved <= 100:
        raise PricingError(f"discount.percent must be within 0..100, got {approved}", "discount.percent")
    threshold = None
    if discount.get("threshold_min_net") is not None:
        threshold = _money(discount["threshold_min_net"], "discount.threshold_min_net",
                           name="threshold_min_net")
    default_scope = discount.get("default_scope")
    if default_scope is None:
        default_scope = "base"  # rule 12: positions without a group use the default base-only rule
    elif default_scope not in _SCOPES:
        raise PricingError(f"default_scope must be one of {'|'.join(_SCOPES)}", "discount.default_scope")

    fx_rates = payload["fx_rates"]
    if not isinstance(fx_rates, list):
        raise PricingError("fx_rates must be a list", "fx_rates")
    for i, fx in enumerate(fx_rates):
        fpath = f"fx_rates.{i}"
        _check_keys(fx, fpath, required=("key", "eur_per_unit"), optional=("dated", "valid_from", "valid_to"))
        _str(fx["key"], _sub(fpath, "key"), name="key")
        _dec(fx["eur_per_unit"], _sub(fpath, "eur_per_unit"), nonneg=False, name="eur_per_unit")
        _opt_iso(fx.get("dated"), _sub(fpath, "dated"))

    bonus_bands = _parse_bands("project_bonus", payload.get("project_bonus"))

    pools = payload.get("setup_pools") or []
    if not isinstance(pools, list):
        raise PricingError("setup_pools must be a list", "setup_pools")
    parsed_pools = []
    for i, pool in enumerate(pools):
        ppath = f"setup_pools.{i}"
        _check_keys(pool, ppath, required=("pool", "fee", "members", "allocation"))
        if pool["allocation"] != _ALLOC:
            raise PricingError(f"allocation must be {_ALLOC!r}", _sub(ppath, "allocation"))
        members = pool["members"]
        if not isinstance(members, dict) or not members:
            raise PricingError("members must be a non-empty object of position_id -> allocation mass",
                               _sub(ppath, "members"))
        parsed_pools.append({
            "name": _str(pool["pool"], _sub(ppath, "pool"), name="pool"),
            "fee": _money(pool["fee"], _sub(ppath, "fee"), name="fee"),
            "members": {k: _money(v, _sub(ppath, f"members.{k}"), name="allocation mass") for k, v in members.items()},
        })

    metal_index = payload.get("metal_index")
    if metal_index is not None:
        _check_keys(metal_index, "metal_index", required=("messages",))
        msgs = metal_index["messages"]
        if not isinstance(msgs, list):
            raise PricingError("messages must be a list", "metal_index.messages")
        for i, m in enumerate(msgs):
            mpath = f"metal_index.messages.{i}"
            _check_keys(m, mpath, required=("code", "rate"), optional=("valid_from", "valid_to", "dated"))
            _str(m["code"], _sub(mpath, "code"), name="code")
            _dec(m["rate"], _sub(mpath, "rate"), name="rate")
            _opt_iso(m.get("valid_from"), _sub(mpath, "valid_from"))
            _opt_iso(m.get("valid_to"), _sub(mpath, "valid_to"))

    positions: list[dict] = []
    seen_pids: dict[str, int] = {}
    for i, raw in enumerate(payload["positions"]):
        path = f"positions.{i}"
        pos = _parse_position(path, raw, scenario)
        if pos["pid"] in seen_pids:
            raise PricingError(f"duplicate position_id (already at positions.{seen_pids[pos['pid']]})",
                               _sub(path, "position_id"))
        seen_pids[pos["pid"]] = i
        positions.append(pos)

    for pool_index, pool in enumerate(parsed_pools):
        for member in pool["members"]:
            if member not in seen_pids:
                raise PricingError(f"pool member {member!r} is not a known position_id",
                                   f"setup_pools.{pool_index}.members.{member}")

    # Rules 3/8: one missing material forces no_bid before any further derivation.
    if any(p["material"] is None for p in positions):
        answer = {"tender_reference": reference, "decision": "no_bid", "currency": "EUR",
                  "discount_percent": 0.0, "positions": [], "total_net": 0.0}
        derived = {
            "effective_discount_percent": _norm(Decimal(0)),
            "threshold": {"required": None if threshold is None else _norm(_cent(threshold)),
                          "undiscounted_net": "0.00", "met": threshold is None},
            "positions": {},
        }
        checks = {"arithmetics_consistent": True, "total_is_sum_of_lines": True, "all_money_positive": True}
        return {"answer": answer, "derived": derived, "checks": checks}

    for p in positions:
        i = seen_pids[p["pid"]]
        path = f"positions.{i}"
        if p["requirement"]:
            _requirement_match(path, p["requirement"], p["material"]["attrs"])

    scenario_str = scenario.isoformat()
    base_pp: dict[str, Decimal] = {}
    meta: dict[str, dict] = {}
    for p in positions:
        i = seen_pids[p["pid"]]
        path = f"positions.{i}"
        material = p["material"]
        pieces = p["final_pieces"]
        if pieces <= 0:
            raise PricingError("final quantity must be at least 1 piece", _sub(path, "tender_quantity"))
        # Rules 20/28: a mass surcharge may only silently price on its own tariff when the position carries no
        # metal_index_code. With a code set, that code is the authority: its dated rate must match the tariff
        # (or the tariff is explicitly marked stale via max_stale_rate and the dated rate wins). No bypass:
        # mass_surcharge.index_code must name the position's metal_index_code, and index_code without a
        # position metal_index_code is rejected outright.
        if p["mass"] is not None and p["metal_code"] is not None:
            if p["mass"]["index_code"] is not None and p["mass"]["index_code"] != p["metal_code"]:
                raise PricingError(
                    f"mass_surcharge.index_code {p['mass']['index_code']!r} does not match the position's "
                    f"metal_index_code {p['metal_code']!r}", _sub(path, "mass_surcharge.index_code"))
            if metal_index is None:
                raise PricingError(f"metal_index_code {p['metal_code']!r} set but no metal_index messages given",
                                   _sub(path, "metal_index_code"))
            try:
                msg = select_dated(metal_index["messages"], "code", p["metal_code"], scenario_str)
            except PricingError as exc:
                raise _wrap(exc, "metal_index.messages") from None
            if msg is None:
                raise PricingError(
                    f"no metal index message for code {p['metal_code']!r} valid on {scenario_str} "
                    "(inclusive windows; not the last listed)", _sub(path, "metal_index_code"))
            rate = _dec(msg["rate"], "metal_index.messages", name="rate")
            if rate <= 0:
                raise PricingError(f"index rate must be positive, got {rate}", "metal_index.messages")
            if rate != p["mass"]["eur"]:
                stale = p["mass"]["max_stale"]
                if stale is not None and p["mass"]["eur"] <= _money(
                        stale, _sub(path, "mass_surcharge.max_stale_rate"), name="max_stale_rate"):
                    p["mass"]["eur"] = rate  # explicitly marked stale tariff: the dated index rate wins
                else:
                    raise PricingError(
                        f"mass_surcharge.eur_per_kg {p['mass']['eur']} does not match the dated index rate {rate} "
                        f"for code {p['metal_code']!r} — use the dated rate, or mark a stale tariff with "
                        "max_stale_rate", _sub(path, "mass_surcharge.eur_per_kg"))
        elif p["mass"] is not None and p["mass"]["index_code"] is not None:
            raise PricingError(
                "mass_surcharge.index_code is set but the position has no metal_index_code — set metal_index_code "
                "so the dated rate can be selected (rule 28)", _sub(path, "mass_surcharge.index_code"))

        scenario_conds = []
        for j, cond in enumerate(material["conditions"]):
            if _window_ok(_sub(path, f"price_conditions.{j}"), cond, scenario, require_window=False):
                scenario_conds.append((j, cond))
        if len(scenario_conds) != 1:
            raise PricingError(
                f"{len(scenario_conds)} price conditions valid on {scenario_str}; exactly one is required "
                "(fix the condition windows)", _sub(path, "price_conditions"))
        j, cond = scenario_conds[0]
        if cond["currency"] == "EUR":
            rate = Decimal(1)
        else:
            if not cond["fx_key"]:
                raise PricingError(f"currency {cond['currency']} needs an fx_key",
                                   _sub(path, f"price_conditions.{j}.fx_key"))
            try:
                fx = select_dated(fx_rates, "key", cond["fx_key"], scenario_str)
            except PricingError as exc:
                raise _wrap(exc, "fx_rates") from None
            if fx is None:
                raise PricingError(
                    f"no fx rate for key {cond['fx_key']!r} dated <= {scenario_str}",
                    _sub(path, f"price_conditions.{j}.fx_key"))
            rate = _dec(fx["eur_per_unit"], "fx_rates", name="eur_per_unit")
            if rate <= 0:
                raise PricingError(f"fx rate for {cond['fx_key']!r} must be positive, got {rate}", "fx_rates")
        scale = None
        for s in material["scales"]:
            if s["min_pieces"] <= pieces:
                scale = s
        if scale is None:
            raise PricingError(
                f"final quantity {pieces} is below every scale min_pieces (smallest "
                f"{material['scales'][0]['min_pieces']})", _sub(path, "quantity_scales"))
        per_piece_native = scale["price"] / cond["cpu"]  # rule 22: normalize price unit first
        converted = _cent(per_piece_native * rate)       # the ONLY post-FX rounding
        base_pp[p["pid"]] = converted
        metal = None
        if p["metal_code"] is not None:
            if metal_index is None:
                raise PricingError(f"metal_index_code {p['metal_code']!r} set but no metal_index messages given",
                                   _sub(path, "metal_index_code"))
            try:
                msg = select_dated(metal_index["messages"], "code", p["metal_code"], scenario.isoformat())
            except PricingError as exc:
                raise _wrap(exc, "metal_index.messages") from None
            if msg is None:
                raise PricingError(
                    f"no metal index message for code {p['metal_code']!r} valid on {scenario.isoformat()} "
                    "(inclusive windows; not the last listed)", _sub(path, "metal_index_code"))
            metal = {"code": p["metal_code"], "rate": _norm(_dec(msg["rate"], "metal_index.messages", name="rate"))}
        meta[p["pid"]] = {"currency": cond["currency"], "fx_rate": _norm(rate), "scale_min": scale["min_pieces"],
                          "metal": metal}

    # Rule 16: inclusive threshold on the undiscounted converted goods value (scale prices only).
    undiscounted_net = sum((base_pp[p["pid"]] * p["final_pieces"] for p in positions), Decimal(0))
    threshold_met = threshold is None or undiscounted_net >= threshold
    effective = approved if threshold_met else Decimal(0)
    factor = 1 - effective / 100

    # Rule 15: pooled demand per material number decides stock vs production for every position of that material.
    pooled: dict[str, int] = {}
    for p in positions:
        key = p["material"]["material_number"]
        pooled[key] = pooled.get(key, 0) + p["final_pieces"]

    # Rule 25: shared setup pools allocated by final pieces x allocation mass.
    setup_alloc = {p["pid"]: Decimal(0) for p in positions}
    for pool_index, pool in enumerate(parsed_pools):
        by_pid = {p["pid"]: p for p in positions}
        weights = [(pid, by_pid[pid]["final_pieces"] * mass) for pid, mass in sorted(pool["members"].items())]
        shares = _allocate_cents(pool["fee"], weights, f"setup_pools.{pool_index}")
        for (pid, _), share in zip(weights, shares):
            setup_alloc[pid] += share

    # Rule 26: marginal bonus bands on the eligible undiscounted goods value, allocated in cents.
    bonus_alloc = {p["pid"]: Decimal(0) for p in positions}
    if bonus_bands is not None:
        eligible = [(p["pid"], base_pp[p["pid"]] * p["final_pieces"]) for p in positions if p["eligible"]]
        total_goods = sum((g for _, g in eligible), Decimal(0))
        credit = Decimal(0)
        for band in bonus_bands:
            portion = min(total_goods, band["max"]) - band["min"] if band["max"] is not None else total_goods - band["min"]
            if portion > 0:
                credit += portion * band["pct"] / 100
        credit = _cent(credit)
        if credit > 0 and total_goods <= 0:
            raise PricingError("bonus credit > 0 but eligible goods total is 0 — cannot allocate",
                               "project_bonus.bands")
        if credit > 0:
            shares = _allocate_cents(credit, eligible, "project_bonus")
            for (pid, _), share in zip(eligible, shares):
                bonus_alloc[pid] = share

    answer_positions = []
    derived_positions = {}
    total_net = Decimal(0)
    for p in positions:
        i = seen_pids[p["pid"]]
        path = f"positions.{i}"
        material = p["material"]
        pieces = p["final_pieces"]
        pid = p["pid"]
        base = base_pp[pid]
        mass_pp = p["mass"]["kg"] * p["mass"]["eur"] if p["mass"] else Decimal(0)
        scope = p["scope"] if p["scope"] is not None else default_scope
        disc_base = base * factor if scope in ("base", "base_plus_mass") else base
        disc_mass = mass_pp * factor if scope == "base_plus_mass" else mass_pp
        setup_total = p["setup"] + setup_alloc[pid]
        if p["pack"] is None:
            cartons, carton_cost = 0, Decimal(0)
        else:
            cartons = -(-pieces // p["pack"]["per_carton"])
            carton_cost = Decimal(cartons) * p["pack"]["cost"]
        shared = setup_total + carton_cost - bonus_alloc[pid]
        unit_price = _cent(disc_base + disc_mass + shared / pieces)  # rule 27: one final round
        if unit_price < 0:
            raise PricingError(
                f"final unit price is negative ({unit_price}) — check the setup/bonus amounts",
                _sub(path, "ordinary_setup_fee"))
        line = unit_price * pieces
        total_net += line

        # Rule 24 then rule 15 for lead time.
        commit = p["commit"]
        if commit is not None and commit["material"] == material["material_number"] \
                and pooled[material["material_number"]] <= commit["ceiling"]:
            lead, delivery = commit["weeks"], "committed"
        elif material["stock"] >= pooled[material["material_number"]]:
            lead, delivery = material["stock_weeks"], "stock"
        else:
            lead, delivery = material["production_weeks"], "production"

        surcharge_report = _cent(mass_pp + setup_total / pieces)      # reports, never fed back (rule 18)
        packaging_report = _cent(carton_cost / pieces)
        answer_positions.append({"position": pid, "material_number": material["material_number"],
                                 "quantity": pieces, "unit_price": float(unit_price), "lead_time_weeks": lead,
                                 "base_unit_price": float(base), "surcharge_per_unit": float(surcharge_report),
                                 "packaging_per_unit": float(packaging_report),
                                 "rebate_total": float(bonus_alloc[pid])})
        derived_positions[pid] = {"final_pieces": pieces, "base_per_piece": _norm(base),
                                  "currency": meta[pid]["currency"], "fx_rate": meta[pid]["fx_rate"],
                                  "scale_min_pieces": meta[pid]["scale_min"], "mass_per_piece": _norm(mass_pp),
                                  "setup_total": _norm(setup_total), "carton_cost": _norm(carton_cost),
                                  "cartons": cartons, "bonus_allocated": _norm(bonus_alloc[pid]),
                                  "lead_time_weeks": lead, "delivery": delivery, "metal_index": meta[pid]["metal"],
                                  "line_total": _norm(line), "unit_price": _norm(unit_price)}

    answer = {"tender_reference": reference, "decision": "bid", "currency": "EUR",
              "discount_percent": float(effective), "positions": answer_positions,
              "total_net": float(_cent(total_net))}

    # Independent re-check of the emitted (float) answer against the cent-derived values.
    lines_sum = sum((Decimal(str(a["unit_price"])) * a["quantity"] for a in answer_positions), Decimal(0))
    arith = all(
        Decimal(str(a["unit_price"])) == Decimal(derived_positions[a["position"]]["unit_price"])
        and Decimal(str(a["base_unit_price"])) == Decimal(derived_positions[a["position"]]["base_per_piece"])
        and Decimal(str(a["rebate_total"])) == Decimal(derived_positions[a["position"]]["bonus_allocated"])
        and Decimal(str(a["unit_price"])) * a["quantity"] == Decimal(derived_positions[a["position"]]["line_total"])
        for a in answer_positions
    ) and _cent(lines_sum) == _cent(total_net)
    money_ok = all(Decimal(str(a[f])) >= 0 for a in answer_positions
                   for f in ("unit_price", "base_unit_price", "surcharge_per_unit", "packaging_per_unit",
                             "rebate_total")) and Decimal(str(answer["total_net"])) >= 0
    checks = {"arithmetics_consistent": bool(arith),
              "total_is_sum_of_lines": bool(_cent(lines_sum) == _cent(total_net)),
              "all_money_positive": bool(money_ok)}
    derived = {
        "effective_discount_percent": _norm(effective),
        "threshold": {"required": None if threshold is None else _norm(_cent(threshold)),
                      "undiscounted_net": _norm(_cent(undiscounted_net)), "met": bool(threshold_met)},
        "positions": derived_positions,
    }
    return {"answer": answer, "derived": derived, "checks": checks}


PAYLOAD_SCHEMA_DOC = """calculate_bid payload — all facts about THIS tender, gathered from the sites at run time.
Rules: every listed key is required unless marked optional; unknown keys are errors; all dates are "YYYY-MM-DD";
money accepts int/float/decimal-string (never negative); EUR is the bid currency; round nothing yourself — the
calculator does the only correct rounding.
Top level:
  scenario_date    calendar date every validity window resolves against (scenario day; else "YYYY-MM-DD" from brief)
  tender_reference exact reference string, reported verbatim
  discount         {"percent": approved % as string ("0" if no approval),
                    "threshold_min_net": optional inclusive minimum undiscounted goods value,
                    "default_scope": "base"|"base_plus_mass"|"none" for positions without a Konditionsgruppe
                    (rule 12 default is "base")}
   fx_rates         [{"key","eur_per_unit","dated"}] latest dated <= scenario_date per key; two windowed rates
                    for one key that disagree need a `dated` (rule 21)
  metal_index      optional {"messages":[{"code","rate","valid_from","valid_to"}]} — checked+reported, NEVER money;
                   for a code pick the record whose INCLUSIVE window contains scenario_date, not the last listed;
                   two windowed records over it that disagree need a `dated` (rule 21)
  project_bonus    optional/null {"bands":[{"min_net","max_net"|null,"percent"}],"allocation":
                   "largest_remainder_cents_asc"}; bands marginal, min inclusive, max EXCLUSIVE (null = open),
                   ascending, non-overlapping, at most one unbounded (last); eligible = project positions only
  setup_pools      [{"pool","fee","members":{position_id: allocation mass},"allocation":same as above}]
  positions        >=1 entries, one per tender position:
    position_id            short id ("P-1")
    tender_quantity/tender_unit  as published; unit "Satz" or "Stück"
    pieces_per_set         required whenever any unit of this position is "Satz" (pieces per set, from spec)
    quantity_events        Q&A: {"answer": n, "dated": "YYYY-MM-DD"|null, "withdraws": m|null, "quantity": q|null,
                           "unit": u|null}. Dated events after scenario_date never apply (rule 21). withdraws=m
                           deactivates answer m; withdrawn never contribute (rule 13). Of the surviving changes the
                           LATEST DATED one wins (rule 1); answer-number order only when every survivor is undated;
                           an undated one would lose to a dated survivor — date it. No survivor => tender quantity;
                           null unit = the position's tender unit.
    requirement            attributes to match against material.attrs EXACTLY ({} if none): numbers equal,
                           strings case-insensitive trim-equal, lists order-insensitive; amendments already applied
    material               the ERP material whose attrs match, or null => whole bid is no_bid:
      material_number, attrs, stock (pieces), lead_time_stock_weeks, lead_time_production_weeks,
      price_conditions  [{"valid_from","valid_to"|null,"currency","fx_key"|null,"price_unit_pieces"}]
                        — exactly one window (inclusive) must contain scenario_date; EUR needs no fx_key
      quantity_scales   [{"min_pieces","price"}] in the condition currency; highest min_pieces <= final pieces
    konditionsgruppe   key or null (report only)
    discount_scope     null (use default_scope) | "base" | "base_plus_mass" | "none" (setup/packaging/bonus never)
    mass_surcharge     null | {"kg_per_piece","eur_per_kg","index_code"?,"max_stale_rate"?} (per piece, EUR before
                       discount). With metal_index_code set the dated index rate is the authority: eur_per_kg must
                       equal it or be marked stale (index_code = that code, max_stale_rate >= tariff) so it is
                       replaced; a silent mismatch never prices.
    ordinary_setup_fee EUR for this position, "0.00" when known-zero
    packaging          null | {"eur_per_carton","pieces_per_carton"} — ceil(pieces/carton) cartons, never discounted
    delivery_commitment null | {"material_number","ceiling_pieces","lead_time_weeks"} — applies only if material
                       matches AND pooled pieces for that material <= ceiling (inclusive)
    project_bonus_eligible  true/false
    metal_index_code   optional code or null
EXAMPLE (invented values):
{
  "scenario_date": "2099-01-15",
  "tender_reference": "T-EXAMPLE-1",
  "discount": {"percent": "7.5", "threshold_min_net": "1000.00", "default_scope": "base"},
  "fx_rates": [{"key": "FX-USD-99", "eur_per_unit": "0.92", "dated": "2099-01-10"}],
  "metal_index": {"messages": [{"code": "STL-X", "rate": "2.50", "valid_from": "2099-01-01",
                                 "valid_to": "2099-01-31"}]},
  "project_bonus": {"bands": [{"min_net": "0.00", "max_net": "2000.00", "percent": "1"},
                               {"min_net": "2000.00", "max_net": null, "percent": "2"}],
                     "allocation": "largest_remainder_cents_asc"},
  "setup_pools": [{"pool": "R-X", "fee": "210.00", "members": {"P-1": "12.5", "P-2": "4.0"},
                    "allocation": "largest_remainder_cents_asc"}],
  "positions": [
    {"position_id": "P-1", "tender_quantity": 8, "tender_unit": "Satz", "pieces_per_set": 2,
     "quantity_events": [{"answer": 7, "dated": "2099-01-05", "quantity": 6, "unit": null},
                          {"answer": 12, "dated": "2099-01-10", "withdraws": 7, "quantity": 10, "unit": "Stück"}],
     "requirement": {"bore_mm": 80, "stroke_mm": 300, "certification": "ED"},
     "material": {"material_number": "M-EXAMPLE-9",
       "attrs": {"bore_mm": 80, "stroke_mm": 300, "certification": "ED"},
       "stock": 24, "lead_time_stock_weeks": 1, "lead_time_production_weeks": 6,
       "price_conditions": [{"valid_from": "2098-01-01", "valid_to": null, "currency": "EUR",
                              "fx_key": null, "price_unit_pieces": 1}],
       "quantity_scales": [{"min_pieces": 1, "price": "120.00"}, {"min_pieces": 10, "price": "9.99"}]},
     "konditionsgruppe": null, "discount_scope": null,
     "mass_surcharge": {"kg_per_piece": "12.4", "eur_per_kg": "2.50"},
     "ordinary_setup_fee": "35.00",
     "packaging": {"eur_per_carton": "18.00", "pieces_per_carton": 6},
     "delivery_commitment": {"material_number": "M-EXAMPLE-9", "ceiling_pieces": 30, "lead_time_weeks": 3},
     "project_bonus_eligible": true, "metal_index_code": "STL-X"},
    {"position_id": "P-2", "tender_quantity": 4, "tender_unit": "Stück", "pieces_per_set": null,
     "quantity_events": [], "requirement": {},
     "material": {"material_number": "M-EXAMPLE-9",
       "attrs": {"bore_mm": 80, "stroke_mm": 300, "certification": "ED"},
       "stock": 24, "lead_time_stock_weeks": 1, "lead_time_production_weeks": 6,
       "price_conditions": [{"valid_from": "2099-01-01", "valid_to": "2099-01-31", "currency": "USD",
                              "fx_key": "FX-USD-99", "price_unit_pieces": 10}],
       "quantity_scales": [{"min_pieces": 1, "price": "108.50"}]},
     "konditionsgruppe": "K2", "discount_scope": "base_plus_mass", "mass_surcharge": null,
     "ordinary_setup_fee": "0.00", "packaging": null, "delivery_commitment": null,
      "project_bonus_eligible": false, "metal_index_code": null}]
 }"""
