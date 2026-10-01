#!/usr/bin/env bash
# Brings up a local WooCommerce store, installs WooCommerce, seeds fictional
# data and writes read-only REST keys to dev/.env. Safe to re-run.
set -euo pipefail
cd "$(dirname "$0")"
PORT="${WOO_DEV_PORT:-8088}"
URL="http://localhost:${PORT}"
wp() { docker compose run --rm -T cli wp "$@"; }

docker compose up -d
echo "waiting for wordpress on ${URL}"
for _ in $(seq 1 60); do
  curl -fs -o /dev/null "${URL}/wp-login.php" 2>/dev/null && break || sleep 2
done

if ! wp core is-installed >/dev/null 2>&1; then
  ADMIN_PASS="$(openssl rand -hex 12)"
  wp core install --url="${URL}" --title="Chai Point Demo Store" \
    --admin_user=admin --admin_password="${ADMIN_PASS}" \
    --admin_email=admin@example.test --skip-email
  echo "WP_ADMIN_PASSWORD=${ADMIN_PASS}" > .admin
fi
# The image can lag behind; current WooCommerce needs the newest core.
wp core update --quiet
wp plugin is-installed woocommerce || wp plugin install woocommerce
wp plugin activate woocommerce
wp rewrite structure '/%postname%/' --hard
wp option update woocommerce_currency INR
wp option update woocommerce_default_country IN:KA
wp option update woocommerce_manage_stock yes
wp option update woocommerce_notify_low_stock_amount 5

wp eval-file /seed/seed.php

# Two key pairs: one read-only (what the connector should get), one
# read_write (only so the tests can prove the connector never writes).
wp eval-file /seed/keys.php > .env
echo "WOO_BASE_URL=${URL}" >> .env
echo "done; keys in dev/.env"
