"""Stable primary reasons for filtering; they describe evidence, not proof of provenance."""
import json
REASONS = {"integrity":"Integrity failure", "lossy":"Lossy codec evidence", "transcode":"Possible transcode", "cutoff":"Frequency cutoff", "uncertain":"Uncertain result", "pass":"Passed"}

def classify(status, integrity, verdict, raw=None):
    if not integrity:
        return "integrity"
    if status == "PASS":
        return "pass"
    verdict = (verdict or "").upper().replace(" ", "_")
    if verdict in {"LOSSY", "KNOWN_LOSSY"}:
        return "lossy"
    if verdict in {"FAKE", "FAKE_LOSSLESS", "UPSCALED", "PROBABLE_TRANSCODE"}:
        return "transcode"
    try:
        value = json.loads(raw or "{}").get("result", {}).get("authenticity", {}).get("spectral", {})
        if value.get("codec_fingerprint"):
            return "lossy"
        if value.get("cutoff_hz") or value.get("cutoff_hz_str") or value.get("segment_wall_hz"):
            return "cutoff"
    except (ValueError, AttributeError, TypeError):
        pass
    return "uncertain"
