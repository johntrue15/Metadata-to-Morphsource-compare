#!/usr/bin/env python3
"""
Literature search module for AutoResearchClaw.

Queries PubMed (NCBI E-utilities) and Google Scholar for open-access
papers related to a specimen's taxonomy, anatomy, and research topic.
Returns structured results: title, abstract, DOI, year, relevance.

Usage:
    from literature_search import search_literature
    papers = search_literature(
        taxon="Chamaeleo calyptratus",
        anatomy_terms=["skull", "optic nerve", "cranial morphology"],
        research_topic="optic nerve specialization in chameleons",
    )
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from typing import List, Optional

log = logging.getLogger("LitSearch")

PUBMED_SEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
PUBMED_FETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
SCHOLAR_SEARCH = "https://scholar.google.com/scholar"

HEADERS = {
    "User-Agent": "AutoResearchClaw/1.0 (morphosource-research-agent; contact: research@example.com)",
    "Accept": "application/xml, text/html, */*",
}


@dataclass
class Paper:
    title: str
    authors: str = ""
    abstract: str = ""
    year: str = ""
    doi: str = ""
    pmid: str = ""
    journal: str = ""
    source: str = ""
    url: str = ""
    relevance_score: float = 0.0
    keywords: List[str] = field(default_factory=list)

    def to_dict(self):
        return asdict(self)

    def short(self):
        return f"{self.authors[:40]} ({self.year}) {self.title[:80]}"


# ---------------------------------------------------------------------------
# PubMed via NCBI E-utilities (free, no API key needed for low volume)
# ---------------------------------------------------------------------------


def _pubmed_search(query: str, max_results: int = 15) -> List[str]:
    """Search PubMed and return PMIDs."""
    params = urllib.parse.urlencode({
        "db": "pubmed",
        "term": query,
        "retmax": max_results,
        "retmode": "xml",
        "sort": "relevance",
    })
    url = f"{PUBMED_SEARCH}?{params}"
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            xml_data = resp.read().decode("utf-8")
        root = ET.fromstring(xml_data)
        ids = [id_elem.text for id_elem in root.findall(".//Id") if id_elem.text]
        log.info("PubMed search '%s' → %d results", query[:60], len(ids))
        return ids
    except Exception as exc:
        log.warning("PubMed search failed: %s", exc)
        return []


def _pubmed_fetch(pmids: List[str]) -> List[Paper]:
    """Fetch paper details from PubMed by PMIDs."""
    if not pmids:
        return []
    params = urllib.parse.urlencode({
        "db": "pubmed",
        "id": ",".join(pmids),
        "retmode": "xml",
        "rettype": "abstract",
    })
    url = f"{PUBMED_FETCH}?{params}"
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            xml_data = resp.read().decode("utf-8")
    except Exception as exc:
        log.warning("PubMed fetch failed: %s", exc)
        return []

    papers = []
    root = ET.fromstring(xml_data)
    for article in root.findall(".//PubmedArticle"):
        try:
            medline = article.find(".//MedlineCitation")
            art = medline.find(".//Article") if medline else None
            if art is None:
                continue

            title_elem = art.find(".//ArticleTitle")
            title = "".join(title_elem.itertext()) if title_elem is not None else ""

            abstract_parts = []
            for abs_text in art.findall(".//Abstract/AbstractText"):
                label = abs_text.get("Label", "")
                text = "".join(abs_text.itertext())
                if label:
                    abstract_parts.append(f"{label}: {text}")
                else:
                    abstract_parts.append(text)
            abstract = " ".join(abstract_parts)

            authors_list = []
            for author in art.findall(".//AuthorList/Author"):
                last = author.findtext("LastName", "")
                init = author.findtext("Initials", "")
                if last:
                    authors_list.append(f"{last} {init}".strip())
            authors = ", ".join(authors_list[:5])
            if len(authors_list) > 5:
                authors += " et al."

            year = ""
            pub_date = art.find(".//Journal/JournalIssue/PubDate")
            if pub_date is not None:
                year = pub_date.findtext("Year", "")
                if not year:
                    medline_date = pub_date.findtext("MedlineDate", "")
                    if medline_date:
                        year = medline_date[:4]

            journal = art.findtext(".//Journal/Title", "")

            pmid = medline.findtext(".//PMID", "") if medline else ""

            doi = ""
            for eid in article.findall(".//ArticleIdList/ArticleId"):
                if eid.get("IdType") == "doi":
                    doi = eid.text or ""
                    break

            mesh_terms = []
            for mesh in medline.findall(".//MeshHeadingList/MeshHeading/DescriptorName") if medline else []:
                if mesh.text:
                    mesh_terms.append(mesh.text)

            paper = Paper(
                title=title.strip(),
                authors=authors,
                abstract=abstract.strip(),
                year=year,
                doi=doi,
                pmid=pmid,
                journal=journal,
                source="PubMed",
                url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else "",
                keywords=mesh_terms[:10],
            )
            papers.append(paper)
        except Exception as exc:
            log.debug("Error parsing PubMed article: %s", exc)
            continue

    log.info("PubMed fetch: %d papers parsed", len(papers))
    return papers


def search_pubmed(query: str, max_results: int = 15) -> List[Paper]:
    """Search PubMed and return parsed papers."""
    pmids = _pubmed_search(query, max_results)
    if not pmids:
        return []
    time.sleep(0.5)
    return _pubmed_fetch(pmids)


# ---------------------------------------------------------------------------
# Google Scholar (scraping fallback — rate-limited, use sparingly)
# ---------------------------------------------------------------------------


def search_scholar(query: str, max_results: int = 10) -> List[Paper]:
    """Search Google Scholar via scraping. Returns basic metadata."""
    params = urllib.parse.urlencode({"q": query, "num": max_results, "hl": "en"})
    url = f"{SCHOLAR_SEARCH}?{params}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Accept": "text/html",
    })

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as exc:
        log.warning("Scholar search failed: %s", exc)
        return []

    papers = []
    # Parse result blocks — each <div class="gs_r gs_or gs_scl">
    # Title is in <h3 class="gs_rt"><a href="...">Title</a></h3>
    title_pattern = re.compile(r'<h3[^>]*class="gs_rt"[^>]*>.*?<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>', re.DOTALL)
    snippet_pattern = re.compile(r'<div[^>]*class="gs_rs"[^>]*>(.*?)</div>', re.DOTALL)
    author_pattern = re.compile(r'<div[^>]*class="gs_a"[^>]*>(.*?)</div>', re.DOTALL)

    titles = title_pattern.findall(html)
    snippets = snippet_pattern.findall(html)
    authors_raw = author_pattern.findall(html)

    for i, (href, raw_title) in enumerate(titles[:max_results]):
        title = re.sub(r"<[^>]+>", "", raw_title).strip()
        snippet = re.sub(r"<[^>]+>", "", snippets[i]).strip() if i < len(snippets) else ""
        author_line = re.sub(r"<[^>]+>", "", authors_raw[i]).strip() if i < len(authors_raw) else ""

        year_match = re.search(r"\b(19|20)\d{2}\b", author_line)
        year = year_match.group() if year_match else ""
        authors = author_line.split(" - ")[0].strip() if " - " in author_line else author_line

        papers.append(Paper(
            title=title,
            authors=authors[:80],
            abstract=snippet,
            year=year,
            source="GoogleScholar",
            url=href if href.startswith("http") else "",
        ))

    log.info("Scholar search '%s' → %d results", query[:60], len(papers))
    return papers


# ---------------------------------------------------------------------------
# Unified search with auto-generated queries
# ---------------------------------------------------------------------------


def build_queries(
    taxon: str = "",
    anatomy_terms: Optional[List[str]] = None,
    research_topic: str = "",
    layer1_data: Optional[dict] = None,
) -> List[str]:
    """Generate search queries from specimen data and research context."""
    queries = []
    anatomy_terms = anatomy_terms or []

    if taxon and anatomy_terms:
        queries.append(f'"{taxon}" {" ".join(anatomy_terms[:3])} morphometry')
        queries.append(f'"{taxon}" skull landmarks geometric morphometrics')

    if taxon:
        genus = taxon.split()[0]
        queries.append(f"{genus} cranial morphology CT scan")
        queries.append(f'"{taxon}" skull anatomy')

    if research_topic:
        queries.append(research_topic)

    if anatomy_terms:
        queries.append(f"reptile {' '.join(anatomy_terms[:2])} landmark protocol")

    if layer1_data:
        specimen_type = layer1_data.get("specimen", {}).get("element", "")
        if "skull" in specimen_type.lower():
            queries.append(f"{taxon.split()[0] if taxon else 'lizard'} skull landmark placement protocol")
            queries.append(f"squamate cranial morphometrics geometric landmark")

    # Deduplicate while preserving order
    seen = set()
    unique = []
    for q in queries:
        q_lower = q.lower().strip()
        if q_lower not in seen and q_lower:
            seen.add(q_lower)
            unique.append(q)

    return unique[:8]


def search_literature(
    taxon: str = "",
    anatomy_terms: Optional[List[str]] = None,
    research_topic: str = "",
    layer1_data: Optional[dict] = None,
    max_pubmed: int = 15,
    max_scholar: int = 8,
) -> List[Paper]:
    """Run a comprehensive literature search using PubMed and Google Scholar.

    Returns a deduplicated, relevance-scored list of papers.
    """
    queries = build_queries(taxon, anatomy_terms, research_topic, layer1_data)
    log.info("Literature search with %d queries", len(queries))

    all_papers: List[Paper] = []
    seen_titles = set()

    for i, query in enumerate(queries):
        log.info("Query %d/%d: %s", i + 1, len(queries), query)

        # PubMed
        pm_papers = search_pubmed(query, max_results=max_pubmed)
        for p in pm_papers:
            key = p.title.lower().strip()[:80]
            if key not in seen_titles:
                seen_titles.add(key)
                p.relevance_score = 1.0 - (i * 0.1)
                all_papers.append(p)
        time.sleep(0.5)

        # Scholar (only first few queries to avoid rate limiting)
        if i < 3:
            gs_papers = search_scholar(query, max_results=max_scholar)
            for p in gs_papers:
                key = p.title.lower().strip()[:80]
                if key not in seen_titles:
                    seen_titles.add(key)
                    p.relevance_score = 0.8 - (i * 0.1)
                    all_papers.append(p)
            time.sleep(2)

    # Sort by relevance then year
    all_papers.sort(key=lambda p: (-p.relevance_score, -(int(p.year) if p.year.isdigit() else 0)))

    log.info("Total unique papers found: %d", len(all_papers))
    return all_papers


# ---------------------------------------------------------------------------
# CLI for standalone testing
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    taxon = sys.argv[1] if len(sys.argv) > 1 else "Chamaeleo calyptratus"
    topic = sys.argv[2] if len(sys.argv) > 2 else "optic nerve morphology chameleon skull"

    papers = search_literature(
        taxon=taxon,
        anatomy_terms=["skull", "cranial", "optic nerve", "orbit", "morphology"],
        research_topic=topic,
    )

    print(f"\n{'='*70}")
    print(f"Found {len(papers)} papers")
    print(f"{'='*70}\n")
    for i, p in enumerate(papers[:20], 1):
        print(f"{i:2d}. [{p.source}] {p.short()}")
        if p.doi:
            print(f"    DOI: {p.doi}")
        if p.abstract:
            print(f"    Abstract: {p.abstract[:150]}...")
        print()

    with open("/tmp/literature_results.json", "w") as f:
        json.dump([p.to_dict() for p in papers], f, indent=2)
    print(f"Saved to /tmp/literature_results.json")
