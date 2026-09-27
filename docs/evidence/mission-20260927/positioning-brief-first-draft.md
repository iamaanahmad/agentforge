# Agentforge 0.2.0 Positioning Brief
**Test Draft – Unvalidated Positioning Options**

## Three Positioning Options

### Option 1: Developer Tool for Personal AI Automation
**Audience:** Individual developers and technical founders building their own AI workflows  
**Factual Benefit:** MIT-licensed and self-hosted architecture gives you full code access and local deployment control. Saved tasks, chats, and artifacts create a persistent workspace where you own the data. Exact approval gates for external writes mean you review every action before execution—critical for personal projects where mistakes have direct consequences.

### Option 2: Controlled AI Assistant for Solo Technical Projects
**Audience:** Solo technical operators (consultants, indie builders, researchers) managing multiple concurrent projects  
**Factual Benefit:** Single-owner design with mission plans and saved tasks provides structured project memory without team collaboration overhead. Self-hosted deployment means no per-seat pricing or account limits. Exact approval workflow prevents autonomous actions from changing external state without your explicit consent—useful when context-switching between client work or experiments.

### Option 3: Transparent AI Infrastructure for Privacy-Focused Builders
**Audience:** Security-conscious developers and founders requiring code auditability  
**Factual Benefit:** MIT license enables full code inspection and modification. Self-hosted deployment removes SaaS vendor dependencies. Exact approval gates create an audit trail for every external write. Saved artifacts and mission plans provide local records of AI interactions without relying on third-party service logs.

## Recommendation

**Option 2: Controlled AI Assistant for Solo Technical Projects**

**Reason:** This positioning aligns verified features (single-owner, mission plans, approval gates, self-hosted) with a specific workflow problem—managing complexity across multiple projects without team overhead. The exact approval mechanism addresses a concrete pain point for solo operators who need AI assistance but cannot afford runaway automation. Unlike Option 1 (broader "developers"), this focuses on a behavioral pattern (concurrent project juggling). Unlike Option 3 (privacy-first), it leads with workflow value rather than defensive positioning, which may resonate better before security concerns are validated as a primary buyer motivation.

## Interview Questions to Test Demand

1. **Current workflow probe:** "When you're managing 3+ technical projects simultaneously, how do you currently track context, tasks, and decisions across them? What breaks down first when you context-switch?"

2. **Automation risk assessment:** "Have you used AI coding assistants or automation tools in client or personal projects? What stops you from giving them more autonomous access? Can you describe a specific time you wished for more control?"

3. **Willingness to self-host:** "If a tool solved [problem from Q1], would you prefer a hosted service or self-hosting? What would make self-hosting worth the setup time for you?"

## Limits

### Unknown and Unverified
- **Buyer demand:** No validated evidence that technically able founders, solo operators, or any other audience will pay for agentforge or prioritize its feature set over alternatives.
- **Pricing:** No pricing model, willingness-to-pay data, or revenue validation exists.
- **Production operation:** Deployment reliability, performance at scale, update/maintenance burden, and support requirements are unverified.
- **Broad autonomous reliability:** Whether the system performs well across diverse real-world tasks and domains is untested.

### Audience Status
Technically able founders are a **hypothesis**, not validated buyers. No customer interviews, usage data, or purchase signals confirm this or any other audience segment.

### AWS Data Transfer Disclosure
Agentforge uses AWS Bedrock for model requests. **Task content is transferred to AWS** during these requests. Self-hosting the agentforge application **does not mean all data stays on the host**—model inference data flows to AWS infrastructure. This affects the "privacy-focused" and "self-hosted" positioning claims and must be disclosed to users evaluating data residency requirements.

---

**Word count:** 612  
**Status:** Test positioning draft for validation, not marketing copy
