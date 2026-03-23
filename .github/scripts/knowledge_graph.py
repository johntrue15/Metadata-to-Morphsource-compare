#!/usr/bin/env python3
"""
Knowledge graph builder for AutoResearchClaw.

Builds an incremental graph of connections between MorphoSource media,
specimens, papers, institutions, and taxa. Exports to JSON (Neo4j/Cytoscape
compatible), Mermaid diagrams, and summary statistics.

Usage:
    from knowledge_graph import KnowledgeGraph
    kg = KnowledgeGraph()
    kg.add_media_record(record)
    kg.export_json("knowledge_graph.json")
    mermaid = kg.to_mermaid()
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Set

from _helpers import safe_first

log = logging.getLogger("KnowledgeGraph")


@dataclass
class Node:
    id: str
    type: str
    label: str
    properties: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Edge:
    source: str
    target: str
    relation: str
    properties: Dict[str, Any] = field(default_factory=dict)


class KnowledgeGraph:
    """Incrementally built knowledge graph of MorphoSource records."""

    def __init__(self):
        self.nodes: Dict[str, Node] = {}
        self.edges: List[Edge] = []
        self._edge_set: Set[tuple] = set()

    # --- Node helpers ---

    def _add_node(self, node_id: str, node_type: str, label: str, **props) -> str:
        if node_id not in self.nodes:
            self.nodes[node_id] = Node(id=node_id, type=node_type, label=label, properties=props)
        else:
            self.nodes[node_id].properties.update(props)
        return node_id

    def _add_edge(self, source: str, target: str, relation: str, **props):
        key = (source, target, relation)
        if key not in self._edge_set:
            self._edge_set.add(key)
            self.edges.append(Edge(source=source, target=target, relation=relation, properties=props))

    # --- Add records ---

    def add_media_record(self, record: Dict[str, Any], media_list_id: str = ""):
        """Add a MorphoSource media record to the graph."""
        mid = safe_first(record.get("id"))
        if not mid:
            return

        title = safe_first(record.get("title"))
        mtype = safe_first(record.get("media_type"))
        modality = safe_first(record.get("modality"))
        visibility = safe_first(record.get("visibility"))

        media_id = self._add_node(
            f"media:{mid}", "Media", title or f"Media {mid}",
            media_id=mid, media_type=mtype, modality=modality, visibility=visibility,
        )

        # Parent media
        parent_id = safe_first(record.get("media_parent_id"))
        if parent_id:
            parent_nid = self._add_node(f"media:{parent_id}", "Media", f"Media {parent_id}")
            self._add_edge(media_id, parent_nid, "DERIVED_FROM")

        # Specimen
        specimen_id = safe_first(record.get("physical_object_id"))
        specimen_title = safe_first(record.get("physical_object_title"))
        if specimen_id:
            spec_nid = self._add_node(
                f"specimen:{specimen_id}", "Specimen", specimen_title or f"Specimen {specimen_id}",
                specimen_id=specimen_id,
            )
            self._add_edge(media_id, spec_nid, "BELONGS_TO")

            # Institution
            org = safe_first(record.get("physical_object_organization"))
            if org:
                org_nid = self._add_node(f"institution:{_slug(org)}", "Institution", org)
                self._add_edge(spec_nid, org_nid, "HELD_BY")

            # Taxonomy
            taxon_name = safe_first(record.get("physical_object_taxonomy_name"))
            if taxon_name:
                tax_nid = self._add_node(f"taxon:{_slug(taxon_name)}", "Taxon", taxon_name)
                self._add_edge(spec_nid, tax_nid, "IS_TAXON")

            # GBIF hierarchy
            gbif = record.get("physical_object_taxonomy_gbif", [])
            if isinstance(gbif, list) and len(gbif) >= 2:
                for rank_name in gbif[:4]:
                    if rank_name and rank_name != taxon_name:
                        rnid = self._add_node(f"taxon:{_slug(rank_name)}", "Taxon", rank_name)
                        self._add_edge(f"taxon:{_slug(taxon_name)}", rnid, "PART_OF_TAXONOMY")

        # DOIs / Citations
        for field_name in ("doi", "cite_as", "description"):
            text = safe_first(record.get(field_name))
            if text:
                for doi_match in re.finditer(r'10\.\d{4,}/[^\s,;"\'\]>)]+', text):
                    doi = doi_match.group().rstrip(".")
                    paper_nid = self._add_node(f"paper:{doi}", "Paper", doi, doi=doi)
                    self._add_edge(media_id, paper_nid, "CITED_IN")
                    if specimen_id:
                        self._add_edge(paper_nid, f"specimen:{specimen_id}", "REFERENCES")

        # Media list
        if media_list_id:
            list_nid = self._add_node(f"list:{media_list_id}", "MediaList", f"List {media_list_id}")
            self._add_edge(media_id, list_nid, "IN_LIST")

    def add_citation(self, doi: str, title: str = "", authors: str = "", year: str = "",
                     journal: str = "", linked_media: str = "", linked_specimen: str = ""):
        """Add a paper citation to the graph."""
        paper_nid = self._add_node(
            f"paper:{doi}", "Paper", title or doi,
            doi=doi, authors=authors, year=year, journal=journal,
        )
        if linked_media:
            self._add_edge(f"media:{linked_media}", paper_nid, "CITED_IN")
        if linked_specimen:
            self._add_edge(paper_nid, f"specimen:{linked_specimen}", "REFERENCES")

    def add_analysis_data(self, media_id: str, analysis: Dict[str, Any]):
        """Enrich a media node with Slicer analysis data."""
        nid = f"media:{media_id}"
        if nid in self.nodes:
            self.nodes[nid].properties["analyzed"] = True
            self.nodes[nid].properties["vertices"] = analysis.get("vertices")
            self.nodes[nid].properties["volume_mm3"] = analysis.get("volume_mm3")
            self.nodes[nid].properties["surface_area_mm2"] = analysis.get("surface_area_mm2")
            self.nodes[nid].properties["distances"] = analysis.get("distances")

    # --- Verification ---

    def verify_connections(self) -> Dict[str, Any]:
        """Verify graph integrity and find interesting patterns."""
        media_nodes = {n.id for n in self.nodes.values() if n.type == "Media"}
        paper_nodes = {n.id for n in self.nodes.values() if n.type == "Paper"}

        connected_media = set()
        for e in self.edges:
            if e.source in media_nodes:
                connected_media.add(e.source)
            if e.target in media_nodes:
                connected_media.add(e.target)

        orphaned = media_nodes - connected_media

        # Papers referencing multiple specimens
        paper_specimens = defaultdict(set)
        for e in self.edges:
            if e.relation == "REFERENCES" and e.source in paper_nodes:
                paper_specimens[e.source].add(e.target)
        multi_ref_papers = {p: list(specs) for p, specs in paper_specimens.items() if len(specs) > 1}

        # Taxa shared across institutions
        taxon_institutions = defaultdict(set)
        for e in self.edges:
            if e.relation == "IS_TAXON":
                spec = e.source
                taxon = e.target
                for e2 in self.edges:
                    if e2.source == spec and e2.relation == "HELD_BY":
                        taxon_institutions[taxon].add(e2.target)
        shared_taxa = {t: list(insts) for t, insts in taxon_institutions.items() if len(insts) > 1}

        return {
            "total_nodes": len(self.nodes),
            "total_edges": len(self.edges),
            "orphaned_media": list(orphaned),
            "multi_reference_papers": multi_ref_papers,
            "taxa_shared_across_institutions": shared_taxa,
            "node_counts": dict(Counter(n.type for n in self.nodes.values())),
            "edge_counts": dict(Counter(e.relation for e in self.edges)),
        }

    # --- Statistics ---

    def stats(self) -> Dict[str, int]:
        counts = Counter(n.type for n in self.nodes.values())
        return {
            "media": counts.get("Media", 0),
            "specimens": counts.get("Specimen", 0),
            "papers": counts.get("Paper", 0),
            "institutions": counts.get("Institution", 0),
            "taxa": counts.get("Taxon", 0),
            "media_lists": counts.get("MediaList", 0),
            "total_nodes": len(self.nodes),
            "total_edges": len(self.edges),
        }

    def summary(self) -> str:
        s = self.stats()
        return (
            f"{s['media']} media, {s['specimens']} specimens, "
            f"{s['papers']} papers, {s['institutions']} institutions, "
            f"{s['taxa']} taxa, {s['total_edges']} connections"
        )

    # --- Export ---

    def export_json(self, path: str):
        """Export as JSON (Neo4j/Cytoscape compatible)."""
        data = {
            "nodes": [
                {"id": n.id, "type": n.type, "label": n.label, **n.properties}
                for n in self.nodes.values()
            ],
            "edges": [
                {"source": e.source, "target": e.target, "relation": e.relation, **e.properties}
                for e in self.edges
            ],
            "stats": self.stats(),
        }
        Path(path).write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        log.info("Exported graph to %s (%d nodes, %d edges)", path, len(self.nodes), len(self.edges))

    def to_mermaid(self, max_nodes: int = 30) -> str:
        """Generate a Mermaid diagram of the graph (truncated for readability)."""
        lines = ["graph LR"]

        type_shapes = {
            "Media": ("([", "])"),
            "Specimen": ("[[", "]]"),
            "Paper": ("{{", "}}"),
            "Institution": ("[(", ")]"),
            "Taxon": ("((", "))"),
            "MediaList": ("[/", "/]"),
        }

        included = set()
        for node in list(self.nodes.values())[:max_nodes]:
            safe_id = _mermaid_id(node.id)
            safe_label = node.label[:30].replace('"', "'")
            o, c = type_shapes.get(node.type, ("[", "]"))
            lines.append(f'    {safe_id}{o}"{safe_label}"{c}')
            included.add(node.id)

        for edge in self.edges:
            if edge.source in included and edge.target in included:
                s = _mermaid_id(edge.source)
                t = _mermaid_id(edge.target)
                lines.append(f"    {s} -->|{edge.relation}| {t}")

        if len(self.nodes) > max_nodes:
            lines.append(f"    truncated[\"... +{len(self.nodes) - max_nodes} more nodes\"]")

        return "\n".join(lines)

    def to_dict(self) -> Dict:
        return {
            "nodes": {nid: asdict(n) for nid, n in self.nodes.items()},
            "edges": [asdict(e) for e in self.edges],
            "stats": self.stats(),
        }


# --- Helpers ---

def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower().strip())[:60].strip("_")


def _mermaid_id(node_id: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "_", node_id)


# ---------------------------------------------------------------------------
# Add records from search results
# ---------------------------------------------------------------------------

def build_graph_from_search_results(search_results: List[Dict], media_list_id: str = "") -> KnowledgeGraph:
    """Build a knowledge graph from AutoResearchClaw search results."""
    kg = KnowledgeGraph()

    for result in search_results:
        data = result.get("result_data", {})
        response = data.get("response", data)
        for key in ("media", "physical_objects"):
            items = response.get(key, [])
            for item in items:
                kg.add_media_record(item, media_list_id=media_list_id)

    log.info("Built graph: %s", kg.summary())
    return kg


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    kg = KnowledgeGraph()

    # Add test data
    kg.add_media_record({
        "id": ["000769445"], "title": ["Skull"], "media_type": ["Mesh"],
        "modality": ["MicroNanoXRayComputedTomography"], "visibility": ["open"],
        "media_parent_id": ["000408242"],
        "physical_object_id": ["000408235"],
        "physical_object_title": ["uf:herp:191369"],
        "physical_object_organization": ["FLMNH Division of Herpetology"],
        "physical_object_taxonomy_name": ["Chamaeleo calyptratus"],
        "physical_object_taxonomy_gbif": ["Animalia", "Chordata", "Reptilia", "Squamata", "Chamaeleonidae", "Chamaeleo", "calyptratus"],
        "cite_as": ["cite Almecija et al., 2024 SciData (doi: https://doi.org/10.1038/s41597-024-04261-5)"],
        "doi": ["10.17602/M2/M769445"],
    })

    kg.add_media_record({
        "id": ["000408242"], "title": ["Skull CT Stack"], "media_type": ["Volumetric Image Series"],
        "modality": ["MicroNanoXRayComputedTomography"], "visibility": ["open"],
        "physical_object_id": ["000408235"],
        "physical_object_title": ["uf:herp:191369"],
        "physical_object_organization": ["FLMNH Division of Herpetology"],
        "physical_object_taxonomy_name": ["Chamaeleo calyptratus"],
    })

    kg.add_citation(
        "10.1038/s41597-024-04261-5",
        title="Primate Phenotypes", authors="Almecija et al.", year="2024",
        journal="Scientific Data", linked_media="000769445", linked_specimen="000408235",
    )

    print(f"\nGraph: {kg.summary()}")
    print(f"\nVerification:")
    v = kg.verify_connections()
    print(json.dumps(v, indent=2))

    print(f"\nMermaid diagram:\n")
    print(kg.to_mermaid())

    kg.export_json("/tmp/test_knowledge_graph.json")
    print(f"\nExported to /tmp/test_knowledge_graph.json")
