from datetime import date

import receipt_validate as rv

TODAY = date(2026, 8, 1)

LINES = [
    {"line_id": "173-1", "material_code": "CMLRE00007", "item": "ALT/GPT 1x160+1x40mL",
     "pack": "1x160+1x40mL", "qty": 10},
    {"line_id": "173-2", "material_code": "CMLRE00010", "item": "ALBUMIN 1x250mL",
     "pack": "1x250mL", "qty": 4},
    {"line_id": "173-3", "material_code": "CMLRE00019", "item": "CONTROL SERUM I 5x5mL",
     "pack": "5x5mL", "qty": 2},
]


def outstanding(prior=None):
    return rv.outstanding_from(LINES, prior or [])


def rows(*tuples):
    out = []
    for i, t in enumerate(tuples):
        code, qty, lot, exp = (list(t) + [None] * 4)[:4]
        out.append({"row_no": 9 + i, "code": code, "qty": qty, "lot": lot,
                    "expiry": exp, "invoice_qty": None, "note": None})
    return out


def run(rs, prior=None, **kw):
    kw.setdefault("invoice_no", "INV-4471")
    kw.setdefault("today", TODAY)
    return rv.validate(rs, outstanding(prior), **kw)


def statuses(res):
    return [r["status"] for r in res.report]


def test_outstanding_omits_fully_received_lines():
    prior = [{"line_id": "173-2", "qty_received": 4}]
    o = outstanding(prior)
    assert set(o) == {"CMLRE00007", "CMLRE00019"}


def test_outstanding_carries_partial_receipts():
    prior = [{"line_id": "173-1", "qty_received": 6}]
    assert outstanding(prior)["CMLRE00007"]["received"] == 6


def test_happy_path():
    res = run(rows(("CMLRE00007", 10, "L1", "15-Mar-2027"),
                   ("CMLRE00010", 4, "L2", "2027-01-31")))
    assert not res.blocked and not res.needs_confirm
    assert res.summary["total_units"] == 14
    assert res.receipts[0]["expiry"] == "2027-03-15"


def test_same_code_twice_is_legitimate_two_lots():
    res = run(rows(("CMLRE00007", 6, "LOT-A", "15-Mar-2027"),
                   ("CMLRE00007", 4, "LOT-B", "15-Jun-2027")))
    assert not res.blocked
    assert len(res.receipts) == 2
    line = next(l for l in res.summary["lines_after"] if l["code"] == "CMLRE00007")
    assert line["received_now"] == 10 and line["state"] == "complete"


def test_slash_date_is_refused_not_guessed():
    res = run(rows(("CMLRE00007", 1, "L1", "03/15/2027")))
    assert statuses(res) == [rv.ST_AMBIGUOUS_EXPIRY]
    assert res.blocked


def test_real_date_object_is_trusted():
    r = rows(("CMLRE00007", 1, "L1", None))
    r[0]["expiry"] = date(2027, 3, 15)
    assert not run(r).blocked


def test_unreadable_expiry_blocks():
    assert statuses(run(rows(("CMLRE00007", 1, "L1", "next year")))) == [rv.ST_BAD_EXPIRY]


def test_missing_lot_and_expiry_block_but_na_passes():
    assert statuses(run(rows(("CMLRE00007", 1, "", "15-Mar-2027")))) == [rv.ST_MISSING_LOT]
    assert statuses(run(rows(("CMLRE00007", 1, "L1", "")))) == [rv.ST_MISSING_EXPIRY]
    res = run(rows(("CMLRE00007", 1, "n/a", "n/a")))
    assert not res.blocked and res.receipts[0]["expiry"] == "n/a"


def test_not_outstanding_and_unknown_code():
    prior = [{"line_id": "173-2", "qty_received": 4}]
    res = run(rows(("CMLRE00010", 1, "L1", "15-Mar-2027"),
                   ("", 1, "L1", "15-Mar-2027")), prior=prior)
    assert statuses(res) == [rv.ST_NOT_OUTSTANDING, rv.ST_UNKNOWN_CODE]


def test_bad_quantities():
    for bad in (2.5, "abc"):
        assert statuses(run(rows(("CMLRE00007", bad, "L1", "15-Mar-2027")))) == [rv.ST_BAD_QTY]


def test_zero_without_a_note_blocks():
    assert statuses(run(rows(("CMLRE00007", 0, "L1", "15-Mar-2027")))) == [rv.ST_NOTHING_ENTERED]


def test_negative_correction_allowed_but_not_below_zero():
    prior = [{"line_id": "173-1", "qty_received": 6}]
    r = rows(("CMLRE00007", -2, "L1", "15-Mar-2027"))
    r[0]["note"] = "miscount on 30-Jul"
    ok = run(r, prior=prior)
    assert not ok.blocked and ok.receipts[0]["qty_received"] == -2
    r2 = rows(("CMLRE00007", -9, "L1", "15-Mar-2027"))
    r2[0]["note"] = "over-correction"
    assert statuses(run(r2, prior=prior)) == [rv.ST_NEGATIVE_TOTAL]


def test_over_receipt_confirms_rather_than_blocks():
    res = run(rows(("CMLRE00007", 12, "L1", "15-Mar-2027")))
    assert not res.blocked and res.needs_confirm
    assert statuses(res) == [rv.ST_OVER_RECEIPT]
    assert len(res.receipts) == 1


def test_expired_and_short_dated_confirm():
    res = run(rows(("CMLRE00007", 1, "L1", "01-Jan-2026")))
    assert statuses(res) == [rv.ST_EXPIRED] and not res.blocked
    res2 = run(rows(("CMLRE00010", 1, "L1", "15-Sep-2026")))
    assert statuses(res2) == [rv.ST_SHORT_DATED]


def test_expired_is_still_reported_when_line_is_also_over():
    res = run(rows(("CMLRE00007", 12, "L1", "01-Jan-2026")))
    assert res.report[0]["status"] == rv.ST_OVER_RECEIPT
    assert "expired" in res.report[0]["message"]


def test_missing_invoice_number_blocks():
    res = run(rows(("CMLRE00007", 1, "L1", "15-Mar-2027")), invoice_no="")
    assert res.blocked
    assert res.report[-1]["row_no"] is None


def test_all_complete_only_when_every_line_is_done():
    partial = run(rows(("CMLRE00007", 10, "L1", "15-Mar-2027")))
    assert partial.summary["all_complete"] is False
    full = run(rows(("CMLRE00007", 10, "L1", "15-Mar-2027"),
                    ("CMLRE00010", 4, "L2", "15-Mar-2027"),
                    ("CMLRE00019", 2, "L3", "15-Mar-2027")))
    assert full.summary["all_complete"] is True


def test_blank_rows_skipped():
    r = rows(("CMLRE00007", 10, "L1", "15-Mar-2027")) + rows((None, None, None, None))
    res = run(r)
    assert res.summary["rows_populated"] == 1 and len(res.report) == 1


def test_prefilled_code_alone_does_not_make_a_row_populated():
    """The generator pre-fills the CML code on every lot slot. A row the
    receiver never touched must be skipped, not blocked as a bad quantity."""
    r = rows(("CMLRE00007", 10, "L1", "15-Mar-2027"),
             ("CMLRE00007", None, None, None),
             ("CMLRE00010", None, None, None))
    res = run(r)
    assert statuses(res) == [rv.ST_OK]
    assert res.summary["rows_populated"] == 1


def test_row_with_only_a_lot_typed_is_still_checked():
    r = rows(("CMLRE00007", None, "LOT-X", None))
    assert statuses(run(r)) == [rv.ST_BAD_QTY]


# ---- working hours (order_due) ----
import os as _os
_os.environ.setdefault("BOT_TOKEN", "1:x")
_os.environ.setdefault("SPREADSHEET_ID", "x")
_os.environ.setdefault("GOOGLE_CREDENTIALS_JSON", "{}")
from datetime import datetime as _dt  # noqa: E402
import flow as _flow  # noqa: E402


def _due(y, m, d, hh, mm=0):
    when, now = _flow.next_working_moment(_dt(y, m, d, hh, mm))
    return when.strftime("%a %d-%b %H:%M"), now


def test_inside_working_hours_is_now():
    assert _due(2026, 8, 7, 10) == ("Fri 07-Aug 10:00", True)
    assert _due(2026, 8, 8, 16, 30) == ("Sat 08-Aug 16:30", True)


def test_after_close_rolls_to_next_working_morning():
    assert _due(2026, 8, 7, 19) == ("Sat 08-Aug 08:00", False)


def test_saturday_evening_and_sunday_roll_to_monday():
    assert _due(2026, 8, 8, 18) == ("Mon 10-Aug 08:00", False)
    assert _due(2026, 8, 9, 9) == ("Mon 10-Aug 08:00", False)


def test_before_opening_waits_for_the_same_day():
    assert _due(2026, 8, 10, 6) == ("Mon 10-Aug 08:00", False)


def test_five_pm_is_outside_working_hours():
    assert _due(2026, 8, 7, 17) == ("Sat 08-Aug 08:00", False)
