"""Documentação da API: serve a spec OpenAPI e uma página Swagger UI para a navegar."""

from pathlib import Path

from django.conf import settings
from django.http import HttpResponse, HttpResponseNotFound

_OPENAPI_PATH = Path(settings.BASE_DIR) / "docs" / "openapi.yaml"

_SWAGGER_UI_HTML = """<!doctype html>
<html lang="pt">
<head>
  <meta charset="utf-8">
  <title>MatchGrants — API</title>
  <link rel="stylesheet" href="https://unpkg.com/swagger-ui-dist@5/swagger-ui.css">
</head>
<body>
  <div id="swagger-ui"></div>
  <script src="https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
  <script>
    window.onload = () => SwaggerUIBundle({
      url: "/docs/openapi.yaml",
      dom_id: "#swagger-ui",
      presets: [SwaggerUIBundle.presets.apis],
    });
  </script>
</body>
</html>
"""


def swagger_ui(request):
    """GET /docs/ — página Swagger UI (carrega a spec de /docs/openapi.yaml via CDN)."""
    return HttpResponse(_SWAGGER_UI_HTML, content_type="text/html")


def openapi_spec(request):
    """GET /docs/openapi.yaml — a spec OpenAPI em si (YAML)."""
    if not _OPENAPI_PATH.is_file():
        return HttpResponseNotFound("openapi.yaml não encontrado.")
    return HttpResponse(_OPENAPI_PATH.read_bytes(), content_type="application/yaml")
