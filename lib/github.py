"""
lib/github.py — shared GitHub API helpers.

Used by both bootstrap.py and build.py.
Run scripts from the repo root so this module is importable.
"""

import base64
import tomllib
from typing import Any

import httpx

BASE_URL = "https://api.github.com"

# Bump this when bootstrap produces new required fields.
# build.py will exit with a helpful message if projects.yaml is behind.
BOOTSTRAP_VERSION = 2


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #

def make_client(token: str) -> httpx.Client:
    """Return a configured httpx.Client with auth and GitHub API headers."""
    return httpx.Client(
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=15.0,
    )


# --------------------------------------------------------------------------- #
# Pagination
# --------------------------------------------------------------------------- #

def paginate(
    client: httpx.Client,
    url: str,
    params: dict,
    desc: str = "",
    item_label=None,
) -> list[dict]:
    """Fetch all pages from a paginated GitHub API endpoint.

    GitHub's Link-header pagination can hand back an item already seen on an
    earlier page (observed with sort=updated), so results are deduplicated by
    "id" once collection is complete.
    """
    results = []
    seen_urls = set()
    seen_ids = set()
    while url:
        if url in seen_urls:
            break
        seen_urls.add(url)
        response = client.get(url, params=params)
        response.raise_for_status()
        for item in response.json():
            if item["id"] in seen_ids:
                continue
            seen_ids.add(item["id"])
            results.append(item)
        url = response.links.get("next", {}).get("url")
        params = {}  # already encoded in the next URL
        if desc:
            latest = item_label(results[-1]) if item_label and results else ""
            if latest and len(latest) > 40:
                latest = latest[:37] + "..."
            suffix = f" — {latest}" if latest else ""
            print(f"  {desc}: {len(results)} fetched...{suffix}", end="\r", flush=True)
    if desc:
        print()
    return results


# --------------------------------------------------------------------------- #
# README fetching
# --------------------------------------------------------------------------- #

def fetch_repo_readme(client: httpx.Client, username: str, repo_name: str) -> str:
    """
    Fetch the README for a repo.
    GitHub returns the content base64-encoded — we decode it here.
    Returns an empty string if no README exists.
    """
    url = f"{BASE_URL}/repos/{username}/{repo_name}/readme"
    try:
        response = client.get(url)
        response.raise_for_status()
        return base64.b64decode(response.json()["content"]).decode("utf-8")
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return ""
        raise


def fetch_gist_readme(client: httpx.Client, gist: dict) -> str:
    """
    Fetch the first .md file from a gist.
    The gist dict is the full API response object (from /gists or /gists/{id}).
    Returns an empty string if no .md file exists.
    """
    md_filename = next(
        (fname for fname in gist["files"] if fname.endswith(".md")), None
    )
    if not md_filename:
        return ""
    raw_url = gist["files"][md_filename]["raw_url"]
    response = client.get(raw_url)
    response.raise_for_status()
    return response.text


# --------------------------------------------------------------------------- #
# portfolio.toml fetching
# --------------------------------------------------------------------------- #

def fetch_repo_portfolio(
    client: httpx.Client, username: str, repo_name: str
) -> dict[str, Any]:
    """
    Fetch and parse portfolio.toml from a repo's root.
    Returns {} if the file doesn't exist or can't be parsed.
    """
    url = f"{BASE_URL}/repos/{username}/{repo_name}/contents/portfolio.toml"
    try:
        response = client.get(url)
        response.raise_for_status()
        raw = base64.b64decode(response.json()["content"]).decode("utf-8")
        return tomllib.loads(raw)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return {}
        raise
    except tomllib.TOMLDecodeError as e:
        print(f"  [warn] portfolio.toml parse error in {repo_name}: {e}")
        return {}


def fetch_gist_portfolio(client: httpx.Client, gist: dict) -> dict[str, Any]:
    """
    Fetch and parse portfolio.toml from a gist's files.
    Returns {} if the file doesn't exist or can't be parsed.
    """
    if "portfolio.toml" not in gist["files"]:
        return {}
    raw_url = gist["files"]["portfolio.toml"]["raw_url"]
    try:
        response = client.get(raw_url)
        response.raise_for_status()
        return tomllib.loads(response.text)
    except tomllib.TOMLDecodeError as e:
        print(f"  [warn] portfolio.toml parse error in gist {gist['id']}: {e}")
        return {}


# --------------------------------------------------------------------------- #
# Pinned items
# --------------------------------------------------------------------------- #

# GitHub enforces a maximum of 6 pinned items per user profile.
_PINNED_QUERY = """
query($login: String!) {
  user(login: $login) {
    pinnedItems(first: 6, types: [REPOSITORY, GIST]) {
      nodes {
        ... on Repository { name }
        ... on Gist { name }
      }
    }
  }
}
"""

def fetch_pinned_names(client: httpx.Client, username: str) -> set[str]:
    """
    Return the names of a user's pinned repos and gists via the GraphQL API.
    For repos the name is the repo name; for gists it's the gist ID.
    Returns an empty set if the query fails.
    """
    response = client.post(
        f"{BASE_URL}/graphql",
        json={"query": _PINNED_QUERY, "variables": {"login": username}},
    )
    response.raise_for_status()
    data = response.json()
    nodes = data.get("data", {}).get("user", {}).get("pinnedItems", {}).get("nodes", [])
    return {node["name"] for node in nodes if node.get("name")}


# --------------------------------------------------------------------------- #
# Topics (tags)
# --------------------------------------------------------------------------- #

def fetch_latest_release_date(
    client: httpx.Client, username: str, repo_name: str
) -> str | None:
    """
    Return the published_at date of the latest release for a repo, or None
    if the repo has no releases or if the request fails.
    """
    url = f"{BASE_URL}/repos/{username}/{repo_name}/releases/latest"
    try:
        response = client.get(url)
        response.raise_for_status()
        return response.json().get("published_at")
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return None
        raise


def extract_topics(repo: dict) -> list[str]:
    """
    Extract GitHub topics from a repo API response object.
    Topics are already present in the repo dict — no extra API call needed.
    Returns [] if none are set.
    """
    return repo.get("topics", [])


# --------------------------------------------------------------------------- #
# License
# --------------------------------------------------------------------------- #

def extract_license(repo: dict) -> str:
    """
    Extract the SPDX license identifier from a repo API response object.
    Falls back to the license name if SPDX ID is missing or NOASSERTION.
    Returns '' if no license is set.
    """
    lic = repo.get("license")
    if not lic:
        return ""
    spdx = lic.get("spdx_id", "")
    if spdx and spdx != "NOASSERTION":
        return spdx
    return lic.get("name", "")
