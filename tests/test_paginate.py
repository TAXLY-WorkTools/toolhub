"""Tests for lib.github.paginate.

Coverage:
- paginate: dedupes items that GitHub returns on more than one page
  (observed with sort=updated pagination — see build regression where
  every project on the live site appeared 3x)

Run from the repo root:
    uv run --group test pytest
"""
import httpx

from lib.github import paginate


def _page_response(url: str, items: list[dict], next_url: str | None) -> httpx.Response:
    headers = {}
    if next_url:
        headers["Link"] = f'<{next_url}>; rel="next"'
    return httpx.Response(200, json=items, headers=headers, request=httpx.Request("GET", url))


def test_paginate_dedupes_items_repeated_across_pages():
    """GitHub can hand back the same item on more than one page; paginate()
    must not return it twice."""
    page1 = [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]
    # Page 2 repeats id=2 from page 1, as observed in practice.
    page2 = [{"id": 2, "name": "b"}, {"id": 3, "name": "c"}]

    url1 = "https://api.github.com/user/repos"
    url2 = "https://api.github.com/user/repos?page=2"
    responses = {
        url1: _page_response(url1, page1, url2),
        url2: _page_response(url2, page2, None),
    }

    def fake_get(url, params=None):
        return responses[url]

    client = httpx.Client()
    client.get = fake_get  # bypass real network + params encoding for this fixed URL map

    results = paginate(client, url1, {"per_page": 100})

    assert [item["id"] for item in results] == [1, 2, 3]
