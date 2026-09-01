"""Supporting-document rules: who may see a file, and what may be attached.

The routing tests are the ones that matter. Everything else here is input
validation; `delivery_stages` is the wall between a supplier's quotation and
the two groups that must never hold one.
"""
import attachments as att
from config import Config


# ---------- who receives an attachment ----------
def test_the_four_review_stages_receive_in_flow_order():
    assert att.delivery_stages("Other", ["gm", "book", "board", "fin"]) == [
        "book", "fin", "gm", "board"]


def test_the_stock_controller_never_receives_an_attachment():
    """He is price-blind and a quotation is a price. Naming him explicitly in
    the configuration must not be enough to reach him."""
    assert "stock" not in att.delivery_stages("Other", ["stock", "fin"])


def test_the_approved_po_group_never_receives_an_attachment():
    """Its job is to forward documents to the supplier. A competitor's
    quotation sitting in that thread is one tap from the supplier."""
    assert "approved" not in att.delivery_stages("Other", ["approved", "fin"])


def test_the_cash_advance_group_never_receives_an_attachment():
    assert "cash" not in att.delivery_stages("Other", ["cash", "gm"])


def test_a_configuration_of_nothing_but_banned_stages_delivers_nothing():
    """A misconfiguration degrades to silence, never to a leak."""
    assert att.delivery_stages("Other", ["stock", "approved", "cash"]) == []


def test_config_filters_the_ban_before_the_list_is_ever_used():
    Config.ATTACH_STAGES_RAW = ["book", "stock", "approved", "fin"]
    try:
        assert Config.attach_stages() == ["book", "fin"]
    finally:
        Config.ATTACH_STAGES_RAW = ["book", "fin", "gm", "board"]


# ---------- what may be attached ----------
def test_a_pdf_under_the_limits_is_accepted():
    ok, why = att.check("quotation.pdf", 500_000, 0, 5, 10 * 1024 * 1024)
    assert ok and why == ""


def test_an_archive_is_refused():
    ok, why = att.check("everything.zip", 1000, 0, 5, 10 * 1024 * 1024)
    assert not ok and ".zip" in why


def test_a_file_with_no_extension_is_refused():
    ok, why = att.check("scan", 1000, 0, 5, 10 * 1024 * 1024)
    assert not ok and "what kind of file" in why


def test_an_oversized_file_is_refused_and_the_message_says_the_size():
    ok, why = att.check("big.pdf", 12 * 1024 * 1024, 0, 5, 10 * 1024 * 1024)
    assert not ok and "12.0 MB" in why and "10 MB" in why


def test_the_count_limit_blocks_the_sixth_file():
    ok, why = att.check("sixth.pdf", 1000, 5, 5, 10 * 1024 * 1024)
    assert not ok and "5 files" in why


def test_the_extension_check_ignores_case():
    ok, _ = att.check("QUOTE.PDF", 1000, 0, 5, 10 * 1024 * 1024)
    assert ok


# ---------- naming and fingerprinting ----------
def test_the_archive_name_leads_with_the_po_and_keeps_the_original():
    assert att.archive_name(41, 2, "Borey quote.pdf") == "PO_41_2_Borey quote.pdf"


def test_path_separators_cannot_survive_into_the_archive_name():
    name = att.archive_name(7, 1, "../../etc/passwd.pdf")
    assert "/" not in name and ".." not in name


def test_a_nameless_file_still_gets_a_name():
    assert att.archive_name(7, 1, "").startswith("PO_7_1_")


def test_the_digest_is_stable_and_changes_with_the_content():
    assert att.digest(b"abc") == att.digest(b"abc")
    assert att.digest(b"abc") != att.digest(b"abd")


def test_the_summary_line_states_an_absence_rather_than_staying_silent():
    assert att.summary_line(0) == "Supporting documents: none attached"
    assert att.summary_line(3) == "Supporting documents: 3 attached"


def test_every_caption_warns_against_forwarding():
    cap = att.caption(12, 1, 2, "quote.pdf", "Sokly")
    assert "Do not forward" in cap and "PO #12" in cap and "1/2" in cap
