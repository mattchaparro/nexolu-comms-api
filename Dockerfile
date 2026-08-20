# FastAPI + Uvicorn -- mismo patron que nexolu-ia-core y nexolu-payments-core.
#
# Sin gcc a proposito: todas las dependencias de pyproject.toml bajan como
# wheel precompilado para linux x86_64 (nada se compila desde source) -
# instalarlo agregaba ~240MB sin necesidad, verificado en vivo el
# 2026-08-20 (build identico con/sin gcc, mismo resultado, imagen final
# 536MB -> 293MB en ia-core).
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml ./
COPY nexolu_comms_api ./nexolu_comms_api
COPY alembic ./alembic
COPY alembic.ini ./

RUN pip install --no-cache-dir .

EXPOSE 8000
# El esquema se maneja con `alembic upgrade head` como paso de deploy (ver
# deploy/README.md en nexolu-infra) -- este contenedor NUNCA migra solo al
# arrancar.
CMD ["uvicorn", "nexolu_comms_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
