"""Taxonomic normalisation utilities for :mod:`morpho`.

The package operates on heterogeneous spreadsheet exports.  These datasets
carry varying representations of taxa (capitalisation differences, synonyms or
truncated epithets).  In order to keep the URL builder predictable we provide a
handful of helpers that resolve a user supplied name to a canonical spelling.

The resolver intentionally does **not** attempt to be authoritative.  Instead it
applies lightweight heuristics:

* collapse whitespace and punctuation for robust matching;
* look-up known synonyms captured from the datasets bundled with this
  repository; and
* estimate the taxonomic rank based on how many terms are present.

If no confident match is available the input name is still echoed back with a
low confidence score which allows downstream components to make conservative
choices (e.g. broad keyword searches instead of strict filters).
"""
from __future__ import annotations

import re
from typing import Dict, Optional

from .schemas import TaxonResolution

# Synonym set derived from the confirmed matches distributed with the
# repository.  Only a small subset is required to demonstrate the behaviour, the
# resolver falls back to the original user input otherwise.
_CANONICAL_SYNONYMS: Dict[str, str] = {
    "ambystoma mexicanum": "Ambystoma mexicanum",
    "ambystoma tigrinum": "Ambystoma tigrinum",
    "regina septemvittata": "Regina septemvittata",
    "smaug warreni": "Smaug warreni",
    "smaug barbertonensis": "Smaug barbertonensis",
    "crotalus adamanteus": "Crotalus adamanteus",
    "acanthophis laevis": "Aipysurus laevis",
    "hemidactylus mabouia": "Hemidactylus mabouia",
    "ophidion raithmai": "Ophiomorus raithmai",
}


def _normalise(text: str) -> str:
    """Return a lowercase token with punctuation removed."""

    collapsed = re.sub(r"[\s_\-]+", " ", text.strip())
    cleaned = re.sub(r"[^0-9a-z ]", "", collapsed, flags=re.IGNORECASE)
    return cleaned.lower()


def guess_rank(name: str) -> Optional[str]:
    """Guess the taxonomic rank based on the number of tokens present."""

    if not name:
        return None
    parts = _normalise(name).split()
    if len(parts) == 1:
        return "genus"
    if len(parts) == 2:
        return "species"
    if len(parts) == 3:
        return "subspecies"
    return None


def resolve_taxon(name: Optional[str]) -> Optional[TaxonResolution]:
    """Resolve a user provided taxon into a canonical representation.

    Parameters
    ----------
    name:
        Raw user supplied text.  ``None`` short-circuits resolution.

    Returns
    -------
    :class:`TaxonResolution` or ``None`` when ``name`` is falsy.
    """

    if not name:
        return None

    normalised = _normalise(name)
    canonical = _CANONICAL_SYNONYMS.get(normalised)
    confidence = 0.6

    if canonical is None:
        # Attempt to capitalise tokens heuristically.
        canonical = " ".join(token.capitalize() for token in normalised.split())
        confidence = 0.4
    else:
        confidence = 0.9

    rank = guess_rank(canonical)
    return TaxonResolution(
        input_name=name,
        matched_name=canonical if canonical else None,
        rank=rank,
        confidence=confidence,
        notes=None if canonical else "No canonical mapping found",
    )


__all__ = ["resolve_taxon", "guess_rank"]
