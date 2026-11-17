# Recorrido de demo

Prerrequisito: `docker compose up --build` (ver README). UI en http://localhost:3000. Para los fallos se usa el panel de control del **mock** (no lo usa la aplicación):

```bash
C='-H X-Control-Token:control-demo-token'
M=http://localhost:4010
```

## 1. Flujo principal
1. Entra como **alice** (organizadora). Crea un viaje: *Lisbon*, origen *New York*, fechas futuras (p. ej. 2030-06-10 → 2030-06-13), presupuesto `5000`, 2 viajeros.
2. En **Overview** agrega a `bob@example.com` como *editor* y a `carol@example.com` como *viewer*.
3. En **Search** busca *accommodation* y *activity*; observa `complete`, la frescura y las etiquetas "indicativo". Agrega un hotel y dos actividades. En **Itinerary** ve costos (estimados), issues (p. ej. noches sin alojamiento, solapes) y el total vs presupuesto.
4. Pulsa **Book** en el hotel: revisa precio/disponibilidad actuales, confirma → `confirmed`. El costo pasa de *estimate* a *confirmed*.
5. En **Documents** → *Generate*. Verás `queued → running → ready` sin recargar (SSE). Descarga el PDF. Prueba `PDF_RENDER_DELAY_S=5` en `.env` para ver el estado "running" y recarga la página a mitad: el estado se restaura.

## 2. Fallos y concurrencia (reproducibles)

| Escenario | Comando (mock) | Qué observar |
|---|---|---|
| **Búsqueda parcial** | `curl -X POST $M/_control/faults $C -H 'content-type: application/json' -d '{"operation":"searchOffers","kind":"error","supplier":"stayhub","status":503}'` | Search → *partial* con aviso de `stayhub`; los resultados de `cozy-nights` siguen. |
| **429 con Retry-After** | `... -d '{"operation":"searchOffers","kind":"error","status":429,"retry_after":3}'` y busca con proveedor único | Mensaje de reintento, sin esperas infinitas. |
| **Precio cambió antes de reservar** | `curl -X POST $M/_control/offers/<offerId> $C -H 'content-type: application/json' -d '{"price":"9999.00"}'` | *Book* muestra el cambio y exige aceptar nuevos términos explícitamente. |
| **Reservó pero se perdió la respuesta** | `... -d '{"operation":"createBooking","kind":"drop_response","delay_ms":30000}'` y reserva | Estado **unknown** (no éxito ni fallo). Borra la regla (`curl -X DELETE $M/_control/faults $C`) y pulsa *Reconcile now* (o espera al worker) → `confirmed`, sin duplicados. |
| **Reserva pendiente** | Reserva en un hotel `cozy-nights` | `pending`; `curl -X POST $M/_control/clock $C -H 'content-type: application/json' -d '{"advance_seconds":60}'` → el worker la confirma. |
| **Fallo parcial** | `error 503` en `createBooking` para un `offerId` | Una reserva `confirmed` y otra `failed` visibles, con próximos pasos. |
| **Edición concurrente** | Abre el mismo viaje como **alice** y **bob**; ambos agregan un ítem | El segundo recibe el panel de conflicto: no se guardó, qué cambió, estado actual y reintento. |
| **Archivo perdido** | `docker compose exec api sh -c 'rm /data/documents/*/*.pdf'` y descarga | `DOCUMENT_REGENERATING`, el worker regenera desde el snapshot y la descarga vuelve a funcionar. |
| **Worker muere a mitad** | `docker compose kill worker && docker compose up -d worker` durante una generación | El lease expira y el job se retoma; nunca queda un `ready` sin archivo. |
| **Permisos** | Entra como **carol** (viewer) y **dave** (sin acceso) | Carol ve y descarga, sin controles de edición (y la API devuelve 403); Dave no ve el viaje (404), ni su SSE, ni vía asistente/MCP. |

Reinicia el estado del mock con `curl -X POST $M/_control/reset $C`.

## 3. Asistente (requiere `ANTHROPIC_API_KEY`)
Como carol: "¿Qué hotel recomiendas dentro del presupuesto restante?". Abre *tool calls*: `get_trip`, `search_offers` vía MCP. Pídele "reserva el más barato": explica que es de solo lectura. Con `curl -X POST $M/_control/faults ... searchOffers 503`, el asistente informa que la búsqueda falló en vez de inventar.
