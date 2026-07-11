#!/bin/sh
set -eu

: "${OPENHANDS_DB_NAME:?OPENHANDS_DB_NAME is required}"
: "${OPENHANDS_DB_USER:?OPENHANDS_DB_USER is required}"
: "${OPENHANDS_DB_PASSWORD:?OPENHANDS_DB_PASSWORD is required}"

psql \
  --set=ON_ERROR_STOP=1 \
  --set=openhands_db="$OPENHANDS_DB_NAME" \
  --set=openhands_user="$OPENHANDS_DB_USER" \
  --set=openhands_password="$OPENHANDS_DB_PASSWORD" <<'SQL'
SELECT format(
  'CREATE ROLE %I LOGIN PASSWORD %L',
  :'openhands_user',
  :'openhands_password'
)
WHERE NOT EXISTS (
  SELECT FROM pg_catalog.pg_roles WHERE rolname = :'openhands_user'
) \gexec

SELECT format(
  'ALTER ROLE %I WITH LOGIN PASSWORD %L',
  :'openhands_user',
  :'openhands_password'
) \gexec

SELECT format(
  'CREATE DATABASE %I OWNER %I',
  :'openhands_db',
  :'openhands_user'
)
WHERE NOT EXISTS (
  SELECT FROM pg_catalog.pg_database WHERE datname = :'openhands_db'
) \gexec

SELECT format(
  'ALTER DATABASE %I OWNER TO %I',
  :'openhands_db',
  :'openhands_user'
) \gexec
SQL

echo "OpenHands database '$OPENHANDS_DB_NAME' is ready."
