"""Which tenant content is synthetic — decided by the operator, never by the
uploader (issue #78).

The build-phase residency gate (ADR-0023) lets generation run offshore only
while every tenant chunk is synthetic. A `synthetic` flag the uploader sets
would make that gate self-attested: a careless or hostile ingester could mark
a real policy synthetic and send it offshore. So the flag is not an input.

The registry is a checked-in file, `corpus/synthetic/registry.json`, listing
one hash per synthetic chunk and per synthetic clause: sha256 over the owning
tenant and the exact text (a clause's canonical text). Clauses are registered
too because clause text reaches the generator on its own — a repair prompt
carries the clause's canonical text — so an upload pairing registered chunks
with unregistered clauses is real data. It is written only by
`scripts/register_synthetic_fixtures.py` from the fixtures under
`docs/research/fixtures/synthetic-policies/`, and changes only by PR — like
`corpus/mappings/`, an operator path. An upload request has no way to add to
it.

`CorpusLifecycle` (the write path) derives every chunk's `synthetic` flag from
the registry, refuses a chunk that claims to be synthetic without being
registered, and counts an unregistered tenant clause as real data. Content
that is byte-for-byte a registered fixture under the same tenant is synthetic
whoever uploads it; anything else is real.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from pathlib import Path

from compliance_rag.corpus.models import Chunk, ClauseRecord, Scope

SYNTHETIC_REGISTRY_PATH = (
    Path(__file__).resolve().parents[3] / "corpus" / "synthetic" / "registry.json"
)


class SyntheticClaimRefused(PermissionError):
    """A chunk arrived marked synthetic, but the operator never registered it."""


def synthetic_key(tenant_id: str, text: str) -> str:
    return hashlib.sha256(f"{tenant_id}\0{text}".encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class SyntheticRegistry:
    keys: AbstractSet[str]
    """`synthetic_key` of every registered chunk. A frozenset when loaded
    from the file; read-only to this class either way."""
    source: str
    """Where the registered content came from, for whoever reads the file."""

    @classmethod
    def load(cls, path: Path = SYNTHETIC_REGISTRY_PATH) -> SyntheticRegistry:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls(keys=frozenset(str(k) for k in raw["keys"]), source=str(raw["source"]))

    @classmethod
    def of(
        cls, chunks: Iterable[Chunk], clauses: Iterable[ClauseRecord] = (), *, source: str
    ) -> SyntheticRegistry:
        """Built by the operator script from fixtures — not an upload path."""
        keys = {synthetic_key(c.tenant_id, c.text) for c in chunks}
        keys |= {synthetic_key(c.tenant_id, c.canonical_text) for c in clauses}
        return cls(keys=frozenset(keys), source=source)

    def save(self, path: Path = SYNTHETIC_REGISTRY_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        document = {"source": self.source, "keys": sorted(self.keys)}
        path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")

    def vouches_for(self, chunk: Chunk) -> bool:
        return self.vouches_for_text(chunk.tenant_id, chunk.text)

    def vouches_for_text(self, tenant_id: str, text: str) -> bool:
        """`text` (a chunk's, or a clause's canonical text) is registered
        synthetic under `tenant_id`: the policy prefix's check (ADR-0032)."""
        return synthetic_key(tenant_id, text) in self.keys

    def real_clauses(self, clauses: Iterable[ClauseRecord]) -> tuple[str, ...]:
        """Ids of tenant clauses the registry doesn't vouch for — real data."""
        return tuple(
            c.clause_id
            for c in clauses
            if c.scope is Scope.TENANT and not self.vouches_for_text(c.tenant_id, c.canonical_text)
        )

    def mark(self, chunks: tuple[Chunk, ...]) -> tuple[Chunk, ...]:
        """Every chunk's `synthetic` flag, as the registry decides it.

        A chunk claiming synthetic that the registry doesn't vouch for is
        refused outright — downgrading it silently to real would hide that
        someone tried."""
        vouched = [self.vouches_for(c) for c in chunks]
        pairs = list(zip(chunks, vouched, strict=True))
        claimed = [c.chunk_id for c, ok in pairs if c.synthetic and not ok]
        if claimed:
            raise SyntheticClaimRefused(
                f"{len(claimed)} chunk(s) marked synthetic but not in the operator's synthetic "
                f"registry (first: {claimed[0]!r}). An upload cannot mark its own content "
                "synthetic; only scripts/register_synthetic_fixtures.py can, by PR (issue #78)"
            )
        return tuple(c.model_copy(update={"synthetic": ok}) for c, ok in pairs)
