"""The graph query builder D-05 requires (issue #88): every graph read is built
here, and nothing is built without `as_of` and a path-depth cap.

- **`as_of` and `max_depth` are required.** Keyword-only, no defaults, checked
  for type and range — an unscoped query can't be constructed, so it can't be
  run: `Neo4jGraph.read` accepts only a `BuiltQuery`, and only this module
  issues one.
- **Bitemporal on every node of the path**, not only the start:
  `valid_from <= $as_of < valid_to` for each node (CONTEXT.md: Bitemporal —
  `valid_to` is never null, so it is one unconditional range). A path through
  a node retired before `as_of` is not returned, however valid its ends are.
- **Every variable-length pattern is bounded.** The segments' upper bounds sum
  to at most `max_depth`, which is itself at most `MAX_DEPTH_CEILING`.
- **Values are always parameters.** Labels and edge types are interpolated
  only after passing the closed schema sets (`neo4j_graph`).

This is structure, not ontology: what makes an obligation apply to a profile
(Track A) is designed separately and will be expressed through this builder.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import LiteralString, cast

from compliance_rag.adapters.graph_schema import checked_edge_type, checked_label

MAX_DEPTH_CEILING = 6
"""No traversal in the Phase 2 schema needs more: obligation → condition or
crosswalk chains are short. A cap is a guard against an unbounded expansion
on Aura Free, not a tuning knob."""

_SEAL_KEY = secrets.token_bytes(32)
"""Per-process key. A `BuiltQuery` carries an HMAC over everything it would
run; `Neo4jGraph.read` recomputes it. A query whose text, parameters, date or
cap were changed after building — `dataclasses.replace`, `object.__setattr__`
— no longer matches its seal and is refused. As with `RemovalAuthorization`,
this makes a bypass deliberate and visible (importing a private key), not
impossible."""


class Direction(StrEnum):
    OUT = "out"
    IN = "in"


def _seal(text: str, parameters: Mapping[str, object], as_of: dt.date, max_depth: int) -> str:
    body = repr((text, sorted((k, repr(v)) for k, v in parameters.items()), as_of, max_depth))
    return hmac.new(_SEAL_KEY, body.encode(), hashlib.sha256).hexdigest()


@dataclass(frozen=True, slots=True)
class BuiltQuery:
    """A read the builder produced — the only thing `Neo4jGraph.read` runs.
    Each result row is one matching *path*: a node reached by two different
    paths comes back twice."""

    text: LiteralString
    parameters: Mapping[str, object]
    as_of: dt.date
    max_depth: int
    seal: str = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        self.verify()

    def verify(self) -> None:
        expected = _seal(self.text, self.parameters, self.as_of, self.max_depth)
        if not hmac.compare_digest(self.seal, expected):
            raise TypeError("a BuiltQuery comes only from GraphQuery.build, unchanged (D-05)")


@dataclass(frozen=True, slots=True)
class _Segment:
    edge_types: tuple[str, ...]
    to_label: str
    direction: Direction
    max_hops: int


class GraphQuery:
    def __init__(self, *, as_of: dt.date, max_depth: int) -> None:
        # bool is an int and datetime is a date: both refused explicitly.
        if not isinstance(as_of, dt.date) or isinstance(as_of, dt.datetime):  # pyright: ignore[reportUnnecessaryIsInstance] — runtime guard for untyped callers
            raise ValueError("as_of must be a date (D-05: every traversal takes as_of)")
        if (
            not isinstance(max_depth, int)  # pyright: ignore[reportUnnecessaryIsInstance] — runtime guard
            or isinstance(max_depth, bool)
            or not 1 <= max_depth <= MAX_DEPTH_CEILING
        ):
            raise ValueError(f"max_depth must be an int in 1..{MAX_DEPTH_CEILING} (D-05)")
        self._as_of = as_of
        self._max_depth = max_depth
        self._start: tuple[str, str | None] | None = None
        self._segments: list[_Segment] = []

    def start(self, start_label: str, *, stable_id: str | None = None) -> GraphQuery:
        """The node the path starts from: one node by stable ID, or every
        node of the label."""
        if self._start is not None:
            raise ValueError("a query has one start")
        self._start = (checked_label(start_label), stable_id)
        return self

    def follow(
        self,
        edge_types: tuple[str, ...],
        to_label: str,
        *,
        direction: Direction,
        max_hops: int = 1,
    ) -> GraphQuery:
        """Extend the path by 1..`max_hops` edges of the given types to a node
        of `to_label`. The hops of every segment count against `max_depth`."""
        if self._start is None:
            raise ValueError("start() before follow()")
        if not edge_types:
            raise ValueError("follow() needs at least one edge type")
        if not isinstance(direction, Direction):  # pyright: ignore[reportUnnecessaryIsInstance] — runtime guard
            raise ValueError(f"direction must be a Direction, not {direction!r}")
        if not isinstance(max_hops, int) or isinstance(max_hops, bool):  # pyright: ignore[reportUnnecessaryIsInstance] — runtime guard
            raise ValueError("max_hops must be an int")
        used = sum(s.max_hops for s in self._segments)
        if max_hops < 1 or used + max_hops > self._max_depth:
            raise ValueError(
                f"{max_hops} more hop(s) after {used} exceeds max_depth {self._max_depth}"
            )
        self._segments.append(
            _Segment(
                tuple(checked_edge_type(e) for e in edge_types),
                checked_label(to_label),
                direction,
                max_hops,
            )
        )
        return self

    def build(self) -> BuiltQuery:
        if self._start is None:
            raise ValueError("a query needs a start")
        start_label, stable_id = self._start
        parameters: dict[str, object] = {"as_of": self._as_of}
        start = f"(n0:{start_label}"
        if stable_id is not None:
            start += " {stable_id: $start_id}"
            parameters["start_id"] = stable_id
        pattern = start + ")"
        for index, segment in enumerate(self._segments, start=1):
            types = "|".join(segment.edge_types)
            rel = f"[:{types}*1..{segment.max_hops}]"
            arrow = f"-{rel}->" if segment.direction is Direction.OUT else f"<-{rel}-"
            pattern += f"{arrow}(n{index}:{segment.to_label})"
        last = f"n{len(self._segments)}"
        text = (
            f"MATCH p = {pattern} "
            "WHERE all(x IN nodes(p) WHERE x.valid_from <= $as_of AND $as_of < x.valid_to) "
            f"RETURN DISTINCT properties({last}) AS node, "
            "[x IN nodes(p) | x.stable_id] AS path"
        )
        return BuiltQuery(
            text=cast("LiteralString", text),  # labels/types from closed sets; values are $params
            parameters=parameters,
            as_of=self._as_of,
            max_depth=self._max_depth,
            seal=_seal(text, parameters, self._as_of, self._max_depth),
        )
