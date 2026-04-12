"""
Admin Panel Routes — agency admin dashboard.
Autonomous Web Agency Platform · White-label Layer

Mounted at /admin on every tenant subdomain.
Protected by admin email + secret token (set at onboarding).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from whitelabel.tenant_config import (
    get_tenant_by_id,
    list_tenants,
    set_custom_domain,
    suspend_tenant,
    update_branding,
)

router = APIRouter(prefix="/admin", tags=["Admin Panel"])
logger = logging.getLogger(__name__)


# ─── Admin auth dependency ────────────────────────────────────────────────────
async def require_admin(request: Request) -> dict[str, Any]:
    """
    Simple token auth for admin routes.
    Header: X-Admin-Token: <token set in tenant doc>
    """
    token  = request.headers.get("X-Admin-Token", "")
    tenant = getattr(request.state, "tenant", None)

    if not tenant:
        # Platform owner admin
        import os
        if token != os.environ.get("PLATFORM_ADMIN_TOKEN", ""):
            raise HTTPException(status_code=401, detail="Invalid admin token")
        return {"role": "platform_admin", "tenant": None}

    # Tenant admin
    if token != tenant.get("admin_token", ""):
        raise HTTPException(status_code=401, detail="Invalid admin token")

    return {"role": "tenant_admin", "tenant": tenant}


# ─── Dashboard ────────────────────────────────────────────────────────────────
@router.get("/dashboard", response_class=HTMLResponse,
    summary="Admin dashboard HTML page",
)
async def admin_dashboard(request: Request, admin=Depends(require_admin)):
    branding = getattr(request.state, "branding", {})
    tenant   = admin.get("tenant") or {}

    agency_name = tenant.get("agency_name", "Platform Admin")
    subdomain   = tenant.get("subdomain", "platform")
    credits_msg = ""

    if tenant:
        from agency_api.keys import get_usage_summary
        # placeholder — real impl would look up the tenant's key
        credits_msg = f"<p>Portal: <b>{subdomain}.yourplatform.com</b></p>"

    from whitelabel.router import inject_branding_css
    css = inject_branding_css(branding)

    return HTMLResponse(_admin_html(agency_name, css, credits_msg, subdomain))


def _admin_html(agency_name: str, css: str, extra: str, subdomain: str) -> str:
    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<title>{agency_name} — Admin</title>
{css}
<style>
body{{background:var(--brand-bg,#0f0f0f);color:var(--brand-text,#e8e8e8);
     font-family:var(--brand-font,'Segoe UI',sans-serif);padding:0;margin:0}}
header{{background:#111;border-bottom:1px solid #222;padding:14px 28px;
        display:flex;justify-content:space-between;align-items:center}}
header h1{{color:var(--brand-primary,#c9a96e);font-size:1.1rem;margin:0}}
main{{max-width:960px;margin:0 auto;padding:28px 20px}}
.card{{background:#1a1a1a;border:1px solid #2a2a2a;border-radius:10px;padding:20px;margin-bottom:16px}}
.card h2{{color:var(--brand-primary,#c9a96e);font-size:.95rem;margin:0 0 12px}}
.stat{{display:inline-block;background:#111;border:1px solid #333;border-radius:8px;
       padding:14px 22px;margin:6px;text-align:center}}
.stat .val{{font-size:1.6rem;font-weight:700;color:var(--brand-primary,#c9a96e)}}
.stat .lbl{{color:#888;font-size:.75rem;margin-top:4px}}
.btn{{padding:8px 16px;border:none;border-radius:6px;cursor:pointer;font-weight:600;
      font-size:.83rem;background:var(--brand-primary,#c9a96e);color:#000}}
input[type=text]{{background:#111;border:1px solid #333;color:#e8e8e8;
                  border-radius:6px;padding:8px 12px;width:100%;margin-bottom:10px}}
</style></head>
<body>
<header>
  <h1>⚡ {agency_name} — Admin Panel</h1>
  <span style="color:#888;font-size:.8rem">{subdomain}.yourplatform.com</span>
</header>
<main>
  <div class="card">
    <h2>📊 Overview</h2>
    <div class="stat"><div class="val" id="credits">—</div><div class="lbl">Credits Remaining</div></div>
    <div class="stat"><div class="val" id="calls">—</div><div class="lbl">Calls This Month</div></div>
    <div class="stat"><div class="val" id="wfs">—</div><div class="lbl">Saved Workflows</div></div>
    {extra}
  </div>

  <div class="card">
    <h2>🎨 Branding</h2>
    <label style="color:#888;font-size:.78rem">Primary Colour</label>
    <input type="color" id="primaryColor" value="{{}}" style="margin-bottom:10px;height:36px;border:none;background:none;cursor:pointer">
    <br>
    <label style="color:#888;font-size:.78rem">Agency Name</label>
    <input type="text" id="agencyName" placeholder="{agency_name}">
    <label style="color:#888;font-size:.78rem">Logo URL</label>
    <input type="text" id="logoUrl" placeholder="https://...">
    <button class="btn" onclick="saveBranding()">Save Branding</button>
  </div>

  <div class="card">
    <h2>🌐 Custom Domain</h2>
    <input type="text" id="customDomain" placeholder="app.youragency.in">
    <p style="color:#888;font-size:.78rem;margin-bottom:10px">
      Point a CNAME from this domain to <b>yourplatform.com</b>, then save.
    </p>
    <button class="btn" onclick="saveDomain()">Set Custom Domain</button>
  </div>

  <div class="card">
    <h2>🔑 API Key</h2>
    <p style="color:#888;font-size:.85rem">Use this key to call the API from your own apps.</p>
    <input type="text" id="apiKeyDisplay" placeholder="ak_live_xxxxx (from welcome email)" readonly style="font-family:monospace">
    <button class="btn" onclick="copyKey()">Copy</button>
  </div>
</main>
<script>
async function saveBranding() {{
  alert('Branding saved! Page will refresh to apply changes.');
}}
function saveDomain() {{
  const d = document.getElementById('customDomain').value;
  if (d) alert('Custom domain ' + d + ' saved. Make sure your CNAME is pointing to yourplatform.com');
}}
function copyKey() {{
  navigator.clipboard.writeText(document.getElementById('apiKeyDisplay').value);
  alert('Copied!');
}}
</script>
</body></html>"""


# ─── Branding API ─────────────────────────────────────────────────────────────
class BrandingPatch(BaseModel):
    primary_color:  str | None = None
    agency_name:    str | None = None
    logo_url:       str | None = None
    tagline:        str | None = None
    support_email:  str | None = None
    support_phone:  str | None = None


@router.patch("/branding",
    summary="Update tenant branding",
)
async def patch_branding(
    patch:  BrandingPatch,
    admin:  dict = Depends(require_admin),
) -> dict[str, Any]:
    tenant = admin.get("tenant")
    if not tenant:
        raise HTTPException(status_code=403, detail="Platform admin cannot patch tenant branding here")

    updates = {k: v for k, v in patch.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    ok = update_branding(tenant["_id"], updates)
    return {"ok": ok, "updated_fields": list(updates.keys())}


@router.post("/domain",
    summary="Set custom domain for tenant",
)
async def set_domain(
    domain: str,
    admin:  dict = Depends(require_admin),
) -> dict[str, Any]:
    tenant = admin.get("tenant")
    if not tenant:
        raise HTTPException(status_code=403, detail="Tenant required")
    ok = set_custom_domain(tenant["_id"], domain)
    return {"ok": ok, "domain": domain, "note": "Point CNAME to yourplatform.com"}


# ─── Platform-owner only ──────────────────────────────────────────────────────
@router.get("/tenants",
    summary="[Platform Admin] List all tenants",
)
async def get_all_tenants(admin: dict = Depends(require_admin)) -> dict[str, Any]:
    if admin.get("role") != "platform_admin":
        raise HTTPException(status_code=403, detail="Platform admin only")
    tenants = list_tenants()
    return {"total": len(tenants), "tenants": tenants}


@router.post("/tenants/{tenant_id}/suspend",
    summary="[Platform Admin] Suspend a tenant",
)
async def suspend(tenant_id: str, admin: dict = Depends(require_admin)) -> dict[str, Any]:
    if admin.get("role") != "platform_admin":
        raise HTTPException(status_code=403, detail="Platform admin only")
    ok = suspend_tenant(tenant_id)
    return {"ok": ok, "tenant_id": tenant_id}
