"""Planning Center OAuth (authorization-code grant) - the token-exchange
and refresh mechanics behind the "Connect via Planning Center" flow in
web/pco_oauth_router.py. Deliberately separate from planning_center.py
(the resource-API client): this module only ever talks to PCO's own
/oauth/* endpoints, never People/Registrations/Services.
"""
import httpx

AUTHORIZE_URL = "https://api.planningcenteronline.com/oauth/authorize"
TOKEN_URL = "https://api.planningcenteronline.com/oauth/token"

# People (person/phone lookups, campuses), Registrations (signups) and
# Services (service types/plans/team members) - the same three PCO
# products the PAT-based client calls (see planning_center.py), now
# requested explicitly since OAuth, unlike a PAT, actually has a scope
# concept.
SCOPES = "people registrations services"


def build_authorize_url(client_id: str, redirect_uri: str, state: str) -> str:
    return (
        f"{AUTHORIZE_URL}?client_id={client_id}&redirect_uri={redirect_uri}"
        f"&response_type=code&scope={SCOPES.replace(' ', '+')}&state={state}"
    )


async def exchange_code_for_tokens(
    client_id: str, client_secret: str, code: str, redirect_uri: str
) -> dict:
    """Returns PCO's raw token response: {access_token, refresh_token,
    token_type, expires_in, created_at, scope}."""
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
            },
        )
    response.raise_for_status()
    return response.json()


async def refresh_access_token(client_id: str, client_secret: str, refresh_token: str) -> dict:
    """Same response shape as exchange_code_for_tokens - PCO issues a new
    refresh_token on every refresh, so callers must persist the new one,
    not keep reusing the original."""
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
                "client_secret": client_secret,
            },
        )
    response.raise_for_status()
    return response.json()
