# Cost

Full itemized spend table lands on D12 (PLAN.md §4) once real infra has run
for real and there are real numbers to show. This section — the safety
guardrails — lands now, D3, before any AWS resource with an hourly cost
exists, because that's the order that actually prevents a surprise bill
rather than documenting one after the fact.

## The actual risk shape

Two different cost profiles exist in this system, and they need different
defenses:

**Usage-based** (Bedrock tokens, S3 storage/requests, Fargate task-seconds,
Lambda invocations, EventBridge, CloudFront requests) — costs scale with
what you actually do. A few minutes of demo running is genuinely cents.
This is what `CAIRN_APPROVAL_USD` (default $0.50, `.env.example`) guards at
the application level: the agent escalates to a human before launching
anything it projects will cost more than that, per action.

**Fixed/idle-cost** (an Application Load Balancer, a NAT Gateway) — these
bill *per hour they exist*, whether or not anything is happening. An ALB
alone is real money sitting idle; a NAT Gateway is worse. These are the
ones that turn "I ran a hackathon demo" into "I left something on for a
week." No per-action approval threshold catches this category — the only
defense is not creating the resource, or not leaving it running.

## Design commitments (binding for the D7 Terraform)

- **No NAT Gateway, ever.** ECS Fargate tasks run in public subnets with a
  security group that allows outbound only (to CockroachDB Cloud, S3,
  Bedrock) and no unsolicited inbound. A hackathon demo does not need
  private-subnet isolation, and a NAT Gateway's idle hourly cost is not
  justified by the security benefit here.
- **Infra goes up only to be used, and comes down right after.** The ALB,
  ECS services, and CloudFront distribution are stood up for a working
  session (development, testing, demo recording) and torn down at the end
  of it — not left running between sessions. `make teardown` is the
  command that makes this a 30-second decision instead of a remembered
  chore.
- **Two-region is the first thing cut if cost pressure shows up before
  region parity is needed for the video.** PLAN.md §6 already lists this;
  cost is a second, independent reason to default to it.
- **CockroachDB Cloud runs on the Standard trial**, which is time-boxed and
  free for its duration — it is not an AWS cost and does not count against
  the AWS budget alert. `make teardown`'s `ccloud cluster delete` still
  removes it before the trial matters again.

## What you should do once (not my call — it's your account)

You've already set an AWS Budget alert at $1. Two things worth knowing
about that:

1. **It's a notification, not a circuit breaker.** AWS Budget *alerts*
   email you after the threshold is crossed; they don't stop anything by
   themselves. If you want an actual stop, AWS Budgets supports **Budget
   Actions** — e.g., auto-attaching a deny-all IAM policy when the
   threshold breaches. That's a real engineering task (needs an IAM role +
   policy ARN wired into the budget), and it belongs in D7's Terraform
   alongside the rest of the account-level plumbing, not bolted on ad hoc
   today. Flagging it now so it's not forgotten.
2. **Expect the alert to fire almost immediately once any real infra goes
   up**, possibly within hours from the ALB alone — a $1 threshold is
   below the idle cost of a single ALB running for a day. That firing is
   the alert doing its job, not evidence of a leak. The thing to actually
   watch is whether spend *keeps climbing* after teardown — if it does,
   something wasn't torn down, and that's `make teardown` (or the AWS
   Console, billing exceptions do not wait for a `make` target) is the
   fix.

## Emergency stop

`scripts/emergency_stop.sh` (lands with D7's infra, since it has nothing to
tear down until then) will be the one-command "stop everything now"
button — ECS services to zero, CloudFront disabled, ALB deleted, S3
emptied, CockroachDB cluster deleted. Until D7, there is no AWS resource
in this project with an hourly cost, so there is nothing yet to stop.
