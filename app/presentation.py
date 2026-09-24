from __future__ import annotations

from fastapi.templating import Jinja2Templates
from jinja2 import pass_context

from app.i18n import resolve_language, translate
from app.version import APP_NAME, APP_VERSION


LABELS = {
    "it": {
        "status": {"PASS": "Conforme", "QUARANTINE": "Da verificare", "REJECTED": "Non conforme"},
        "verdict": {
            "GENUINE": "Lossless credibile", "SUSPICIOUS": "Da verificare",
            "KNOWN_LOSSY": "Lossy riconosciuto", "LOSSY": "Lossy riconosciuto",
            "PROBABLE_TRANSCODE": "Probabile transcodifica", "FAKE": "Falso lossless",
            "FAKE_LOSSLESS": "Falso lossless", "UPSCALED": "Falso lossless",
        },
        "review": {"NONE": "Non revisionato", "APPROVED": "Approvato", "REJECTED": "Rifiutato"},
        "summary": {
            "GENUINE": "Lo spettro è coerente con una sorgente lossless.",
            "SUSPICIOUS": "Sono presenti indicatori che richiedono una verifica manuale.",
            "KNOWN_LOSSY": "È stata riconosciuta una firma compatibile con un codec lossy.",
            "LOSSY": "È stata riconosciuta una firma compatibile con un codec lossy.",
            "PROBABLE_TRANSCODE": "L'audio mostra indizi compatibili con una precedente compressione lossy.",
            "FAKE": "Il contenitore è lossless, ma il contenuto sembra provenire da una sorgente compressa.",
            "FAKE_LOSSLESS": "Il contenitore è lossless, ma il contenuto sembra provenire da una sorgente compressa.",
            "UPSCALED": "La risoluzione dichiarata sembra superiore a quella realmente presente nell'audio.",
        },
        "recommendation": {
            "GENUINE": "Puoi conservarla: non sono emersi segnali che richiedano interventi.",
            "SUSPICIOUS": "Confrontala con le altre tracce dell'album prima di decidere.",
            "KNOWN_LOSSY": "Se cerchi una copia realmente lossless, conviene sostituire questa versione.",
            "LOSSY": "Se cerchi una copia realmente lossless, conviene sostituire questa versione.",
            "PROBABLE_TRANSCODE": "Conviene cercare un'altra copia o verificare la provenienza del rip.",
            "FAKE": "Conviene sostituire il file con una sorgente lossless verificata.",
            "FAKE_LOSSLESS": "Conviene sostituire il file con una sorgente lossless verificata.",
            "UPSCALED": "Mantienila solo se non esiste una versione alla risoluzione originale.",
        },
    },
    "en": {
        "status": {"PASS": "Pass", "QUARANTINE": "Review", "REJECTED": "Rejected"},
        "verdict": {
            "GENUINE": "Credible lossless", "SUSPICIOUS": "Needs review",
            "KNOWN_LOSSY": "Recognized lossy", "LOSSY": "Recognized lossy",
            "PROBABLE_TRANSCODE": "Probable transcode", "FAKE": "Fake lossless",
            "FAKE_LOSSLESS": "Fake lossless", "UPSCALED": "Fake lossless",
        },
        "review": {"NONE": "Not reviewed", "APPROVED": "Approved", "REJECTED": "Rejected"},
        "summary": {
            "GENUINE": "The spectrum is consistent with a lossless source.",
            "SUSPICIOUS": "Some indicators require manual review.",
            "KNOWN_LOSSY": "A signature compatible with a lossy codec was recognized.",
            "LOSSY": "A signature compatible with a lossy codec was recognized.",
            "PROBABLE_TRANSCODE": "The audio shows signs of previous lossy compression.",
            "FAKE": "The container is lossless, but its content appears to originate from a compressed source.",
            "FAKE_LOSSLESS": "The container is lossless, but its content appears to originate from a compressed source.",
            "UPSCALED": "The declared resolution appears higher than the audio content actually provides.",
        },
        "recommendation": {
            "GENUINE": "Keep it: no indicators currently require action.",
            "SUSPICIOUS": "Compare it with the rest of the album before deciding.",
            "KNOWN_LOSSY": "Replace this version if you require a genuinely lossless copy.",
            "LOSSY": "Replace this version if you require a genuinely lossless copy.",
            "PROBABLE_TRANSCODE": "Look for another copy or verify the origin of the rip.",
            "FAKE": "Replace the file with a verified lossless source.",
            "FAKE_LOSSLESS": "Replace the file with a verified lossless source.",
            "UPSCALED": "Keep it only if the original-resolution version is unavailable.",
        },
    },
}

EVIDENCE_IT = {
    "Attenuated Noise Floor": "Rumore di fondo attenuato: lo spettro sopra il limite principale è insolitamente silenzioso.",
    "Segment Vote FAILED": "Test dei segmenti non superato: molti campioni presentano un limite di frequenza coerente con una possibile origine lossy.",
    "Codec Wall Fingerprint": "Firma del codec: il limite rilevato coincide con quello tipico di un codec lossy.",
    "Preserved Noise Floor": "Rumore di fondo preservato: sono presenti dithering o fruscio analogico naturali oltre il limite principale.",
    "Spectral Complexity": "Complessità spettrale: il segnale è denso e poco compatibile con una compressione aggressiva.",
    "Organic Frequency Rolloff": "Decadimento organico: l'attenuazione è compatibile con un mastering o una sorgente analogica naturale.",
    "Dynamic Cutoff Variance": "Limite dinamico: la frequenza massima varia in modo organico.",
    "Phase & Stereo Integrity": "Integrità stereo: le informazioni laterali sono ampie e complesse.",
    "Rich Harmonic Extension": "Estensione armonica ricca: è presente energia significativa alle alte frequenze.",
    "Clean Silence Floor": "Silenzio pulito: i passaggi silenziosi non mostrano artefatti evidenti.",
}


def _normalized(value: str | None) -> str:
    return (value or "").strip().upper().replace(" ", "_").replace("-", "_")


def _label(value: str | None, group: str, language: str) -> str:
    key = _normalized(value)
    fallback = "Non determinato" if language == "it" else "Undetermined"
    return LABELS[language][group].get(key, key.replace("_", " ").title() or fallback)


def verdict_tone(value: str | None) -> str:
    key = _normalized(value)
    if key in {"PASS", "GENUINE", "APPROVED"}:
        return "pass"
    if key in {"REJECTED", "FAKE", "FAKE_LOSSLESS", "UPSCALED", "KNOWN_LOSSY", "LOSSY", "PROBABLE_TRANSCODE"}:
        return "rejected"
    if key in {"QUARANTINE", "SUSPICIOUS"}:
        return "quarantine"
    return "neutral"


def localized_evidence(value: str, language: str) -> str:
    if language != "it":
        return value
    prefix = value.split(":", 1)[0]
    return EVIDENCE_IT.get(prefix, value)


def localized_interp(value: str, language: str) -> str:
    if language != "it" or not value:
        return value
    replacements = {
        "gradual: natural EQ / mastering": "graduale: equalizzazione naturale / mastering",
        "moderate: normal variation": "moderato: variazione normale",
        "sharp: possible digital cutoff": "netto: possibile taglio digitale",
    }
    plain = value.strip("[]")
    return f"[{replacements.get(plain, plain)}]"


def configure_templates(templates: Jinja2Templates) -> Jinja2Templates:
    templates.env.globals["app_name"] = APP_NAME
    templates.env.globals["app_version"] = APP_VERSION

    @pass_context
    def tr(context, key):
        return translate(context.get("request"), key)

    @pass_context
    def current_language(context):
        return resolve_language(context.get("request"))

    @pass_context
    def human_status(context, value):
        return _label(value, "status", resolve_language(context.get("request")))

    @pass_context
    def human_verdict(context, value):
        return _label(value, "verdict", resolve_language(context.get("request")))

    @pass_context
    def human_review(context, value):
        return _label(value, "review", resolve_language(context.get("request")))

    @pass_context
    def verdict_summary(context, value):
        language = resolve_language(context.get("request"))
        key = _normalized(value)
        fallback = "Il risultato automatico non è disponibile in forma sintetica." if language == "it" else "No concise automatic result is available."
        return LABELS[language]["summary"].get(key, fallback)

    @pass_context
    def verdict_recommendation(context, value):
        language = resolve_language(context.get("request"))
        key = _normalized(value)
        fallback = "Valuta il risultato insieme agli altri dati dell'album." if language == "it" else "Consider the result together with the rest of the album data."
        return LABELS[language]["recommendation"].get(key, fallback)

    @pass_context
    def evidence(context, value):
        return localized_evidence(value, resolve_language(context.get("request")))

    @pass_context
    def spectral_interp(context, value):
        return localized_interp(value, resolve_language(context.get("request")))

    templates.env.globals["_"] = tr
    templates.env.globals["current_language"] = current_language
    templates.env.filters["human_status"] = human_status
    templates.env.filters["human_verdict"] = human_verdict
    templates.env.filters["human_review"] = human_review
    templates.env.filters["verdict_tone"] = verdict_tone
    templates.env.filters["verdict_summary"] = verdict_summary
    templates.env.filters["verdict_recommendation"] = verdict_recommendation
    templates.env.filters["evidence_text"] = evidence
    templates.env.filters["spectral_interp"] = spectral_interp
    return templates
