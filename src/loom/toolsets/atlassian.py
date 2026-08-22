"""Where an Atlassian request goes, which depends on how it is authenticated.

Shared by Jira and Confluence because the rule is the provider's, not the
product's, and a second copy is a second thing to get wrong the next time
Atlassian changes it.

**The rule.** A 3LO OAuth token is not usable against the site host. Atlassian's
own documentation is explicit — *"Requests that use OAuth 2.0 (3LO) are made via
api.atlassian.com (not https://your-domain.atlassian.net)"* — and the failure is
the worst shape available: ``401 Client must be authenticated to access this
resource``, which reads as a bad token. The obvious response is to connect
again, which produces another perfectly good token and the same 401. Observed
three reconnects deep before anyone suspected the host.

Basic auth is the mirror image: it works against the site and *not* against
``api.atlassian.com``. So the host follows the credential rather than being
configured once, and the ``Authorization`` header already carries which one is
in play.

**The site is matched, never guessed.** A token can grant several sites, and
writing an issue into the wrong Jira is not something a retry undoes.
"""

from __future__ import annotations

from typing import Any

__all__ = ["ATLASSIAN_API", "api_root", "is_bearer", "resolve_cloud_id"]

#: Where 3LO requests go. The path segment after it is the product — ``jira``,
#: ``confluence`` — followed by the site's cloud id.
ATLASSIAN_API = "https://api.atlassian.com"

#: Atlassian's own listing of what a token can reach. The only way to turn a
#: token into a cloud id; there is nothing in the token itself to read.
ACCESSIBLE_RESOURCES = f"{ATLASSIAN_API}/oauth/token/accessible-resources"


def is_bearer(headers: dict[str, str]) -> bool:
    """Whether these headers carry a 3LO token rather than Basic auth."""
    return headers.get("Authorization", "").startswith("Bearer ")


def api_root(product: str, cloud_id: str) -> str:
    """The 3LO base for *product* on the site *cloud_id* names."""
    return f"{ATLASSIAN_API}/ex/{product}/{cloud_id}"


async def resolve_cloud_id(
    authorization: str, *, site_url: str = "", classify: Any = None
) -> str:
    """The id of the site this token is for.

    *site_url* is matched when one was configured, because "the first site you
    can see" is a guess and the cost of getting it wrong is a write against
    somebody else's instance. Falls back to the first only when nothing matches
    — a token granting exactly one site is the common case and should not need
    the site configured.

    *classify* is the caller's error mapper, so a failure here surfaces as that
    toolset's own exception type rather than as something from a shared module
    nobody imported on purpose.
    """
    import httpx

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            ACCESSIBLE_RESOURCES,
            headers={"Authorization": authorization, "Accept": "application/json"},
        )
    if resp.status_code >= 400:
        raise classify(resp) if classify else RuntimeError(
            f"could not list Atlassian sites: {resp.status_code}"
        )

    sites = resp.json() or []
    if not sites:
        raise ValueError(
            "this Atlassian credential grants access to no site. Re-run "
            "`loom connect` for it and choose a site when asked."
        )

    if site_url:
        wanted = site_url.rstrip("/")
        for site in sites:
            if str(site.get("url", "")).rstrip("/") == wanted:
                return str(site["id"])
    return str(sites[0]["id"])
