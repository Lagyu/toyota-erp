#!/usr/bin/env bash
# TLS_MODE=letsencrypt on a stack from a production image, against Pebble (Let's Encrypt's ACME
# test server) beside it on the stack's outbound network: Traefik obtains the site's certificate
# through the HTTP-01 challenge (Pebble reaches the site's name on port 80 through the proxy's
# network alias, as the internet reaches a real host), the HTTP entrypoint redirects, and the
# site answers over HTTPS with a chain to the root Pebble issued it under.
#   acme-check.sh IMAGE:TAG
# Report in .local/acme/report.md; the stack's logs in .local/acme/compose.log.
set -euo pipefail
source "$(dirname "$0")/../lib/common.sh"
# shellcheck source=scripts/lib/stack.sh
source "$(dirname "$0")/../lib/stack.sh"
export LOG_TAG=acme
load_env_file "$REPO_ROOT/infra/ci-tools.env"
image=${1:?usage: acme-check.sh IMAGE:TAG}
out=$REPO_ROOT/.local/acme
site=erp.hinode.test
http_port=18096 https_port=18443
rm -rf "$out"
mkdir -p "$out/pebble"
report=$out/report.md
failures=0
result() { # check, ok|FAILED, detail
	echo "| $1 | $2 | $3 |" >>"$report"
	[[ $2 == ok ]] || failures=$((failures + 1))
}

# Pebble's API certificate, for the name the proxy reaches it by, from a CA made for this run
# (Traefik's ACME client trusts it through LEGO_CA_CERTIFICATES)
openssl req -x509 -newkey rsa:2048 -nodes -days 2 -subj "/CN=ACME check API CA" \
	-keyout "$out/pebble/ca-key.pem" -out "$out/pebble/ca.pem" 2>/dev/null
openssl req -newkey rsa:2048 -nodes -subj "/CN=pebble" \
	-keyout "$out/pebble/api-key.pem" -out "$out/pebble/api.csr" 2>/dev/null
openssl x509 -req -in "$out/pebble/api.csr" -CA "$out/pebble/ca.pem" -CAkey "$out/pebble/ca-key.pem" \
	-CAcreateserial -days 2 -extfile <(printf 'subjectAltName=DNS:pebble,DNS:localhost,IP:127.0.0.1') \
	-out "$out/pebble/api.pem" 2>/dev/null
# HTTP-01 validation on port 80, where Traefik's web entrypoint answers the challenge
cat >"$out/pebble/config.json" <<EOF
{"pebble": {"listenAddress": "0.0.0.0:14000", "managementListenAddress": "0.0.0.0:15000",
  "certificate": "/etc/pebble/api.pem", "privateKey": "/etc/pebble/api-key.pem",
  "httpPort": 80, "tlsPort": 443, "ocspResponderURL": "", "externalAccountBindingRequired": false}}
EOF
chmod 755 "$out/pebble"
chmod 644 "$out/pebble/"*  # read by the containers' own users
cat >"$out/compose.pebble.yaml" <<EOF
services:
  pebble:
    image: $PEBBLE_IMAGE
    command: ["-config", "/etc/pebble/config.json"]
    environment:
      PEBBLE_VA_NOSLEEP: "1"
      PEBBLE_WFE_NONCEREJECT: "0"
    networks: [egress]
    ports: ["127.0.0.1:15000:15000"]
    volumes: ["$out/pebble:/etc/pebble:ro"]
  proxy:
    depends_on: [pebble]
    environment:
      LEGO_CA_CERTIFICATES: /etc/pebble/ca.pem
    volumes: ["$out/pebble/ca.pem:/etc/pebble/ca.pem:ro"]
EOF

stack_init acme "$http_port" "$image" ENVIRONMENT=production TLS_MODE=letsencrypt \
	LETSENCRYPT_EMAIL=ops@hinode.test "SITE_NAME=$site" "SITES_RULE=Host(\`$site\`)" \
	"HTTP_PUBLISH_PORT=127.0.0.1:$http_port" "HTTPS_PUBLISH_PORT=127.0.0.1:$https_port" \
	ACME_CA_SERVER=https://pebble:14000/dir "COMPOSE_EXTRA_FILES=$out/compose.pebble.yaml"
stack_site=$site
# the test CA's keys never leave the runner
trap 'stack_down "$out/compose.log"; rm -f "$out"/pebble/*key*.pem' EXIT
cat >"$report" <<EOF
# TLS_MODE=letsencrypt against an ACME test server

\`$image\` with Pebble ($PEBBLE_IMAGE), $(date -u +%Y-%m-%dT%H:%M:%SZ).

| Check | Result | Detail |
|---|---|---|
EOF
stack_up

# Traefik asks for the certificate of the router's Host rule when it starts
issuer="" names=""
for _ in $(seq 1 60); do
	cert=$(openssl s_client -connect "127.0.0.1:$https_port" -servername "$site" </dev/null 2>/dev/null || true)
	issuer=$(openssl x509 -noout -issuer <<<"$cert" 2>/dev/null || true)
	[[ $issuer == *Pebble* ]] && break
	sleep 5
done
names=$(openssl x509 -noout -ext subjectAltName <<<"$cert" 2>/dev/null | tail -1 | xargs || true)
if [[ $issuer == *Pebble* && $names == *"DNS:$site"* ]]; then
	result "Certificate obtained through the HTTP-01 challenge" ok "${issuer#issuer=}; $names"
else
	result "Certificate obtained through the HTTP-01 challenge" FAILED "issuer '${issuer:-none}' (see compose.log)"
fi
# the root of the certificates Pebble issues (made when it starts)
curl -sk "https://127.0.0.1:15000/roots/0" >"$out/pebble-root.pem" || true
code=$(curl -s -o /dev/null -w '%{http_code}' --cacert "$out/pebble-root.pem" \
	--resolve "$site:$https_port:127.0.0.1" "https://$site:$https_port/api/method/automotive_core.ops.health.live" \
	|| true)
if [[ $code == 200 ]]; then
	result "HTTPS with a chain to the issuing root" ok "health endpoint 200, verified against Pebble's root"
else
	result "HTTPS with a chain to the issuing root" FAILED "HTTP '${code:-none}'"
fi
redirect=$(curl -s -o /dev/null -w '%{http_code} %{redirect_url}' -H "Host: $site" \
	"http://127.0.0.1:$http_port/login" || true)
if [[ $redirect =~ ^30[178]\ https://$site ]]; then
	result "HTTP redirects to HTTPS" ok "$redirect"
else
	result "HTTP redirects to HTTPS" FAILED "'$redirect'"
fi
printf '\n%s failed check(s).\n' "$failures" >>"$report"
cat "$report"
exit $((failures > 0))
