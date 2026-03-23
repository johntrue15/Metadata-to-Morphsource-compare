#!/usr/bin/env python3
"""
Citation and paper extraction from MorphoSource records.

Parses cite_as, doi, funding, and description fields from MorphoSource
API records to extract DOIs and fetch paper metadata from CrossRef.

Usage:
    from citation_extractor import extract_citations
    papers = extract_citations(media_record)
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

log = logging.getLogger("CitationExtractor")

CROSSREF_API = "https://api.crossref.org/works"
DOI_PATTERN = re.compile(r'10\.\d{4,}/[^\s,;"\'\]>)]+')


@dataclass
class Citation:
    doi: str = ""
    title: str = ""
    authors: str = ""
    journal: str = ""
    year: str = ""
    url: str = ""
    source_field: str = ""
    abstract: str = ""

    def to_dict(self):
        return asdict(self)

    def short(self):
        return f"{self.authors[:40]} ({self.year}) {self.title[:80]}"


# ---------------------------------------------------------------------------
# DOI extraction from record fields
# ---------------------------------------------------------------------------


def _first(value: Any) -> str:
    if isinstance(value, list):
        return str(value[0]) if value else ""
    return str(value) if value is not None else ""


def extract_dois_from_record(record: Dict[str, Any]) -> List[Dict[str, str]]:
    """Extract DOIs from all text fields in a MorphoSource record."""
    doi_sources = []
    seen = set()

    fields_to_check = [
        ("doi", record.get("doi")),
        ("cite_as", record.get("cite_as")),
        ("description", record.get("description")),
        ("funding", record.get("funding")),
        ("short_description", record.get("short_description")),
        ("external_identifier", record.get("external_identifier")),
    ]

    for field_name, value in fields_to_check:
        text = _first(value) if value else ""
        if not text:
            continue
        for match in DOI_PATTERN.finditer(text):
            doi = match.group().rstrip(".")
            if doi not in seen:
                seen.add(doi)
                doi_sources.append({"doi": doi, "source_field": field_name})

    return doi_sources


# ---------------------------------------------------------------------------
# CrossRef API lookup
# ---------------------------------------------------------------------------


def fetch_crossref_metadata(doi: str) -> Optional[Dict[str, Any]]:
    """Fetch paper metadata from CrossRef API by DOI."""
    url = f"{CROSSREF_API}/{urllib.parse.quote(doi, safe='')}"
    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "User-Agent": "AutoResearchClaw/1.0 (mailto:research@morphosource-agent.dev)",
    })

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data.get("message", {})
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            log.debug("DOI not found in CrossRef: %s", doi)
        else:
            log.warning("CrossRef lookup failed for %s: HTTP %d", doi, exc.code)
        return None
    except Exception as exc:
        log.warning("CrossRef lookup failed for %s: %s", doi, exc)
        return None


def _parse_crossref(cr: Dict[str, Any], doi: str, source_field: str) -> Citation:
    """Parse CrossRef response into a Citation object."""
    # Authors
    authors_list = cr.get("author", [])
    author_strs = []
    for a in authors_list[:5]:
        name = f"{a.get('family', '')} {a.get('given', '')}".strip()
        if name:
            author_strs.append(name)
    if len(authors_list) > 5:
        author_strs.append("et al.")
    authors = ", ".join(author_strs)

    # Title
    titles = cr.get("title", [])
    title = titles[0] if titles else ""

    # Journal
    container = cr.get("container-title", [])
    journal = container[0] if container else ""

    # Year
    date_parts = cr.get("published-print", cr.get("published-online", cr.get("created", {})))
    year = ""
    if isinstance(date_parts, dict):
        parts = date_parts.get("date-parts", [[]])
        if parts and parts[0]:
            year = str(parts[0][0])

    # Abstract
    abstract = cr.get("abstract", "")
    if abstract:
        abstract = re.sub(r"<[^>]+>", "", abstract).strip()[:500]

    # URL
    url = cr.get("URL", f"https://doi.org/{doi}")

    return Citation(
        doi=doi, title=title, authors=authors, journal=journal,
        year=year, url=url, source_field=source_field, abstract=abstract,
    )


# ---------------------------------------------------------------------------
# Main extraction function
# ---------------------------------------------------------------------------


def extract_citations(record: Dict[str, Any], fetch_metadata: bool = True) -> List[Citation]:
    """Extract all citations from a MorphoSource record.

    Parses DOIs from record fields and optionally fetches metadata from CrossRef.
    """
    doi_sources = extract_dois_from_record(record)
    citations = []

    for ds in doi_sources:
        doi = ds["doi"]
        source = ds["source_field"]

        if fetch_metadata:
            cr = fetch_crossref_metadata(doi)
            if cr:
                citation = _parse_crossref(cr, doi, source)
                citations.append(citation)
                log.info("Citation: %s", citation.short())
                continue

        citations.append(Citation(doi=doi, source_field=source, url=f"https://doi.org/{doi}"))

    # Also extract citation text even without DOIs
    cite_as = _first(record.get("cite_as"))
    if cite_as and not any(c.doi for c in citations):
        citations.append(Citation(
            title=cite_as[:200],
            source_field="cite_as",
            authors=cite_as.split(" provided")[0] if " provided" in cite_as else "",
        ))

    log.info("Extracted %d citations from record", len(citations))
    return citations


def extract_citations_from_search_results(search_results: List[Dict]) -> List[Citation]:
    """Extract citations from multiple search result records."""
    all_citations = []
    seen_dois = set()

    for result in search_results:
        data = result.get("result_data", {})
        response = data.get("response", data)

        for key in ("media", "physical_objects"):
            items = response.get(key, [])
            for item in items:
                for citation in extract_citations(item, fetch_metadata=True):
                    if citation.doi and citation.doi in seen_dois:
                        continue
                    if citation.doi:
                        seen_dois.add(citation.doi)
                    all_citations.append(citation)

    log.info("Extracted %d unique citations from %d search results",
             len(all_citations), len(search_results))
    return all_citations


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    test_record = {
        "doi": ["10.17602/M2/M433745"],
        "cite_as": [
            "When re-using this media, please cite Almecija et al., 2024 SciData "
            "(doi: https://doi.org/10.1038/s41597-024-04261-5)."
        ],
        "description": ["See also Smith et al. 2023 (10.1002/jmor.21234) for methodology."],
        "funding": ["NSF DBI-1661386"],
    }

    doi_arg = sys.argv[1] if len(sys.argv) > 1 else None
    if doi_arg:
        test_record = {"doi": [doi_arg]}

    print("Extracting citations from test record...")
    citations = extract_citations(test_record)
    for i, c in enumerate(citations, 1):
        print(f"\n{i}. {c.short()}")
        print(f"   DOI: {c.doi}")
        print(f"   Journal: {c.journal}")
        print(f"   Source: {c.source_field}")
        if c.abstract:
            print(f"   Abstract: {c.abstract[:150]}...")

    with open("/tmp/citations_test.json", "w") as f:
        json.dump([c.to_dict() for c in citations], f, indent=2)
    print(f"\nSaved to /tmp/citations_test.json")
