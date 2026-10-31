"""What both graph stores share: relation-name rules, errors, and the relation-name cache."""
import re
import time

from . import config

_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_LABEL = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
EXCLUSIVE_RELATIONS = ("incompatible_with", "contradicts")


class GraphUnavailable(RuntimeError):
    """The graph cannot be reached (Aura paused, network down, auth failed)."""


class GraphLimitError(RuntimeError):
    """The graph refused a write because a plan limit was hit. Never retried."""


class ReadOnlyGraph(RuntimeError):
    """A write was attempted on a read-only graph (snapshot, judge mode)."""


def rel_name(relation: str) -> str:
    r = str(relation).lower()
    if not _NAME.match(r):  # relationship types can't be parameterised in Cypher, so whitelist
        raise ValueError(f"invalid relation name: {relation!r}")
    return r


def label(name: str) -> str:
    if not _LABEL.match(name):
        raise ValueError(f"invalid label: {name!r}")
    return name


NO_FLAGS = {"functional": False, "transitive": False, "acyclic": False}


class TTLCache:
    """A loaded-at-once mapping refreshed after RELATION_CACHE_SECONDS, so per-question lookups (relation
    names and rules, aliases) do not hit Aura every time. Writes through this process update it directly."""

    def __init__(self, loader):
        self._loader, self._data, self._at = loader, None, 0.0

    def get(self) -> dict:
        if self._data is None or time.time() - self._at > config.RELATION_CACHE_SECONDS:
            self._data, self._at = dict(self._loader()), time.time()
        return self._data

    def put(self, key, value) -> None:
        if self._data is not None:
            self._data[key] = value

    def invalidate(self) -> None:
        self._data = None
