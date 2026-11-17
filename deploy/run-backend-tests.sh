#!/bin/sh
# Run the backend test suite of this checkout against throwaway PostgreSQL + Redis.
B="$(cd "$(dirname "$0")/../backend" && pwd)"
IMAGE="${MDM_TEST_IMAGE:-mangadex-webdl:local}"
docker network create mdmtest >/dev/null 2>&1
docker run -d --rm --name mdmtest-db --network mdmtest -e POSTGRES_PASSWORD=t -e POSTGRES_USER=t -e POSTGRES_DB=t postgres:17 >/dev/null
docker run -d --rm --name mdmtest-redis --network mdmtest redis:7.4-alpine >/dev/null
for i in $(seq 1 30); do docker exec mdmtest-db pg_isready -U t >/dev/null 2>&1 && break; sleep 1; done; sleep 2
docker run --rm -u 0 --network mdmtest \
  -e MDM_TEST_DATABASE_URL=postgresql+psycopg://t:t@mdmtest-db:5432/t -e MDM_TEST_REDIS_URL=redis://mdmtest-redis:6379/15 \
  -v $B/app:/app/app:ro -v $B/tests:/app/tests:ro -v $B/alembic:/app/alembic:ro -v $B/alembic.ini:/app/alembic.ini:ro \
  -v $B/requirements-dev.txt:/tmp/req-dev.txt:ro -w /app -e PYTHONDONTWRITEBYTECODE=1 \
  --entrypoint sh "$IMAGE" -c "pip install -q pytest==9.1.1 respx==0.23.1 >/dev/null 2>&1; python -m pytest -q -p no:cacheprovider $*" 
rc=$?
docker rm -f mdmtest-db mdmtest-redis >/dev/null 2>&1; docker network rm mdmtest >/dev/null 2>&1
exit $rc
