#!/bin/sh
# Provisions the single-user realm used by the ChatGPT MCP connection.
set -eu

KCADM=/opt/keycloak/bin/kcadm.sh
SERVER=http://keycloak:8080/auth
REALM=agent-mcp
CLIENT_ID=chatgpt-agent-mcp
CLIENT_SECRET=${KEYCLOAK_CHATGPT_CLIENT_SECRET:?KEYCLOAK_CHATGPT_CLIENT_SECRET is required}
USER_NAME=${KEYCLOAK_MCP_USERNAME:?KEYCLOAK_MCP_USERNAME is required}
# A deterministic UUID makes the Keycloak `sub` claim known before first login.
USER_ID=00000000-0000-4000-8000-000000000001

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

for role in workspace:read workspace:write command:run browser:use network:fetch; do
  if ! "$KCADM" get "roles/$role" -r "$REALM" >/dev/null 2>&1; then
    "$KCADM" create roles -r "$REALM" -s name="$role"
  fi
done

if ! "$KCADM" get users -r "$REALM" -q username="$USER_NAME" | grep -q '"id"'; then
  "$KCADM" create users -r "$REALM" -s id="$USER_ID" -s username="$USER_NAME" -s enabled=true -s emailVerified=true
  "$KCADM" set-password -r "$REALM" --username "$USER_NAME" --new-password "$KEYCLOAK_MCP_PASSWORD"
  "$KCADM" add-roles -r "$REALM" --uusername "$USER_NAME" \
    --rolename workspace:read --rolename workspace:write --rolename command:run \
    --rolename browser:use --rolename network:fetch
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

echo "Keycloak realm '$REALM' is ready for client '$CLIENT_ID'."
