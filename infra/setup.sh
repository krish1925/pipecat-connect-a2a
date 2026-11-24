#!/usr/bin/env bash
# Registers your A2A server with Amazon Connect. Every operation used here exists in
# botocore's service model (qconnect, appintegrations, connect, secretsmanager, kms).
# Review before running: it creates billable resources (KMS key, secret).
#
#   REGION=us-east-1 INSTANCE_ID=<connect instance id> ACCESS_URL=wss://<host>/a2a \
#   NAMESPACE=pipecat-a2a ./infra/setup.sh
set -euo pipefail
: "${INSTANCE_ID:?}" "${ACCESS_URL:?}"
REGION="${REGION:-us-east-1}"; NAMESPACE="${NAMESPACE:-pipecatbot}"
export AWS_DEFAULT_REGION="$REGION"

API_KEY="${A2A_API_KEY:-$(openssl rand -hex 24)}"
echo "A2A_API_KEY=$API_KEY   # export this for your bot"

# 1. Customer-managed KMS key (the default aws/secretsmanager key does not work) and secret
KMS_ARN=$(aws kms create-key --description "pipecat-connect-a2a" --query KeyMetadata.Arn --output text)
SECRET_ARN=$(aws secretsmanager create-secret --name "pipecat-connect-a2a/$NAMESPACE" \
  --kms-key-id "$KMS_ARN" --secret-string "$API_KEY" --query ARN --output text)

# 2. A2A_SERVER application (ApplicationType cannot be changed later)
APP_ARN=$(aws appintegrations create-application --name "$NAMESPACE" --namespace "$NAMESPACE" \
  --application-type A2A_SERVER \
  --application-source-config "ExternalUrlConfig={AccessUrl=$ACCESS_URL}" \
  --auth-config "AuthType=API_KEY,CredentialProviderIdentifier=$SECRET_ARN" \
  --permissions '[]' --query Arn --output text)
echo "application: $APP_ARN"

cat <<EOF

Remaining steps (use the shapes from the AWS docs; list operations with
  python -c "import boto3; print(boto3.client('qconnect').meta.service_model.operation_names)"):
 3. connect update-security-profile --applications Namespace=$NAMESPACE,ApplicationPermissions=[...],Type=THIRD_PARTY_APPLICATION
 4. qconnect create-ai-agent (type ORCHESTRATION) with multiAgentConfigurations[].handoffAgentConfiguration
      {agentTarget:{applicationId:<app id>}, audioStreamingEnabled:true, immediateHandoff:true}
    and toolConfigurations for Complete and Escalate only; then create-ai-agent-version.
 5. qconnect update-assistant-ai-agent --ai-agent-type ORCHESTRATION --configuration aiAgentId=<id>
 6. Point the Lex V2 bot (Nova Sonic locale, one AMAZON.QInConnectIntent) at the number's flow.
EOF
