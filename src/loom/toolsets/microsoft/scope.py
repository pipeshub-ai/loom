"""Whose data a Graph client acts on.

Almost every personal-workload path in Graph begins with a user: `/me/drive`,
`/me/onenote/notebooks`, `/me/messages`, `/me/joinedTeams`. And almost every one
of them **fails under an app-only token**, because client credentials
authenticate the *application* and there is no signed-in person for `/me` to
mean. Graph answers a 400 whose message is roughly "/me request is only valid
with delegated authentication flow" — an error that arrives from inside
whichever step happened to run first, and reads as a broken toolset rather than
a missing argument.

Six clients now need that refusal. Writing it six times would be six chances to
word it differently and six places to forget it, so it lives here.

The rule is narrow on purpose: **refuse what cannot work, document what might
not.** A `/me` path under app-only genuinely cannot resolve, so it raises. Other
Microsoft app-only restrictions — Teams message sending, OneNote — are
ambiguous or contradicted by Microsoft's own reference, and those are documented
in the manifests instead of being refused here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import quote

from loom.toolsets.microsoft.errors import GraphPermanentError

if TYPE_CHECKING:
    from loom.toolsets.microsoft.auth import MicrosoftAuth

__all__ = ["user_root"]


def user_root(
    auth: MicrosoftAuth,
    user_id: str = "",
    *,
    workload: str = "this data",
    env_hint: str = "",
    alternatives: str = "",
) -> str:
    """Return ``/me`` or ``/users/{id}``, refusing an impossible combination.

    Args:
        auth: The credentials in use. Only ``is_app_only`` is consulted.
        user_id: A user id or userPrincipalName to act on behalf of. Empty
            means the signed-in user, which only exists under delegated
            credentials.
        workload: What the caller is reaching for, for the error message —
            "this mailbox", "these notebooks". Named so the failure says which
            toolset raised it rather than leaving that to a traceback.
        env_hint: The environment variable that would supply ``user_id`` for
            this toolset, e.g. ``MS_OUTLOOK_USER``. Included in the message
            because a host configuring a daemon wants the name, not the concept.
        alternatives: Any *other* way this toolset can be addressed without a
            user — OneDrive's ``drive_id=`` reaches a drive directly, with no
            user involved. A refusal that lists only the fixes this function
            knows about would send a caller the long way round.

    Returns:
        A path prefix to build on, with no trailing slash.

    Raises:
        GraphPermanentError: when the token is app-only and no user was named.
            Permanent, deliberately: waiting does not create a signed-in user,
            and a retry policy should stop rather than spend its budget.
    """
    if user_id:
        return f"/users/{quote(user_id, safe='')}"
    if auth.is_app_only:
        fixes = [f"set {env_hint}"] if env_hint else []
        fixes.append("pass user_id=")
        if alternatives:
            fixes.append(alternatives)
        fixes.append("or authenticate as a person by setting MS_REFRESH_TOKEN")
        raise GraphPermanentError(
            f"This token authenticates the application, not a person, so "
            f"'/me' does not exist and {workload} cannot be resolved. "
            f"To fix: {', '.join(fixes)}.",
            status=0,
            code="appOnlyNeedsUser",
        )
    return "/me"
