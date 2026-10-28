"""Thinking-trace events. A step is sent once when it starts and again (same id) when it settles."""


class Trace:
    def __init__(self) -> None:
        self._n = 0

    def start(self, stage: str, title: str, detail: str = "", **data) -> dict:
        self._n += 1
        return {"type": "step", "id": f"s{self._n}", "stage": stage, "title": title, "detail": detail,
                "status": "running", **data}

    @staticmethod
    def settle(step: dict, status: str, detail: str | None = None, title: str | None = None, **data) -> dict:
        """status: ok | warn | fail | info"""
        step.update(status=status, **data)
        if detail is not None:
            step["detail"] = detail
        if title is not None:
            step["title"] = title
        return dict(step)

    def note(self, stage: str, title: str, detail: str = "", status: str = "info", **data) -> dict:
        return self.settle(self.start(stage, title, detail, **data), status)
