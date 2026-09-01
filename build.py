# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "httpx",
#   "python-dotenv",
#   "ruamel.yaml",
#   "markdown-it-py",
#   "jinja2",
# ]
# ///
"""
build.py — generate the static portfolio site from projects.yaml.

Usage (run from repo root):
    uv run build.py

Reads from .env:
    GITHUB_TOKEN      — personal access token (read-only contents scope)
    GH_USERNAME   — your GitHub username
    CACHE_TTL_HOURS   — float, how old a cached README can be before re-fetching
                        (default: 1.0 — set to 0 on CI to always re-fetch)

Output:
    output/           — ready to deploy to GitHub Pages
"""

import json
import os
import re
import shutil
import sys
import time
import tomllib
from pathlib import Path

import httpx
from dotenv import load_dotenv
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markdown_it import MarkdownIt
from ruamel.yaml import YAML

# Guard: must be run from the repo root so lib/ is importable
if not Path("lib/github.py").exists():
    sys.exit("ERROR: Run this script from the repo root directory.")

from lib.github import (
    BOOTSTRAP_VERSION,
    fetch_gist_portfolio,
    fetch_gist_readme,
    fetch_pinned_names,
    fetch_repo_portfolio,
    fetch_repo_readme,
    make_client,
)

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

load_dotenv()

TOKEN = os.getenv("GITHUB_TOKEN")
USERNAME = os.getenv("GH_USERNAME")
CACHE_TTL_HOURS = float(os.getenv("CACHE_TTL_HOURS", "1.0"))


def _derive_site_url() -> str:
    """
    Derive the canonical site URL for the Atom feed when site.toml has no url.
    Priority:
      1. CUSTOM_DOMAIN env var  → https://{domain}
      2. GITHUB_REPOSITORY env var (set by Actions) → https://{user}.github.io/{repo}
      3. Empty string (feed IDs will be relative — valid but not ideal)
    """
    custom = os.getenv("CUSTOM_DOMAIN", "").strip()
    if custom:
        return f"https://{custom}"
    gh_repo = os.getenv("GITHUB_REPOSITORY", "").strip()  # "owner/repo"
    if gh_repo and "/" in gh_repo:
        owner, repo = gh_repo.split("/", 1)
        return f"https://{owner}.github.io/{repo}"
    return ""

if not TOKEN or not USERNAME:
    sys.exit(
        "ERROR: GITHUB_TOKEN and GH_USERNAME must be set in .env\n"
        "See .env.example for reference."
    )

CACHE_DIR = Path(".cache")
OUTPUT_DIR = Path("output")
PROJECTS_FILE = Path("projects.yaml")
SITE_FILE = Path("site.toml")

_SITE_DEFAULTS: dict = {
    "title": "~/tools",
    "description": "Tools & Projects",
    "footer": 'Built with <a href="https://tools.vandragt.com/toolhub/">ToolHub</a>',
    "url": "",
    "navigation": {
        "back_link_url": "",
        "back_link_label": "Home",
    },
    "feed": {
        "max_entries": 20,
    },
    "sections": {
        "active": "Active projects",
        "archived": "Archived",
        "back_link": "all projects",
    },
    "theme": {
        "templates_dir": "templates",
        "static_dir": "static",
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    result = {**base}
    for key, value in override.items():
        if isinstance(value, dict):
            result[key] = _deep_merge(result.get(key, {}), value)
        else:
            result[key] = value
    return result


def _derive_parent_url(site_url: str) -> str:
    """Derive a parent/home URL from site_url for the backlink.

    Rules (applied in order):
    - Subdomain (e.g. tools.example.com or tools.example.com/path):
      strip the leading subdomain label → https://example.com
    - Subdirectory on root domain (e.g. example.com/toolhub):
      strip the path → https://example.com
    - Root domain with no path (e.g. example.com): no backlink → ""

    Note: hostnames like username.github.io are treated as subdomains by
    this heuristic (result: github.io). For such deployments set
    back_link_url explicitly in site.toml.
    """
    from urllib.parse import urlparse
    parsed = urlparse(site_url)
    host = parsed.hostname or ""
    parts = host.split(".")

    # Subdomain present — strip it (works for subdomain-only and subdomain+path)
    if len(parts) > 2:
        return f"{parsed.scheme}://{'.'.join(parts[1:])}"

    # No subdomain but deployed in a subdirectory — link to the origin
    if parsed.path.strip("/"):
        return f"{parsed.scheme}://{host}"

    return ""


def load_site_config() -> dict:
    """Load site.toml if present, merging with defaults."""
    if not SITE_FILE.exists():
        config = _deep_merge({}, _SITE_DEFAULTS)
    else:
        with SITE_FILE.open("rb") as f:
            config = _deep_merge(_deep_merge({}, _SITE_DEFAULTS), tomllib.load(f))
    if not config["navigation"]["back_link_url"] and config.get("url"):
        config["navigation"]["back_link_url"] = _derive_parent_url(config["url"])
    return config


# --------------------------------------------------------------------------- #
# Cache helpers
# --------------------------------------------------------------------------- #

def is_stale(cache_file: Path, ttl_hours: float) -> bool:
    """Return True if the cache file is missing or older than ttl_hours."""
    if not cache_file.exists():
        return True
    if ttl_hours == 0:
        return True
    age_seconds = time.time() - cache_file.stat().st_mtime
    return age_seconds > ttl_hours * 3600


def get_readme(client: httpx.Client, project: dict) -> str:
    """Return README markdown, using cache if fresh enough."""
    CACHE_DIR.mkdir(exist_ok=True)
    cache_file = CACHE_DIR / f"{project['name']}.md"

    if not is_stale(cache_file, CACHE_TTL_HOURS):
        print(f"  [cache] {project['name']}")
        return cache_file.read_text(encoding="utf-8")

    print(f"  [fetch] {project['name']}")
    try:
        if project["type"] == "repo":
            content = fetch_repo_readme(client, USERNAME, project["name"])
        else:
            # Pass a minimal gist-like dict — only files + id needed
            content = fetch_gist_readme(client, {
                "id": project["gist_id"],
                "files": {project["md_file"]: {"raw_url": _gist_raw_url(client, project)}},
            })
    except httpx.HTTPStatusError as e:
        print(f"  [warn]  Could not fetch README for {project['name']}: {e}")
        if cache_file.exists():
            print(f"  [warn]  Using stale cache for {project['name']}")
            return cache_file.read_text(encoding="utf-8")
        return f"_README not available for {project['name']}._"

    cache_file.write_text(content, encoding="utf-8")
    return content


def unique_slug(name: str, gist_id: str, used_slugs: set[str]) -> str:
    """Return an output slug for `name` that isn't already in `used_slugs`,
    adding it to the set. Falls back to appending a counter so that 3+
    same-named entries (e.g. from an upstream duplicate-fetch bug) don't all
    collapse onto one slug and overwrite each other's page."""
    suffix = gist_id[:7] if gist_id else name[:7]
    slug = name
    n = 1
    while slug in used_slugs:
        n += 1
        slug = f"{name}-{suffix}" if n == 2 else f"{name}-{suffix}-{n}"
    used_slugs.add(slug)
    return slug


def _gist_raw_url(client: httpx.Client, project: dict) -> str:
    """Resolve the raw_url for a gist's .md file via the API."""
    from lib.github import BASE_URL
    response = client.get(f"{BASE_URL}/gists/{project['gist_id']}")
    response.raise_for_status()
    return response.json()["files"][project["md_file"]]["raw_url"]


def get_portfolio(client: httpx.Client, project: dict) -> dict:
    """Fetch portfolio.toml for a project, using cache if fresh enough."""
    import json
    CACHE_DIR.mkdir(exist_ok=True)
    cache_file = CACHE_DIR / f"{project['name']}.portfolio.json"

    if not is_stale(cache_file, CACHE_TTL_HOURS):
        return json.loads(cache_file.read_text(encoding="utf-8"))

    if project["type"] == "repo":
        data = fetch_repo_portfolio(client, USERNAME, project["name"])
    else:
        from lib.github import BASE_URL
        response = client.get(f"{BASE_URL}/gists/{project['gist_id']}")
        response.raise_for_status()
        data = fetch_gist_portfolio(client, response.json())

    cache_file.write_text(json.dumps(data), encoding="utf-8")
    return data


# --------------------------------------------------------------------------- #
# Atom feed helpers
# --------------------------------------------------------------------------- #

def _to_atom_date(iso: str) -> str:
    """
    Normalise a GitHub ISO-8601 timestamp to an RFC 3339 date-time with a Z
    suffix, as required by Atom. GitHub always returns UTC strings ending in Z
    so this is mostly a pass-through with safety normalisation.
    """
    if not iso:
        return ""
    # Ensure the string ends with Z (GitHub always uses UTC)
    iso = iso.strip()
    if not iso.endswith("Z") and not re.search(r"[+-]\d{2}:\d{2}$", iso):
        iso += "Z"
    # Ensure T separator (should already be present from GitHub)
    return iso


def _feed_updated_for(project: dict) -> str:
    """Return the Atom <updated> value for a project entry."""
    return _to_atom_date(
        project.get("latest_release_at") or project.get("created_at", "")
    )


# --------------------------------------------------------------------------- #
# Markdown rendering
# --------------------------------------------------------------------------- #

def render_markdown(md_text: str, raw_base: str = "") -> str:
    """
    Convert markdown to HTML.

    - 启用 GFM 表格支持（.enable("table")）
    - 若提供 raw_base，将 README 中的相对路径图片替换为 raw.githubusercontent.com 绝对路径
      （解决在 toolhub 静态站上相对路径图片裂图的问题）

    Args:
        md_text: Markdown 文本
        raw_base: 仓库 raw 内容基础 URL，例如
                  https://raw.githubusercontent.com/TAXLY-WorkTools/demo/main/
    """
    md = MarkdownIt("commonmark").enable("table")
    html = md.render(md_text)

    if raw_base:
        # 在渲染后的 HTML 中替换 <img src="相对路径"> 为绝对路径
        # （在 HTML 层面替换比在 Markdown 文本层面替换更可靠，
        #  能正确处理文件名含括号/中文等特殊字符的情况）
        def _replace_img_src(match):
            src = match.group(1)
            # 已经是绝对路径（http/https/data:/协议相对）则不处理
            if src.startswith(("http://", "https://", "data:", "//")):
                return match.group(0)
            # 去掉开头的 ./
            if src.startswith("./"):
                src = src[2:]
            return f'<img src="{raw_base}{src}"'

        html = re.sub(r'<img src="([^"]+)"', _replace_img_src, html)

    return html


# --------------------------------------------------------------------------- #
# Load projects
# --------------------------------------------------------------------------- #

def load_projects() -> list[dict]:
    """Read and return the list of projects from projects.yaml."""
    yaml = YAML()
    data = yaml.load(PROJECTS_FILE)
    file_version = data.get("version", 0)
    if file_version < BOOTSTRAP_VERSION:
        sys.exit(
            f"ERROR: projects.yaml is at version {file_version}, "
            f"but version {BOOTSTRAP_VERSION} is required.\n"
            "Re-run: uv run bootstrap.py"
        )
    return data["projects"]


# --------------------------------------------------------------------------- #
# Build site
# --------------------------------------------------------------------------- #

def _build_feed(env, projects: list[dict], site_config: dict) -> None:
    """Render and write output/feed.xml as an Atom 1.0 feed."""
    from jinja2 import Environment

    max_entries = site_config.get("feed", {}).get("max_entries", 20)

    # Annotate each project with its Atom <updated> value; skip archived entries
    feed_projects = []
    for p in projects:
        if p.get("archived"):
            continue
        feed_updated = _feed_updated_for(p)
        if not feed_updated:
            continue  # skip entries with no date at all
        feed_projects.append({**p, "feed_updated": feed_updated})

    # Sort by feed_updated descending, then cap to max_entries
    feed_projects.sort(key=lambda p: p["feed_updated"], reverse=True)
    feed_projects = feed_projects[:max_entries]

    # Feed <updated> = most recent entry updated date (lexicographic on ISO strings)
    feed_updated = feed_projects[0]["feed_updated"] if feed_projects else ""

    # Determine the canonical site URL: site.toml wins, then env-derived fallback
    site_url = (site_config.get("url") or _derive_site_url()).rstrip("/")
    feed_url = (site_url + "/feed.xml") if site_url else "feed.xml"

    # Register a simple filter so the template can format dates
    env.filters["atom_date"] = _to_atom_date

    feed_template = env.get_template("feed.xml")
    rendered = feed_template.render(
        projects=feed_projects,
        feed_updated=feed_updated,
        site_url=site_url,
        feed_url=feed_url,
        author=site_config.get("author") or USERNAME,
    )
    (OUTPUT_DIR / "feed.xml").write_text(rendered, encoding="utf-8")
    print(f"  [feed]  feed.xml written ({len(feed_projects)} entries)")


def build(
    projects: list[dict],
    client: httpx.Client,
    pinned: set[str] | None = None,
    site_config: dict | None = None,
) -> None:
    """Render the full static site into output/."""
    site_config = site_config or _deep_merge({}, _SITE_DEFAULTS)
    static_dir = Path(site_config["theme"]["static_dir"])
    templates_dir = Path(site_config["theme"]["templates_dir"])

    if OUTPUT_DIR.exists():
        shutil.rmtree(OUTPUT_DIR)
    OUTPUT_DIR.mkdir()

    if static_dir.exists():
        shutil.copytree(static_dir, OUTPUT_DIR / "static")
        shutil.copy2("templates/upload.html", "output/upload.html")

    env = Environment(
        loader=FileSystemLoader(templates_dir),
        autoescape=select_autoescape(["html", "xml"]),
    )
    env.globals["site"] = site_config
    env.globals["sections"] = site_config["sections"]

    pinned = pinned or set()
    used_slugs: set[str] = set()
    enriched_projects = []
    site_url = (site_config.get("url") or _derive_site_url()).rstrip("/")

    project_template = env.get_template("project.html")
    for project in projects:
        # Merge portfolio.toml fields into the project dict
        portfolio = get_portfolio(client, project)
        # A project is pinned by repo name or gist ID
        pin_key = project["name"] if project["type"] == "repo" else project.get("gist_id", "")
        # Deduplicate output slugs (repo and gist can share the same name)
        slug = unique_slug(project["name"], project.get("gist_id", ""), used_slugs)
        enriched = {**project, **portfolio, "pinned": pin_key in pinned, "slug": slug}
        # Drop website links that point back at this site itself
        if site_url:
            for url_field in ("live_url", "homepage"):
                url_val = enriched.get(url_field, "")
                if url_val and url_val.rstrip("/").startswith(site_url):
                    enriched[url_val] = None
        enriched_projects.append(enriched)

        readme_md = get_readme(client, project)
        # 构造仓库 raw 基础 URL，用于把 README 中的相对路径图片转为绝对路径
        raw_base = f"https://raw.githubusercontent.com/{USERNAME}/{project['name']}/main/"
        readme_html = render_markdown(readme_md, raw_base)

        page_dir = OUTPUT_DIR / slug
        page_dir.mkdir(parents=True, exist_ok=True)

        rendered = project_template.render(
            project=enriched,
            readme_html=readme_html,
        )
        (page_dir / "index.html").write_text(rendered, encoding="utf-8")

    def section_order(p: dict) -> int:
        if p.get("pinned"):
            return 0
        if p.get("archived"):
            return 2
        return 1

    # Sort by recency descending first (stable), then by section (stable)
    enriched_projects.sort(key=lambda p: p.get("updated_at", ""), reverse=True)
    enriched_projects.sort(key=section_order)

    index_template = env.get_template("index.html")
    rendered_index = index_template.render(projects=enriched_projects)
    (OUTPUT_DIR / "index.html").write_text(rendered_index, encoding="utf-8")

    _build_feed(env, enriched_projects, site_config)

    print(f"\nBuilt {len(projects)} project pages → {OUTPUT_DIR}/")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main() -> None:
    if not PROJECTS_FILE.exists():
        sys.exit(
            "ERROR: projects.yaml not found.\n"
            "Run bootstrap.py first to generate it."
        )

    site_config = load_site_config()
    print(f"Site: {site_config['title']}")

    print(f"Loading projects from {PROJECTS_FILE}...")
    projects = load_projects()
    print(f"  Found {len(projects)} projects")
    print(f"  Cache TTL: {CACHE_TTL_HOURS}h")

    print("\nFetching pinned items...")
    with make_client(TOKEN) as client:
        pinned = fetch_pinned_names(client, USERNAME)
        print(f"  Pinned: {', '.join(sorted(pinned)) or 'none'}")

        print("\nFetching READMEs and portfolio metadata...")
        build(projects, client, pinned, site_config)


if __name__ == "__main__":
    main()
