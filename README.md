# In Game Now Notifications

AWS SAM serverless app that watches a small list of League of Legends players and
sends Discord/email alerts when a tracked player enters a game, then stores post-game
stats in DynamoDB.

## Riot API key

The stack takes only the ARN of a Secrets Manager secret; never put the key itself in
the template or in `samconfig.toml`.

Create the secret before deploying:

```bash
aws secretsmanager create-secret --name in-game-now-notifications/riot-api-key \
  --secret-string "$RIOT_API_KEY"
```

Riot development keys expire every 24 hours. Rotate the secret value without
redeploying anything:

```bash
aws secretsmanager put-secret-value --secret-id in-game-now-notifications/riot-api-key \
  --secret-string "$RIOT_API_KEY"
```

For local development, the `RIOT_API_KEY` environment variable takes precedence over
Secrets Manager.

## CI/CD and deployment

`.github/workflows/ci.yml` runs ruff and pytest on every push and pull request,
validates and builds the SAM template, and deploys to AWS on pushes to `main`.

The deploy job assumes an IAM role through GitHub OIDC. Required repository secrets:

| Secret | Purpose |
|---|---|
| `AWS_ROLE_ARN` | IAM role assumed by the deploy job |
| `RIOT_API_KEY_SECRET_ARN` | Secrets Manager ARN that holds the Riot API key |
| `SES_SENDER_EMAIL` | Verified SES sender identity |
| `RECIPIENT_EMAIL` | Notification and alarm recipient |
| `DISCORD_WEBHOOK_URL` | Discord webhook for alerts |

One-time AWS setup:

1. Create a GitHub OIDC identity provider for
   `https://token.actions.githubusercontent.com` with audience `sts.amazonaws.com`.
2. Create an IAM role trusted by that provider, scoped to this repository and ideally
   to refs under `refs/heads/main`, with permission to deploy the stack (CloudFormation,
   S3, IAM, Lambda, DynamoDB, SNS, SQS, Step Functions, EventBridge, CloudWatch, and
   X-Ray).
3. Store the role ARN as the `AWS_ROLE_ARN` repository secret.

After the first deploy, confirm the alarm email subscription from the alarms topic once.
