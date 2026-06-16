"""In-memory recent event cache for frontend state restoration."""
import threading


class RecentEventStore:
    def __init__(self, limit: int = 50):
        self.limit = limit
        self._events = []
        self._lock = threading.Lock()

    def remember(self, event_data: dict, fallback_event_id):
        if not isinstance(event_data, dict):
            return
        event_id = event_data.get("event_id") or fallback_event_id()
        event_data["event_id"] = event_id
        with self._lock:
            for idx, item in enumerate(self._events):
                if item.get("event_id") == event_id:
                    merged = dict(item)
                    merged.update(event_data)
                    if item.get("timeline") or event_data.get("timeline"):
                        seen = set()
                        merged["timeline"] = [
                            step for step in [*(item.get("timeline") or []), *(event_data.get("timeline") or [])]
                            if not ((step.get("stage"), step.get("timestamp"), step.get("detail")) in seen
                                    or seen.add((step.get("stage"), step.get("timestamp"), step.get("detail"))))
                        ][-20:]
                    self._events[idx] = merged
                    return
            self._events.insert(0, dict(event_data))
            del self._events[self.limit:]

    def recent(self, limit: int = 20) -> list:
        with self._lock:
            return [dict(e) for e in self._events[:max(1, min(limit, self.limit))]]
