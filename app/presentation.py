from __future__ import annotations

from fastapi.templating import Jinja2Templates

from app.version import APP_VERSION


STATUS_LABELS = {
    "PASS": "Conforme",
    "QUARANTINE": "Da verificare",
    "REJECTED": "Non conforme",
}

VERDICT_LABELS = {
    "GENUINE": "Lossless credibile",
    "SUSPICIOUS": "Da verificare",
    "KNOWN_LOSSY": "Lossy riconosciuto",
    "LOSSY": "Lossy riconosciuto",
    "PROBABLE_TRANSCODE": "Probabile transcodifica",
    "FAKE": "Falso lossless",
    "FAKE_LOSSLESS": "Falso lossless",
    "UPSCALED": "Falso lossless",
}

REVIEW_LABELS = {
    "NONE": "Non revisionato",
    "APPROVED": "Approvato",
    "REJECTED": "Rifiutato",
}

VERDICT_SUMMARIES = {
    "GENUINE": "Lo spettro è coerente con una sorgente lossless.",
    "SUSPICIOUS": "Sono presenti indicatori che richiedono una verifica manuale.",
    "KNOWN_LOSSY": "È stata riconosciuta una firma compatibile con un codec lossy.",
    "LOSSY": "È stata riconosciuta una firma compatibile con un codec lossy.",
    "PROBABLE_TRANSCODE": "L'audio mostra indizi compatibili con una precedente compressione lossy.",
    "FAKE": "Il contenitore è lossless, ma il contenuto sembra provenire da una sorgente compressa.",
    "FAKE_LOSSLESS": "Il contenitore è lossless, ma il contenuto sembra provenire da una sorgente compressa.",
    "UPSCALED": "La risoluzione dichiarata sembra superiore a quella realmente presente nell'audio.",
}


def _normalized(value: str | None) -> str:
    return (value or "").strip().upper().replace(" ", "_").replace("-", "_")


def human_status(value: str | None) -> str:
    key = _normalized(value)
    return STATUS_LABELS.get(key, key.replace("_", " ").title() or "Non determinato")


def human_verdict(value: str | None) -> str:
    key = _normalized(value)
    return VERDICT_LABELS.get(key, key.replace("_", " ").title() or "Non determinato")


def human_review(value: str | None) -> str:
    key = _normalized(value)
    return REVIEW_LABELS.get(key, key.replace("_", " ").title() or "Non revisionato")


def verdict_tone(value: str | None) -> str:
    key = _normalized(value)
    if key in {"PASS", "GENUINE", "APPROVED"}:
        return "pass"
    if key in {"REJECTED", "FAKE", "FAKE_LOSSLESS", "UPSCALED", "KNOWN_LOSSY", "LOSSY", "PROBABLE_TRANSCODE"}:
        return "rejected"
    if key in {"QUARANTINE", "SUSPICIOUS"}:
        return "quarantine"
    return "neutral"


def verdict_summary(value: str | None) -> str:
    key = _normalized(value)
    return VERDICT_SUMMARIES.get(key, "Il risultato automatico non è disponibile in forma sintetica.")


def configure_templates(templates: Jinja2Templates) -> Jinja2Templates:
    templates.env.globals["app_version"] = APP_VERSION
    templates.env.filters["human_status"] = human_status
    templates.env.filters["human_verdict"] = human_verdict
    templates.env.filters["human_review"] = human_review
    templates.env.filters["verdict_tone"] = verdict_tone
    templates.env.filters["verdict_summary"] = verdict_summary
    return templates
