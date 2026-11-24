#!/usr/bin/env bash
# Registers your A2A server with Amazon Connect (the parts that have a CLI).
# Follows https://docs.aws.amazon.com/connect/latest/adminguide/a2a-setup-external.html
# and docs/AWS_SETUP.md. Creates billable resources (KMS key ~$1/mo, secret ~$0.40/mo).
#
# Prereqs (see docs/AWS_SETUP.md): Connect Customer-tier instance, assistant (AI agents
# domain) associated, a security profile your AI agent will use.
#
#   INSTANCE_ID=... ASSISTANT_ID=... SECURITY_PROFILE_ID=... \
#   ACCESS_URL=wss://your-host/a2a ./infra/setup.sh
set -euo pipefail
: "${INSTANCE_ID:?}" "${ASSISTANT_ID:?}" "${SECURITY_PROFILE_ID:?}" "${ACCESS_URL:?}"
REGION="${REGION:-us-east-1}"
NAME="${NAME:-pipecat-bot}"
NAMESPACE="${NAMESPACE:-pipecatbot}"   # <=32 chars, must not start with aws. or amazon.
export AWS_DEFAULT_REGION="$REGION"

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
INSTANCE_ARN="arn:aws:connect:$REGION:$ACCOUNT_ID:instance/$INSTANCE_ID"
API_KEY="${A2A_API_KEY:-$(openssl rand -hex 24)}"

echo "== 1. KMS key (customer managed; aws/secretsmanager does NOT work) + secret"
KEY_POLICY=$(cat <<EOF
{"Version":"2012-10-17","Statement":[
 {"Sid":"AccountAdmin","Effect":"Allow","Principal":{"AWS":"arn:aws:iam::$ACCOUNT_ID:root"},"Action":"kms:*","Resource":"*"},
 {"Sid":"AllowConnectToDecryptSecret","Effect":"Allow","Principal":{"Service":"connect.amazonaws.com"},
  "Action":"kms:Decrypt","Resource":"*",
  "Condition":{"StringEquals":{"kms:ViaService":"secretsmanager.$REGION.amazonaws.com"}}}]}
EOF
)
KMS_ID=$(aws kms create-key --description "$NAME a2a" --policy "$KEY_POLICY" --query KeyMetadata.KeyId --output text)
SECRET_ARN=$(aws secretsmanager create-secret --name "$NAME/a2a-api-key" --kms-key-id "$KMS_ID" \
  --secret-string "$API_KEY" --query ARN --output text)
aws secretsmanager put-resource-policy --secret-id "$SECRET_ARN" --resource-policy "$(cat <<EOF
{"Version":"2012-10-17","Statement":[{"Sid":"AllowConnectToRetrieveSecret","Effect":"Allow",
 "Principal":{"Service":"connect.amazonaws.com"},"Action":"secretsmanager:GetSecretValue","Resource":"*",
 "Condition":{"StringEquals":{"aws:SourceAccount":"$ACCOUNT_ID"},"ArnEquals":{"aws:SourceArn":"$INSTANCE_ARN"}}}]}
EOF
)" >/dev/null

echo "== 2. A2A_SERVER application (type cannot be changed later)"
read -r APP_ID APP_ARN < <(aws appintegrations create-application --name "$NAME" --namespace "$NAMESPACE" \
  --application-type A2A_SERVER \
  --application-source-config "{\"ExternalUrlConfig\":{\"AccessUrl\":\"$ACCESS_URL\"}}" \
  --auth-config "{\"AuthType\":\"API_KEY\",\"CredentialProviderIdentifier\":\"$SECRET_ARN\"}" \
  --query '[Id,Arn]' --output text)

echo "== 3. Associate the application with the instance"
aws connect create-integration-association --instance-id "$INSTANCE_ID" \
  --integration-type APPLICATION --integration-arn "$APP_ARN" >/dev/null

echo "== 4. Orchestrating AI agent (audio streaming, immediate handoff)"
cat > /tmp/create-ai-agent.json <<EOF
{"assistantId":"$ASSISTANT_ID","name":"$NAME-orchestrator","type":"ORCHESTRATION","visibilityStatus":"PUBLISHED",
 "configuration":{"orchestrationAIAgentConfiguration":{"connectInstanceArn":"$INSTANCE_ARN",
  "multiAgentConfigurations":[{"handoffAgentConfiguration":{"agentTarget":{"applicationId":"$APP_ID"},
   "instruction":{"instruction":"Transfer the conversation to this agent for all voice interactions. It uses its own voice, answers questions and can hand the caller to a human."},
   "audioStreamingEnabled":true,"immediateHandoff":true}}]}}}
EOF
read -r AGENT_ID AGENT_ARN < <(aws qconnect create-ai-agent --cli-input-json file:///tmp/create-ai-agent.json \
  --query '[aiAgent.aiAgentId,aiAgent.aiAgentArn]' --output text)

echo "== 5. Security profile: allow the third-party agent, associate profile with the AI agent"
aws connect update-security-profile --instance-id "$INSTANCE_ID" --security-profile-id "$SECURITY_PROFILE_ID" \
  --allowed-ai-agents "[{\"Arn\":\"$APP_ARN\",\"Type\":\"THIRD_PARTY\"}]"
aws connect associate-security-profiles --instance-id "$INSTANCE_ID" --entity-arn "$AGENT_ARN" \
  --entity-type AI_AGENT --security-profiles "Id=$SECURITY_PROFILE_ID"

echo "== 6. Publish a version (changes do not apply until you do)"
VERSION_ARN=$(aws qconnect create-ai-agent-version --assistant-id "$ASSISTANT_ID" --ai-agent-id "$AGENT_ID" \
  --query aiAgent.aiAgentArn --output text)
# Docs say to associate the profile with the versioned ARN too.
aws connect associate-security-profiles --instance-id "$INSTANCE_ID" --entity-arn "$VERSION_ARN" \
  --entity-type AI_AGENT --security-profiles "Id=$SECURITY_PROFILE_ID" || \
  echo "(versioned-ARN association failed; check the ARN form in the console/docs)"

cat <<EOF

Done. Save these:
  export A2A_API_KEY=$API_KEY
  application id: $APP_ID   ai agent id: $AGENT_ID

Still manual (console): Lex V2 bot + alias with a Nova Sonic locale and one
AMAZON.QInConnectIntent, add it to the instance, build the contact flow, assign it to your
number, and point the AI agent at the flow. See docs/AWS_SETUP.md steps 7-9.
EOF
