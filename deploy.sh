#!/bin/bash
# Deploy de ESTE servicio, independiente de los otros 3. Corre desde el
# droplet, asumiendo la estructura de hermanos de nexolu-infra (ver su
# README.md): este repo y nexolu-infra clonados uno al lado del otro.
set -e
cd "$(dirname "$0")"

echo "==> git pull"
git pull origin main

echo "==> Reconstruyendo y reiniciando comms-api"
cd ../nexolu-infra
docker compose build comms-api

echo "==> Migrando esquema (alembic upgrade head)"
docker compose run --rm comms-api alembic upgrade head

docker compose up -d comms-api

echo "==> Listo. Verificar: curl -s https://comms.nexolu.co/health"
