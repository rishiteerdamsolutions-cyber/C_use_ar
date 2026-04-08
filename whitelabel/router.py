"""
Tenant Router — subdomain / custom-domain → tenant lookup middleware.
Autonomous Web Agency Platform · White-label Layer

Attaches `request.state.tenant` and `request.state.branding`
to every incoming request so any route can read the active tenant.

How it works:
  Request to  digitaledge.yourplatform.com  → subdomain = 'digitaledge'
  Request to  app.digitaledge.in            → custom domain lookup
  Request to  yourplatform.com              → platform default (no tenant)
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from whitelabel.tenant_config import (
    DEFAULT_BRANDING,
    get_branding,
    get_tenant_by_domain,
    get_tenant_by_subdomain,
)

logger = logging.getLogger(__name__)

PLATFORM_DOMAIN = os.environ.get("PLATFORM_DOMAIN", "yourplatform.com")


def _extract_subdomain(host: str) -> Optional[str]:
    """
    Extract subdomain from Host header.

    'digitaledge.yourplatform.com' → 'digitaledge'
    'yourplatform.com'             → None
    'localhost'                    → None
    """
    host = host.split(":")[0].lower()    # strip port
    if host == PLATFORM_DOMAIN or host == "localhost" or host == "127.0.0.1":
        return None
    if host.endswith(f".{PLATFORM_DOMAIN}"):
        sub = host[: -(len(PLATFORM_DOMAIN) + 1)]
        return sub if sub else None
    return None


def _resolve_tenant(host: str) -> tuple[Optional[dict], dict[str, Any]]:
    """
    Given a Host header, return (tenant_doc, branding_dict).
    tenant_doc is None for the platform itself.
    """
    subdomain = _extract_subdomain(host)

    if subdomain:
        # Subdomain match: digitaledge.yourplatform.com
        tenant = get_tenant_by_subdomain(subdomain)
        if tenant:
            return tenant, tenant.get("branding", DEFAULT_BRANDING)
        logger.warning("Unknown subdomain: %s", subdomain)
        return None, DEFAULT_BRANDING

    # Custom domain match: app.digitaledge.in
    tenant = get_tenant_by_domain(host)
    if tenant:
        return tenant, tenant.get("branding", DEFAULT_BRANDING)

    # Platform itself
    return None, DEFAULT_BRANDING


class TenantMiddleware(BaseHTTPMiddleware):
    """
    FastAPI/Starlette middleware that resolves the active tenant
    from the Host header and attaches it to request.state.

    Usage in routes:
        tenant   = request.state.tenant    # None = platform owner
        branding = request.state.branding  # always a dict
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        host = request.headers.get("host", "")
        try:
            tenant, branding = _resolve_tenant(host)
        except Exception as exc:
            logger.error("Tenant resolution error for host=%s: %s", host, exc)
            tenant, branding = None, DEFAULT_BRANDING

        request.state.tenant   = tenant
        request.state.branding = branding

        if tenant:
            logger.debug(
                "Tenant resolved: %s (%s)",
                tenant.get("subdomain"), tenant.get("agency_name"),
            )

        response = await call_next(request)

        # Inject tenant name into response headers for debugging
        if tenant:
            response.headers["X-Tenant"] = tenant.get("subdomain", "unknown")

        return response


def inject_branding_css(branding: dict[str, Any]) -> str:
    """
    Generate a <style> block with CSS variables from the tenant branding.
    Inject this into every HTML page's <head>.

    Usage:
        css = inject_branding_css(request.state.branding)
        # inject into HTML template
    """
    return f"""
<style>
  :root {{
    --brand-primary:   {branding.get('primary_color', '#c9a96e')};
    --brand-bg:        {branding.get('bg_color', '#0f0f0f')};
    --brand-text:      {branding.get('text_color', '#e8e8e8')};
    --brand-font:      '{branding.get('font', 'Segoe UI')}', system-ui, sans-serif;
  }}
  body {{ font-family: var(--brand-font); }}
  .brand-primary {{ color: var(--brand-primary) !important; }}
  .brand-bg {{ background: var(--brand-bg) !important; }}
  .btn-brand {{
    background: var(--brand-primary);
    color: #000;
    border: none;
    border-radius: 7px;
    padding: 9px 18px;
    font-weight: 600;
    cursor: pointer;
  }}
  .btn-brand:hover {{ opacity: .88; }}
  header .logo-text {{ color: var(--brand-primary); }}
</style>
""".strip()
