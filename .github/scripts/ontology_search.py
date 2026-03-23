#!/usr/bin/env python3
"""
DRAGON-AI style ontology-guided search for MorphoSource.

Uses UBERON (cross-species anatomy ontology) via the EBI OLS API to
expand anatomy terms into synonyms, parent terms, and related structures.
Maps ontology terms to MorphoSource search parameters.

Usage:
    from ontology_search import expand_search_terms, lookup_anatomy_term
    terms = expand_search_terms("skull")
    # -> ["skull", "cranium", "neurocranium", "calvaria", "braincase"]
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional

log = logging.getLogger("OntologySearch")

OLS_API = "https://www.ebi.ac.uk/ols4/api"


# ---------------------------------------------------------------------------
# UBERON lookup via OLS REST API
# ---------------------------------------------------------------------------


@dataclass
class OntologyTerm:
    term_id: str = ""
    label: str = ""
    description: str = ""
    synonyms: List[str] = field(default_factory=list)
    parents: List[str] = field(default_factory=list)
    related: List[str] = field(default_factory=list)
    ontology: str = "uberon"

    def all_labels(self) -> List[str]:
        labels = [self.label] + self.synonyms + self.parents + self.related
        return list(dict.fromkeys(l.lower() for l in labels if l))

    def to_dict(self):
        return asdict(self)


def lookup_anatomy_term(term: str, ontology: str = "uberon") -> Optional[OntologyTerm]:
    """Query the EBI OLS API for a UBERON anatomy term."""
    params = urllib.parse.urlencode({
        "q": term,
        "ontology": ontology,
        "rows": 5,
        "exact": "false",
    })
    url = f"{OLS_API}/search?{params}"
    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "User-Agent": "AutoResearchClaw/1.0",
    })

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        log.warning("OLS lookup failed for '%s': %s", term, exc)
        return None

    docs = data.get("response", {}).get("docs", [])
    if not docs:
        log.debug("No OLS results for '%s'", term)
        return None

    best = docs[0]
    result = OntologyTerm(
        term_id=best.get("obo_id", best.get("short_form", "")),
        label=best.get("label", term),
        description=((best.get("description") or [""])[0] if best.get("description") else ""),
        synonyms=best.get("synonym", [])[:10],
        ontology=ontology,
    )

    log.info("OLS: '%s' -> %s (%s), %d synonyms",
             term, result.label, result.term_id, len(result.synonyms))
    return result


def get_term_hierarchy(term_id: str, ontology: str = "uberon") -> List[str]:
    """Get parent terms from the ontology hierarchy."""
    encoded_iri = urllib.parse.quote(
        urllib.parse.quote(f"http://purl.obolibrary.org/obo/{term_id.replace(':', '_')}", safe=""),
        safe=""
    )
    url = f"{OLS_API}/ontologies/{ontology}/terms/{encoded_iri}/parents"
    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "User-Agent": "AutoResearchClaw/1.0",
    })

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        terms = data.get("_embedded", {}).get("terms", [])
        return [t.get("label", "") for t in terms if t.get("label")]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# MorphoSource-specific term mapping
# ---------------------------------------------------------------------------

MORPHOSOURCE_PART_MAP: Dict[str, List[str]] = {
    "skull": ["Skull", "Cranium", "Cranial", "Head"],
    "cranium": ["Skull", "Cranium", "Cranial"],
    "mandible": ["Mandible", "Jaw", "Dentary"],
    "tibia": ["Tibia", "Leg", "Hindlimb"],
    "femur": ["Femur", "Thigh", "Hindlimb"],
    "humerus": ["Humerus", "Arm", "Forelimb"],
    "vertebra": ["Vertebra", "Vertebrae", "Spine", "Vertebral"],
    "pelvis": ["Pelvis", "Innominate", "Os coxa"],
    "scapula": ["Scapula", "Shoulder"],
    "rib": ["Rib", "Ribs", "Thorax"],
    "sternum": ["Sternum"],
    "orbit": ["Skull", "Cranium", "Orbit", "Orbital"],
    "tooth": ["Tooth", "Teeth", "Dental", "Dentition"],
    "endocast": ["Endocast", "Braincase", "Endocranial"],
    "brain": ["Endocast", "Braincase", "Brain"],
    "hand": ["Hand", "Manus", "Metacarpal", "Manual"],
    "foot": ["Foot", "Pes", "Metatarsal", "Pedal"],
    "patella": ["Patella", "Kneecap"],
    "clavicle": ["Clavicle"],
    "radius": ["Radius", "Forearm"],
    "ulna": ["Ulna", "Forearm"],
    "fibula": ["Fibula", "Leg"],
    "sacrum": ["Sacrum", "Sacral"],
    "atlas": ["Atlas", "Cervical", "Vertebra"],
    "axis": ["Axis", "Cervical", "Vertebra"],
}

ANATOMY_SEARCH_KEYWORDS: Dict[str, List[str]] = {
    "skull": ["skull", "cranium", "cranial", "neurocranium", "calvaria", "braincase"],
    "orbit": ["orbit", "orbital", "eye socket", "optic", "periorbital"],
    "mandible": ["mandible", "jaw", "dentary", "ramus"],
    "tibia": ["tibia", "tibial", "shinbone", "crus"],
    "femur": ["femur", "femoral", "thigh bone"],
    "humerus": ["humerus", "humeral", "upper arm"],
    "vertebra": ["vertebra", "vertebrae", "vertebral", "spine", "spinal"],
    "pelvis": ["pelvis", "pelvic", "innominate", "ilium", "ischium", "pubis"],
    "tooth": ["tooth", "teeth", "dental", "dentition", "molar", "incisor"],
    "endocast": ["endocast", "endocranial", "brain cavity", "cranial cavity"],
}


def expand_search_terms(term: str, use_ontology: bool = True) -> List[str]:
    """Expand an anatomy term into a list of search-relevant synonyms.

    Combines local MorphoSource mapping with UBERON ontology lookups.
    """
    term_lower = term.lower().strip()
    terms = {term_lower}

    # Local mapping first (fast, no API call)
    if term_lower in ANATOMY_SEARCH_KEYWORDS:
        terms.update(ANATOMY_SEARCH_KEYWORDS[term_lower])

    parts = MORPHOSOURCE_PART_MAP.get(term_lower, [])
    terms.update(p.lower() for p in parts)

    # UBERON lookup for additional synonyms
    if use_ontology:
        onto_term = lookup_anatomy_term(term)
        if onto_term:
            terms.update(onto_term.all_labels())
            if onto_term.term_id:
                parents = get_term_hierarchy(onto_term.term_id)
                terms.update(p.lower() for p in parents[:5])

    result = sorted(terms - {""})
    log.info("Expanded '%s' -> %d terms: %s", term, len(result), result[:8])
    return result


def get_morphosource_part_terms(term: str) -> List[str]:
    """Get MorphoSource 'part' field values for an anatomy term."""
    term_lower = term.lower().strip()
    parts = MORPHOSOURCE_PART_MAP.get(term_lower, [])
    if not parts:
        parts = [term.capitalize()]
    return parts


def enrich_query_with_ontology(query: str) -> str:
    """Take a search query and enrich it with ontology synonyms.

    Detects anatomy terms in the query and adds UBERON synonyms.
    """
    query_lower = query.lower()
    enrichments = []

    for term in ANATOMY_SEARCH_KEYWORDS:
        if term in query_lower:
            expanded = expand_search_terms(term, use_ontology=False)
            new_terms = [t for t in expanded if t not in query_lower]
            if new_terms:
                enrichments.extend(new_terms[:3])

    if enrichments:
        enriched = f"{query} {' '.join(enrichments[:5])}"
        log.debug("Enriched query: '%s' -> '%s'", query, enriched)
        return enriched
    return query


# ---------------------------------------------------------------------------
# CLI for testing
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    term = sys.argv[1] if len(sys.argv) > 1 else "skull"
    print(f"\nLooking up: '{term}'")
    print("=" * 50)

    onto = lookup_anatomy_term(term)
    if onto:
        print(f"  ID: {onto.term_id}")
        print(f"  Label: {onto.label}")
        print(f"  Description: {onto.description[:100]}")
        print(f"  Synonyms: {onto.synonyms}")
        if onto.term_id:
            parents = get_term_hierarchy(onto.term_id)
            print(f"  Parents: {parents}")

    print(f"\nExpanded search terms:")
    expanded = expand_search_terms(term)
    for t in expanded:
        print(f"  - {t}")

    print(f"\nMorphoSource part terms:")
    parts = get_morphosource_part_terms(term)
    for p in parts:
        print(f"  - {p}")

    print(f"\nEnriched query test:")
    test_q = f"Chamaeleo calyptratus {term} CT scan"
    enriched = enrich_query_with_ontology(test_q)
    print(f"  Original: {test_q}")
    print(f"  Enriched: {enriched}")
