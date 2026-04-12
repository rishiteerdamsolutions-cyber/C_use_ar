"""
Tenant Config — per-agency branding and settings.
Autonomous Web Agency Platform · White-label Layer

Every white-label agency gets their own tenant document in MongoDB.
One codebase, N tenants — branding injected at request time.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ─── Default branding (platform owner's own theme) ────────────────────────────
DEFAULT_BRANDING = {
    "agency_name":    "Autonomous Web Agency",
    "tagline":        "One command. Live website.",
    "logo_url":       "/static/logo.png",
    "favicon_url":    "/static/favicon.ico",
    "primary_color":  "#c9a96e",
    "bg_color":       "#0f0f0f",
    "text_color":     "#e8e8e8",
    "font":           "Segoe UI",
    "support_email":  "support@yourplatform.com",
    "support_phone":  "",
    "website":        "https://yourplatform.com",
    "footer_text":    "Powered by Autonomous Web Agency",
    "show_powered_by": True,
}


# ─── CRUD ─────────────────────────────────────────────────────────────────────
def create_tenant(
    subdomain:     str,
    agency_name:   str,
    owner_email:   str,
    owner_name:    str,
    branding:      Optional[dict[str, Any]] = None,
    plan:          str = "whitelabel_monthly",
) -> str:
    """
    Create a new white-label tenant in MongoDB.

    Args:
        subdomain:   e.g. 'digitaledge' → digitaledge.yourplatform.com
        agency_name: Displayed name of the agency.
        owner_email: Admin email.
        owner_name:  Admin name.
        branding:    Override any keys from DEFAULT_BRANDING.
        plan:        Billing plan (whitelabel_monthly = ₹9,999/mo).

    Returns:
        tenant_id (MongoDB _id string)
    """
    from agency_api.database import get_collection, Collections

    col = get_collection(Collections.TENANTS)

    # Check subdomain uniqueness
    if col.find_one({"subdomain": subdomain.lower()}):
        raise ValueError(f"Subdomain '{subdomain}' is already taken")

    merged_branding = {**DEFAULT_BRANDING, **(branding or {})}
    merged_branding["agency_name"] = agency_name

    doc = {
        "subdomain":    subdomain.lower(),
        "agency_name":  agency_name,
        "owner_email":  owner_email,
        "owner_name":   owner_name,
        "branding":     merged_branding,
        "plan":         plan,
        "status":       "active",
        "created_at":   datetime.now(timezone.utc),
        "renewed_at":   datetime.now(timezone.utc),
        "api_calls_used": 0,
        "custom_domain": None,    # e.g. app.digitaledge.in
        "features": {
            "all_templates":    True,
            "custom_templates": False,
            "api_access":       True,
            "white_label":      True,
            "remove_powered_by": False,
        },
    }

    result    = col.insert_one(doc)
    tenant_id = str(result.inserted_id)
    logger.info("Tenant created: %s (%s) tenant_id=%s", subdomain, agency_name, tenant_id)
    return tenant_id


def get_tenant_by_subdomain(subdomain: str) -> Optional[dict[str, Any]]:
    """Look up a tenant by their subdomain."""
    from agency_api.database import get_collection, Collections
    col = get_collection(Collections.TENANTS)
    doc = col.find_one({"subdomain": subdomain.lower(), "status": "active"})
    if doc:
        doc["_id"] = str(doc["_id"])
    return doc


def get_tenant_by_domain(custom_domain: str) -> Optional[dict[str, Any]]:
    """Look up a tenant by their custom domain (e.g. app.digitaledge.in)."""
    from agency_api.database import get_collection, Collections
    col = get_collection(Collections.TENANTS)
    doc = col.find_one({"custom_domain": custom_domain.lower(), "status": "active"})
    if doc:
        doc["_id"] = str(doc["_id"])
    return doc


def get_tenant_by_id(tenant_id: str) -> Optional[dict[str, Any]]:
    from agency_api.database import get_collection, Collections
    from bson import ObjectId  # type: ignore
    col = get_collection(Collections.TENANTS)
    doc = col.find_one({"_id": ObjectId(tenant_id)})
    if doc:
        doc["_id"] = str(doc["_id"])
    return doc


def update_branding(tenant_id: str, branding_patch: dict[str, Any]) -> bool:
    """Patch branding fields for a tenant (logo, colours, etc.)."""
    from agency_api.database import get_collection, Collections
    from bson import ObjectId  # type: ignore
    col = get_collection(Collections.TENANTS)
    patch = {f"branding.{k}": v for k, v in branding_patch.items()}
    result = col.update_one({"_id": ObjectId(tenant_id)}, {"$set": patch})
    logger.info("Branding updated for tenant %s", tenant_id)
    return result.modified_count > 0


def set_custom_domain(tenant_id: str, domain: str) -> bool:
    """Set a custom domain for a tenant (e.g. app.digitaledge.in)."""
    from agency_api.database import get_collection, Collections
    from bson import ObjectId  # type: ignore
    col    = get_collection(Collections.TENANTS)
    result = col.update_one(
        {"_id": ObjectId(tenant_id)},
        {"$set": {"custom_domain": domain.lower()}},
    )
    logger.info("Custom domain set: %s → %s", tenant_id, domain)
    return result.modified_count > 0


def suspend_tenant(tenant_id: str) -> bool:
    """Suspend a tenant (non-payment or policy violation)."""
    from agency_api.database import get_collection, Collections
    from bson import ObjectId  # type: ignore
    col    = get_collection(Collections.TENANTS)
    result = col.update_one(
        {"_id": ObjectId(tenant_id)},
        {"$set": {"status": "suspended"}},
    )
    logger.warning("Tenant suspended: %s", tenant_id)
    return result.modified_count > 0


def list_tenants(status: str = "active") -> list[dict[str, Any]]:
    """List all tenants (admin use)."""
    from agency_api.database import get_collection, Collections
    col  = get_collection(Collections.TENANTS)
    docs = list(col.find({"status": status}, sort=[("created_at", -1)]))
    for d in docs:
        d["_id"] = str(d["_id"])
    return docs


def get_branding(subdomain: str) -> dict[str, Any]:
    """
    Get branding for a subdomain. Returns DEFAULT_BRANDING if tenant not found.
    Safe to call on every request — used for CSS/HTML injection.
    """
    tenant = get_tenant_by_subdomain(subdomain)
    if tenant:
        return tenant.get("branding", DEFAULT_BRANDING)
    return DEFAULT_BRANDING
