from pathlib import Path

"""Original role definitions; all roles share the same server-enforced tool policy."""
AGENTS = [
    {
        "id": "strategist",
        "name": "Strategist",
        "description": "Turn evidence into a focused growth plan.",
        "skills": ["Opportunity ranking", "Positioning", "Weekly planning"],
    },
    {
        "id": "product_engineer",
        "name": "Product engineer",
        "description": "Inspect code and propose tested product changes.",
        "skills": ["Code review", "Bug diagnosis", "GitHub changes"],
    },
    {
        "id": "product_analyst",
        "name": "Product analyst",
        "description": "Explain activation, retention, and usage from supplied evidence.",
        "skills": ["Funnel analysis", "Experiment design", "Data quality"],
    },
    {
        "id": "organic_growth_engineer",
        "name": "Organic growth engineer",
        "description": "Create useful search content and improve discoverability.",
        "skills": ["SEO briefs", "Content strategy", "Search research"],
    },
    {
        "id": "research_analyst",
        "name": "Research analyst",
        "description": "Find and compare customer, competitor, and market evidence.",
        "skills": ["Web research", "Competitor maps", "Source synthesis"],
    },
    {
        "id": "customer_support_engineer",
        "name": "Customer support engineer",
        "description": "Investigate supplied issues and prepare accurate customer replies.",
        "skills": ["Issue triage", "Reply drafting", "Escalation"],
    },
    {
        "id": "outreach_engineer",
        "name": "Outreach engineer",
        "description": "Research prospects and prepare personal outreach for approval.",
        "skills": ["Prospect research", "Email drafting", "Follow-up plans"],
    },
    {
        "id": "paid_acquisition_engineer",
        "name": "Paid acquisition engineer",
        "description": "Develop campaign plans and creative without activating spend.",
        "skills": ["Ad copy", "Campaign planning", "Measurement design"],
    },
    {
        "id": "financial_analyst",
        "name": "Financial analyst",
        "description": "Analyze supplied revenue, pricing, costs, and retention data.",
        "skills": ["Unit economics", "Pricing analysis", "Revenue forecasts"],
    },
]


def agent_prompt(agent_id):
    agent = next(a for a in AGENTS if a["id"] == agent_id)
    from .skills import skill_index

    playbook = (Path(__file__).parent / "playbooks" / (agent_id + ".md")).read_text()
    return (
        playbook
        + "\n"
        + skill_index()
        + "\n"
        + f"You are Agent4Good's {agent['name']}. {agent['description']} Skills: {', '.join(agent['skills'])}."
    )
