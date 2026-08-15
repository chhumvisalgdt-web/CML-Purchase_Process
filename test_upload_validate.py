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
