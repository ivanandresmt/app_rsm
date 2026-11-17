"""Writes the API's OpenAPI document (source for the generated TypeScript client)."""
import json
import sys

from app.api.main import create_app

out = sys.argv[1] if len(sys.argv) > 1 else "../frontend/openapi.json"
with open(out, "w", encoding="utf-8") as f:
    json.dump(create_app().openapi(), f, indent=2)
print("wrote", out)
