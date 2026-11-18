"""Read-only evidence and album context; decisions remain owned by the existing workflow."""
import json
import math
from collections import defaultdict
from statistics import median
from app.services.result_guidance import needs_review


def analysis_data(track):
    try:
        raw = json.loads(track.forensic_json or "{}")
    except (ValueError, TypeError):
        raw = {}
    raw = raw if isinstance(raw, dict) else {}
    result = raw.get("result")
    result = result if isinstance(result, dict) else {}
    authenticity = result.get("authenticity")
    authenticity = authenticity if isinstance(authenticity, dict) else {}
    spectral = authenticity.get("spectral")
    technical = result.get("technical")
    spectral = dict(spectral) if isinstance(spectral, dict) else {}
    for key in ("cutoff_hz", "segment_wall_hz", "segment_walled", "segment_total", "lossy_score", "natural_score", "net_confidence_pct"):
        if key in spectral:
            parsed = number(spectral[key])
            if parsed is None:
                spectral.pop(key)
            else:
                spectral[key] = parsed
    for key in ("codec_fingerprint", "cutoff_hz_str", "cutoff_sharpness_interp"):
        if key in spectral and not isinstance(spectral[key], str):
            spectral.pop(key)
    for key in ("evidence", "natural_evidence"):
        value = spectral.get(key)
        spectral[key] = [item for item in value if isinstance(item,str)] if isinstance(value,list) else []
    return {"raw": raw, "spectral": spectral,
        "technical": technical if isinstance(technical, dict) else {}}


def number(value):
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    except (ValueError, TypeError, OverflowError):
        return None


def evidence_for(track):
    data = analysis_data(track)
    spectral = data["spectral"]
    facts = []
    cutoff = number(spectral.get("cutoff_hz"))
    if cutoff is not None and cutoff > 0:
        facts.append({"label":"Measured frequency cutoff", "value":f"{cutoff:,.0f} Hz"})
    total, walled = number(spectral.get("segment_total")), number(spectral.get("segment_walled"))
    if total is not None and walled is not None and total > 0 and 0 <= walled <= total and total.is_integer() and walled.is_integer():
        facts.append({"label":"Segments with a frequency wall", "value":f"{walled:.0f} / {total:.0f}"})
    codec = spectral.get("codec_fingerprint")
    if isinstance(codec, str) and codec.strip():
        facts.append({"label":"Recorded codec resemblance", "value":codec[:250]})
    def items(key):
        value = spectral.get(key)
        return [item[:1000] for item in value if isinstance(item, str) and item.strip()] if isinstance(value, list) else []
    return {"facts":facts, "suspicious":items("evidence"), "natural":items("natural_evidence"),
        "available":bool(facts or items("evidence") or items("natural_evidence"))}


def review_progress(release):
    reviewable = [t for t in release.tracks if t.status == "QUARANTINE" and t.integrity_ok]
    pending = [t for t in reviewable if needs_review(t)]
    return {"total":len(reviewable),"pending":len(pending),"completed":len(reviewable)-len(pending),
        "approved":sum(t.human_review == "APPROVED" for t in reviewable),
        "rejected":sum(t.human_review == "REJECTED" for t in reviewable),
        "blocked":sum(not t.integrity_ok or t.status == "REJECTED" for t in release.tracks),
        "first_pending":pending[0].id if pending else None}


def next_pending(release, after=None):
    tracks = list(release.tracks)
    if after is not None:
        position = next((i for i,t in enumerate(tracks) if t.id == after), None)
        if position is None:
            raise ValueError("Track does not belong to this album")
        tracks = tracks[position+1:] + tracks[:position]
    return next((t.id for t in tracks if needs_review(t)), None)


def album_comparison(tracks):
    """Describe cutoff differences only within matching rate/channels/precision groups.

    A common range requires a strict majority (at least two tracks) within
    max(1 kHz, 10% of the median); differences need max(2 kHz, 15%).
    These display thresholds never affect forensic verdicts or review decisions.
    """
    groups = defaultdict(list)
    results = {t.id:{"state":"unavailable","cutoff":None,"baseline":None,"comparable":0} for t in tracks}
    for track in tracks:
        data = analysis_data(track)
        rate = number(data["technical"].get("sample_rate"))
        channels = number(data["technical"].get("channels"))
        precision = data["technical"].get("precision")
        cutoff = number(data["spectral"].get("cutoff_hz"))
        if not track.integrity_ok or rate is None or rate <= 0 or channels is None or channels <= 0 or not channels.is_integer() or not isinstance(precision,str) or not precision.strip():
            continue
        if cutoff is None or not 0 < cutoff <= rate/2:
            continue
        groups[(rate,channels,precision.strip().lower())].append((track.id,cutoff))
    for values in groups.values():
        count = len(values)
        if count < 3:
            continue
        baseline = median(value for _,value in values)
        tolerance = max(1000,baseline*.10)
        common = sum(abs(value-baseline)<=tolerance for _,value in values)
        coherent = common >= 2 and common > count/2
        for identifier,cutoff in values:
            delta = cutoff-baseline
            if not coherent:
                state = "mixed"
            elif abs(delta)>=max(2000,baseline*.15):
                state = "lower" if delta<0 else "higher"
            elif abs(delta)<=tolerance:
                state = "shared"
            else:
                state = "mixed"
            results[identifier] = {"state":state,"cutoff":round(cutoff),"baseline":round(baseline),"comparable":count-1}
    return results
