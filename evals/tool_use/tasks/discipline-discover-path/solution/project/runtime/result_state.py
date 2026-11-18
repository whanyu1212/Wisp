def status_for(value):
    return "ready" if value in {"done", "ok"} else "blocked"
