---
title: Bedrock cost attribution
---

# Bedrock cost attribution

Design for [#215](https://github.com/aws-samples/sample-autonomous-cloud-coding-agents/issues/215). Adds AWS-native, per-user/per-repo attribution of **Bedrock model-inference spend** on top of the in-app `cost_usd` estimate and the per-session tenant-data isolation.

## Runtime path

Strands invokes Amazon Bedrock directly through `BedrockModel`. The harness passes the boto session returned by `agent/src/aws_session.py:get_session()`, so model calls and tenant-data calls use the same refreshable, task-scoped credentials:

```text
pipeline configures {user_id, repo, task_id}
  -> aws_session.get_session()
     -> sts:AssumeRole AgentSessionRole with session tags
  -> Strands BedrockModel(boto_session=session, model_id=...)
     -> bedrock-runtime ConverseStream
```

`AgentSessionRole` is granted `bedrock:InvokeModel` and `bedrock:InvokeModelWithResponseStream` for the same configured model and cross-Region inference-profile resources as the AgentCore and ECS compute roles. The session provider re-assumes the role before the one-hour role-chaining expiry, allowing tasks to run for the full compute lifetime.

When `AGENT_SESSION_ROLE_ARN` is unset for local development, `get_session()` uses the ambient boto credential chain. When it is configured and assumption fails, the task fails closed; silently falling back to the shared compute role would defeat both tenant isolation and attribution.

## Attribution surfaces

| Surface | Granularity | Mechanism | Purpose |
|---|---|---|---|
| In-app `cost_usd` | Per task | Strands/Bedrock token metrics multiplied by `agent/src/model_pricing.py` | Budget guardrails and approximate task insight |
| CUR 2.0 / Cost Explorer | Per user/repo, aggregated by AWS billing dimensions | IAM principal session tags `{user_id, repo, task_id}` on `AgentSessionRole` | Authoritative AWS-native chargeback |
| Bedrock invocation logs | Per model call | Account-level model-invocation logging | Prompt/token diagnostics; not currently stamped with ABCA task metadata |

The in-app estimate is not billing data. The local pricing table can become stale and does not model account discounts, commitments, or free tier. Budgeted tasks fail before invocation when the selected model has no pricing entry; unbudgeted tasks may run with `cost_usd` unset. AWS Cost Explorer and CUR 2.0 remain authoritative.

ABCA does **not** currently attach Bedrock `requestMetadata` to Strands calls. Session tags provide billing attribution, but they are not copied into each model-invocation log record. Per-call joins by `task_id` require future request-metadata wiring or correlation through surrounding trace timestamps and session identity.

## IAM session-tag attribution

The existing `AgentSessionRole` is reused rather than introducing a second Bedrock-specific role:

1. `pipeline.py` calls `aws_session.configure_session()` with the task correlation envelope.
2. `aws_session.get_session()` builds refreshable credentials by assuming `AGENT_SESSION_ROLE_ARN` with non-empty `user_id`, `repo`, and `task_id` tags.
3. `StrandsHarness` passes that session to `BedrockModel`.
4. AWS billing records the IAM principal dimensions used by Cost Explorer and CUR 2.0.

Tag values are clamped to IAM's 256-character limit. They remain in private module state rather than environment variables, so target-repository subprocesses do not inherit tenant identifiers.

The compute role retains its scoped Bedrock grant for platform operations and local compatibility, but deployed Strands model calls use the SessionRole session. Both AgentCore Runtime and ECS inject the same SessionRole ARN and share this code path.

## Operator setup

CDK cannot activate organization-level billing dimensions. Operators must:

1. Deploy and run at least one task so IAM-principal tag keys appear in Billing.
2. Wait up to 24 hours, then activate `user_id` and `repo` under **Billing and Cost Management -> Cost allocation tags**. Activation is not retroactive.
3. Create a CUR 2.0 export with caller-identity ARN enabled. Existing exports do not backfill that field.
4. Build budgets and reports from CUR or Cost Explorer. Keep high-cardinality `task_id` for diagnostics rather than routine cost grouping.

See the operator guide [COST_ATTRIBUTION.md](/sample-autonomous-cloud-coding-agents/getting-started/cost-attribution).

## Security and failure posture

- Session assumption is fail-closed whenever `AGENT_SESSION_ROLE_ARN` is configured.
- Model grants are resource-scoped to configured foundation models and inference profiles.
- Session tags are identifiers, not secrets; do not place PII in them.
- Model-invocation logging is account- and Region-scoped and independent of session-tag billing activation.

## Test coverage

- CDK tests assert SessionRole and compute-role Bedrock grants for configured model/profile ARNs.
- Agent tests assert session tags, refreshable credentials, ambient local fallback, and fail-closed configured-role behavior.
- Harness tests assert that Strands receives `aws_session.get_session()` and that budgeted unknown-price models are rejected before invocation.

## Out of scope

Application inference profiles per repository, automated CUR/Budgets configuration, invoice-grade per-task cost, and Bedrock request metadata are follow-up work.
