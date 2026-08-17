# Cost attribution (operator guide)

How to attribute **Amazon Bedrock model-inference spend** to users and repositories in a multi-user ABCA deployment. This is the operator-facing companion to [BEDROCK_COST_ATTRIBUTION.md](../design/BEDROCK_COST_ATTRIBUTION.md) and [COST_MODEL.md](../design/COST_MODEL.md#cost-attribution).

> [!WARNING]
> **The in-app `cost_usd` is a client-side estimate, not authoritative billing data.** The Strands harness reads Bedrock token metrics and applies the local rates in `agent/src/model_pricing.py`. Pricing changes, AWS discounts, commitments, and free tier can make it differ from the invoice. Use it for per-task budget guardrails and approximate insight. Use AWS Cost Explorer or CUR 2.0 for financial decisions.

## Two cost meters

| Meter | Granularity | Source of truth for | Where |
|---|---|---|---|
| **In-app `cost_usd`** | Per task | `max_budget_usd` guardrails and approximate task cost | Task metadata / control panel |
| **CUR session-tag chargeback** | Per user/repo, aggregated by AWS billing dimensions | AWS-native FinOps chargeback | Cost Explorer / CUR 2.0 |

The in-app meter is immediate but estimated. Session tags reach the AWS bill but appear on billing's aggregation schedule.

Bedrock model-invocation logging remains useful for model, prompt, and token diagnostics. ABCA does not currently stamp `{task_id,user_id,repo}` as Bedrock `requestMetadata`, so invocation logs are not a third task-attribution meter.

## What the platform does automatically

In deployed AgentCore and ECS tasks, Strands calls Bedrock through the refreshable boto session returned by `aws_session.get_session()`. That session assumes `AgentSessionRole` with `{user_id, repo, task_id}` IAM session tags. The same path is used for tenant DynamoDB/S3 access and direct Bedrock model calls.

No credential-export helper, subprocess SDK, or model-provider-specific environment settings are involved. Local runs without `AGENT_SESSION_ROLE_ARN` use the normal ambient AWS credential chain.

Operator setup is still required in AWS Billing. Cost-allocation tag keys do not become selectable until tagged calls have occurred.

## FinOps checklist

> **Ordering matters.** Deploy, run at least one task, wait up to 24 hours, then activate the tags. Activation is not retroactive.
>
> Use **Billing and Cost Management -> Cost allocation tags**, not Resource Groups Tag Editor.

1. Open **Cost allocation tags -> User-defined cost allocation tags** and activate the IAM-principal keys `user_id` and `repo`.
2. Create a CUR 2.0 export with **caller-identity ARN** included. Existing exports do not backfill identity fields, so create a new export when necessary.
3. Build Cost Explorer views, CUR queries, and AWS Budgets grouped by `user_id` or `repo`.
4. Keep `task_id` out of routine cost grouping because it is high-cardinality; use task metadata for per-task estimates.

If the keys do not appear after tagged tasks and a 24-hour wait, verify that the task used `AgentSessionRole` and that IAM-principal cost allocation is available in the account and Region.

## Model-invocation logs

The stack configures account-level Bedrock model-invocation logging in its Region and writes records to `/aws/bedrock/model-invocation-logs/<stack>`. Confirm it with:

```bash
aws bedrock get-model-invocation-logging-configuration --region <stack-region>
```

These logs support model-level token and latency analysis. Without ABCA request metadata, query by model and time window:

```text
fields modelId,
       input.inputTokenCount as inTokens,
       output.outputTokenCount as outTokens
| stats sum(inTokens) as totalInput,
        sum(outTokens) as totalOutput,
        count() as calls by modelId
| sort totalInput desc
```

Do not treat a timestamp-based join between invocation logs and a task as invoice-grade attribution. CUR session tags are the authoritative supported path.

## Caveats

- **Unknown pricing and budgets:** a task with `max_budget_usd` is rejected before invocation if `model_pricing.py` has no rate for its model. An unbudgeted task may run with `cost_usd` unset.
- **Turn-boundary enforcement:** the harness evaluates accumulated usage after each model turn. A single turn can cross the configured dollar threshold before the next call is stopped.
- **Tag activation is delayed and non-retroactive:** run a tagged task first, then activate the keys.
- **No PII in tags:** `user_id` and `repo` are recorded in billing data.
