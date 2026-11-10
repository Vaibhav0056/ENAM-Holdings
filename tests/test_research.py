"""Phase 4: transcript roles (the speaker check), management-only text, Q&A pairing, annual-report page choice."""
from finn.research import transcripts as T
from finn.research.engine import ar_pages

PAGES = [
    "Polycab India Limited Q2 FY27 Earnings Conference Call\nMANAGEMENT: MR. ADITYA NATHANY – CHIEF FINANCIAL OFFICER\n"
    "MR. NIYANT MARU – HEAD, INVESTOR RELATIONS\nMODERATOR: Ladies and gentlemen, welcome.",
    "Moderator: Ladies and gentlemen, good day.\nAditya Nathany: We expect EBITDA margins of 11% to 13% by FY30.\n"
    "We will grow 1.5x of market growth.\nModerator: We will now begin the question-and-answer session. The first question "
    "is from Kevin.\nKevin Shah: What is the margin outlook for FMEG?\nAditya Nathany: We target 8% to 10% by FY30.\n"
    "Moderator: Next question.\nUmang Jain: Will you keep 1.5x?\nNiyant Maru: Yes, that is our guidance.",
]


def test_roles_from_management_block():
    turns = T.split_turns(PAGES)
    roles = {t["speaker"]: t["role"] for t in turns}
    assert roles["Aditya Nathany"] == "management" and roles["Niyant Maru"] == "management"
    assert roles["Kevin Shah"] == "analyst" and roles["Moderator"] == "moderator"
    first = next(t for t in turns if t["speaker"] == "Aditya Nathany")
    assert "11% to 13%" in first["text"] and "1.5x" in first["text"]   # continuation lines join the turn


def test_management_text_excludes_analysts_and_tags_pages():
    text = T.management_text(T.split_turns(PAGES))
    assert "[PAGE 2] [Aditya Nathany] We expect EBITDA margins" in text
    assert "What is the margin outlook" not in text and "Ladies and gentlemen" not in text


def test_speaker_check():
    turns = T.split_turns(PAGES)
    assert T.is_management("Mr. Aditya Nathany", turns)
    assert not T.is_management("Kevin Shah", turns)


def test_qa_pairs():
    pairs = T.qa_pairs(T.split_turns(PAGES))
    assert [(p["analyst"], p["answered_by"]) for p in pairs] == [("Kevin Shah", "Aditya Nathany"), ("Umang Jain", "Niyant Maru")]


def test_roles_fall_back_to_opening_speakers_without_a_management_block():
    pages = ["Moderator: Welcome.\nRahul Mehta: Revenue grew 20%. We aim for 15% growth in FY27.\n"
             "Moderator: The first question is from Priya.\nPriya Rao: Thanks.\nRahul Mehta: Sure."]
    roles = {t["speaker"]: t["role"] for t in T.split_turns(pages)}
    assert roles == {"Moderator": "moderator", "Rahul Mehta": "management", "Priya Rao": "analyst"}


def test_annual_report_page_choice_prefers_business_pages():
    filler = "lorem ipsum " * 60
    pages = [filler, "Notes to the financial statements revenue revenue revenue " * 20,
             "Our business segments: revenue growth, EBITDA margin, strategy and key performance highlights. " * 10, filler]
    assert ar_pages(pages, k=1) == [3]
