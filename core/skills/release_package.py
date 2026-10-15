"""Versioned data-only bundles, generalized before any durable candidate write."""
import hashlib
import json
import re
from pathlib import Path, PurePosixPath

from core.skills.frontmatter import parse_frontmatter
from core.skills.registry import REQUIRED_FIELDS, SkillMeta, _validate
from memory import policy
from memory.repositories.tasks import encode


def require(ok, reason):
    if not ok:
        raise ValueError(reason)


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def generalize(value, private_terms=()):
    require(isinstance(private_terms, (list, tuple)) and len(private_terms) <= 64
            and all(isinstance(x, str) and x for x in private_terms), 'private_terms_invalid')
    def scrub(text):
        require(isinstance(text, str), 'bundle_text_required')
        for term in sorted(private_terms, key=len, reverse=True):
            text = text.replace(term, '[private]')
        text = re.sub(r'(?i)\b[A-Z]:[\\/][^\s<>"\x27]+|\\\\[^\s<>"\x27]+|(?<![\w:])/[\w.-]+(?:/[^\s<>"\x27]+)?', '[local-path]', text)
        text = re.sub(r'(?i)\b[\w.+-]+@[\w.-]+\.[a-z]{2,}\b|(?<!\d)1[3-9]\d{9}(?!\d)', '[personal]', text)
        text = re.sub(r'(?:姓名|身份证)[：:]\s*[^\s,，;；]+', '[personal]', text)
        require(not policy.sensitive(text), 'candidate_credentials_refused')
        return text
    def visit(item):
        if isinstance(item, str):
            return scrub(item)
        if isinstance(item, list):
            return [visit(x) for x in item]
        require(isinstance(item, dict), 'bundle_data_only')
        return {scrub(k): visit(v) for k, v in item.items()}
    return visit(value)


def validate(bundle, registry):
    require(isinstance(bundle, dict) and set(bundle) == {'skill_md', 'resources', 'examples', 'dependencies'}, 'bundle_fields')
    require(len(encode(bundle).encode()) <= 32768, 'bundle_too_large')
    fields, _ = parse_frontmatter(bundle['skill_md'])
    require(not set(fields) - set(REQUIRED_FIELDS) - {'needs_memory'}, 'frontmatter_extension_refused')
    meta = _validate(Path('__data_only_release__'), bundle['skill_md'], registry)
    require(isinstance(meta, SkillMeta), str(meta))
    require(re.fullmatch(r'\d+\.\d+\.\d+', meta.version), 'semantic_version_required')
    resources = bundle['resources']
    require(isinstance(resources, dict) and len(resources) <= 8, 'resources_invalid')
    for name, content in resources.items():
        path = PurePosixPath(name)
        require(not path.is_absolute() and len(path.parts) == 2 and path.parts[0] in {'references', 'assets'}
                and path.suffix in {'.md', '.txt', '.json'} and '\\' not in name and ':' not in name
                and isinstance(content, str), 'scripts_or_resource_path_refused')
    examples = bundle['examples']
    require(isinstance(examples, list) and 1 <= len(examples) <= 8, 'examples_required')
    for example in examples:
        require(isinstance(example, dict) and set(example) == {'input', 'success', 'failure', 'disabled'}
                and all(isinstance(v, str) and v.strip() for v in example.values()), 'example_four_outcomes_required')
    deps = bundle['dependencies']
    require(isinstance(deps, dict) and len(deps) <= 10 and meta.name not in deps, 'dependencies_invalid')
    require(all(re.fullmatch(r'[a-z0-9][a-z0-9-]*', k) and re.fullmatch(r'[0-9a-f]{64}', v)
                for k, v in deps.items()), 'dependency_exact_hash_required')
    return meta


def load(artifacts, ref):
    from core.harness.validators import Validator
    return json.loads(Validator(artifacts._db, artifacts)._artifact(ref))


def materialize(bundle, registry, ref):
    meta = validate(bundle, registry)
    meta.sha256, meta.source = digest(bundle), ref
    meta.body += ''.join('\n\nResource ' + k + ':\n' + v for k, v in sorted(bundle['resources'].items()))
    return meta
