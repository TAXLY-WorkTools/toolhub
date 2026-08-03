"""Tests for build.unique_slug.

Coverage:
- unique_slug: repo/gist name collisions get distinct slugs
- unique_slug: 3+ collisions (e.g. from an upstream pagination duplicate)
  don't silently collapse onto the same slug and overwrite each other's page

Run from the repo root:
    uv run --group test pytest
"""
from build import unique_slug


def test_unique_slug_no_collision():
    used = set()
    assert unique_slug("fafi", "", used) == "fafi"


def test_unique_slug_two_collisions_get_distinct_slugs():
    used = set()
    first = unique_slug("fafi", "", used)
    second = unique_slug("fafi", "", used)
    assert first != second


def test_unique_slug_three_collisions_all_distinct():
    """Regression: repeated same-name entries (e.g. a duplicate-fetch bug)
    must not all fall back to the same slug and overwrite one page."""
    used = set()
    slugs = [unique_slug("fafi", "", used) for _ in range(3)]
    assert len(set(slugs)) == 3
