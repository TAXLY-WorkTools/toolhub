# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "httpx",
#   "python-dotenv",
#   "ruamel.yaml",
# ]
# ///
"""
bootstrap.py — seed projects.yaml from your public GitHub repos and gists.

Usage (run from repo root):
    uv run bootstrap.py

Reads from .env:
    GITHUB_TOKEN    — personal access token (read-only contents scope)
    GH_USERNAME — your GitHub username

Output:
    projects.yaml   — review and trim this manually; live_url/docs_url are
                      extracted automatically from portfolio.toml at build time.
"""

import os
import re
import sys
from pathlib import Path

# Guard: must be run from the repo root so lib/ is importable
if not Path("lib/github.py").exists():
    sys.exit("ERROR: Run this script from the repo root directory.")

import httpx
from dotenv import load_dotenv
from ruamel.yaml import YAML

from lib.github import (
    BOOTSTRAP_VERSION,
    extract_license,
    extract_topics,
    fetch_gist_portfolio,
    fetch_latest_release_date,
    fetch_repo_portfolio,
    make_client,
    paginate,
)

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

load_dotenv()

TOKEN = os.getenv("GITHUB_TOKEN")
USERNAME = os.getenv("GH_USERNAME")

if not TOKEN or not USERNAME:
    sys.exit(
        "ERROR: GITHUB_TOKEN and GH_USERNAME must be set in .env\n"
        "See .env.example for reference."
    )

BASE_URL = "https://api.github.com"
OUTPUT_FILE = Path("projects.yaml")
EXCLUDE_FILE = Path("exclude.txt")


# --------------------------------------------------------------------------- #
# Exclusions
# --------------------------------------------------------------------------- #

def load_exclusions() -> set[str]:
    """
    Load exclusion list from exclude.txt if it exists.
    Each line is a repo name or gist ID to skip.
    Lines starting with # are comments.
    """
    if not EXCLUDE_FILE.exists():
        return set()
    lines = EXCLUDE_FILE.read_text(encoding="utf-8").splitlines()
    return {line.strip() for line in lines if line.strip() and not line.startswith("#")}


# --------------------------------------------------------------------------- #
# Label mapping and Chinese name extraction
# --------------------------------------------------------------------------- #

def load_label_map() -> dict[str, str]:
    """Load labels.json and build a key -> Chinese name mapping."""
    label_map = {}
    labels_file = Path("labels.json")
    if not labels_file.exists():
        print("  ⚠️ labels.json not found, tags will remain in English")
        return label_map
    try:
        import json
        data = json.loads(labels_file.read_text(encoding="utf-8"))
        for l in data.get("departments", []):
            label_map[l["id"]] = l["name"]
        for l in data.get("categories", []):
            label_map[l["id"]] = l["name"]
        for ct in data.get("custom_types", []):
            for l in ct.get("labels", []):
                label_map[l["id"]] = l["name"]
        # 加载新增的标签类型
        for l in data.get("tech_stacks", []):
            label_map[l["id"]] = l["name"]
        for l in data.get("tutorial_status", []):
            label_map[l["id"]] = l["name"]
        for l in data.get("file_sizes", []):
            label_map[l["id"]] = l["name"]
        print(f"  📋 Loaded {len(label_map)} label mappings from labels.json")
    except Exception as e:
        print(f"  ⚠️ Failed to load labels.json: {e}")
    return label_map


def extract_cn_name(description: str) -> tuple[str, str]:
    """Extract Chinese name from description prefix 【...】.
    Returns (cn_name, clean_description).
    If no 【...】 prefix found, returns ("", original_description).
    """
    if not description:
        return "", description
    m = re.match(r"^【(.+?)】", description)
    if m:
        cn_name = m.group(1).strip()
        clean_desc = description[m.end():].strip()
        return cn_name, clean_desc
    return "", description


def extract_author(description: str) -> tuple[str, str]:
    """Extract author from description suffix 【作者：xxx】.
    Returns (author, clean_description).
    If no 【作者：xxx】 suffix found, returns ("", original_description).
    """
    if not description:
        return "", description
    # 匹配描述末尾的 【作者：xxx】
    m = re.search(r"【作者：(.+?)】\s*$", description)
    if m:
        author = m.group(1).strip()
        clean_desc = description[:m.start()].strip()
        return author, clean_desc
    return "", description


# --------------------------------------------------------------------------- #
# Fetch repos and gists
# --------------------------------------------------------------------------- #

def fetch_repos(client: httpx.Client) -> list[dict]:
    """Return all public repos for the TAXLY-WorkTools org.

    Uses the public /orgs/{org}/repos endpoint instead of /user/repos so the
    repo listing no longer depends on which account the GITHUB_TOKEN belongs
    to or on its account-level permissions. This keeps bootstrap.py working
    even if the token's user/repos access is restricted.
    """
    return paginate(
        client,
        f"{BASE_URL}/orgs/{USERNAME}/repos",
        {"type": "public", "per_page": 100, "sort": "updated"},
        desc="repos",
    )


def fetch_gists(client: httpx.Client) -> list[dict]:
    """Return public gists that contain at least one .md file."""
    all_gists = paginate(
        client,
        f"{BASE_URL}/users/{USERNAME}/gists",
        {"per_page": 100},
        desc="gists",
        item_label=lambda g: g.get("description") or g["id"],
    )
    # Extra guard: skip any secret gists the API may return for authenticated users
    public_gists = [g for g in all_gists if g.get("public", True)]
    return [g for g in public_gists if any(f.endswith(".md") for f in g["files"])]


# --------------------------------------------------------------------------- #
# Shape into projects.yaml entries
# --------------------------------------------------------------------------- #

def repo_to_entry(client: httpx.Client, repo: dict, label_map: dict[str, str]) -> dict:
    """
    Convert a repo API response to a projects.yaml entry.
    Fetches portfolio.toml and GitHub topics automatically.
    Extracts Chinese name from 【...】 prefix in description.
    Extracts author from 【作者：xxx】 suffix in description.
    Converts tag IDs to Chinese names using label_map.
    """
    name = repo["name"]
    print(f"  [repo] {name}")
    portfolio = fetch_repo_portfolio(client, USERNAME, name)
    topics = extract_topics(repo)
    license_id = extract_license(repo)

    latest_release_at = fetch_latest_release_date(client, USERNAME, name)

    # Extract Chinese name from description 【...】 prefix
    raw_desc = repo["description"] or ""
    cn_name, clean_desc = extract_cn_name(raw_desc)
    if cn_name:
        print(f"    📝 Chinese name: {cn_name}")

    # Extract author from description 【作者：xxx】 suffix
    author, clean_desc = extract_author(clean_desc)
    if author:
        print(f"    👤 Author: {author}")

    # Convert tag IDs to Chinese names
    cn_tags = [label_map.get(t, t) for t in topics]

    entry = {
        "name": name,
        "type": "repo",
        "repo_url": repo["html_url"],
        "description": clean_desc,
        "tags": cn_tags,
        "created_at": repo.get("created_at", ""),
        "updated_at": repo.get("pushed_at") or repo.get("updated_at", ""),
        "archived": bool(repo.get("archived")),
        "license": license_id,
    }
    if cn_name:
        entry["cn_name"] = cn_name
    if author:
        entry["author"] = author
    if latest_release_at:
        entry["latest_release_at"] = latest_release_at
    if repo.get("homepage"):
        entry["homepage"] = repo["homepage"]
    return entry


def gist_to_entry(client: httpx.Client, gist: dict) -> dict:
    """
    Convert a gist API response to a projects.yaml entry.
    Fetches portfolio.toml automatically.
    Gists have no topics so tags always come from portfolio.toml.
    """
    name = gist["description"] or gist["id"]
    print(f"  [gist] {name}")
    md_file = next(f for f in gist["files"] if f.endswith(".md"))
    portfolio = fetch_gist_portfolio(client, gist)

    return {
        "name": name,
        "type": "gist",
        "gist_id": gist["id"],
        "gist_url": gist["html_url"],
        "md_file": md_file,
        "description": gist["description"] or "",
        "tags": portfolio.get("tags", []),
        "created_at": gist.get("created_at", ""),
        "updated_at": gist.get("updated_at", ""),
    }


# --------------------------------------------------------------------------- #
# Write output
# --------------------------------------------------------------------------- #

def write_yaml(entries: list[dict]) -> None:
    yaml = YAML()
    yaml.default_flow_style = False
    yaml.indent(mapping=2, sequence=4, offset=2)

    data = {"version": BOOTSTRAP_VERSION, "projects": entries}

    with OUTPUT_FILE.open("w") as f:
        f.write("# projects.yaml — curated list of tools and projects\n")
        f.write("#\n")
        f.write("# live_url and docs_url are NOT stored here — add a portfolio.toml\n")
        f.write("# to each repo/gist instead. See portfolio.toml.example.\n")
        f.write("#\n")
        f.write("# Tags for repos come from GitHub Topics.\n")
        f.write("# Tags for gists come from portfolio.toml.\n")
        f.write("# To exclude entries permanently, add repo names or gist IDs to exclude.txt.\n\n")
        yaml.dump(data, f)

    print(f"\nWritten {len(entries)} entries to {OUTPUT_FILE}")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main() -> None:
    exclusions = load_exclusions()
    if exclusions:
        print(f"Exclusions loaded from {EXCLUDE_FILE}: {len(exclusions)} entries\n")

    with make_client(TOKEN) as client:
        print("Loading label mappings...")
        label_map = load_label_map()

        print("Fetching public repos...")
        repos = fetch_repos(client)
        repos = [r for r in repos if r["name"] not in exclusions]
        print(f"  Found {len(repos)} repos\n")

        print("Fetching gists (filtering to .md only)...")
        gists = fetch_gists(client)
        gists = [g for g in gists if g["id"] not in exclusions]
        print(f"  Found {len(gists)} gists with .md files\n")

        print("Building entries (fetching portfolio.toml where present)...")
        repo_entries = [repo_to_entry(client, r, label_map) for r in repos]
        gist_entries = [gist_to_entry(client, g) for g in gists]

    write_yaml(repo_entries + gist_entries)

    print("\nNext steps:")
    print("  1. Add portfolio.toml to each repo/gist (see portfolio.toml.example)")
    print("  2. Run: uv run build.py")


if __name__ == "__main__":
    main()