# Harness de desarrollo (instrucciones para agentes)

Plataforma de planificación de viajes. Requisitos en `prd.md` y `guide.md`; contrato del proveedor en `openAPI.json` (**no modificar**).

## Estructura
- `backend/` FastAPI + SQLAlchemy 2 + Alembic. Un código, tres procesos: `app.api.main` (API), `app.worker` (jobs), `app.mcp_server` (MCP de solo lectura).
- `mock_provider/` proveedor simulado (FastAPI + SQLite). Superficie pública = `/v1` (contrato); `/_control` es solo para demos/tests.
- `frontend/` Next.js; cliente TS generado de `frontend/openapi.json` (`backend: python -m scripts.export_openapi ../frontend/openapi.json`, luego `npm run gen:api`).
- `docs/` arquitectura, demo, uso de IA.

## Comandos
```bash
# Postgres para tests (puerto 5433)
docker run -d --name rsm-test-pg -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=rsm_test -p 5433:5432 postgres:16-alpine
cd backend && python -m venv .venv && .venv/Scripts/pip install -r requirements-dev.txt
.venv/Scripts/python -m pytest -q            # incluye mock y MCP en subprocesos
.venv/Scripts/ruff check app tests && .venv/Scripts/python -m mypy app
cd ../mock_provider && .venv/Scripts/python -m pytest -q
```

## Invariantes (no romper; cada una tiene tests)
1. Dinero en centavos enteros, USD, nunca float.
2. Toda mutación versionada del viaje hace CAS sobre `trips.version` **antes** de tocar datos; 409 devuelve estado actual.
3. La intención de reserva se hace *commit* **antes** de llamar al proveedor; misma `Idempotency-Key` ⇒ misma reserva. Timeout ⇒ `unknown`, nunca éxito.
4. Los estados terminales del proveedor no se revierten; `accepted_offer/price` son inmutables.
5. Documentos: temp → fsync → rename → hash → `ready`; el PDF sale del snapshot guardado.
6. Autorización solo vía `services/authz.py` (REST, SSE y MCP). No miembros ⇒ 404.
7. MCP: solo herramientas de lectura. Añadir una herramienta que modifique estado está prohibido.
8. La app nunca usa `/_control` ni la BD del mock.

## Estilo
Código sin comentarios obvios; docstrings solo donde explican una garantía. Cambios de esquema ⇒ nueva migración Alembic (`alembic revision --autogenerate`).
