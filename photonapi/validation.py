import re
from functools import wraps
from datetime import datetime


class Field:
    def __init__(self, field_type=str, required=True, default=None, min_val=None,
                 max_val=None, min_length=None, max_length=None, choices=None,
                 pattern=None, custom=None, label=None, description=None,
                 coerce=False, nullable=False, each=None, schema=None):
        self.field_type = field_type
        self.required = required
        self.default = default
        self.min_val = min_val
        self.max_val = max_val
        self.min_length = min_length
        self.max_length = max_length
        self.choices = choices
        self.pattern = pattern
        self.custom = custom
        self.label = label
        self.description = description
        self.coerce = coerce
        self.nullable = nullable
        self.each = each
        self.schema = schema

    def validate(self, name, value):
        if value is None:
            if self.nullable:
                return None
            if self.required and self.default is None:
                return f"'{name}' is required"
            return None

        if self.coerce:
            value = self._coerce(value)

        if self.field_type == int:
            if not isinstance(value, int) or isinstance(value, bool):
                try:
                    int(value)
                except (ValueError, TypeError):
                    return f"'{name}' must be an integer"
        elif self.field_type == float:
            if not isinstance(value, (int, float)):
                try:
                    float(value)
                except (ValueError, TypeError):
                    return f"'{name}' must be a number"
        elif self.field_type == bool:
            if not isinstance(value, bool):
                return f"'{name}' must be true or false"
        elif self.field_type == list:
            if not isinstance(value, list):
                return f"'{name}' must be a list"
            if self.each:
                for i, item in enumerate(value):
                    if isinstance(self.each, Field):
                        err = self.each.validate(f"{name}[{i}]", item)
                        if err:
                            return err
                    elif isinstance(self.each, Schema):
                        _, errors = self.each.validate(item)
                        if errors:
                            return f"'{name}[{i}]': {errors[0]}"
        elif self.field_type == dict:
            if not isinstance(value, dict):
                return f"'{name}' must be an object"
            if self.schema:
                _, errors = self.schema.validate(value)
                if errors:
                    return f"'{name}': {errors[0]}"
        elif self.field_type == str:
            if not isinstance(value, str):
                return f"'{name}' must be a string"
        elif self.field_type == datetime:
            if isinstance(value, str):
                try:
                    datetime.fromisoformat(value)
                except ValueError:
                    return f"'{name}' must be a valid ISO datetime"
            elif not isinstance(value, datetime):
                return f"'{name}' must be a datetime"

        if self.min_val is not None and isinstance(value, (int, float)):
            if value < self.min_val:
                return f"'{name}' must be at least {self.min_val}"

        if self.max_val is not None and isinstance(value, (int, float)):
            if value > self.max_val:
                return f"'{name}' must be at most {self.max_val}"

        if self.min_length is not None and hasattr(value, '__len__'):
            if len(value) < self.min_length:
                return f"'{name}' must be at least {self.min_length} characters"

        if self.max_length is not None and hasattr(value, '__len__'):
            if len(value) > self.max_length:
                return f"'{name}' must be at most {self.max_length} characters"

        if self.choices is not None and value not in self.choices:
            return f"'{name}' must be one of: {', '.join(str(c) for c in self.choices)}"

        if self.pattern is not None:
            if not re.match(self.pattern, str(value)):
                return f"'{name}' does not match required format"

        if self.custom is not None:
            result = self.custom(value)
            if result is not True and result is not None:
                return result if isinstance(result, str) else f"'{name}' failed validation"

        return None

    def _coerce(self, value):
        try:
            if self.field_type == int:
                return int(value)
            elif self.field_type == float:
                return float(value)
            elif self.field_type == bool:
                if isinstance(value, str):
                    return value.lower() in ("true", "1", "yes")
                return bool(value)
            elif self.field_type == str:
                return str(value)
        except (ValueError, TypeError):
            pass
        return value

    def process(self, value):
        if value is None:
            return self.default
        if self.coerce:
            value = self._coerce(value)
        if self.field_type == int:
            return int(value)
        elif self.field_type == float:
            return float(value)
        elif self.field_type == datetime and isinstance(value, str):
            return datetime.fromisoformat(value)
        return value


def String(required=True, min_length=None, max_length=None, pattern=None, **kw):
    return Field(str, required=required, min_length=min_length,
                 max_length=max_length, pattern=pattern, **kw)


def Integer(required=True, min_val=None, max_val=None, **kw):
    return Field(int, required=required, min_val=min_val, max_val=max_val, **kw)


def Number(required=True, min_val=None, max_val=None, **kw):
    return Field(float, required=required, min_val=min_val, max_val=max_val, **kw)


def Boolean(required=True, **kw):
    return Field(bool, required=required, **kw)


def List(each=None, min_length=None, max_length=None, required=True, **kw):
    return Field(list, required=required, each=each, min_length=min_length,
                 max_length=max_length, **kw)


def Object(schema=None, required=True, **kw):
    return Field(dict, required=required, schema=schema, **kw)


def Email(required=True, **kw):
    return Field(str, required=required, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", **kw)


def URL(required=True, **kw):
    return Field(str, required=required,
                 pattern=r"^https?://[^\s/$.?#].[^\s]*$", **kw)


def DateTime(required=True, **kw):
    return Field(datetime, required=required, **kw)


class Schema:
    def __init__(self, strict=True, **fields):
        self.fields = {}
        self.strict = strict
        for name, val in fields.items():
            if isinstance(val, Field):
                self.fields[name] = val
            elif isinstance(val, Schema):
                self.fields[name] = Field(dict, schema=val)
            elif isinstance(val, type):
                self.fields[name] = Field(field_type=val)
            else:
                self.fields[name] = Field(field_type=type(val), default=val, required=False)

    def validate(self, data):
        if not isinstance(data, dict):
            return None, ["Request body must be a JSON object"]

        errors = []
        cleaned = {}

        for name, field in self.fields.items():
            value = data.get(name)
            error = field.validate(name, value)
            if error:
                errors.append(error)
            else:
                cleaned[name] = field.process(value)

        if self.strict:
            unknown = set(data.keys()) - set(self.fields.keys())
            if unknown:
                errors.append(f"Unknown fields: {', '.join(sorted(unknown))}")

        if errors:
            return None, errors
        return cleaned, []

    def partial(self):
        fields = {}
        for name, field in self.fields.items():
            f = Field(
                field_type=field.field_type, required=False, default=field.default,
                min_val=field.min_val, max_val=field.max_val,
                min_length=field.min_length, max_length=field.max_length,
                choices=field.choices, pattern=field.pattern,
                custom=field.custom, coerce=field.coerce, nullable=field.nullable,
                each=field.each, schema=field.schema,
            )
            fields[name] = f
        return Schema(strict=self.strict, **fields)

    def extend(self, **extra_fields):
        fields = dict(self.fields)
        for name, val in extra_fields.items():
            if isinstance(val, Field):
                fields[name] = val
            elif isinstance(val, type):
                fields[name] = Field(field_type=val)
            else:
                fields[name] = Field(field_type=type(val), default=val, required=False)
        return Schema(strict=self.strict, **fields)

    def to_openapi(self):
        properties = {}
        required = []
        for name, field in self.fields.items():
            prop = _field_to_openapi(field)
            properties[name] = prop
            if field.required:
                required.append(name)
        schema = {"type": "object", "properties": properties}
        if required:
            schema["required"] = required
        return schema


def _field_to_openapi(field):
    type_map = {
        str: "string", int: "integer", float: "number",
        bool: "boolean", list: "array", dict: "object",
        datetime: "string",
    }
    prop = {"type": type_map.get(field.field_type, "string")}
    if field.field_type == datetime:
        prop["format"] = "date-time"
    if field.description:
        prop["description"] = field.description
    if field.min_val is not None:
        prop["minimum"] = field.min_val
    if field.max_val is not None:
        prop["maximum"] = field.max_val
    if field.min_length is not None:
        prop["minLength"] = field.min_length
    if field.max_length is not None:
        prop["maxLength"] = field.max_length
    if field.choices:
        prop["enum"] = list(field.choices)
    if field.pattern:
        prop["pattern"] = field.pattern
    if field.default is not None:
        prop["default"] = field.default
    if field.nullable:
        prop["nullable"] = True
    if field.field_type == list and field.each:
        if isinstance(field.each, Field):
            prop["items"] = _field_to_openapi(field.each)
        elif isinstance(field.each, Schema):
            prop["items"] = field.each.to_openapi()
    if field.field_type == dict and field.schema:
        prop.update(field.schema.to_openapi())
    return prop


def validate(schema):
    def decorator(fn):
        @wraps(fn)
        def wrapper(req, res, *args, **kwargs):
            data = req.json
            if data is None:
                res.json({"error": "Invalid JSON body", "details": ["Could not parse request body as JSON"]}, 400)
                return res
            cleaned, errors = schema.validate(data)
            if errors:
                res.json({"error": "Validation failed", "details": errors}, 422)
                return res
            req.validated = cleaned
            return fn(req, res, *args, **kwargs)
        return wrapper
    return decorator


def validate_query(schema):
    def decorator(fn):
        @wraps(fn)
        def wrapper(req, res, *args, **kwargs):
            data = {}
            for name in schema.fields:
                val = req.get_query(name)
                if val is not None:
                    data[name] = val
            cleaned, errors = schema.validate(data)
            if errors:
                res.json({"error": "Query validation failed", "details": errors}, 422)
                return res
            req.validated_query = cleaned
            return fn(req, res, *args, **kwargs)
        return wrapper
    return decorator
