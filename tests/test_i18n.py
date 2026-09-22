from starlette.requests import Request

from app.i18n import resolve_language, translate
from app.presentation import localized_evidence, localized_interp


def request(*, language="", cookie=""):
    headers = []
    if language:
        headers.append((b"accept-language", language.encode()))
    if cookie:
        headers.append((b"cookie", cookie.encode()))
    return Request({"type": "http", "method": "GET", "path": "/", "headers": headers})


def test_browser_italian_selects_italian():
    req = request(language="it-IT,it;q=0.9,en;q=0.8")
    assert resolve_language(req) == "it"
    assert translate(req, "Settings") == "Impostazioni"


def test_any_other_browser_language_defaults_to_english():
    req = request(language="de-DE,de;q=0.9")
    assert resolve_language(req) == "en"
    assert translate(req, "Settings") == "Settings"


def test_cookie_overrides_browser_language():
    req = request(language="it-IT", cookie="lv_language=en")
    assert resolve_language(req) == "en"


def test_known_forensic_explanations_are_localized():
    evidence = "Spectral Complexity: High entropy score indicates dense content."
    assert localized_evidence(evidence, "it").startswith("Complessità spettrale")
    assert localized_evidence(evidence, "en") == evidence
    assert "graduale" in localized_interp("[gradual: natural EQ / mastering]", "it")
