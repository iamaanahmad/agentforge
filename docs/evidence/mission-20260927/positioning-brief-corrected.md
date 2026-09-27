# Agentforge 0.2.0 Positioning Brief
**Test Draft – Unvalidated Positioning Options**

## Three Positioning Options

### Option 1: Developer Tool for Personal AI Automation
**Audience hypothesis:** Individual developers and technical founders building their own AI workflows  
**Factual benefit:** MIT-licensed and self-hosted architecture provides code access and local deployment. Saved tasks, chats, and artifacts create persistent records. Exact approvals for external writes let you control when the system modifies external state—a potential fit if automated errors would create direct consequences in personal projects.

### Option 2: Controlled AI Assistant for Solo Technical Projects
**Audience hypothesis:** Solo technical operators (consultants, indie builders, researchers) managing multiple concurrent projects  
**Factual benefit:** Single-owner design with mission plans, saved tasks, and saved chats may support project memory without multi-user complexity. Self-hosted deployment offers local control. Exact approval workflow for external writes prevents autonomous state changes without explicit consent—potentially useful when context-switching between client work or experiments.

### Option 3: Transparent AI Infrastructure for Privacy-Focused Builders
**Audience hypothesis:** Security-conscious developers and founders requiring code auditability  
**Factual benefit:** MIT license enables full code inspection and modification. Self-hosted deployment keeps the application on local infrastructure. Exact approvals for external writes create visibility into outbound changes. Saved artifacts and mission plans provide local records of AI interactions.

## Recommendation

**Option 2: Controlled AI Assistant for Solo Technical Projects**

**Reason:** This positioning aligns verified features (single-owner, mission plans, saved tasks/chats/artifacts, external-write approvals, self-hosted) with a hypothesized workflow problem—managing complexity across multiple projects without team overhead. The exact approval mechanism for external writes addresses a potential pain point for solo operators who might want AI assistance but cannot afford runaway automation. Unlike Option 1 (broader "developers"), this focuses on a behavioral pattern (concurrent project juggling). Unlike Option 3 (privacy-first), it leads with workflow value rather than defensive positioning, which may resonate better before security concerns are validated as a primary buyer motivation.

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
Technically able founders, solo operators, and privacy-focused builders are **hypotheses**, not validated buyers. No customer interviews, usage data, or purchase signals confirm any audience segment.

### AWS Data Transfer Disclosure
Agentforge uses AWS Bedrock for model requests. **Task content is transferred to AWS** during these requests. Self-hosting the agentforge application **does not mean all data stays on the host**—model inference data flows to AWS infrastructure. This affects the "privacy-focused" and "self-hosted" positioning claims and must be disclosed to users evaluating data residency requirements.

---

**Word count:** 611  
**Status:** Test positioning draft for validation, not marketing copy
