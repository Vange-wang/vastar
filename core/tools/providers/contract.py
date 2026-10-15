"""A deliberately bounded schema dialect; remote annotations never grant capability."""
import hashlib
import json
import re


class ProviderError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def require(value, code):
    if not value:
        raise ProviderError(code)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def identifier(value):
    require(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', value), 'provider_invalid_name')
    return value


def schema(value, depth=0):
    require(isinstance(value, dict) and depth < 5, 'provider_schema_invalid')
    require(not set(value) - {'type', 'properties', 'required', 'additionalProperties', 'items',
                              'enum', 'description', 'title'}, 'provider_schema_unsupported')
    kind = value.get('type')
    require(kind in {'object', 'array', 'string', 'integer', 'number', 'boolean', 'null'}, 'provider_schema_invalid')
    out = {'type': kind}
    if kind == 'object':
        props = value.get('properties', {})
        required = value.get('required', [])
        require(isinstance(props, dict) and len(props) <= 32, 'provider_schema_invalid')
        require(isinstance(required, list) and all(isinstance(k, str) and k in props for k in required),
                'provider_schema_invalid')
        require(value.get('additionalProperties', False) is False, 'provider_schema_unsupported')
        out.update(properties={identifier(k): schema(v, depth + 1) for k, v in props.items()},
                   required=required, additionalProperties=False)
    elif kind == 'array':
        out['items'] = schema(value.get('items'), depth + 1)
    if 'enum' in value:
        require(isinstance(value['enum'], list) and 0 < len(value['enum']) <= 32, 'provider_schema_invalid')
        require(all(v is None or type(v) in (str, int, bool, float) for v in value['enum']), 'provider_schema_invalid')
        out['enum'] = value['enum']
    require(len(json.dumps(out, allow_nan=False).encode()) <= 16384, 'provider_schema_too_large')
    return out


def arguments(value, spec, depth=0):
    require(depth < 6, 'provider_arguments_invalid')
    kinds = {'object': dict, 'array': list, 'string': str, 'integer': int, 'number': (int, float),
             'boolean': bool, 'null': type(None)}
    kind = spec['type']
    require(isinstance(value, kinds[kind]) and not (kind in {'integer', 'number'} and type(value) is bool),
            'provider_arguments_invalid')
    if 'enum' in spec:
        require(value in spec['enum'], 'provider_arguments_invalid')
    if kind == 'object':
        require(not set(value) - set(spec['properties']) and set(spec['required']) <= set(value), 'provider_arguments_invalid')
        for k, v in value.items():
            arguments(v, spec['properties'][k], depth + 1)
    elif kind == 'array':
        require(len(value) <= 64, 'provider_arguments_invalid')
        for v in value:
            arguments(v, spec['items'], depth + 1)
    require(len(json.dumps(value, allow_nan=False).encode()) <= 16384, 'provider_arguments_too_large')
