"""Google OpenID Connect: authorization-code flow and ID-token verification.

The ID token is verified against Google's published signing keys (signature, issuer, audience,
expiry), then we require a verified email in the allowed Google Workspace domain via the signed
`hd` claim (not just the email suffix), and a nonce that matches the one we issued.
"""

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlencode

import httpx
import jwt

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"  # noqa: S105 (URL, not a secret)
JWKS_URI = "https://www.googleapis.com/oauth2/v3/certs"
ISSUERS = ("https://accounts.google.com", "accounts.google.com")


class OIDCError(Exception):
    pass


@dataclass(frozen=True)
class GoogleIdentity:
    email: str
    name: str
    domain: str


class GoogleVerifier(Protocol):
    async def exchange_and_verify(self, code: str, nonce: str) -> GoogleIdentity: ...


def authorization_url(
    client_id: str, redirect_uri: str, state: str, nonce: str, domain: str
) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "nonce": nonce,
        "hd": domain,  # UI hint only; enforced on the verified token below
        "prompt": "select_account",
    }
    return f"{AUTH_ENDPOINT}?{urlencode(params)}"


def check_claims(claims: dict[str, Any], nonce: str, allowed_domain: str) -> GoogleIdentity:
    if claims.get("nonce") != nonce:
        raise OIDCError("nonce mismatch")
    if not claims.get("email_verified"):
        raise OIDCError("email not verified")
    domain = str(claims.get("hd") or "")
    if domain.lower() != allowed_domain.lower():
        raise OIDCError(f"account is not in the {allowed_domain} workspace")
    email = str(claims["email"]).lower()
    return GoogleIdentity(email=email, name=str(claims.get("name") or email), domain=domain)


class HttpGoogleVerifier:
    def __init__(
        self, client_id: str, client_secret: str, redirect_uri: str, allowed_domain: str
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri
        self._domain = allowed_domain
        self._jwks = jwt.PyJWKClient(JWKS_URI, cache_keys=True)

    async def exchange_and_verify(self, code: str, nonce: str) -> GoogleIdentity:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                TOKEN_ENDPOINT,
                data={
                    "code": code,
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "redirect_uri": self._redirect_uri,
                    "grant_type": "authorization_code",
                },
            )
        if resp.status_code != 200:
            raise OIDCError(f"token exchange failed ({resp.status_code})")
        id_token = resp.json().get("id_token")
        if not id_token:
            raise OIDCError("no id_token in response")
        key = await asyncio.to_thread(self._jwks.get_signing_key_from_jwt, id_token)
        try:
            claims: dict[str, Any] = jwt.decode(
                id_token,
                key.key,
                algorithms=["RS256"],
                audience=self._client_id,
                issuer=ISSUERS,
                options={"require": ["exp", "iat", "iss", "aud", "sub", "email"]},
            )
        except jwt.PyJWTError as exc:
            raise OIDCError(f"invalid id_token: {exc}") from exc
        return check_claims(claims, nonce, self._domain)
