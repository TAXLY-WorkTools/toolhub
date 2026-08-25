#!/usr/bin/env python3
"""
Build script for toolhub portfolio site.

Reads projects.yaml and renders each project page + index.
"""

import os
import shutil
import sys
from pathlib import Path
from datetime import datetime, timezone
import json
import re
import subprocess

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markdown_it import MarkdownIt
from mdit_py_plugins.front_matter import front_matter_plugin
from mdit_py_plugins.footnote import footnote_plugin

from lib.cache import Cache
from lib.github import fetch_pinned_names, fetch_repo_data, fetch_gist_data
from lib.config import load_config
from lib.feed import generate_feed

# ----- constants -----
PROJECTS_YAML = "projects.yaml"
OUTPUT_DIR = Path("output")
STATIC_DIR = Path("static")
TEMPLATE_DIR = Path("templates")
SITE_CONFIG = "site.toml"
USERNAME = os.environ.get("GH_USERNAME", "")
TOKEN = os.environ.get("GITHUB_TOKEN", "")
CACHE_TTL_HOURS = float(os.environ.get("CACHE_TTL_HOURS", "1.0"))
PROJECTS_FILE = os.environ.get("PROJECTS_FILE", PROJECTS_YAML)

# ----- markdown setup -----
md = (
    MarkdownIt("commonmark", {"breaks": True, "html": True})
    .use(front_matter_plugin)
    .use(footnote_plugin)
    .enable("table")
)

# ----- helpers -----
def render_markdown(text):
    """Render markdown to HTML, stripping front matter."""
    if not text:
        return ""
    rendered = md.render(text)
    # remove front matter if it was rendered as raw text
    lines = rendered.splitlines()
    if lines and lines[0].strip() == "<hr>" and "---" in rendered:
        # simple heuristic: if first line is <hr> and there's a --- in the output
        # try to strip front matter from source and re-render
        # use regex to strip front matter from source
        import re
        cleaned = re.sub(r"^---\n.*?\n---\n", "", text, flags=re.DOTALL)
        if cleaned != text:
            return md.render(cleaned)
    return rendered

def parse_date(date_str):
    """Parse date string to datetime."""
    if not date_str:
        return None
    try:
        return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    except ValueError:
        return None

def extract_license_from_readme(readme_text):
    """Try to extract license info from README."""
    if not readme_text:
        return None
    # common license patterns
    patterns = [
        r"MIT License",
        r"Apache License, Version 2\.0",
        r"GNU General Public License v3\.0",
        r"GPL-3\.0",
        r"BSD 3-Clause",
        r"BSD 2-Clause",
        r"ISC License",
        r"MPL-2\.0",
        r"Unlicense",
        r"CC0",
        r"LGPL",
        r"AGPL",
    ]
    for pattern in patterns:
        if re.search(pattern, readme_text, re.IGNORECASE):
            return pattern
    return None

def get_project_tags(repo_data):
    """Extract tags from repo topics."""
    tags = repo_data.get("topics", [])
    # also add language as tag if available
    lang = repo_data.get("language")
    if lang and lang.lower() != "none":
        tags.append(f"language-{lang.lower()}")
    return tags

def get_project_description(repo_data, readme_text):
    """Get description from repo data or fallback to README."""
    desc = repo_data.get("description", "").strip()
    if desc:
        return desc
    # fallback: first sentence of README
    if readme_text:
        lines = readme_text.strip().splitlines()
        for line in lines:
            line = line.strip()
            if line and not line.startswith("#") and len(line) > 10:
                # take first non-empty, non-heading line
                return line[:200]
    return ""

# ----- main build -----
def main():
    print(f"Site: ~/{OUTPUT_DIR}")
    print(f"Loading projects from {PROJECTS_FILE}...")

    # load config
    config = load_config(SITE_CONFIG)
    site = config.get("site", {})
    sections = config.get("sections", {})

    # ensure output dir exists
    OUTPUT_DIR.mkdir(exist_ok=True)

    # copy static files
    if STATIC_DIR.exists():
        shutil.copytree(STATIC_DIR, OUTPUT_DIR / "static", dirs_exist_ok=True)

    # --- 新增：复制 upload.html ---
    upload_html = TEMPLATE_DIR / "upload.html"
    if upload_html.exists():
        shutil.copy2(upload_html, OUTPUT_DIR / "upload.html")
        print("✅ 复制 upload.html 到输出目录")
    else:
        print("⚠️ templates/upload.html 不存在，跳过复制")

    # load projects from yaml
    import yaml
    with open(PROJECTS_FILE, "r") as f:
        data = yaml.safe_load(f)

    projects = data.get("projects", [])
    print(f"  Found {len(projects)} projects")

    # enrich projects with repo data if needed
    cache = Cache(ttl_hours=CACHE_TTL_HOURS)

    # load jinja2 environment
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(["html", "xml"]),
    )

    # render index
    template = env.get_template("index.html")
    index_html = template.render(
        site=site,
        sections=sections,
        projects=projects,
        pinned=[p for p in projects if p.get("pinned")],
        active=[p for p in projects if not p.get("pinned") and not p.get("archived")],
        archived=[p for p in projects if p.get("archived")],
    )
    with open(OUTPUT_DIR / "index.html", "w", encoding="utf-8") as f:
        f.write(index_html)
    print("  ✅ index.html")

    # render each project page
    for project in projects:
        slug = project.get("slug")
        if not slug:
            continue

        # create project dir
        project_dir = OUTPUT_DIR / slug
        project_dir.mkdir(exist_ok=True)

        # render project page
        template = env.get_template("project.html")
        project_html = template.render(
            site=site,
            project=project,
            readme_html=render_markdown(project.get("readme", "")),
        )
        with open(project_dir / "index.html", "w", encoding="utf-8") as f:
            f.write(project_html)
        print(f"  ✅ {slug}/index.html")

    # generate feed
    feed_path = OUTPUT_DIR / "feed.xml"
    generate_feed(projects, site, feed_path)
    print(f"  ✅ feed.xml")

    print("🎉 Build complete!")

if __name__ == "__main__":
    main()
