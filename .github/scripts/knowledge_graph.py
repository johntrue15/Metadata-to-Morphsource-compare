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

    def export_html(self, path: str, title: str = "AutoResearchClaw Knowledge Graph"):
        """Export an interactive HTML visualization using vis-network.js."""
        stats = self.stats()
        type_colors = {
            "Media": "#58a6ff",
            "Specimen": "#3fb950",
            "Paper": "#d29922",
            "Institution": "#f85149",
            "Taxon": "#bc8cff",
            "MediaList": "#79c0ff",
        }
        type_shapes = {
            "Media": "dot",
            "Specimen": "diamond",
            "Paper": "triangle",
            "Institution": "square",
            "Taxon": "hexagon",
            "MediaList": "star",
        }

        vis_nodes = []
        for n in self.nodes.values():
            vis_nodes.append({
                "id": n.id,
                "label": n.label[:40],
                "title": f"{n.type}: {n.label}\n{json.dumps(n.properties, default=str)[:200]}",
                "group": n.type,
                "color": type_colors.get(n.type, "#8b949e"),
                "shape": type_shapes.get(n.type, "dot"),
                "size": 12 if n.type in ("Media", "Taxon") else 18,
            })

        vis_edges = []
        for e in self.edges:
            vis_edges.append({
                "from": e.source,
                "to": e.target,
                "label": e.relation,
                "arrows": "to",
                "color": {"color": "#30363d", "opacity": 0.6},
                "font": {"size": 8, "color": "#8b949e"},
            })

        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{title}</title>
<script src="https://unpkg.com/vis-network@9.1.6/standalone/umd/vis-network.min.js"></script>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, sans-serif; background: #0d1117; color: #c9d1d9; }}
  #header {{ padding: 16px 24px; background: #161b22; border-bottom: 1px solid #30363d; }}
  h1 {{ font-size: 1.4em; color: #58a6ff; margin-bottom: 8px; }}
  .stats {{ color: #8b949e; font-size: 0.9em; }}
  .stats span {{ margin-right: 16px; }}
  #graph {{ width: 100%; height: calc(100vh - 120px); }}
  #legend {{ position: fixed; bottom: 16px; left: 16px; background: #161b22;
             border: 1px solid #30363d; border-radius: 8px; padding: 12px; z-index: 10; }}
  .legend-item {{ display: flex; align-items: center; gap: 8px; margin: 4px 0; font-size: 0.85em; }}
  .legend-dot {{ width: 12px; height: 12px; border-radius: 50%; }}
  #search {{ position: fixed; top: 70px; right: 16px; z-index: 10; }}
  #search input {{ background: #0d1117; border: 1px solid #30363d; color: #c9d1d9;
                   padding: 8px 12px; border-radius: 6px; width: 220px; font-size: 0.9em; }}
  #info {{ position: fixed; top: 70px; left: 16px; background: #161b22;
           border: 1px solid #30363d; border-radius: 8px; padding: 12px;
           max-width: 350px; font-size: 0.85em; z-index: 10; display: none; }}
  #info h3 {{ color: #58a6ff; margin-bottom: 6px; }}
  #info pre {{ white-space: pre-wrap; color: #8b949e; font-size: 0.8em; max-height: 200px; overflow-y: auto; }}
</style>
</head>
<body>
<div id="header">
  <h1>{title}</h1>
  <div class="stats">
    <span>{stats['media']} media</span>
    <span>{stats['specimens']} specimens</span>
    <span>{stats['papers']} papers</span>
    <span>{stats['institutions']} institutions</span>
    <span>{stats['taxa']} taxa</span>
    <span>{stats['total_edges']} connections</span>
  </div>
</div>
<div id="graph"></div>
<div id="search"><input type="text" id="searchBox" placeholder="Search nodes..." oninput="searchNodes(this.value)"></div>
<div id="legend">
  <div class="legend-item"><div class="legend-dot" style="background:#58a6ff"></div> Media</div>
  <div class="legend-item"><div class="legend-dot" style="background:#3fb950"></div> Specimen</div>
  <div class="legend-item"><div class="legend-dot" style="background:#d29922"></div> Paper</div>
  <div class="legend-item"><div class="legend-dot" style="background:#f85149"></div> Institution</div>
  <div class="legend-item"><div class="legend-dot" style="background:#bc8cff"></div> Taxon</div>
</div>
<div id="info"><h3 id="infoTitle"></h3><pre id="infoBody"></pre></div>
<script>
const nodesData = {json.dumps(vis_nodes, default=str)};
const edgesData = {json.dumps(vis_edges, default=str)};

const nodes = new vis.DataSet(nodesData);
const edges = new vis.DataSet(edgesData);

const container = document.getElementById("graph");
const data = {{ nodes: nodes, edges: edges }};
const options = {{
  physics: {{
    solver: "forceAtlas2Based",
    forceAtlas2Based: {{ gravitationalConstant: -30, springLength: 80, springConstant: 0.04 }},
    stabilization: {{ iterations: 200 }},
  }},
  interaction: {{ hover: true, tooltipDelay: 100, zoomView: true }},
  edges: {{ smooth: {{ type: "continuous" }}, font: {{ size: 8 }} }},
  nodes: {{ font: {{ size: 11, color: "#c9d1d9" }}, borderWidth: 1 }},
}};

const network = new vis.Network(container, data, options);

network.on("click", function(params) {{
  const info = document.getElementById("info");
  if (params.nodes.length > 0) {{
    const nodeId = params.nodes[0];
    const node = nodes.get(nodeId);
    document.getElementById("infoTitle").textContent = node.label;
    document.getElementById("infoBody").textContent = node.title || "";
    info.style.display = "block";
  }} else {{
    info.style.display = "none";
  }}
}});

function searchNodes(query) {{
  if (!query) {{ nodes.forEach(n => nodes.update({{id: n.id, opacity: 1}})); return; }}
  const q = query.toLowerCase();
  nodes.forEach(n => {{
    const match = n.label.toLowerCase().includes(q) || (n.title || "").toLowerCase().includes(q);
    nodes.update({{id: n.id, opacity: match ? 1 : 0.1}});
  }});
}}
</script>
</body>
</html>"""

        Path(path).write_text(html, encoding="utf-8")
        log.info("Exported interactive HTML graph to %s (%d nodes, %d edges)", path, len(self.nodes), len(self.edges))

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
