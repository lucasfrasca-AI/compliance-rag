"""Register the synthetic policy fixtures as synthetic — the operator path
(issue #78). Writes `corpus/synthetic/registry.json`: one hash per chunk of
the six Acme fixtures in `docs/research/fixtures/synthetic-policies/`, over
the owning tenant and the chunk's exact text.

This script and a PR are the only way content becomes synthetic. The write
path (`CorpusLifecycle`) derives every chunk's flag from the registry and
refuses a chunk that claims to be synthetic without being in it, so an upload
request cannot mark its own content synthetic. Re-run after editing a fixture
or changing the parser or chunker; `tests/test_synthetic_registry.py` fails
until the registry matches.

    uv run python scripts/register_synthetic_fixtures.py
"""

from __future__ import annotations

from compliance_rag.corpus.synthetic_registry import SYNTHETIC_REGISTRY_PATH, SyntheticRegistry
from compliance_rag.eval.golden_corpus import synthetic_fixture_chunks

SOURCE = "docs/research/fixtures/synthetic-policies/ (six Acme policies, tenant acme, v1)"


def main() -> None:
    chunks = synthetic_fixture_chunks()
    SyntheticRegistry.of(chunks, source=SOURCE).save()
    print(f"{len(chunks)} synthetic chunks registered -> {SYNTHETIC_REGISTRY_PATH}")


if __name__ == "__main__":
    main()
