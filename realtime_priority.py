"""Pure scoring and scheduling rules for Automation 3.1."""
import math
from statistics import median

DEFAULT_PRIORITY = {
    "weights": {"quality": 40, "opportunity": 30, "interaction": 20, "diversity": 10},
    "curve": [100, 85, 65, 40, 20, 10],
    "levels": {"urgent": 8.5, "high": 7.5, "normal": 6},
}
MINUTES = (5, 15, 30, 60, 120, 180)
FAIR_WAIT = 900


def valid_number(value, low, high):
    return type(value) in (int, float) and low <= value <= high and math.isfinite(value)


def validate(config):
    if not isinstance(config, dict) or config.keys() != DEFAULT_PRIORITY.keys():
        raise ValueError("Configura los pesos, la curva de vigencia y los niveles de prioridad.")
    weights, curve, levels = (config[key] for key in ("weights", "curve", "levels"))
    if (not isinstance(weights, dict) or weights.keys() != DEFAULT_PRIORITY["weights"].keys()
            or not all(valid_number(value, 0, 100) for value in weights.values())
            or not math.isclose(sum(weights.values()), 100, abs_tol=.001)):
        raise ValueError("Los pesos deben sumar 100 %.")
    if (not isinstance(curve, list) or len(curve) != len(MINUTES) or not all(valid_number(x, 0, 100) for x in curve)
            or any(a < b for a, b in zip(curve, curve[1:]))):
        raise ValueError("La curva requiere seis porcentajes entre 0 y 100 en orden descendente.")
    if (not isinstance(levels, dict) or levels.keys() != DEFAULT_PRIORITY["levels"].keys()
            or not all(valid_number(value, 0, 10) for value in levels.values())
            or not levels["urgent"] > levels["high"] > levels["normal"]):
        raise ValueError("Configura umbrales descendentes entre 0 y 10.")
    return {"weights": dict(weights), "curve": list(curve), "levels": dict(levels)}


def freshness(minutes, curve):
    if minutes <= MINUTES[0]:
        return curve[0]
    for i, (a, b) in enumerate(zip(MINUTES, MINUTES[1:])):
        if minutes <= b:
            return curve[i] + (curve[i + 1] - curve[i]) * (minutes - a) / (b - a)
    return curve[-1]


def assess(row, config, now):
    from editorial_content import WEIGHTS
    from production_selection import score

    kind = row.get("event_kind") or ("live" if row["project"].startswith("live/") else "conventional")
    stamp = row.get("event_at")
    known = kind != "evergreen" and valid_number(stamp, 0, now)
    minutes = (now - stamp) / 60 if known else None
    criteria = row.get("criteria") or {}
    interaction = score((criteria.get("engagement") or {}).get("score"))
    core = [(score((criteria.get(key) or {}).get("score")), weight)
            for key, weight in WEIGHTS.items() if key != "engagement"]
    # Rebuild the editorial portion without engagement when it is an independent
    # input. The legacy aggregate already includes it and must never count it twice.
    if interaction is not None and all(value is not None for value, _ in core):
        editorial = sum(value * weight for value, weight in core) / sum(weight for _, weight in core)
    else:
        editorial = row["editorial"]
        interaction = None
    factors = {
        "quality": .6 * row["detection"] + .4 * editorial,
        "opportunity": freshness(minutes, config["curve"]) / 10 if known else None,
        "interaction": interaction,
        "diversity": score(row.get("diversity")),
    }
    measured = [(value, config["weights"][name]) for name, value in factors.items()
                if value is not None and config["weights"][name]]
    value = round(sum(value * weight for value, weight in measured) / sum(weight for _, weight in measured), 3) if measured else None
    levels = config["levels"]
    level = None
    if value is not None:
        level = ("urgent" if known and value >= levels["urgent"] else "high" if value >= levels["high"]
                 else "normal" if value >= levels["normal"] else "low")
    reason = ("Contenido atemporal" if kind == "evergreen" else "Antigüedad del acontecimiento sin verificar"
              if not known else f"Vigencia temporal {factors['opportunity'] * 10:.0f} %")
    if interaction is None:
        reason += " · interacción independiente sin datos"
    return {"temporal_score": value, "level": level, "event_kind": kind, "event_time_source": "operator" if known else None,
            "event_at": stamp if known else None, "age_minutes": round(minutes, 2) if known else None,
            "factors": factors, "reason": reason}


def distinct(candidate, prior):
    from production_selection import duplicate, text_similarity

    if duplicate(candidate, prior):
        return False
    # A high density of shared four-word sequences is evidence that two
    # excerpts present the same context, even across separately edited sources.
    return text_similarity(candidate["tokens"], prior["tokens"]) < .70


def distribution(values):
    values = sorted(value for value in values if valid_number(value, 0, 2**53))
    if not values:
        return {"samples": 0, "median": None, "p95": None}
    return {"samples": len(values), "median": round(median(values), 2),
            "p95": round(values[max(0, math.ceil(len(values) * .95) - 1)], 2)}


def queue_order(enqueued, priority, ident, now, fair_wait=FAIR_WAIT):
    aged = enqueued <= now - fair_wait
    return (not aged, 0 if aged else -priority, enqueued, ident)
