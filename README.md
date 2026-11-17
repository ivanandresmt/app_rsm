# Plataforma de planificación de viajes

Viajes compartidos con roles, búsqueda en un proveedor externo (mock), itinerario con costos y reservas simuladas robustas ante fallos, documentos PDF y un asistente de IA de solo lectura que usa herramientas **MCP**.

**Stack:** FastAPI + Pydantic v2 · PostgreSQL · SQLAlchemy 2 + Alembic · jobs en Postgres (`SKIP LOCKED`) · SSE · MCP (FastMCP, streamable HTTP) · Next.js 15 (cliente TS generado del OpenAPI) · reportlab · Docker Compose.
Detalle de decisiones, garantías y límites: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). Recorrido de demo: [`docs/DEMO.md`](docs/DEMO.md). Uso de IA: [`docs/AI_USAGE.md`](docs/AI_USAGE.md). Harness de desarrollo: [`CLAUDE.md`](CLAUDE.md).

## Ejecutar
```bash
cp .env.example .env          # opcional: ANTHROPIC_API_KEY para el asistente
docker compose up --build
```
| Servicio | URL |
|---|---|
| Frontend | http://localhost:3000 (login de desarrollo: alice organizadora, bob editor, carol viewer, dave sin acceso) |
| API + OpenAPI | http://localhost:8000 · `/docs` · `/healthz` · `/readyz` |
| MCP (solo lectura) | http://localhost:8001/mcp |
| Mock del proveedor | http://localhost:4010/v1 (control de fallos en `/_control`, ver `mock_provider/README.md`) |

Datos de ejemplo: el mock genera ofertas deterministas para Lisbon, Paris, Rome y Madrid (orígenes extra: New York, London, Santiago) con 2 proveedores por categoría; usa fechas futuras (p. ej. 2030-06-10 → 2030-06-13). Los datos persisten en volúmenes (`pgdata`, `documents`, `mockdata`). Cada servicio tiene healthcheck, apagado ordenado (SIGTERM) y la configuración sale del entorno (`.env.example`).

## Verificaciones y tests
```bash
# Postgres de pruebas
docker run -d --name rsm-test-pg -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=rsm_test -p 5433:5432 postgres:16-alpine

cd backend && python -m venv .venv && .venv/Scripts/pip install -r requirements-dev.txt   # (bin/ en Linux/Mac)
.venv/Scripts/python -m pytest -q                       # 60 tests: levantan el mock y el servidor MCP reales
.venv/Scripts/python -m pytest tests/evals -q           # evals (los de modelo se saltan sin ANTHROPIC_API_KEY)
ANTHROPIC_API_KEY=... .venv/Scripts/python -m pytest tests/evals -m llm   # evals con modelo real (~centavos)
.venv/Scripts/ruff check app tests && .venv/Scripts/python -m mypy app

cd ../mock_provider && python -m venv .venv && .venv/Scripts/pip install -e ".[dev]" && .venv/Scripts/python -m pytest -q   # contrato + Schemathesis
cd ../frontend && npm ci && npm run typecheck && npm run lint && npm run build
```
Qué cubren los tests: autorización por rol en REST/MCP/asistente; ediciones concurrentes (409 con contexto); búsqueda completa/parcial/vacía/fallida/429/lenta; reservas (idempotencia, 8 hilos simultáneos, doble reserva, cambio de precio, no disponible, respuesta perdida → `unknown` → reconciliación sin duplicados, 503, pendiente→confirmada, fallo parcial, crash tras la intención); documentos (snapshot, deduplicación, archivo perdido/corrupto, fallo y reintento, caída antes de publicar); costos y redondeo; eventos reanudables; evals deterministas de recuperación; evals del asistente con modelo (opcionales).

**Límites de los tests:** corren con hilos contra un solo Postgres (no multi-nodo); el frontend se verifica con `tsc`/ESLint/build, no con e2e automatizado; los evals con modelo son estructurales (uso de herramientas, grounding, honestidad), no de calidad de redacción.

## Migraciones
`alembic upgrade head` (la imagen de `api` lo ejecuta al arrancar; es el único proceso que migra). Nuevos cambios: `alembic revision --autogenerate -m "..."`.

## Regenerar el cliente TypeScript
```bash
cd backend && python -m scripts.export_openapi ../frontend/openapi.json
cd ../frontend && npm run gen:api
```

## Despliegue propuesto
Cloud Run / Azure Container Apps + Postgres gestionado + bucket para documentos; ver [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#despliegue-propuesto-solo-documentado).

## Contrato y ambigüedades
`openAPI.json` se usa sin cambios. Ambigüedades resueltas en el mock (destino desconocido ⇒ búsqueda completa vacía, orden de validación en `createBooking`, etc.) están en `mock_provider/README.md`.
