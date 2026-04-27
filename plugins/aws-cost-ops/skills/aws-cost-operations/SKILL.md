---
name: aws-cost-operations
description: AWS cost optimization, monitoring, and operational excellence expert. Use when analyzing AWS bills, estimating costs, setting up CloudWatch alarms, querying logs, auditing CloudTrail activity, or assessing security posture. Essential when user mentions AWS costs, spending, billing, budget, pricing, CloudWatch, observability, monitoring, alerting, CloudTrail, audit, or wants to optimize AWS infrastructure costs and operational efficiency.
context: fork
skills:
  - aws-mcp-setup
allowed-tools:
  - mcp__pricing__*
  - mcp__billing__*
  - mcp__cw__*
  - mcp__aws-mcp__*
  - mcp__awsdocs__*
  - Bash(aws ce *)
  - Bash(aws cloudwatch *)
  - Bash(aws logs *)
  - Bash(aws budgets *)
  - Bash(aws cloudtrail *)
  - Bash(aws sts get-caller-identity)
hooks:
  PreToolUse:
    - matcher: Bash(aws ce *)
      command: aws sts get-caller-identity --query Account --output text
      once: true
---

# AWS Cost & Operations

This skill provides comprehensive guidance for AWS cost optimization, monitoring, observability, and operational excellence with integrated MCP servers.

## AWS Documentation Requirement

Always verify AWS facts using MCP tools (`mcp__aws-mcp__*` or `mcp__*awsdocs*__*`) before answering. The `aws-mcp-setup` dependency is auto-loaded — if MCP tools are unavailable, guide the user through that skill's setup flow.

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

## When to Use This Skill

Use this skill when:
- Optimizing AWS costs and reducing spending
- Estimating costs before deployment
- Monitoring application and infrastructure performance
- Setting up observability and alerting
- Analyzing spending patterns and trends
- Investigating operational issues
- Auditing AWS activity and changes
- Assessing security posture
- Implementing operational excellence

## Cost Optimization Best Practices

### Pre-Deployment Cost Estimation

**Always estimate costs before deploying**:
1. Use **AWS Pricing MCP** to estimate resource costs
2. Compare pricing across different regions
3. Evaluate alternative service options
4. Calculate expected monthly costs
5. Plan for scaling and growth

**Example workflow**:
```
"Estimate the monthly cost of running a Lambda function with
1 million invocations, 512MB memory, 3-second duration in us-east-1"
```

### Cost Analysis and Optimization

**Regular cost reviews**:
1. Use the **Billing and Cost Management MCP** (`billing`) to analyze spending trends and detect anomalies
2. Identify cost anomalies and unexpected charges
3. Review costs by service, region, and environment
4. Compare actual vs. budgeted costs
5. Pull cross-service recommendations from **Cost Optimization Hub** and right-sizing suggestions from **Compute Optimizer** (both exposed via the `billing` server)

**Cost optimization strategies**:
- Right-size over-provisioned resources (use Compute Optimizer recommendations via the `billing` server, then validate with CloudWatch utilization data)
- Use appropriate storage classes (S3, EBS); for S3 at scale, query Storage Lens via the `billing` server to find lifecycle-policy opportunities
- Implement auto-scaling for dynamic workloads
- Leverage Savings Plans and Reserved Instances — pull purchase recommendations and coverage/utilization from the `billing` server
- Delete unused resources and snapshots
- Use cost allocation tags effectively

### Budget Monitoring

**Track spending against budgets**:
1. Use the **`billing` MCP server** to monitor budget status and Free Tier usage
2. Set up budget alerts for threshold breaches (configure the budget + SNS topic separately; the MCP exposes status only)
3. Review budget utilization regularly
4. Adjust budgets based on trends
5. Implement cost controls and governance

## Monitoring and Observability Best Practices

### CloudWatch Metrics and Alarms

**Implement comprehensive monitoring**:
1. Use **CloudWatch MCP** to query metrics and logs
2. Set up alarms for critical metrics:
   - CPU and memory utilization
   - Error rates and latency
   - Queue depths and processing times
   - API gateway throttling
   - Lambda errors and timeouts
3. Create CloudWatch dashboards for visualization
4. Use log insights for troubleshooting

**Example alarm scenarios**:
- Lambda error rate > 1%
- EC2 CPU utilization > 80%
- API Gateway 4xx/5xx error spike
- DynamoDB throttled requests
- ECS task failures

### Application Performance Monitoring

**Monitor application health**:
1. Use **CloudWatch Application Signals MCP** for APM
2. Track service-level objectives (SLOs)
3. Monitor application dependencies
4. Identify performance bottlenecks
5. Set up distributed tracing

### Container and Kubernetes Monitoring

**For containerized workloads**:
1. Use **AWS Managed Prometheus MCP** for metrics
2. Monitor container resource utilization
3. Track pod and node health
4. Create PromQL queries for custom metrics
5. Set up alerts for container anomalies

## Audit and Security Best Practices

### CloudTrail Activity Analysis

**Audit AWS activity**:
1. Use **CloudTrail MCP** to analyze API activity
2. Track who made changes to resources
3. Investigate security incidents
4. Monitor for suspicious activity patterns
5. Audit compliance with policies

**Common audit scenarios**:
- "Who deleted this S3 bucket?"
- "Show all IAM role changes in the last 24 hours"
- "List failed login attempts"
- "Find all actions by a specific user"
- "Track modifications to security groups"

### Security Assessment

**Regular security reviews**:
1. Use **Well-Architected Security Assessment MCP**
2. Assess security posture against best practices
3. Identify security gaps and vulnerabilities
4. Implement recommended security improvements
5. Document security compliance

**Security assessment areas**:
- Identity and Access Management (IAM)
- Detective controls and monitoring
- Infrastructure protection
- Data protection and encryption
- Incident response preparedness

## Using MCP Servers Effectively

### Cost Analysis Workflow

1. **Pre-deployment**: Use the `pricing` MCP to estimate costs
2. **Post-deployment**: Use the `billing` MCP to track actual spending, budgets, and Free Tier usage
3. **Analysis**: Use the `billing` MCP for Cost Explorer breakdowns, forecasts, and anomaly detection
4. **Optimization**: Act on Cost Optimization Hub and Compute Optimizer recommendations (both served by `billing`)

### Monitoring Workflow

1. **Setup**: Configure CloudWatch metrics and alarms
2. **Monitor**: Use CloudWatch MCP to track key metrics
3. **Analyze**: Use Application Signals for APM insights
4. **Troubleshoot**: Query CloudWatch Logs for issue resolution

### Security Workflow

1. **Audit**: Use CloudTrail MCP to review activity
2. **Assess**: Use Well-Architected Security Assessment
3. **Remediate**: Implement security recommendations
4. **Monitor**: Track security events via CloudWatch

### MCP Usage Best Practices

1. **Cost Awareness**: Check pricing before deploying resources
2. **Proactive Monitoring**: Set up alarms for critical metrics
3. **Regular Reviews**: Analyze costs and performance weekly
4. **Audit Trails**: Review CloudTrail logs for compliance
5. **Security First**: Run security assessments regularly
6. **Optimize Continuously**: Act on cost and performance recommendations

## Operational Excellence Guidelines

### Cost Optimization

- **Tag Everything**: Use consistent cost allocation tags
- **Review Monthly**: Analyze spending trends and anomalies
- **Right-size**: Match resources to actual usage
- **Automate**: Use auto-scaling and scheduling
- **Monitor Budgets**: Set alerts for cost overruns

### Monitoring and Alerting

- **Critical Metrics**: Alert on business-critical metrics
- **Noise Reduction**: Fine-tune thresholds to reduce false positives
- **Actionable Alerts**: Ensure alerts have clear remediation steps
- **Dashboard Visibility**: Create dashboards for key stakeholders
- **Log Retention**: Balance cost and compliance needs

### Security and Compliance

- **Least Privilege**: Grant minimum required permissions
- **Audit Regularly**: Review CloudTrail logs for anomalies
- **Encrypt Data**: Use encryption at rest and in transit
- **Assess Continuously**: Run security assessments frequently
- **Incident Response**: Have procedures for security events

## Additional Resources

For detailed operational patterns and best practices, refer to the comprehensive reference:

**File**: `references/operations-patterns.md`

This reference includes:
- Cost optimization strategies
- Monitoring and alerting patterns
- Observability best practices
- Security and compliance guidelines
- Troubleshooting workflows

## CloudWatch Alarms Reference

**File**: `references/cloudwatch-alarms.md`

Common alarm configurations for:
- Lambda functions
- EC2 instances
- RDS databases
- DynamoDB tables
- API Gateway
- ECS services
- Application Load Balancers
