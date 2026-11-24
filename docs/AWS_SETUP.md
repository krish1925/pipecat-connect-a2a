# Amazon Connect setup for pipecat-connect-a2a

Source of truth: AWS's [external agent setup](https://docs.aws.amazon.com/connect/latest/adminguide/a2a-setup-external.html),
[quotas](https://docs.aws.amazon.com/connect/latest/adminguide/a2a-quotas.html) and
[server behaviour](https://docs.aws.amazon.com/connect/latest/adminguide/a2a-external-server.html).
Steps 1-6 are scripted in `infra/setup.sh`. The scripted commands were checked against botocore's
service model but not run against a real account.

## Prerequisites
- AWS account, paid plan, budget alert. Region where Connect A2A is available (docs list none; use us-east-1).
- Connect instance on the **Customer** tier (not Customer Basic). One claimed US voice number.
- A **Connect assistant** (Amazon Q in Connect AI-agents domain) associated with the instance. Console: Connect > AI agents > add domain.
- Your A2A server reachable on public **wss://** (audio is wss only). Dev: Cloudflare tunnel to the bot.
- Caller IAM permissions: secretsmanager:CreateSecret/PutResourcePolicy, kms:CreateKey/CreateAlias, app-integrations:CreateApplication,
  connect:CreateIntegrationAssociation/UpdateSecurityProfile/AssociateSecurityProfiles, wisdom:CreateAIAgent/UpdateAIAgent/CreateAIAgentVersion.
  (AWS's list is a minimum; expect to add sts:GetCallerIdentity and read actions.)

## Steps
1. **KMS key + secret.** Customer-managed key (the default aws/secretsmanager key fails). Key policy allows `connect.amazonaws.com` kms:Decrypt via secretsmanager. Secret = your API key. Resource policy allows Connect GetSecretValue from your account and instance ARN. *(script)*
2. **A2A_SERVER application** via appintegrations create-application, with `AccessUrl` (your wss URL, ending /a2a) and `AuthConfig` API_KEY pointing at the secret ARN. ApplicationType cannot be changed later. *(script)*
3. **Associate the application with the instance** (connect create-integration-association, type APPLICATION). *(script)*
4. **Orchestrating AI agent** (qconnect create-ai-agent, type ORCHESTRATION) with one `handoffAgentConfiguration`: `audioStreamingEnabled: true`, `immediateHandoff: true`, target = application id. Audio rules: exactly one collaborator, no orchestration prompt, no guardrail. AWS's audio example has no tool config. *(script)*
5. **Security profile.** update-security-profile `--allowed-ai-agents [{Arn: <application arn>, Type: THIRD_PARTY}]`, then associate the profile with the AI agent (`--entity-type AI_AGENT`). Add the **Access Contact Control Panel** permission in the console if you want contact context (customer/system address) in INIT_SESSION. *(script)*
6. **Publish a version** (create-ai-agent-version). Config changes do nothing until published. *(script)*
7. **Lex V2 bot** (console, same region): locale using **Nova Sonic speech-to-speech**, exactly one `AMAZON.QInConnectIntent`, build, create an alias. In Connect > Flows > Amazon Lex, add the bot/alias to the instance.
8. **Contact flow** (console): Set recording and analytics behavior (conversational analytics real-time), Connect assistant block, Get customer input using the Lex bot, error branch to a queue, and an Escalate path to a human queue. Publish. AWS does not document the exact block order for the AI agent; expect trial and error.
9. **Phone number:** Routing > Phone numbers > claim a US number > set Flow/IVR to your flow.
10. **Run your server** with `A2A_API_KEY` set to the same key. Call the number.

## Gotchas
- Trace data is mandatory every turn or Connect disables the endpoint (the library does this).
- Never end a call by closing with 1000; send FINISH. Errors on a turn send the contact to the flow's Error branch.
- Quotas: 5 collaborators per agent, 5 concurrent outbound third-party connections per instance.
- Not documented by AWS: supported regions, any enablement/allowlist (ElevenLabs says third-party agents must be enabled per account/region; if the first call fails with an authorization error, open a support case), Escalate/Complete tool JSON, flow block order.
- AWS recommends validating latency and audio quality with your account team before production. Not a dev gate.
- Avoid the external voice transfer connector (SIP, ~$100/connector/day).

## Cleanup
Release the number; delete the application, AI agent, secret (7-30 day recovery window), and schedule KMS key deletion.

## Connection behaviour (from AWS docs)

- Connect opens the WebSocket once per voice call. If it drops without a close frame, or with
  1005/1006/1012/1013/1014, Connect reconnects **once** and sends a new `INIT_SESSION` with the same
  `contextId`. A second drop ends the call (Connect closes with 1011). Close with 1012 when you
  deploy. Resuming a live pipeline on reconnect is not implemented yet.
- Connect retries the upgrade once on HTTP 502/503/504 or no response. A wrong key must return 401
  or 403; those are not retried.
- Idle timeouts and fatal protocol errors arrive as an `ERROR` message (408 / 400) before Connect
  closes the connection.
