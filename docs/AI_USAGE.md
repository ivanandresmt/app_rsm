# Uso de IA en el desarrollo

**Herramientas:** Claude Code (modelo Claude Sonnet 5.5) como agente de desarrollo; un subagente construyó el mock del proveedor y otro el frontend a partir de especificaciones escritas por mí (PRD, contrato OpenAPI y reglas de comportamiento). El *harness* del proyecto está en `CLAUDE.md` (instrucciones para agentes, comandos, invariantes que no deben romperse).

**Cómo se verificó el resultado**
- El contrato `openAPI.json` no se modificó; el mock se valida contra sus esquemas (`jsonschema`) y con un smoke test de Schemathesis.
- Los tests del backend corren contra **Postgres real y el mock real por HTTP** (no dobles en proceso), e inyectan fallos reales (respuesta perdida, 503, 429, latencia). Cada garantía declarada en `docs/ARCHITECTURE.md` indica el test que la cubre.
- `ruff` y `mypy` limpios; en el front `tsc` y ESLint.
- Revisión manual de las zonas de riesgo: orden de commit antes de llamar al proveedor, semántica de `unknown`, publicación atómica de archivos, y permisos en SSE/MCP.
- Los evals con modelo real están separados (`tests/evals/test_assistant_llm.py`, marcados `llm`) y se saltan sin `ANTHROPIC_API_KEY`.
