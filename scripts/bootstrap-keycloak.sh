#!/bin/sh
# Provisions the single-user realm used by the ChatGPT MCP connection.
set -eu

KCADM=/opt/keycloak/bin/kcadm.sh
SERVER=http://keycloak:8080/auth
REALM=agent-mcp
CLIENT_ID=chatgpt-agent-mcp
CLIENT_SECRET=${KEYCLOAK_CHATGPT_CLIENT_SECRET:?KEYCLOAK_CHATGPT_CLIENT_SECRET is required}
USER_NAME=${KEYCLOAK_MCP_USERNAME:?KEYCLOAK_MCP_USERNAME is required}

until "$KCADM" config credentials \
  --server "$SERVER" \
  --realm master \
  --user "$KEYCLOAK_ADMIN_USERNAME" \
  --password "$KEYCLOAK_ADMIN_PASSWORD" >/dev/null 2>&1; do
  sleep 3
done

if ! "$KCADM" get "realms/$REALM" >/dev/null 2>&1; then
  "$KCADM" create realms -s realm="$REALM" -s enabled=true -s displayName="Agent MCP"
fi

for role in workspace:read workspace:write command:run browser:use network:fetch secrets:use database:use deploy:run github:write; do
  if ! "$KCADM" get "roles/$role" -r "$REALM" >/dev/null 2>&1; then
    "$KCADM" create roles -r "$REALM" -s name="$role"
  fi
done

if ! "$KCADM" get users -r "$REALM" -q username="$USER_NAME" | grep -q '"id"'; then
  "$KCADM" create users -r "$REALM" -s username="$USER_NAME" -s enabled=true -s emailVerified=true
  "$KCADM" set-password -r "$REALM" --username "$USER_NAME" --new-password "$KEYCLOAK_MCP_PASSWORD"
fi

"$KCADM" add-roles -r "$REALM" --uusername "$USER_NAME" \
  --rolename workspace:read --rolename workspace:write --rolename command:run \
  --rolename browser:use --rolename network:fetch --rolename secrets:use \
  --rolename database:use --rolename deploy:run --rolename github:write

USER_ID=$("$KCADM" get users -r "$REALM" -q username="$USER_NAME" | sed -n 's/.*"id" *: *"\([^"]*\)".*/\1/p' | head -n 1)
if [ -z "$USER_ID" ]; then
  echo "Could not resolve the Keycloak subject for $USER_NAME" >&2
  exit 1
fi
if [ -f /config/workspaces.json ]; then
  sed -i "s/KEYCLOAK_MCP_SUBJECT/$USER_ID/g" /config/workspaces.json
  if [ "${ENABLE_SELF_IMPROVEMENT_WORKSPACE:-false}" = "true" ]; then
    case "${SELF_IMPROVEMENT_WORKSPACE:-agent-mcp}" in
      *[!A-Za-z0-9_.-]*|"") echo "Invalid SELF_IMPROVEMENT_WORKSPACE" >&2; exit 1 ;;
    esac
    SELF_PATH="/workspaces/${SELF_IMPROVEMENT_WORKSPACE:-agent-mcp}"
    if ! grep -Fq "\"$SELF_PATH\"" /config/workspaces.json; then
      sed -i "s#\"$USER_ID\"[[:space:]]*:[[:space:]]*\[#\"$USER_ID\": [\"$SELF_PATH\", #" /config/workspaces.json
    fi
  fi
fi

if ! "$KCADM" get clients -r "$REALM" -q clientId="$CLIENT_ID" | grep -q '"id"'; then
  "$KCADM" create clients -r "$REALM" \
    -s clientId="$CLIENT_ID" \
    -s enabled=true \
    -s publicClient=false \
    -s clientAuthenticatorType=client-secret \
    -s secret="$CLIENT_SECRET" \
    -s standardFlowEnabled=true \
    -s directAccessGrantsEnabled=false \
    -s 'redirectUris=["https://chatgpt.com/*","https://chat.openai.com/*"]' \
    -s 'webOrigins=["https://chatgpt.com","https://chat.openai.com"]'
fi

echo "Keycloak realm '$REALM' is ready for client '$CLIENT_ID' and user subject '$USER_ID'."
