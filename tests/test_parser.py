"""The deterministic results parser and quote grounding (moved from the Step 1-8 tests)."""
import pytest

from finn import grounding
from finn.enrich import parser


@pytest.mark.parametrize("text,unit", [
    ("CONSOLIDATED FINANCIAL RESULTS\n(₹ million)\nQuarter ended", "million"),
    ("Statement\n(f in Crores)\nParticulars", "crore"),
    ("{t in crore, except per share data and ratios)", "crore"),
    ("Rs. in Lakhs", "lakh"),
])
def test_detect_unit(text, unit):
    assert parser.detect_unit(text) == unit


def test_parse_number_brackets_and_commas():
    assert parser.parse_number("1,81,086.33") == 181086.33
    assert parser.parse_number("(5,220.57)") == -5220.57
    assert parser.parse_number("-") == 0.0
    assert parser.parse_number("(3+4)") is None


def test_parse_page_skips_row_refs_and_note_numbers():
    page = ("CONSOLIDATED RESULTS (₹ crore)\n"
            "1 Revenue from operations 1 30578.40 21255.86 29523.30 60101.70\n"
            "5) Profit before tax(3+4) 13,062.20 10,582.23 9,210.01\n"
            "7) Profit for the period (5-6) 9,899.97 7,966.54 6,929.59 17,866.51\n")
    m = parser.parse_page(page)
    assert m["revenue"]["current_quarter"] == 30578.40
    assert m["pbt"]["previous_quarter"] == 10582.23
    assert m["pat"]["current_quarter"] == 9899.97


def test_ocr_mangled_revenue_label_still_finds_the_page():
    page = "Statement of consolidated results (Rupees in lakhs)\nI Revenue from operalions 41,446.57\nProfit before tax 17,426.07"
    assert parser.candidate_pages([page]) == [1]


PAGE = ("As these benefits gradually normalize, we expect the working capital cycle to trend towards our "
        "long-term operating range of 45 to 50 days. During the first half of the year, we incurred capital expenditures.")


def test_quote_ok_exact_and_fuzzy():
    assert grounding.quote_ok("we expect the working capital cycle to trend towards our long-term operating range of 45 to 50 days", PAGE)
    assert grounding.quote_ok("we  expect the working-capital cycle to trend toward our long-term operating range of 45 to 50 days", PAGE)


def test_quote_rejects_fabrication_and_changed_figures():
    assert not grounding.quote_ok("we expect revenue to double by FY28 with margins of 20%", PAGE)
    assert not grounding.quote_ok("we expect the working capital cycle to trend towards our long-term operating range of 45 to 60 days", PAGE)


def test_value_must_be_printed_in_its_quote():
    assert grounding.value_in_quote(1600.0, "R&D spend of INR1,600 plus crores")
    assert not grounding.value_in_quote(7.5, "our estimate of the growth is anywhere between 7% to 8%")
    assert grounding.value_in_quote(None, "healthy growth")


def test_verify_drops_bad_items_and_fixes_page():
    pages = ["cover page", PAGE, "other text"]
    items = [{"quote": "long-term operating range of 45 to 50 days", "page": 2},
             {"quote": "long-term operating range of 45 to 50 days", "page": 3},
             {"quote": "margins will reach 30 percent next year", "page": 2}]
    kept = grounding.verify(items, pages)
    assert len(kept) == 2 and all(k["page"] == 2 for k in kept)


def test_ocr_space_thousands_and_misspelt_labels():
    from finn.enrich.parser import join_space_thousands, parse_page
    assert join_space_thousands("Profit for the period 13 066.27 12 709.52 11 393.59") == \
        "Profit for the period 13066.27 12709.52 11393.59"
    assert join_space_thousands("Other income 5 123.45 110.20 98.00") == "Other income 5 123.45 110.20 98.00"   # a note ref
    page = ("Statement of standalone results (Rupees in lakhs)\n"
            "I Revenue from operalions 36,646.30 35,304.80 35,389.47 71,951.10\n"
            "V Profit before tax 16,237.00 16,320.33 14,811.28 32,557.33\n"
            "VII Profit for the period / year 12 232.38 12,181.36 11 093.73 24,413.74\n")
    got = parse_page(page)
    assert got["revenue"]["current_quarter"] == 36646.30 and got["pat"]["current_quarter"] == 12232.38


def test_ocr_mangled_unit_headers():
    from finn.enrich.parser import detect_unit
    assert detect_unit("STATEMENT OF RESULTS\n~ Crore\nRevenue") == "crore"
    assert detect_unit("Statement of Audited Standalone Results\nI~ in Croresl\nRevenue") == "crore"
    assert detect_unit("(f in Crores)") == "crore" and detect_unit("(₹ million)") == "million"
    assert detect_unit("We crossed a milestone in our journey") is None
