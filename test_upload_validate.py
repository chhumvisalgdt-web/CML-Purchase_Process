import upload_validate as uv

REAGENT = [
    {"material_code": "11533", "item": "ALANINE AMINOTRANSFERASE (ALT/GPT) (1x160+1x40mL)",
     "supplier": "BOM CO.,LTD", "supplier_reagent": "ALT/GPT 1x160+1x40mL",
     "pack": "1x160+1x40mL", "unit_price": 58.30},
    {"material_code": "11573", "item": "ALBUMIN (Std Inc) (1x250mL)",
     "supplier": "BOM CO.,LTD", "pack": "1x250mL", "unit_price": 20.33},
    {"material_code": "11583", "item": "alpha-AMYLASE - DIRECT 5x5mL",
     "supplier": "BOM CO.,LTD", "pack": "5x5mL", "unit_price": 38.50},
    {"material_code": "BT-0001", "item": "ALBUMIN (Std Inc) (1x250mL)",
     "supplier": "BIO-TECHEM(CAM)", "pack": "1x250mL", "unit_price": 16.90},
    {"material_code": "ZD 3001-0104-2", "item": "Anti-CCD Absorbent",
     "supplier": "BIO-TECHEM(CAM)", "pack": "40Test/Box", "unit_price": 226.01},
    {"material_code": "zd 3001-0104-2", "item": "EUROLINE ANA Profile 1 (IgG)",
     "supplier": "BIO-TECHEM(CAM)", "pack": "16Test/Box", "unit_price": 468.70},
    {"material_code": "11999", "item": "TBC REAGENT", "supplier": "BOM CO.,LTD",
     "pack": "1L", "unit_price": ""},
]
OTHER = [
    {"material_code": "OF-0001", "item": "A4 paper", "supplier": "BOM CO.,LTD",
     "pack": "box", "unit_price": 4.20},
]

IDX = uv.build_index(REAGENT, OTHER)


def rows(*triples):
    return [{"row_no": 8 + i, "code": c, "qty": q, "note": n}
            for i, (c, q, n) in enumerate(triples)]


def statuses(res):
    return [r["status"] for r in res.report]


def test_index_detects_duplicate_regardless_of_case_and_spacing():
    assert "ZD 3001-0104-2" in IDX.dup_codes
    assert "ZD 3001-0104-2" not in IDX.by_code
    assert IDX.suppliers(uv.CAT_REAGENT) == ["BIO-TECHEM(CAM)", "BOM CO.,LTD"]


def test_duplicated_codes_excluded_from_templates():
    codes = [r["material_code"] for r in
             IDX.rows_for(uv.CAT_REAGENT, "BIO-TECHEM(CAM)")]
    assert codes == ["BT-0001"]


def test_numeric_code_from_excel_matches_text_code_from_sheets():
    res = uv.validate(rows((11533, 4, "")), IDX)
    assert not res.blocked
    assert res.items[0]["material_code"] == "11533"
    assert res.items[0]["line_total"] == 233.20
    res_f = uv.validate(rows((11533.0, "4", "")), IDX)
    assert not res_f.blocked


def test_happy_path_totals_and_derivation():
    res = uv.validate(rows(("11533", 4, ""), ("11583", 2, "urgent")), IDX)
    assert not res.blocked
    assert res.summary["supplier"] == "BOM CO.,LTD"
    assert res.summary["category"] == uv.CAT_REAGENT
    assert res.summary["total_units"] == 6
    assert res.items[1]["variant_reason"] == "urgent"


def test_blank_rows_are_skipped_not_flagged():
    res = uv.validate(rows((None, None, None), ("11533", 1, ""),
                           ("", "", "")), IDX)
    assert statuses(res) == [uv.STATUS_OK]
    assert res.summary["rows_read"] == 3
    assert res.summary["rows_populated"] == 1


def test_not_found_and_missing_code():
    res = uv.validate(rows(("99999", 1, ""), ("", 3, "")), IDX)
    assert statuses(res) == [uv.STATUS_NOT_FOUND, uv.STATUS_MISSING_CODE]
    assert res.blocked


def test_duplicate_code_blocks_and_never_resolves():
    res = uv.validate(rows(("ZD 3001-0104-2", 1, "")), IDX)
    assert statuses(res) == [uv.STATUS_DUPLICATE_CODE]
    assert res.items == []


def test_no_price_blocks():
    res = uv.validate(rows(("11999", 1, "")), IDX)
    assert statuses(res) == [uv.STATUS_NO_PRICE]


def test_bad_qty_variants():
    for bad in (0, -2, 4.5, "abc", None, ""):
        res = uv.validate(rows(("11533", bad, "note")), IDX)
        assert statuses(res) == [uv.STATUS_BAD_QTY], bad


def test_duplicate_line():
    res = uv.validate(rows(("11533", 2, ""), ("11533", 3, "")), IDX)
    assert statuses(res) == [uv.STATUS_OK, uv.STATUS_DUPLICATE_LINE]


def test_supplier_conflict_uses_first_line_as_anchor():
    res = uv.validate(rows(("11533", 1, ""), ("BT-0001", 1, "")), IDX)
    assert statuses(res) == [uv.STATUS_OK, uv.STATUS_SUPPLIER_CONFLICT]
    assert res.summary["supplier"] == "BOM CO.,LTD"


def test_mixed_category_blocks():
    res = uv.validate(rows(("11533", 1, ""), ("OF-0001", 5, "")), IDX)
    assert statuses(res) == [uv.STATUS_OK, uv.STATUS_MIXED_CATEGORY]


def test_over_limit_blocks_only_the_thirteenth_line():
    wide = [{"material_code": f"W-{i:03d}", "item": f"Widget {i}",
             "supplier": "BOM CO.,LTD", "pack": "ea", "unit_price": 1.0}
            for i in range(13)]
    idx = uv.build_index(wide, [])
    res = uv.validate(rows(*[(f"W-{i:03d}", 1, "") for i in range(13)]), idx)
    assert statuses(res) == [uv.STATUS_OK] * 12 + [uv.STATUS_OVER_LIMIT]
    assert res.blocked
    assert len(res.items) == 12


def test_cheaper_elsewhere_is_reported_without_prices():
    res = uv.validate(rows(("11573", 1, "")), IDX)
    assert not res.blocked
    assert res.summary["cheaper_elsewhere"] == ["ALBUMIN (Std Inc) (1x250mL)"]
    text = repr(res.summary)
    assert "20.33" not in text and "16.9" not in text


def test_no_cheaper_flag_when_chosen_supplier_is_cheapest():
    res = uv.validate(rows(("BT-0001", 1, "")), IDX)
    assert res.summary["cheaper_elsewhere"] == []


def test_template_provenance_mismatch_is_reported_not_blocking():
    res = uv.validate(rows(("11533", 1, "")), IDX,
                      template_supplier="BIO-TECHEM(CAM)",
                      template_category=uv.CAT_REAGENT)
    assert not res.blocked
    assert res.summary["template_matches"] is False


def test_report_covers_every_populated_row_in_sheet_order():
    res = uv.validate(rows(("11533", 1, ""), ("99999", 1, ""),
                           ("11583", 2, "")), IDX)
    assert [r["row_no"] for r in res.report] == [8, 9, 10]
    assert res.summary["rows_blocked"] == 1
    assert res.summary["blocked_by_status"] == {uv.STATUS_NOT_FOUND: 1}


def test_excluded_count_is_scoped_to_the_template():
    """A duplicated code is unusable everywhere, but the requester is told how
    many items are missing from HER file -- not the master list's total."""
    dup_rows = [
        {"material_code": "DUP-1", "item": "A", "supplier": "Alpha", "unit_price": 5},
        {"material_code": "DUP-1", "item": "B", "supplier": "Alpha", "unit_price": 9},
        {"material_code": "OK-1", "item": "C", "supplier": "Beta", "unit_price": 3},
    ]
    idx = uv.build_index(dup_rows, [])
    assert idx.dup_codes == {"DUP-1"}
    assert idx.excluded_for(uv.CAT_REAGENT, "Alpha") == 1
    assert idx.excluded_for(uv.CAT_REAGENT, "Beta") == 0


# ---- names as identifiers (does the requester's form dare ask for one?) ----

def test_a_name_identifies_one_row_inside_one_suppliers_list():
    row = IDX.item(uv.CAT_REAGENT, "BOM CO.,LTD", "ALBUMIN (Std Inc) (1x250mL)")
    assert row["material_code"] == "11573"
    assert IDX.item(uv.CAT_REAGENT, "BIO-TECHEM(CAM)",
                    "ALBUMIN (Std Inc) (1x250mL)")["material_code"] == "BT-0001"


def test_the_same_name_under_two_suppliers_is_not_a_clash():
    """It is the opposite of a fault: identical names are how an equivalent is
    declared, and both rows stay orderable."""
    assert IDX.clashing_names() == []
    assert not IDX.name_is_ambiguous(uv.CAT_REAGENT, "BOM CO.,LTD",
                                     "ALBUMIN (Std Inc) (1x250mL)")


def test_lookup_by_name_ignores_case_and_spacing():
    assert IDX.item(uv.CAT_REAGENT, "BOM CO.,LTD",
                    "  albumin (std inc)   (1x250mL) ")["material_code"] == "11573"


def test_the_same_name_twice_under_one_supplier_is_a_clash():
    """One product in two pack sizes. The name can no longer say which is
    meant, so it identifies nothing -- the same rule a duplicated code gets."""
    clash = [
        {"material_code": "P-40", "item": "HBs Ag", "supplier": "Borey",
         "pack": "40/Kit", "unit_price": 18.5},
        {"material_code": "P-50", "item": "HBs Ag", "supplier": "Borey",
         "pack": "50/Kit", "unit_price": 18.5},
        {"material_code": "P-99", "item": "Dengue NS1", "supplier": "Borey",
         "pack": "25 Tests", "unit_price": 74.8},
    ]
    idx = uv.build_index(clash, [])
    assert idx.clashing_names() == [("Borey", "HBs Ag")]
    assert idx.name_is_ambiguous(uv.CAT_REAGENT, "Borey", "hbs ag")
    assert idx.item(uv.CAT_REAGENT, "Borey", "HBs Ag") is None
    # Both codes stay perfectly good; it is only the NAME that is unusable.
    assert idx.by_code["P-40"]["pack"] == "40/Kit"
    assert idx.excluded_names_for(uv.CAT_REAGENT, "Borey") == 1
    assert idx.item(uv.CAT_REAGENT, "Borey", "Dengue NS1")["material_code"] == "P-99"


def test_a_third_row_with_the_same_name_does_not_resurrect_it():
    same = [{"material_code": f"P-{i}", "item": "HBs Ag", "supplier": "Borey",
             "unit_price": 1.0} for i in range(3)]
    idx = uv.build_index(same, [])
    assert idx.item(uv.CAT_REAGENT, "Borey", "HBs Ag") is None
    assert len(idx.clashing_names()) == 1


def test_a_duplicated_code_is_not_reachable_by_name_either():
    """ZD 3001-0104-2 is two different products. Both rows are unusable, so
    neither may come back through the name index."""
    assert IDX.item(uv.CAT_REAGENT, "BIO-TECHEM(CAM)", "Anti-CCD Absorbent") is None
    assert IDX.item(uv.CAT_REAGENT, "BIO-TECHEM(CAM)",
                    "EUROLINE ANA Profile 1 (IgG)") is None


def test_has_scope_knows_a_real_list_from_an_invented_one():
    assert IDX.has_scope(uv.CAT_REAGENT, "BOM CO.,LTD")
    assert IDX.has_scope(uv.CAT_OTHER, "BOM CO.,LTD")
    assert not IDX.has_scope(uv.CAT_REAGENT, "NOT A SUPPLIER")
    assert not IDX.has_scope(uv.CAT_OTHER, "BIO-TECHEM(CAM)")


# ---- cross-supplier alternatives ----
ALT_MASTER = [
    {"material_code": "CMLRE00016", "item": "HBs Ag (25 Tests)",
     "supplier": "B Scientific", "unit_price": 15.40, "pack": "25 Tests",
     "tests_per_pack": 25},
    {"material_code": "CMLRE00300", "item": "HBs Ag (25 Tests)",
     "supplier": "Borey Pharma", "unit_price": 18.50, "pack": "50/Kit",
     "tests_per_pack": 50},
    {"material_code": "CMLRE00301", "item": "HBs Ag (25 Tests)",
     "supplier": "Borey Pharma", "unit_price": 18.50, "pack": "40/Kit",
     "tests_per_pack": 40},
    {"material_code": "CMLRE00011", "item": "CHIKUNGUNYA IgM/IgG (25 Tests)",
     "supplier": "B Scientific", "unit_price": 83.60, "pack": "25 Tests",
     "tests_per_pack": 25},
    {"material_code": "CMLRE00099", "item": "MYSTERY KIT",
     "supplier": "B Scientific", "unit_price": 10.0, "pack": "Box",
     "tests_per_pack": ""},
    {"material_code": "CMLRE00098", "item": "MYSTERY KIT",
     "supplier": "Borey Pharma", "unit_price": 9.0, "pack": "Box",
     "tests_per_pack": ""},
]
ALT_IDX = uv.build_index(ALT_MASTER, [])


def _alts(codes_qty):
    items = [{"material_code": c, "qty": q, "unit_price": None} for c, q in codes_qty]
    for it in items:
        it["unit_price"] = ALT_IDX.by_code[it["material_code"]]["unit_price"]
    return uv.alternatives_for(items, ALT_IDX)


def test_line_without_an_equivalent_is_absent():
    """The table is a signal, not furniture -- a line with no alternative does
    not appear, and a PO with none at all produces no table."""
    assert _alts([("CMLRE00011", 1)]) == []


def test_one_row_per_supplier_not_per_pack():
    """Borey sells the same test as a 40 and a 50 kit. Listing both would push
    the reader to compare Borey with Borey."""
    rows = _alts([("CMLRE00016", 4)])
    assert len(rows) == 1
    assert rows[0]["alt_supplier"] == "Borey Pharma"
    assert rows[0]["alt_pack"] == "50/Kit"          # their better per-test offer


def test_diff_is_per_test_not_per_pack():
    """$15.40/25 = $0.616 against $18.50/50 = $0.370. On headline price the
    alternative looks 20% DEARER; per test it is 40% cheaper."""
    r = _alts([("CMLRE00016", 4)])[0]
    assert r["price"] == 15.40 and r["alt_price"] == 18.50
    assert round(r["diff_pct"]) == -40
    assert round(r["diff_amount"], 2) == -24.60     # 4 packs x 25 tests


def test_missing_pack_size_gives_no_percentage():
    """Rather than compare raw prices and present it as normalised."""
    r = _alts([("CMLRE00099", 1)])[0]
    assert r["diff_pct"] is None and r["diff_amount"] is None
    assert r["alt_price"] == 9.0                    # both prices still shown


def test_dearer_alternative_is_shown_as_positive():
    """So the table also confirms a choice that was already right."""
    r = _alts([("CMLRE00300", 1)])[0]
    assert r["diff_pct"] > 0


def test_cheaper_alternative_names_supplier_and_percentage():
    """What the requester's template shows: who else sells it, and by how much
    they differ per test. Relative only - no price reaches her file."""
    sup, pct = uv.cheaper_alternative("CMLRE00016", ALT_IDX)
    assert sup == "Borey Pharma" and round(pct) == -40


def test_no_equivalent_gives_no_supplier():
    assert uv.cheaper_alternative("CMLRE00011", ALT_IDX) == (None, None)


def test_alternative_named_even_when_pack_size_unknown():
    """'Someone else sells this' is worth knowing even when the gap cannot be
    calculated. The supplier is named; the percentage stays blank."""
    sup, pct = uv.cheaper_alternative("CMLRE00099", ALT_IDX)
    assert sup == "Borey Pharma" and pct is None


def test_zero_is_a_real_answer_not_a_blank():
    """Excel hands back the integer 0, and `str(v or "")` collapsed it to "".
    Zero on hand is the strongest argument FOR an order, so losing it is the
    worst possible value to lose."""
    import side_excel as sx
    lines = [{"line_id": "1-1", "material_code": "C1", "item": "Reagent", "qty": 1}]
    counts, errs = sx.validate_stock([{"row_no": 7, "code": "C1", "on_hand": 0}], lines)
    assert not errs and counts[0]["on_hand"] == 0
    # a genuinely empty cell must still be rejected
    _, errs2 = sx.validate_stock([{"row_no": 7, "code": "C1", "on_hand": None}], lines)
    assert errs2


def test_lone_zero_quantity_is_reported_not_skipped():
    idx = uv.build_index(
        [{"material_code": "C1", "item": "X", "supplier": "S", "unit_price": 1.0}], [])
    res = uv.validate([{"row_no": 8, "code": None, "qty": 0, "note": None}], idx)
    assert res.summary["rows_populated"] == 1


def test_generated_files_route_themselves():
    """Each file carries its own kind and PO number, so two POs arriving in a
    row cannot leave the first one's file unroutable."""
    import side_excel as sx, post_handlers as ph
    lines = [{"line_id": "190-1", "material_code": "C1", "item": "X",
              "pack": "100 T", "qty": 1}]
    assert ph._peek(sx.build_stock_file(190, lines)["bytes"]) == ("stock", "190")
    assert ph._peek(sx.build_price_file(191, lines)["bytes"]) == ("price", "191")
    # a file with no _meta at all falls back to the group's pending request
    assert ph._peek(b"not a workbook") == ("", "")


def test_stock_stage_has_no_buttons_at_all():
    """The returned file is the check, and this stage cannot reject, so there
    is nothing left to tap."""
    import flow
    assert flow.action_keyboard("stock", 1) is None


def test_only_finance_and_above_can_reject():
    import flow
    assert not flow.can_reject("stock")
    assert not flow.can_reject("book")
    assert all(flow.can_reject(s) for s in ("fin", "gm", "board"))


def test_no_reject_button_below_finance():
    """A button that is not there cannot be pressed -- but old cards still
    carry one, which is why bot._cb_action re-checks."""
    import flow
    book = [b.text for row in flow.action_keyboard("book", 1).inline_keyboard
            for b in row]
    assert "\u274c Reject" not in book and "\u2705 Booked" in book
    for st in ("fin", "gm", "board"):
        labels = [b.text for row in flow.action_keyboard(st, 1).inline_keyboard
                  for b in row]
        assert "\u274c Reject" in labels


# ---- rejection reasons ----
import os as _o
_o.environ.setdefault("BOT_TOKEN", "1:x"); _o.environ.setdefault("SPREADSHEET_ID", "x")
_o.environ.setdefault("GOOGLE_CREDENTIALS_JSON", "{}")
import bot as _bot  # noqa: E402

NOW = 1_000_000.0
PEND_A = {101: {"po_no": "190", "user_id": 7, "stage": "fin", "card_id": 1,
                "at": NOW}}
PEND_TWO = {101: {"po_no": "190", "user_id": 7, "stage": "fin", "card_id": 1,
                  "at": NOW},
            102: {"po_no": "191", "user_id": 7, "stage": "fin", "card_id": 2,
                  "at": NOW}}
STALE = {101: {"po_no": "190", "user_id": 7, "stage": "fin", "card_id": 1,
               "at": NOW - 7 * 24 * 3600}}


def test_one_outstanding_rejection_needs_no_reply():
    k, e, err = _bot.pick_rejection(PEND_A, 7, None, now=NOW)
    assert e["po_no"] == "190" and not err


def test_reply_selects_the_right_po():
    _, e, err = _bot.pick_rejection(PEND_TWO, 7, 102, now=NOW)
    assert e["po_no"] == "191" and not err


def test_two_outstanding_without_a_reply_refuses_to_guess():
    """Filing the reason against the wrong PO is worse than asking again."""
    k, e, err = _bot.pick_rejection(PEND_TWO, 7, None, now=NOW)
    assert e is None and "#190" in err and "#191" in err


def test_someone_elses_message_is_ignored():
    assert _bot.pick_rejection(PEND_A, 99, None, now=NOW) == (None, None, "")


def test_a_second_rejection_does_not_evict_the_first():
    """One slot per group meant the second Reject discarded the first, parking
    that PO at its stage with no record anyone had tried."""
    assert len(PEND_TWO) == 2
    for uid_entry in PEND_TWO.values():
        assert uid_entry["po_no"] in ("190", "191")


def test_a_stale_prompt_needs_a_real_reply():
    """A week-old Reject that was never answered must not swallow an unrelated
    message as its reason."""
    _, e, err = _bot.pick_rejection(STALE, 7, None, now=NOW)
    assert e is None and "been open a while" in err


def test_replying_to_a_stale_prompt_still_works():
    """The boss is entitled to take his time; he just has to point at it."""
    _, e, err = _bot.pick_rejection(STALE, 7, 101, now=NOW)
    assert e["po_no"] == "190" and not err


# ---- who is told when a PO is rejected ----
import flow as _f  # noqa: E402

# At the Board, so on the non-urgent path everything below it has signed off.
REJECTED_PO = {
    "po_no": "187", "supplier": "BIO-TECHEM(CAM)", "requester_name": "Sophea",
    "stock_by": "Dara", "stock_at": "19-Aug-2026 09:12",
    "book_by": "Lina", "book_at": "20-Aug-2026 14:40",
    "fin_by": "Mr. Sok", "fin_at": "21-Aug-2026 10:02",
    "gm_by": "Mr. Ny", "gm_at": "21-Aug-2026 11:30",
}
SECRET = "too expensive, 40% cheaper at Borey Pharma"


def _by_chat(po, stage="board", reason=SECRET):
    return {n["chat"]: n for n in
            _f.rejection_notices(po, "187", stage, "Mr. Chan", reason)}


def test_ordering_group_is_always_told_and_gets_the_reason():
    n = _by_chat(REJECTED_PO)["approved"]
    assert SECRET in n["text"] and n["with_reason"]
    assert "do not order it" in n["text"].lower()


def test_stock_and_bookkeeping_are_told_but_never_the_reason():
    """The stock controller is price-blind. A rejection reason is very often a
    price, so it must not reach him through the notification either."""
    got = _by_chat(REJECTED_PO)
    for key in ("stock", "book"):
        assert key in got, f"{key} signed off and must be told"
        assert SECRET not in got[key]["text"]
        assert "Reason" not in got[key]["text"]
        assert not got[key]["with_reason"]


def test_bookkeeping_is_told_to_void_the_qbo_order():
    assert "QBO" in _by_chat(REJECTED_PO)["book"]["text"]


def test_finance_and_gm_are_told_and_do_get_the_reason():
    """They approve on price and see every figure already, so withholding it
    from them would only make the chase harder."""
    got = _by_chat(REJECTED_PO)
    for key in ("fin", "gm"):
        assert key in got and got[key]["with_reason"]
        assert SECRET in got[key]["text"]


def test_stages_above_the_rejecter_are_never_told():
    """Finance rejects: the GM has not seen this PO and has nothing to undo."""
    got = _by_chat(REJECTED_PO, stage="fin")
    assert set(got) == {"approved", "stock", "book"}


def test_the_rejecting_stage_is_not_told_about_its_own_rejection():
    assert "gm" not in _by_chat(REJECTED_PO, stage="gm")


def test_a_stage_that_never_acted_is_not_told():
    """Reject at Finance and bookkeeping may not have booked it yet -- there is
    nothing to undo, so the message would be noise."""
    po = dict(REJECTED_PO, book_by="", book_at="")
    got = _by_chat(po, stage="fin")
    assert "stock" in got and "book" not in got


def test_nobody_below_is_told_when_nobody_below_acted():
    po = dict(REJECTED_PO, stock_by="", stock_at="", book_by="", book_at="")
    assert set(_by_chat(po, stage="fin")) == {"approved"}


def test_a_blank_reason_says_so_rather_than_leaving_a_gap():
    """The reason is optional now, so the notice has to be honest about it."""
    for blank in ("", None, "   "):
        n = _by_chat(REJECTED_PO, reason=blank)["approved"]
        assert "No reason was given." in n["text"]
        assert "Reason:" not in n["text"]


def test_every_notice_names_the_po_and_the_rejecting_stage():
    for n in _f.rejection_notices(REJECTED_PO, "187", "board", "Mr. Chan", ""):
        assert "#187" in n["text"]
        assert "Board of director" in n["text"]
