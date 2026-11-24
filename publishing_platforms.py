"""Declare available operations without treating OAuth scopes as app approval."""

PLATFORMS = ("tiktok", "youtube", "facebook", "instagram")


def capabilities(service):
    with service.lock:
        connected = bool(service.account)
        token = service.token or {}
        inbox = connected and "video.upload" in token.get("scopes", ())
    return {platform: {
        "connected": connected if platform == "tiktok" else False,
        "inbox": inbox if platform == "tiktok" else False,
        "direct_post": False,
        "local_inbox_schedule": inbox if platform == "tiktok" else False,
        "status_api": inbox if platform == "tiktok" else False,
        "metrics_api": False,
        "metrics_csv": True,
        "manual_package": platform != "tiktok",
        "consent_required": True,
        "reason": ("Direct Post requiere una aplicación aprobada y una prueba autorizada."
                   if platform == "tiktok" else "No hay una integración de publicación autorizada."),
    } for platform in PLATFORMS}
