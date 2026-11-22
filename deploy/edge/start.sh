#!/bin/sh
# Starts Caddy: the edge's command (see compose.yaml).
#
# A browser that opens the page by IP address sends no server name (SNI) in the TLS handshake, and
# behind Docker's port publishing Caddy cannot see which address it was opened with: it answers
# with the certificate of its default server name, or fails the handshake without one. That name,
# DOMO_DEFAULT_SNI for the Caddyfile, is the first IPv4 address of DOMO_SITE_ADDRESSES (else
# its first entry: names always come with SNI).
set -eu
set -f # the addresses are words, never file patterns

default_sni=""
first=""
for address in $(printf '%s' "${DOMO_SITE_ADDRESSES:-localhost}" | tr ',' ' '); do
	[ -n "$first" ] || first="$address"
	case "$address" in
	*[!0-9.]*) ;;
	*)
		default_sni="$address"
		break
		;;
	esac
done
DOMO_DEFAULT_SNI="${default_sni:-${first:-localhost}}"
export DOMO_DEFAULT_SNI

exec caddy run --config /etc/caddy/Caddyfile --adapter caddyfile
