# Arquitectura, garantías y límites

Monolito modular en Python (FastAPI) que corre como **tres procesos del mismo código** (API, worker, servidor MCP), más el **mock del proveedor** como servicio aparte (representa algo externo) y un frontend Next.js.

```
Next.js ──REST + SSE──▶ API (FastAPI) ──SQL──▶ PostgreSQL ◀──SQL── Worker (jobs: PDF, reconciliación)
                         │   ▲                                     │
                         │   └── MCP (streamable HTTP, JWT del usuario) ── MCP server (solo lectura)
                         │                                         
                         └────────── HTTP (contrato openAPI.json) ───▶ Mock del proveedor (SQLite propia)
```

## Decisiones clave y alternativas

| Decisión | Por qué | Alternativa descartada |
|---|---|---|
| Postgres para todo (datos, jobs, eventos) | Transacciones, `UNIQUE`/índices parciales para idempotencia, `FOR UPDATE SKIP LOCKED` para la cola | Redis+Celery: más piezas, peor atomicidad con el estado |
| Cola de jobs = tabla `jobs` con lease | Durable; si el worker muere, el lease expira y otro worker retoma el job | Cola en memoria (se pierde) |
| Bloqueo optimista (`trips.version` + `If-Match`) | Ninguna edición se pierde en silencio; el 409 trae el estado actual y qué cambió | Last-write-wins; bloqueos pesimistas |
| SSE + re-fetch del estado | Notificaciones unidireccionales; el cliente no depende de recibir cada evento | WebSockets |
| Eventos en tabla `events` escritos en la misma transacción que el cambio | Un evento existe ⇔ el cambio hizo commit; `Last-Event-ID` permite reanudar | Notificar tras el commit (puede perderse) |
| `reportlab` para el PDF | Sin dependencias de sistema (WeasyPrint exige Pango/GTK); misma garantía de publicación atómica | WeasyPrint |
| Almacenamiento en volumen local | Suficiente; la publicación es temp→fsync→rename atómico | MinIO/S3 (despliegue propuesto) |
| Asistente: loop propio de tool-calling con el SDK de Anthropic | Simple y defendible; las herramientas se descubren por MCP | LangGraph/LangChain |
| Cliente TS generado del OpenAPI (`openapi-typescript`) | Contrato front/back único | Tipos escritos a mano |
| Sync SQLAlchemy + endpoints `def` | Menos superficie de error; FastAPI los ejecuta en un threadpool. Solo SSE y el asistente son `async` | Async ORM |

## Garantías (y cómo se prueban)

**Edición concurrente (US-06).** Toda mutación versionada hace primero un `UPDATE trips SET version=version+1 WHERE id=? AND version=?`. Esa sentencia toma el lock de fila: los editores concurrentes se serializan y el perdedor re-evalúa el `WHERE`, no coincide y recibe `409 VERSION_CONFLICT` con `current_version`, `changes_since` (quién hizo qué) y el viaje completo. Remoción de ítems y reservas se serializan con `SELECT … FOR UPDATE` del ítem. Tests: `test_concurrent_edits_never_lose_a_change`, `test_parallel_edits_exactly_one_wins` (6 hilos).

**Reservas (US-07/08).**
- Antes de llamar al proveedor se hace *commit* de la intención (`bookings`, `UNIQUE(trip_id, idempotency_key)`, `client_reference` único). Un reintento con la misma clave devuelve la misma reserva (200, `replayed`); misma clave con otro cuerpo → `IDEMPOTENCY_CONFLICT`. Un índice único parcial impide dos reservas vivas (`pending|confirmed|unknown`) del mismo ítem aunque se usen claves distintas.
- La reserva usa exactamente la revisión y el precio que el usuario revisó (`expectedOfferRevision`, `expectedPrice`). Si la revisión difiere de la del itinerario hace falta `accept_changed_terms=true` (el servidor lo exige, no solo la UI).
- Estados locales: `pending` (aceptada, sin resultado final), `confirmed`, `failed`, `unknown`. Un rechazo explícito del proveedor (409 `OFFER_CHANGED/UNAVAILABLE`, 4xx, 429/503: el contrato dice que no crean reserva) → `failed` con `request_failed=true`. Un timeout / respuesta perdida → `unknown` (nunca éxito ni fallo limpio).
- Reconciliación (worker): `GET /bookings/by-reference/{ref}`; si 404 y pasó la gracia, **repite el mismo POST con la misma `Idempotency-Key`** (solo puede crear una reserva) dentro de una ventana acotada (15 min); fuera de ella queda `unknown` y el usuario puede *reconciliar ahora* o *abandonar* (solo si el proveedor confirma que no tiene registro). Pendientes se consultan con backoff. Un estado terminal del proveedor nunca se revierte localmente. Un barredor periódico re-encola reservas atascadas (p. ej. el proceso murió tras el commit de la intención).
- Se guarda `accepted_offer` y `accepted_price` (snapshot inmutable del proveedor); los costos confirmados usan ese precio aunque el proveedor cambie después. Se guarda el `requestId` del proveedor.
- Tests: `test_bookings.py` (15) incluyendo respuesta perdida real (`drop_response` en el mock), 503, precio cambiado, no disponible, concurrencia de 8 hilos, crash tras el commit de la intención, pendiente→confirmada con reloj del mock.

**Documentos (US-09/10).** El PDF se genera desde un **snapshot congelado** guardado en la fila (versión del viaje + `as_of` + hash). Publicación: archivo temporal → `fsync` → `os.replace` → sha256 → recién entonces `status=ready`. Solicitudes repetidas con los mismos datos colapsan en un documento (índice único parcial). La descarga re-verifica tamaño y hash; si el archivo falta o está corrupto, el documento vuelve a `queued` y se regenera desde el snapshot (también lo hace la auditoría periódica). Jobs fallidos se reintentan con backoff; agotados → `failed` + `POST /documents/{id}/retry`. Tests: ciclo completo, archivo borrado, archivo corrupto, fallo de render, caída entre rename y publicación.

**Autorización (US-02).** Un único servicio (`services/authz.py`) usado por REST, SSE y MCP. Los no miembros reciben **404** (no se revela la existencia del viaje); rol insuficiente → 403. SSE revalida la membresía en cada ciclo y corta la conexión si se revoca. MCP recibe el JWT del usuario en cada petición; un `trip_id` solo no da acceso. Tests: `test_trips_authz.py`, `test_mcp_assistant_events.py`.

**Proveedor lento o caído.** `httpx` con timeouts de conexión/lectura y un **deadline total** por llamada (8 s por defecto); reintentos acotados que respetan `Retry-After` (con tope); la UI nunca espera indefinidamente: búsquedas devuelven `503/429` con mensaje, reservas pasan a `unknown` y se reconcilian en segundo plano.

**Frescura.** Cada oferta muestra `valid_until`/`expired`; la búsqueda se cachea 30 s en BD y la respuesta indica `from_cache`, `age_seconds` y una nota de que son precios indicativos. Solo se cachean búsquedas completas. Un ítem del itinerario es una *estimación* (snapshot de la oferta); se marca `price_stale` si venció.

## Costos y redondeo (US-05)
Todo en **USD y centavos enteros**; sin floats. Los precios del proveedor tienen exactamente 2 decimales, así que las sumas son exactas y no se redondea nada. La única división es el reparto informativo por viajero: *round-half-up* al centavo. Los precios de oferta cubren a todo el grupo y toda la estadía/segmento/actividad: no se multiplican. Cada línea es `estimate | pending | unknown | confirmed`; `failed` no suma. `unknown` y `pending` suman al precio revisado/aceptado (criterio conservador) pero se muestran por separado.

## Reglas simplificadas (documentadas, US-04)
- Solapes: se comparan actividades y transporte entre sí, y alojamientos entre sí; un hotel que se solapa con una actividad **no** es conflicto.
- Las fechas de ventana (`OUTSIDE_TRIP_DATES`) usan la fecha local de la oferta (su propio offset).
- Noches sin alojamiento = noches de la ventana del viaje no cubiertas por estadías.
- No se modelan tiempos de traslado, husos horarios entre ítems ni escalas.

## Alcance y supuestos
- Destinos y datos: los del mock (Lisbon, Paris, Rome, Madrid; orígenes extra New York, London, Santiago).
- Identidad: usuarios sembrados (`alice` organizadora, `bob`, `carol`, `dave`) y `POST /auth/dev-login` que emite JWT. Agregar participantes = por email de un usuario sembrado.
- Cancelaciones, reembolsos, pagos y moneda: fuera de alcance. Un ítem con reserva viva no se puede quitar.
- Una sola versión (`trips.version`) cubre datos del viaje e itinerario: simple y segura, a costa de falsos conflictos entre ediciones no relacionadas (el 409 los hace baratos de resolver).

## Limitaciones conocidas
- El reintento automático del POST tras un `unknown` repite una reserva que el usuario aprobó hace minutos; está acotado por ventana, y el proveedor solo la acepta si revisión y precio aún coinciden. "Abandonar" tiene una carrera residual si el proveedor procesa tarde.
- Polling de 1 s en SSE (no LISTEN/NOTIFY): simple y robusto, algo más de carga.
- El worker de PDF y la API comparten un volumen local; en la nube corresponde un bucket.
- El asistente no mantiene memoria entre sesiones (el cliente envía el historial) y no hay *streaming* de tokens.
- Los tests de concurrencia usan hilos contra Postgres real; no se prueba multi-nodo.

## Despliegue propuesto (solo documentado)
Contenedores en **Cloud Run** o **Azure Container Apps** (api, worker, mcp, frontend; el worker con mínimo 1 instancia sin escalado a cero), **Postgres gestionado** (Cloud SQL / Azure Database for PostgreSQL), un **bucket** (GCS / Blob) para documentos con el mismo patrón temp→rename (o *upload* a clave nueva + verificación de hash), secretos en Secret Manager / Key Vault, `/healthz` y `/readyz` como probes, migraciones como paso de despliegue previo (job). El mock se sustituye por el proveedor real vía `PROVIDER_BASE_URL`.
