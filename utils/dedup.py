"""
Fuzzy deduplication utilities for race names.
"""
import re
import unicodedata

from thefuzz import fuzz


SIMILARITY_THRESHOLD = 82  # tune as needed


def normalise_name(name: str) -> str:
    """Lower-case, strip accents/diacritics, collapse whitespace, remove punctuation."""
    # Remove Vietnamese diacritics via NFD decomposition
    nfd = unicodedata.normalize("NFD", name)
    ascii_name = nfd.encode("ascii", "ignore").decode("ascii")
    ascii_name = ascii_name.lower()
    ascii_name = re.sub(r"[^a-z0-9\s]", " ", ascii_name)
    ascii_name = re.sub(r"\s+", " ", ascii_name).strip()
    return ascii_name


def make_slug(name: str, date: str = "") -> str:
    """
    Create a deterministic slug for deduplication/db key.
    Combines normalised race name + year (from date) so the same race
    in 2026 and 2027 stays separate.
    """
    base = normalise_name(name)
    base = re.sub(r"\s+", "-", base)
    year = ""
    if date:
        m = re.search(r"(20\d{2})", date)
        if m:
            year = f"-{m.group(1)}"
    return f"{base}{year}"


def find_best_match(
    candidate: str,
    existing_slugs: list[str],
    threshold: int = SIMILARITY_THRESHOLD,
) -> str | None:
    """
    Compare *candidate* slug against *existing_slugs* using token-sort ratio.
    Returns the best matching slug if above threshold, else None.
    """
    best_score = 0
    best_slug = None
    for slug in existing_slugs:
        score = fuzz.token_sort_ratio(candidate, slug)
        if score > best_score:
            best_score = score
            best_slug = slug
    if best_score >= threshold:
        return best_slug
    return None


def resolve_slug(
    race_name: str,
    date: str,
    existing_slugs: list[str],
) -> str:
    """
    Return the canonical slug for this race:
    - If a fuzzy match is found in existing_slugs, reuse it.
    - Otherwise return a freshly generated slug.
    """
    candidate = make_slug(race_name, date)
    match = find_best_match(candidate, existing_slugs)
    return match if match else candidate
