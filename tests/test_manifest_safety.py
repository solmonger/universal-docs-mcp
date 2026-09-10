"""Synthetic-only privacy and dependency interpretation regressions."""
import json

import pytest

from universal_docs_mcp.lockfile import (
    parse_package_json, parse_pyproject, parse_cargo_toml,
    parse_cargo_lock, parse_requirements, read_pins,
)
from universal_docs_mcp import server


@pytest.mark.parametrize("spec", [
    "git+https://build-user:SYNTHETIC_PASSWORD@github.com/acme/demo.git",
    "https://example.test/demo.tgz?token=SYNTHETIC_QUERY#SYNTHETIC_FRAGMENT",
    "git@github.com:acme/SYNTHETIC_PATH.git",
    "file:../SYNTHETIC_LOCAL_PATH",
    "https%3A%2F%2FSYNTHETIC_ENCODED%40example.test",
])
def test_non_registry_specs_are_redacted_before_serialization(spec):
    pin = parse_package_json(json.dumps({"dependencies": {"demo": spec}}), "package.json")[0]
    assert "SYNTHETIC" not in repr(pin)
    assert "SYNTHETIC" not in json.dumps(pin.to_dict())
    assert pin.spec == "[redacted]"
    assert pin.pinned is None
    assert pin.to_dict()["spec_redacted"] is True
    assert pin.to_dict()["registry_lookup"] is False


def test_version_ranges_remain_useful():
    pins = parse_package_json('{"dependencies":{"demo":"^1.2.3","exact":"1.2.3"}}', 'package.json')
    assert pins[0].spec == '^1.2.3'
    assert pins[1].pinned == '1.2.3'


@pytest.mark.parametrize("parser,text", [
    (parse_package_json, '{bad SYNTHETIC_JSON'),
    (parse_package_json, '[]'),
    (parse_package_json, '{"dependencies":[]}'),
    (parse_package_json, '{"dependencies":{"demo":{"token":"SYNTHETIC_NESTED"}}}'),
    (parse_pyproject, '[project\ndependencies=SYNTHETIC_TOML'),
    (parse_pyproject, '[project]\ndependencies="SYNTHETIC_STRING"'),
    (parse_cargo_toml, '[dependencies\ndemo=SYNTHETIC_TOML'),
    (parse_cargo_lock, 'package="SYNTHETIC_STRING"'),
    (parse_requirements, 'demo=SYNTHETIC_INVALID'),
    (parse_requirements, '-r SYNTHETIC_PRIVATE.txt'),
])
def test_invalid_manifests_are_errors_not_empty_success(parser, text):
    with pytest.raises(ValueError) as exc:
        parser(text, 'manifest')
    assert 'SYNTHETIC' not in str(exc.value)


@pytest.mark.asyncio
async def test_manifest_failure_does_not_echo_private_path(tmp_path):
    path = tmp_path / 'SYNTHETIC_PRIVATE' / 'package.json'
    result = await server._handle_project_dependencies({'manifest_path': str(path)})
    payload = json.loads(result[0].text)
    assert payload['found'] is False
    assert payload['error'] == 'manifest_error'
    assert 'SYNTHETIC' not in result[0].text


@pytest.mark.parametrize("spec,expected", [("1.2.3", None), ("=1.2", None), ("=1.2.3", "1.2.3"), ("^1.2.3", None)])
def test_cargo_ranges_are_not_exact_pins(spec, expected):
    assert parse_cargo_toml('[dependencies]\ndemo="' + spec + '"', 'Cargo.toml')[0].pinned == expected


def test_python_extras_do_not_hide_pin():
    pin = parse_requirements('requests[socks]==2.32.3; python_version >= "3.10"', 'requirements.txt')[0]
    assert pin.pinned == '2.32.3'
    assert pin.to_dict()['marker'] == 'python_version >= "3.10"'
    assert pin.to_dict()['extras'] == ['socks']


def test_direct_reference_cannot_be_used_as_registry_pin():
    pin = parse_requirements('demo @ https://user:SYNTHETIC_PASSWORD@example.test/demo.whl', 'requirements.txt')[0]
    assert pin.spec == '[redacted]'
    assert pin.to_dict()['registry_lookup'] is False


def test_cargo_source_and_rename_are_not_lost():
    pins = parse_cargo_toml('[dependencies]\nalias={package="real",version="=1.2.3"}\nprivate={git="https://SYNTHETIC_TOKEN@example.test/repo",version="=1.2.3"}', 'Cargo.toml')
    assert pins[0].name == 'real'
    assert pins[1].pinned is None
    assert pins[1].to_dict()['registry_lookup'] is False


def test_cargo_lock_git_source_is_not_registry_version():
    pin = parse_cargo_lock('[[package]]\nname="demo"\nversion="1.2.3"\nsource="git+https://SYNTHETIC_TOKEN@example.test/demo"', 'Cargo.lock')[0]
    assert pin.pinned is None
    assert pin.to_dict()['registry_lookup'] is False


@pytest.mark.parametrize('spec', ['1', '1.2'])
def test_npm_partial_versions_are_not_pins(spec):
    assert parse_package_json(json.dumps({'dependencies': {'demo': spec}}), 'package.json')[0].pinned is None


def test_manifest_read_is_bounded_and_source_does_not_echo_parent(tmp_path):
    path = tmp_path / 'package.json'
    path.write_text('{"dependencies":{"demo":"1.2.3"}}')
    assert read_pins(path)[0].source == 'package.json'
    path.write_bytes(b' ' * (1024 * 1024 + 1))
    with pytest.raises(ValueError, match='manifest_too_large'):
        read_pins(path)


