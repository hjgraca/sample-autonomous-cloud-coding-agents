---
title: Model configuration
---

**This is the canonical reference for which model the agent uses and where to change it.** The model ID is configured across four independent layers in three languages, so read this section before changing a default — a mismatch between the layers fails every task on the stack at turn 0, not just an edge case.

### The four layers

| # | Layer | What it controls | Where | ID form |
|---|---|---|---|---|
| 1 | **IAM invoke allowlist** | Which models the agent's roles may invoke at all. The outer gate — everything below fails without it. | `DEFAULT_BEDROCK_MODEL_IDS` (`cdk/src/constructs/bedrock-models.ts:34`); override with CDK context `bedrockModels` (key at `:48`, resolver at `:67`) | **Bare** (`anthropic.claude-…`) |
| 2 | **Platform default model** | The model used when nothing narrower is set. | `agent/src/config.py` (`MODEL_ID` fallback) and `agent/src/models.py` (`TaskConfig.model_id`) | Prefixed (`us.anthropic.…`) |
| 3 | **Per-repo override** | One repository's model, with no agent redeploy. | Blueprint `agent.modelId` → RepoTable `model_id` → ECS injects `MODEL_ID` | Prefixed (`us.anthropic.…`) |
| 4 | **Per-task / local** | One task's model. The orchestrator payload carries `model_id`; local batch runs read `MODEL_ID` from the shell via `agent/run.sh`. | Task payload `model_id`; shell `MODEL_ID` | Prefixed (`us.anthropic.…`) |

### Environment variables

| Variable | Who sets it | ID form | Purpose |
|---|---|---|---|
| `MODEL_ID` | ECS strategy from the repo Blueprint; you, in the shell, for local batch runs | Prefixed inference profile | The model Strands passes to its Bedrock provider. Unset → the `agent/src/config.py` fallback. |

### Precedence — narrowest wins

```text
per-task payload model_id            (layer 4)
  > blueprint agent.modelId          (layer 3, arrives as stack env MODEL_ID)
  > local shell MODEL_ID
  > agent/src/config.py fallback     (layer 2 — us.anthropic.claude-opus-4-8)
```

Every one of those is gated by the **IAM invoke allowlist** (layer 1), which is itself gated by **account-level Bedrock model access**. Both gates are silent until invocation: a model that resolves fine through precedence still fails at turn 0 with `AccessDenied` if it is not in the grant list, and fails again if your account has not completed [Bedrock model access](https://docs.aws.amazon.com/bedrock/latest/userguide/model-access.html) for it.

### Bare vs. prefixed IDs — the one rule that bites

Layer 1 takes **bare foundation-model IDs**; every other layer takes the **prefixed inference-profile ID**. This asymmetry is deliberate: both grant sites derive the inference-profile ARN by *adding* the `us.` prefix themselves, so a prefixed entry in `bedrockModels` would produce an invalid `us.us.anthropic.…` ARN. The resolver rejects a `us.`/`eu.`/`apac.`-prefixed entry at `cdk/src/constructs/bedrock-models.ts:84` so the typo fails at synth rather than at runtime.

In the other direction, a **bare** ID cannot be invoked on demand at all. Verified:

```console
$ aws bedrock-runtime invoke-model --model-id anthropic.claude-opus-5 ...
ValidationException: Invocation of model ID anthropic.claude-opus-5 with on-demand
throughput isn't supported. Retry your request with the ID or ARN of an inference
profile that contains this model.
```

So: `bedrockModels` context → `anthropic.claude-opus-4-8`. Everywhere else → `us.anthropic.claude-opus-4-8`.

### Bumping the default model

1. Add the **bare** ID to `DEFAULT_BEDROCK_MODEL_IDS` (`cdk/src/constructs/bedrock-models.ts`) and deploy, so the grant exists before anything tries to use it.
2. Confirm account-level Bedrock access for the model in the target Region.
3. Update the **prefixed** ID in `agent/src/config.py` and `agent/src/models.py`.
4. **Add and verify local pricing.** Update `agent/src/model_pricing.py` with the current [Bedrock pricing](https://aws.amazon.com/bedrock/pricing/) and its tests. A task with `max_budget_usd` is rejected before model invocation when its model has no known price; unbudgeted tasks may run with `cost_usd` unavailable.
5. The doc-drift test (`cdk/test/contracts/model-default-docs-parity.test.ts`) fails until the documented defaults here and in `agent/README.md` match `config.py`. That failure is the reminder, not a nuisance — update both.

### Cost and model selection

Model choice is a **cost** decision, which is why it is adjustable per repo and per task without a code change.

**Per-token rate vs. token volume.** Measured on the pinned toolchain, same one-turn prompt, same system prompt:

| Model | Input tokens | Reported `cost_usd` | Implied input rate |
|---|---|---|---|
| `us.anthropic.claude-opus-4-8` | 32,145 | $0.160850 | **$5.00/MTok** |
| `us.anthropic.claude-opus-5` | 37,584 | $0.188020 | **$5.00/MTok** |

Token ratio 1.169; cost ratio 1.169 — identical. **The per-token rate is unchanged; the whole delta is token volume on an identical prompt.** Read it that way: "Opus 5 costs ~17% more per task" invites the wrong remedy (switch models), while "same rate, more tokens" points at the real levers — prompt size, prompt caching, and `max_turns`.

**Where can I set `max_budget_usd`?**

| Surface | How | Status |
|---|---|---|
| Per task, CLI | `bgagent submit --max-budget <dollars>` (`cli/src/commands/submit.ts:69`), range 0.01–100 | Works |
| Per task, REST | `max_budget_usd` in the `POST /v1/tasks` body | Works |
| Local batch only | `MAX_BUDGET_USD` shell env, when running `entrypoint.py` directly | Works locally; **ignored** by the deployed AgentCore **server** mode, which reads the budget from the `/invocations` JSON body |
| Per repo, Blueprint | `agent.maxBudgetUsd` on the repo's `Blueprint` construct | Works — persisted to `RepoTable.max_budget_usd`; same `0.01`–`100` range as the CLI, validated at CDK synth so an out-of-range value cannot deploy. See [Per-repo overrides](/sample-autonomous-cloud-coding-agents/customizing/per-repo-overrides). |
| Platform default | — | None by design: **unset means unlimited** |

**Unlimited-by-default is deliberate — pair it with the escape hatch.** Because no platform budget ceiling applies, the documented mitigation for cost is choosing a lighter-token model rather than relying on a cap:

- **Per repo:** Blueprint `agent.modelId` (e.g. `us.anthropic.claude-sonnet-4-6`) — no code change, no agent redeploy
- **Per task:** `model_id` in the task payload
- **Platform-wide:** the `bedrockModels` context plus the layer-2 call sites above

The model must be in the IAM grant list (layer 1) or the task fails at turn 0 with `AccessDenied` — the grant is the gate, so a lighter model is only reachable if it is granted.

**Trust boundary on the number.** `cost_usd` is the harness's **client-side estimate** from the local `model_pricing.py` table and Strands/Bedrock token metrics — not authoritative billing. It drifts when Bedrock pricing changes or discounts and commitments apply. See [Cost attribution](/sample-autonomous-cloud-coding-agents/getting-started/cost-attribution); authoritative cost comes from AWS Cost Explorer / CUR 2.0.