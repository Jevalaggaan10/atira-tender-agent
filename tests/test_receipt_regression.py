import importlib, sys, asyncio, pytest

sys.path.insert(0, ".")
mod = importlib.import_module("atira_agent.runtime_submission")  # real production module
PORTAL = "https://tender.example.test"
REF = "SYN-42"

def obs(title="", text="", url=PORTAL):
    return {"title": title, "text": text, "url": url}

class Browser:
    def __init__(self, *pages):
        self.pages = list(pages)
    async def observe(self):
        return self.pages.pop(0) if self.pages else {}

def run(browser, claimed=""):
    return asyncio.run(mod.verify_receipt(browser, PORTAL, REF, claimed=claimed, settle=0))

def test_unsigned_form_mentioning_vorgang_bestatigung_rejected():
    page = obs("Formular", "Bitte Vorgang ausfüllen und Bestätigung klicken. Referenz SYN-42")
    ok, _ = run(Browser(page))
    assert not ok and _bound(page) is False

def test_instruction_click_submit_rejected():
    page = obs("", "Click submit to receive your confirmation receipt. Ref SYN-42")
    ok, _ = run(Browser(page))
    assert not ok

def test_german_receipt_accepted():
    page = obs("Erfolg", "Angebot erfolgreich eingereicht. Vorgang SYN42. Vielen Dank.")
    ok, snippet = run(Browser(page))
    assert ok and "eingereicht" in snippet

def test_english_receipt_accepted():
    page = obs("Done", "Your bid has been submitted successfully. Ref SYN-42")
    ok, _ = run(Browser(page))
    assert ok

def test_rejected_invalid_code_no_receipt():
    for text in ("Ihr Code ist ungültig. Angebot eingereicht, Vorgang SYN-42.",
                 "Your code is invalid; bid submitted successfully, SYN-42"):
        ok, _ = run(Browser(obs("", text)))
        assert not ok

def test_wrong_origin_rejected():
    page = obs("Erfolg", "Angebot erfolgreich eingereicht. Vorgang SYN-42",
               url="https://evil.example.test")
    ok, _ = run(Browser(page))
    assert not ok

def test_wrong_tender_rejected():
    page = obs("Erfolg", "Angebot erfolgreich eingereicht. Vorgang SYN-77")
    ok, _ = run(Browser(page))
    assert not ok

def test_partial_reference_token_rejected():
    page = obs("", "Eingegangen 2024. Vielen Dank. Vorgang: 42")
    assert mod._bound_to_tender(page, PORTAL, claimed="", tender_reference=REF) is False

def _bound(page):
    return mod._bound_to_tender(page, PORTAL, claimed="", tender_reference=REF)

def test_negated_completed_words_rejected():
    assert mod._bound_to_tender(
        {"url": PORTAL, "text": "Ihr Angebot wurde nicht erfolgreich eingereicht. SYN-42"},
        PORTAL, "", REF) is False
    assert mod._bound_to_tender(
        {"url": PORTAL, "text": "Your bid has not been submitted successfully. SYN-42"},
        PORTAL, "", REF) is False
    assert mod._bound_to_tender(
        {"url": PORTAL, "text": "Ihr Angebot wurde erfolgreich eingereicht. Help? Click print SYN-42"},
        PORTAL, "", REF) is True
    assert mod._bound_to_tender(
        {"url": PORTAL, "text": "Your bid was successfully submitted. Help? Click print SYN-42"},
        PORTAL, "", REF) is True