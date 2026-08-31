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