# Migration Plan: Replace `cost-explorer-mcp-server` with `billing-cost-management-mcp-server`

**Audience**: An implementing agent. Follow this plan step by step. Do not deviate without asking the user.

**Goal**: In the `aws-cost-ops` plugin, replace the deprecated `awslabs.cost-explorer-mcp-server` MCP server (short name `costexp`) with the consolidated `awslabs.billing-cost-management-mcp-server` (short name `billing`). Update the skill content and reference docs to use the new server's name and advertise its expanded capabilities (Budgets, Free Tier, Cost Optimization Hub, Compute Optimizer, Savings Plans / RI, S3 Storage Lens, Billing Conductor). Bump versions. Do not modify the `pricing` or `cw` MCP servers.

## Background (read before editing)

- The old server's GitHub path (`https://github.com/awslabs/mcp/tree/main/src/cost-explorer-mcp-server`) returns **404** — it has been retired upstream.
- The new server lives at `https://github.com/awslabs/mcp/tree/main/src/billing-cost-management-mcp-server` and is a superset of the old Cost Explorer server. It covers:
  - **Cost Explorer** (18 tools: cost analysis, forecasting, anomalies, comparisons, dimensions, tags)
  - **AWS Budgets** (budget status and monitoring)
  - **AWS Free Tier** (usage monitoring)
  - **AWS Pricing** (basic pricing lookups — the dedicated `pricing` server stays for deeper workflows)
  - **Cost Optimization Hub** (cross-service recommendations)
  - **Compute Optimizer** (right-sizing for EC2, Lambda, EBS, ECS, RDS, Auto Scaling groups)
  - **Pricing Calculator** (workload estimates)
  - **S3 Storage Lens** (via Athena queries — needs extra env vars, see below)
  - **AWS Billing Conductor** (billing groups, proforma analysis, custom line items, pricing plans/rules)
- `awslabs.aws-pricing-mcp-server` is still active upstream and stays. `awslabs.cloudwatch-mcp-server` is unrelated and stays.
- Short MCP server names in this repo must stay short to respect Bedrock's 64-char tool-name limit. The new short name is **`billing`**.

## Files to modify (exhaustive list)

1. `.claude-plugin/marketplace.json`
2. `plugins/aws-cost-ops/skills/aws-cost-operations/SKILL.md`
3. `plugins/aws-cost-ops/skills/aws-cost-operations/references/operations-patterns.md`
4. `README.md` (repo root)

Do not modify any other files. Do **not** touch `plugins/aws-cost-ops/skills/aws-cost-operations/references/cloudwatch-alarms.md` — it has no cost-MCP references.

## Out of scope (do not do these)

- Do **not** remove or alter the `pricing` MCP server.
- Do **not** remove or alter the `cw` MCP server.
- Do **not** add `STORAGE_LENS_MANIFEST_LOCATION` or other optional env vars to `marketplace.json` — users configure those per-environment. Only document them in the skill body.
- Do **not** run skill-creator evals.
- Do **not** recreate symlinks in `.claude/skills/` (those are developer-local and already link to `plugins/...` paths).
- Do **not** create new reference files. All changes go into existing files.
- Do **not** commit. The user will review and commit.

---

## Step 1 — Update `.claude-plugin/marketplace.json`

### 1a. Replace the `costexp` server block with a `billing` server block

**File**: `.claude-plugin/marketplace.json`

**Find this block** (inside the `aws-cost-ops` plugin's `mcpServers`, approximately lines 91-100):

```json
        "costexp": {
          "type": "stdio",
          "command": "uvx",
          "args": [
            "awslabs.cost-explorer-mcp-server@latest"
          ],
          "env": {
            "FASTMCP_LOG_LEVEL": "ERROR"
          }
        },
```

**Replace with**:

```json
        "billing": {
          "type": "stdio",
          "command": "uvx",
          "args": [
            "awslabs.billing-cost-management-mcp-server@latest"
          ],
          "env": {
            "FASTMCP_LOG_LEVEL": "ERROR"
          }
        },
```

Preserve indentation (8 spaces). Preserve the trailing comma if one was there. Do not reorder server entries — keep `pricing`, then `billing` (replacing `costexp`), then `cw`.

### 1b. Update the plugin description

**Find** (in the `aws-cost-ops` plugin object, the `description` field — around line 64):

```json
      "description": "AWS cost optimization, monitoring, and operational excellence with integrated MCP servers for billing, cost analysis, observability, and security assessment",
```

Leave this description as-is. It already mentions "billing" generically and still applies.

### 1c. Bump the `aws-cost-ops` plugin version

**Find** (in the `aws-cost-ops` plugin object, around line 65):

```json
      "version": "1.2.1",
```

**Replace with**:

```json
      "version": "1.3.0",
```

Rationale: adding a new MCP server is a **minor** version bump per `CLAUDE.md` rules. The old server is replaced (not removed without substitute), so behavior for existing usage is preserved.

### 1d. Bump the marketplace version

**Find** (at top of file, around line 9):

```json
    "version": "2.4.0"
```

**Replace with**:

```json
    "version": "2.5.0"
```

Rationale: a plugin's minor bump rolls up to a marketplace minor bump.

---

## Step 2 — Update `plugins/aws-cost-ops/skills/aws-cost-operations/SKILL.md`

This is the main skill content. Be surgical — only change what's listed.

### 2a. Frontmatter: update `allowed-tools`

**Find** (line 9):

```
  - mcp__costexp__*
```

**Replace with**:

```
  - mcp__billing__*
```

Do not touch `mcp__pricing__*`, `mcp__cw__*`, `mcp__aws-mcp__*`, `mcp__awsdocs__*`, or any `Bash(...)` entries.

### 2b. Rewrite the "Integrated MCP Servers" section

**Find** this section (starts around line 34, ends around line 66):

```markdown
## Integrated MCP Servers

This plugin provides 3 MCP servers:

### Bundled Servers

#### 1. AWS Pricing MCP Server (`pricing`)
**Purpose**: Pre-deployment cost estimation and optimization
- Estimate costs before deploying resources
- Compare pricing across regions
- Calculate Total Cost of Ownership (TCO)
- Evaluate different service options for cost efficiency

#### 2. AWS Cost Explorer MCP Server (`costexp`)
**Purpose**: Detailed cost analysis and reporting
- Analyze historical spending patterns
- Identify cost anomalies and trends
- Forecast future costs
- Analyze cost by service, region, or tag

#### 3. Amazon CloudWatch MCP Server (`cw`)
**Purpose**: Metrics, alarms, and logs analysis
- Query CloudWatch metrics and logs
- Create and manage CloudWatch alarms
- Troubleshoot operational issues
- Monitor resource utilization

> **Note**: The following servers are available separately via the Full AWS MCP Server (see `aws-mcp-setup` skill) and are not bundled with this plugin:
> - AWS Billing and Cost Management MCP — Real-time billing details
> - CloudWatch Application Signals MCP — APM and SLOs
> - AWS Managed Prometheus MCP — PromQL queries for containers
> - AWS CloudTrail MCP — API activity audit
> - AWS Well-Architected Security Assessment MCP — Security posture assessment
```

**Replace with**:

```markdown
## Integrated MCP Servers

This plugin provides 3 MCP servers:

### Bundled Servers

#### 1. AWS Pricing MCP Server (`pricing`)
**Purpose**: Pre-deployment cost estimation and optimization
- Estimate costs before deploying resources
- Compare pricing across regions
- Calculate Total Cost of Ownership (TCO)
- Evaluate different service options for cost efficiency

#### 2. AWS Billing and Cost Management MCP Server (`billing`)
**Purpose**: Post-deployment cost analysis, budget tracking, and optimization recommendations. This is a consolidated server that replaces the retired `cost-explorer-mcp-server` and covers a broader surface area.
- **Cost Explorer**: historical spending analysis, forecasting, anomaly detection, month-over-month comparisons, cost-and-usage breakdowns by service/region/tag
- **AWS Budgets**: monitor budget status, track threshold breaches
- **AWS Free Tier**: monitor Free Tier usage to avoid unexpected charges
- **Cost Optimization Hub**: cross-service cost-saving recommendations
- **Compute Optimizer**: right-sizing recommendations for EC2, Lambda, EBS, ECS, RDS, and Auto Scaling groups
- **Savings Plans / Reserved Instances**: purchase recommendations, coverage, and utilization analysis
- **S3 Storage Lens**: storage cost analysis via Athena (requires `STORAGE_LENS_MANIFEST_LOCATION` env var pointing at the Storage Lens manifest S3 URI; optional `STORAGE_LENS_OUTPUT_LOCATION` for Athena results)
- **Pricing Calculator**: query saved workload estimates
- **AWS Billing Conductor**: billing groups, proforma cost reports, account associations, custom line items, pricing rules

> **IAM**: this server requires a broader permission set than the old `costexp` — notably `compute-optimizer:*`, `cost-optimization-hub:*`, `budgets:ViewBudget`, `freetier:GetFreeTierUsage`, and (for Storage Lens) Athena + S3 read/write on the results bucket. See the upstream README for the full IAM policy: https://github.com/awslabs/mcp/tree/main/src/billing-cost-management-mcp-server

#### 3. Amazon CloudWatch MCP Server (`cw`)
**Purpose**: Metrics, alarms, and logs analysis
- Query CloudWatch metrics and logs
- Create and manage CloudWatch alarms
- Troubleshoot operational issues
- Monitor resource utilization

> **Note**: The following servers are available separately via the Full AWS MCP Server (see `aws-mcp-setup` skill) and are not bundled with this plugin:
> - CloudWatch Application Signals MCP — APM and SLOs
> - AWS Managed Prometheus MCP — PromQL queries for containers
> - AWS CloudTrail MCP — API activity audit
> - AWS Well-Architected Security Assessment MCP — Security posture assessment
```

Critical: the old note listed "AWS Billing and Cost Management MCP" as not-bundled. That line must be **removed** since it's now bundled (this is the new server we just added).

### 2c. Update "Cost Analysis and Optimization" section

**Find** (around line 100):

```markdown
**Regular cost reviews**:
1. Use **Cost Explorer MCP** to analyze spending trends
2. Identify cost anomalies and unexpected charges
3. Review costs by service, region, and environment
4. Compare actual vs. budgeted costs
5. Generate cost optimization recommendations
```

**Replace with**:

```markdown
**Regular cost reviews**:
1. Use the **Billing and Cost Management MCP** (`billing`) to analyze spending trends and detect anomalies
2. Identify cost anomalies and unexpected charges
3. Review costs by service, region, and environment
4. Compare actual vs. budgeted costs
5. Pull cross-service recommendations from **Cost Optimization Hub** and right-sizing suggestions from **Compute Optimizer** (both exposed via the `billing` server)
```

### 2d. Update "Cost optimization strategies" bullets

**Find** (around line 107):

```markdown
**Cost optimization strategies**:
- Right-size over-provisioned resources
- Use appropriate storage classes (S3, EBS)
- Implement auto-scaling for dynamic workloads
- Leverage Savings Plans and Reserved Instances
- Delete unused resources and snapshots
- Use cost allocation tags effectively
```

**Replace with**:

```markdown
**Cost optimization strategies**:
- Right-size over-provisioned resources (use Compute Optimizer recommendations via the `billing` server, then validate with CloudWatch utilization data)
- Use appropriate storage classes (S3, EBS); for S3 at scale, query Storage Lens via the `billing` server to find lifecycle-policy opportunities
- Implement auto-scaling for dynamic workloads
- Leverage Savings Plans and Reserved Instances — pull purchase recommendations and coverage/utilization from the `billing` server
- Delete unused resources and snapshots
- Use cost allocation tags effectively
```

### 2e. Update "Budget Monitoring" section

**Find** (around line 116):

```markdown
### Budget Monitoring

**Track spending against budgets**:
1. Use **Billing and Cost Management MCP** to monitor budgets
2. Set up budget alerts for threshold breaches
3. Review budget utilization regularly
4. Adjust budgets based on trends
5. Implement cost controls and governance
```

**Replace with**:

```markdown
### Budget Monitoring

**Track spending against budgets**:
1. Use the **`billing` MCP server** to monitor budget status and Free Tier usage
2. Set up budget alerts for threshold breaches (configure the budget + SNS topic separately; the MCP exposes status only)
3. Review budget utilization regularly
4. Adjust budgets based on trends
5. Implement cost controls and governance
```

### 2f. Update "Cost Analysis Workflow"

**Find** (around line 201):

```markdown
### Cost Analysis Workflow

1. **Pre-deployment**: Use Pricing MCP to estimate costs
2. **Post-deployment**: Use Billing MCP to track actual spending
3. **Analysis**: Use Cost Explorer MCP for detailed cost analysis
4. **Optimization**: Implement recommendations from Cost Explorer
```

**Replace with**:

```markdown
### Cost Analysis Workflow

1. **Pre-deployment**: Use the `pricing` MCP to estimate costs
2. **Post-deployment**: Use the `billing` MCP to track actual spending, budgets, and Free Tier usage
3. **Analysis**: Use the `billing` MCP for Cost Explorer breakdowns, forecasts, and anomaly detection
4. **Optimization**: Act on Cost Optimization Hub and Compute Optimizer recommendations (both served by `billing`)
```

### 2g. Do NOT change these sections (leave as-is)

- "AWS Documentation Requirement" (line ~30)
- "When to Use This Skill" (line ~68)
- "Pre-Deployment Cost Estimation" example workflow block (line ~83) — it only references Pricing MCP
- "Monitoring and Observability Best Practices" and all CloudWatch sections
- "Audit and Security Best Practices" — these reference CloudTrail / Well-Architected MCPs which come from `aws-mcp-setup`, not this plugin
- "Monitoring Workflow", "Security Workflow", "MCP Usage Best Practices", "Operational Excellence Guidelines"
- "Additional Resources" and "CloudWatch Alarms Reference" pointers at the bottom

---

## Step 3 — Update `plugins/aws-cost-ops/skills/aws-cost-operations/references/operations-patterns.md`

This reference file has 5 lines to update (line numbers are current snapshot; use grep/Edit with the unique surrounding text to find them).

### 3a. Pattern 2 — Monthly Cost Review (line 42)

**Find**:

```markdown
**MCP Servers**: Cost Explorer MCP, Billing and Cost Management MCP
```

**Replace with**:

```markdown
**MCP Server**: AWS Billing and Cost Management MCP (`billing`) — covers Cost Explorer analysis, budget status, and Free Tier monitoring in one server
```

### 3b. Pattern 3 — Right-Sizing Resources (line 62)

**Find**:

```markdown
**MCP Servers**: CloudWatch MCP, Cost Explorer MCP
```

**Replace with**:

```markdown
**MCP Servers**: AWS Billing and Cost Management MCP (`billing`) for Compute Optimizer right-sizing recommendations, CloudWatch MCP (`cw`) for utilization validation
```

Then also update Pattern 3's **Steps** to reflect the new recommendation source. **Find**:

```markdown
**Steps**:
1. Query CloudWatch for resource utilization metrics
2. Identify over-provisioned resources (< 40% utilization)
3. Identify under-provisioned resources (> 80% utilization)
4. Calculate potential savings from right-sizing
5. Plan and execute right-sizing changes
6. Monitor post-change performance
```

**Replace with**:

```markdown
**Steps**:
1. Pull Compute Optimizer recommendations via the `billing` MCP (authoritative right-sizing source across EC2, Lambda, EBS, ECS, RDS, Auto Scaling groups)
2. Cross-check against CloudWatch utilization metrics for the candidate resources
3. Identify over-provisioned resources (< 40% utilization) and under-provisioned resources (> 80% utilization)
4. Calculate potential savings from right-sizing (Compute Optimizer surfaces estimated savings directly)
5. Plan and execute right-sizing changes
6. Monitor post-change performance via CloudWatch
```

### 3c. Pattern 4 — Unused Resource Cleanup (line 82)

**Find**:

```markdown
**MCP Servers**: Cost Explorer MCP, CloudTrail MCP
```

**Replace with**:

```markdown
**MCP Servers**: AWS Billing and Cost Management MCP (`billing`) for cost attribution and Cost Optimization Hub idle-resource recommendations, CloudTrail MCP for last-touch activity
```

### 3d. Troubleshooting / Incident Response (line 364)

**Find**:

```markdown
**MCP Servers**: Cost Explorer MCP, CloudWatch MCP, CloudTrail MCP
```

**Replace with**:

```markdown
**MCP Servers**: AWS Billing and Cost Management MCP (`billing`), CloudWatch MCP (`cw`), CloudTrail MCP
```

### 3e. Adjacent step text (line 367)

**Find**:

```markdown
1. Use Cost Explorer to identify service causing spike
```

**Replace with**:

```markdown
1. Use the `billing` MCP (Cost Explorer + anomaly detection) to identify the service causing the spike
```

### 3f. Summary line (line 390)

**Find**:

```markdown
- **Cost Optimization**: Use Pricing, Cost Explorer, and Billing MCPs for proactive cost management
```

**Replace with**:

```markdown
- **Cost Optimization**: Use the `pricing` and `billing` MCPs for proactive cost management (pricing covers pre-deployment estimation; billing covers Cost Explorer, budgets, Free Tier, Cost Optimization Hub, and Compute Optimizer)
```

### 3g. Do NOT change these

- Pattern 1 (line 19) — already correctly references "AWS Pricing MCP".
- Any other section not listed above.

---

## Step 4 — Update `README.md` (repo root)

### 4a. Update the bundled MCP list for the Cost & Operations plugin

**Find** (around lines 32-42):

```markdown
### 2. AWS Cost & Operations Plugin

Cost optimization, monitoring, and operational excellence with 3 integrated MCP servers.

**Features**:
- Cost estimation and optimization
- Monitoring and observability patterns
- Operational best practices

**Integrated MCP Servers**:
- AWS Pricing
- AWS Cost Explorer
- Amazon CloudWatch
```

**Replace with**:

```markdown
### 2. AWS Cost & Operations Plugin

Cost optimization, monitoring, and operational excellence with 3 integrated MCP servers.

**Features**:
- Cost estimation and optimization
- Monitoring and observability patterns
- Operational best practices

**Integrated MCP Servers**:
- AWS Pricing
- AWS Billing and Cost Management (Cost Explorer, Budgets, Free Tier, Cost Optimization Hub, Compute Optimizer, Savings Plans, S3 Storage Lens, Billing Conductor)
- Amazon CloudWatch
```

Leave the rest of the README untouched.

---

## Step 5 — Verification checklist

Run these checks after editing. Each one must pass before reporting complete.

### 5a. No stale references to the old server

```bash
grep -rn -i "cost-explorer-mcp\|costexp\|awslabs\.cost-explorer" \
  .claude-plugin/ plugins/aws-cost-ops/ README.md docs/
```

**Expected output**: only matches inside `docs/billing-mcp-migration-plan.md` (this file). Zero matches anywhere else. If any match appears in `marketplace.json`, `SKILL.md`, `operations-patterns.md`, or `README.md`, the migration is incomplete — fix it.

### 5b. New server is referenced in the right places

```bash
grep -rn "billing-cost-management-mcp-server\|mcp__billing__\|\`billing\`" \
  .claude-plugin/ plugins/aws-cost-ops/ README.md
```

**Expected**: hits in `marketplace.json` (package name), `SKILL.md` (frontmatter `mcp__billing__*` + body references), `operations-patterns.md` (body references), `README.md` (plugin feature list).

### 5c. JSON still parses

```bash
python3 -c "import json; json.load(open('.claude-plugin/marketplace.json'))" && echo "JSON OK"
```

Must print `JSON OK`. If this fails, you broke the JSON (check trailing commas, quotes, braces).

### 5d. Version bumps landed

```bash
python3 -c "
import json
m = json.load(open('.claude-plugin/marketplace.json'))
print('marketplace:', m['metadata']['version'])
for p in m['plugins']:
    if p['name'] == 'aws-cost-ops':
        print('aws-cost-ops:', p['version'])
"
```

**Expected**:
```
marketplace: 2.5.0
aws-cost-ops: 1.3.0
```

### 5e. The plugin still declares exactly 3 MCP servers

```bash
python3 -c "
import json
m = json.load(open('.claude-plugin/marketplace.json'))
for p in m['plugins']:
    if p['name'] == 'aws-cost-ops':
        print(sorted(p['mcpServers'].keys()))
"
```

**Expected**:
```
['billing', 'cw', 'pricing']
```

No `costexp`. No extra servers.

---

## Step 6 — Reporting back

When done, tell the user:

1. Each of the four files you edited, with a one-line summary per file.
2. The version bumps (marketplace `2.4.0 → 2.5.0`, plugin `1.2.1 → 1.3.0`).
3. That all five verification checks passed (or which failed and why).
4. Do **not** commit. The user will review and commit themselves.

If anything in this plan is ambiguous or doesn't match the current state of the files (e.g., line numbers off because the file changed), stop and ask the user before guessing.
