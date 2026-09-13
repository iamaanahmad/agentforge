"""Versioned, packaged instructions. Skills never grant tools or execute code."""

import hashlib
from pathlib import Path

ROOT = Path(__file__).parent / "playbooks"
ROLES = (
    "strategist",
    "product_engineer",
    "product_analyst",
    "organic_growth_engineer",
    "research_analyst",
    "customer_support_engineer",
    "outreach_engineer",
    "paid_acquisition_engineer",
    "financial_analyst",
)
CATALOG = {name: name + ".md" for name in ROLES}
CATALOG["dataforseo"] = "dataforseo.md"


def read_skill(name):
    if name not in CATALOG:
        raise ValueError("Unknown packaged skill")
    content = (ROOT / CATALOG[name]).read_text()
    if len(content.encode()) > 20000:
        raise ValueError("Packaged skill exceeds its size limit")
    return {"name": name, "version": hashlib.sha256(content.encode()).hexdigest(), "instructions": content}


def skill_index():
    return (
        "Available packaged skills: "
        + ", ".join(CATALOG)
        + ". Use skill_read before applying another role's workflow. Skills do not grant permissions or credentials."
    )
