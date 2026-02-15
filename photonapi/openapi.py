import json
import re
import inspect
from datetime import datetime


def generate_openapi_spec(app, title=None, version=None, description="",
                          servers=None):
    spec = {
        "openapi": "3.1.0",
        "info": {
            "title": title or app.title,
            "version": version or app.version,
            "description": description,
        },
        "servers": servers or [{"url": "/", "description": "Default"}],
        "paths": {},
        "components": {
            "schemas": {},
            "securitySchemes": {
                "bearerAuth": {
                    "type": "http",
                    "scheme": "bearer",
                    "bearerFormat": "JWT",
                },
                "apiKeyAuth": {
                    "type": "apiKey",
                    "in": "header",
                    "name": "X-API-Key",
                },
            },
        },
    }

    for route in app.routes:
        if route.path in ("/docs", "/redoc", "/openapi.json", "/openapi.yaml"):
            continue

        path = _convert_path(route.path)
        if path not in spec["paths"]:
            spec["paths"][path] = {}

        for method in route.methods:
            operation = _build_operation(route, method)
            spec["paths"][path][method.lower()] = operation

    return spec


def _convert_path(path):
    result = re.sub(r"<(?:\w+:)?(\w+)>", r"{\1}", path)
    return result


def _build_operation(route, method):
    op = {
        "operationId": f"{method.lower()}_{route.name}",
        "summary": route.name.replace("_", " ").title(),
        "responses": {
            "200": {
                "description": "Successful response",
                "content": {"application/json": {"schema": {"type": "object"}}},
            },
        },
    }

    if route._param_names:
        op["parameters"] = []
        for pname, ptype in route._param_names:
            param = {
                "name": pname,
                "in": "path",
                "required": True,
                "schema": {"type": "integer" if ptype == "int" else "string"},
            }
            op["parameters"].append(param)

    if method in ("POST", "PUT", "PATCH"):
        op["requestBody"] = {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {"type": "object"},
                },
            },
        }

    handler = route.handler
    while hasattr(handler, "__wrapped__"):
        handler = handler.__wrapped__

    doc = inspect.getdoc(handler)
    if doc:
        lines = doc.strip().split("\n")
        op["summary"] = lines[0]
        if len(lines) > 1:
            op["description"] = "\n".join(lines[1:]).strip()

    op["responses"]["400"] = {"description": "Bad request"}
    op["responses"]["404"] = {"description": "Not found"}
    op["responses"]["500"] = {"description": "Internal server error"}

    if method in ("POST", "PUT", "PATCH"):
        op["responses"]["422"] = {"description": "Validation error"}

    return op


def schema_to_openapi(schema):
    if not hasattr(schema, "fields"):
        return {"type": "object"}

    properties = {}
    required = []

    for name, field in schema.fields.items():
        prop = _field_to_schema(field)
        properties[name] = prop
        if field.required:
            required.append(name)

    result = {"type": "object", "properties": properties}
    if required:
        result["required"] = required
    return result


def _field_to_schema(field):
    type_map = {
        str: "string",
        int: "integer",
        float: "number",
        bool: "boolean",
        list: "array",
        dict: "object",
    }

    schema = {"type": type_map.get(field.field_type, "string")}

    if field.min_val is not None:
        schema["minimum"] = field.min_val
    if field.max_val is not None:
        schema["maximum"] = field.max_val
    if field.min_length is not None:
        schema["minLength"] = field.min_length
    if field.max_length is not None:
        schema["maxLength"] = field.max_length
    if field.choices:
        schema["enum"] = list(field.choices)
    if field.pattern:
        schema["pattern"] = field.pattern
    if field.default is not None:
        schema["default"] = field.default

    return schema


SWAGGER_HTML = """<!DOCTYPE html>
<html>
<head>
    <title>{title} — Swagger UI</title>
    <meta charset="utf-8"/>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <link rel="stylesheet" type="text/css" href="https://unpkg.com/swagger-ui-dist@5/swagger-ui.css">
</head>
<body>
    <div id="swagger-ui"></div>
    <script src="https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
    <script>
    SwaggerUIBundle({{
        url: "{spec_url}",
        dom_id: '#swagger-ui',
        deepLinking: true,
        presets: [SwaggerUIBundle.presets.apis, SwaggerUIBundle.SwaggerUIStandalonePreset],
        layout: "StandaloneLayout"
    }})
    </script>
</body>
</html>"""

REDOC_HTML = """<!DOCTYPE html>
<html>
<head>
    <title>{title} — ReDoc</title>
    <meta charset="utf-8"/>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <link href="https://fonts.googleapis.com/css?family=Montserrat:300,400,700|Roboto:300,400,700" rel="stylesheet">
    <style>body {{ margin: 0; padding: 0; }}</style>
</head>
<body>
    <redoc spec-url='{spec_url}'></redoc>
    <script src="https://cdn.redoc.ly/redoc/latest/bundles/redoc.standalone.js"></script>
</body>
</html>"""


def register_openapi_routes(app, spec_url="/openapi.json", swagger_url="/swagger",
                             redoc_url="/redoc", title=None):
    api_title = title or app.title

    @app.get(spec_url)
    def openapi_spec(req, res):
        spec = generate_openapi_spec(app, title=api_title)
        return spec

    if spec_url.endswith(".json"):
        yaml_url = spec_url.replace(".json", ".yaml")

        @app.get(yaml_url)
        def openapi_yaml(req, res):
            spec = generate_openapi_spec(app, title=api_title)
            try:
                import yaml  # type: ignore[import-not-found]
                res.text(yaml.dump(spec, default_flow_style=False))
                res.set_header("Content-Type", "text/yaml")
                return res
            except ImportError:
                return spec

    if swagger_url:
        @app.get(swagger_url)
        def swagger_ui(req, res):
            html = SWAGGER_HTML.format(title=api_title, spec_url=spec_url)
            res.html(html)
            return res

    if redoc_url:
        @app.get(redoc_url)
        def redoc_ui(req, res):
            html = REDOC_HTML.format(title=api_title, spec_url=spec_url)
            res.html(html)
            return res
