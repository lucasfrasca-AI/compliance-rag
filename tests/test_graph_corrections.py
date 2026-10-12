"""Cosmetic corrections in place (issue #120, the #93 design session's Q4,
ADR-0031): wording a reader sees may be corrected on a loaded node, only when
a person acknowledges exactly which items, and with the previous wording kept
on the node. A change of meaning is still a new id.

On the invented `test-act` fixture, on a local Neo4j (`NEO4J_TEST_URI`).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from compliance_rag.adapters.graph_schema import GraphWriteRefused, NodeRef
from compliance_rag.adapters.neo4j_graph import Neo4jGraph
from compliance_rag.graph.loader import CorrectionAcknowledgement, load_mappings
from compliance_rag.graph.mappings import MappingError, load_mapping_set
from tests.test_graph_loader import FIXTURE, SEP, fixture_document, load_test_corpus, write_yaml
from tests.test_neo4j_graph import cypher, needs_neo4j
from tests.test_neo4j_graph import graph as graph

FIXED = "Keep the invented records secure."


def ack(*ids: str) -> CorrectionAcknowledgement:
    return CorrectionAcknowledgement(frozenset(ids), by="Piero Frasca", reason="typo")


async def _loaded(graph: Neo4jGraph) -> None:
    await load_test_corpus(graph)
    await load_mappings(graph, load_mapping_set([FIXTURE]), today=SEP)


def _corrected(tmp_path: Path, **edits: str) -> Path:
    document = fixture_document()
    document["obligations"][2]["summary"] = edits.get("summary", FIXED)
    if "note" in edits:
        document["crosswalks"][0]["note"] = edits["note"]
    return write_yaml(tmp_path, document)


@needs_neo4j
async def test_a_wording_change_is_refused_unless_acknowledged(
    graph: Neo4jGraph, tmp_path: Path
) -> None:
    await _loaded(graph)
    with pytest.raises(MappingError, match=r"wording changed in loaded items \['OB-TEST-2'\]"):
        await load_mappings(graph, load_mapping_set([_corrected(tmp_path)]), today=SEP)
    (row,) = await cypher(
        graph, "MATCH (o:Obligation {stable_id:'OB-TEST-2'}) RETURN o.summary AS s"
    )
    assert row["s"] == "Destroy the invented records when no longer needed."


@needs_neo4j
async def test_an_acknowledged_correction_is_applied_and_audited(
    graph: Neo4jGraph, tmp_path: Path
) -> None:
    await _loaded(graph)
    before = await graph.counts()
    mappings = load_mapping_set([_corrected(tmp_path)])
    await load_mappings(graph, mappings, today=SEP, corrections=ack("OB-TEST-2"))
    (row,) = await cypher(
        graph,
        "MATCH (o:Obligation {stable_id:'OB-TEST-2'}) RETURN o.summary AS s, o.corrections AS c",
    )
    assert row["s"] == FIXED
    (entry,) = [json.loads(e) for e in row["c"]]
    assert entry["by"] == "Piero Frasca" and entry["reason"] == "typo"
    assert entry["previous"] == {"summary": "Destroy the invented records when no longer needed."}
    assert await graph.counts() == before  # nothing new: the same node, reworded
    # Done: the same YAML again needs no acknowledgement, and changes nothing.
    await load_mappings(graph, mappings, today=SEP)


@needs_neo4j
async def test_the_acknowledgement_must_be_exact(graph: Neo4jGraph, tmp_path: Path) -> None:
    await _loaded(graph)
    with pytest.raises(MappingError, match="acknowledged exactly"):
        await load_mappings(
            graph,
            load_mapping_set([_corrected(tmp_path)]),
            today=SEP,
            corrections=ack("OB-TEST-2", "OB-TEST-1B"),
        )


@needs_neo4j
async def test_a_change_of_meaning_is_refused_even_if_acknowledged(
    graph: Neo4jGraph, tmp_path: Path
) -> None:
    await _loaded(graph)
    document = fixture_document()
    document["obligations"][2]["requires"] = {"conditions": ["COND-TEST-LARGE"]}
    with pytest.raises(MappingError, match="supersede it with a new id"):
        await load_mappings(
            graph,
            load_mapping_set([write_yaml(tmp_path, document)]),
            today=SEP,
            corrections=ack("OB-TEST-2"),
        )


@needs_neo4j
async def test_a_crosswalk_note_is_corrected_on_its_edge(graph: Neo4jGraph, tmp_path: Path) -> None:
    await _loaded(graph)
    document = fixture_document()
    document["crosswalks"][0]["note"] = "Invented relation, reworded."
    await load_mappings(
        graph,
        load_mapping_set([write_yaml(tmp_path, document)]),
        today=SEP,
        corrections=ack("OB-TEST-1B"),
    )
    (row,) = await cypher(
        graph,
        "MATCH (:Obligation {stable_id:'OB-TEST-1B'})-[r:CROSSWALKS_TO]->() "
        "RETURN r.note AS n, r.corrections AS c",
    )
    assert row["n"] == "Invented relation, reworded." and len(row["c"]) == 1


@needs_neo4j
async def test_the_adapter_corrects_wording_only_and_names_who_and_why(graph: Neo4jGraph) -> None:
    await _loaded(graph)
    node = NodeRef("Obligation", "OB-TEST-2")
    with pytest.raises(GraphWriteRefused, match="meaning, not wording"):
        await graph.correct(node, {"instrument_id": "other-act"}, by="Piero Frasca", reason="r")
    with pytest.raises(GraphWriteRefused, match="who made it and why"):
        await graph.correct(node, {"summary": FIXED}, by=" ", reason="r")
