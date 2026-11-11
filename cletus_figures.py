"""Cletus: the figures an answer may state.

supplied_figures(context) is every number a turn's context supplies: the county ranking's figures, the numbers in the lesson sections and the
property field values. unsupplied_figures(answer, context) lists, in order, every percentage and every number followed by the word properties
in an answer that is NOT one of them. The harness check figures_supplied and the guard in cletus_model both use it, so the check that tests
the model and the check that protects a client are the same code. Commas in numbers are ignored.
"""
import re

_PERCENT = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s?(?:%|percent)", re.IGNORECASE)
_COUNT = re.compile(r"(\d[\d,]*)\s+(?:multifamily\s+)?propert(?:y|ies)\b", re.IGNORECASE)


def number(text):
    try:
        return float(str(text).replace(",", ""))
    except ValueError:
        return None


def supplied_figures(context):
    out = set()
    rankings = (context or {}).get("rankings") or {}
    if isinstance(rankings.get("minProperties"), (int, float)) and not isinstance(rankings.get("minProperties"), bool):
        out.add(float(rankings["minProperties"]))
    for c in rankings.get("top") or []:
        for key in ("capRate", "mailablePct", "capRateN", "properties"):
            if isinstance(c.get(key), (int, float)) and not isinstance(c.get(key), bool):
                out.add(float(c[key]))
    for k in (context or {}).get("kb") or []:
        for m in re.finditer(r"\d[\d,]*(?:\.\d+)?", str(k.get("text", ""))):
            n = number(m.group(0))
            if n is not None:
                out.add(n)
    fields = (((context or {}).get("property") or {}).get("fields") or {})
    for v in fields.values():
        n = number(v) if isinstance(v, (int, float, str)) and not isinstance(v, bool) else None
        if n is not None:
            out.add(n)
    return out


def unsupplied_figures(answer, context):
    allowed = supplied_figures(context)
    found = []
    for rx in (_PERCENT, _COUNT):
        for m in rx.finditer(str(answer or "")):
            n = number(m.group(1))
            if n is not None and n not in allowed:
                found.append((m.start(), m.group(0).strip()))
    return [text for _, text in sorted(found)]
