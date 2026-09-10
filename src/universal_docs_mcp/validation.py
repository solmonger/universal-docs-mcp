"""Strict tool inputs. Never include rejected values in public errors."""
import re
from typing import Optional

from packaging.version import Version
from pydantic import BaseModel, ConfigDict, Field, model_validator

ALIASES = {
    'python': 'python', 'pypi': 'python', 'pip': 'python',
    'javascript': 'javascript', 'typescript': 'javascript', 'npm': 'javascript',
    'js': 'javascript', 'ts': 'javascript',
    'rust': 'rust', 'cargo': 'rust', 'crate': 'rust',
}
PACKAGE_PATTERN = r'^(?:@[a-zA-Z0-9][a-zA-Z0-9._-]*/)?[a-zA-Z0-9][a-zA-Z0-9._-]*$'
VERSION_PATTERN = r'^[0-9][a-zA-Z0-9.!+_-]*$'
SEMVER_PATTERN = r'(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?'


class EmptyArgs(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid')


class InfoArgs(EmptyArgs):
    package: str = Field(min_length=1, max_length=214, pattern=PACKAGE_PATTERN)
    ecosystem: Optional[str] = Field(default=None, pattern='^(?:' + '|'.join(sorted(ALIASES)) + ')$')
    force_refresh: bool = False

    @model_validator(mode='after')
    def package_for_ecosystem(self):
        if self.ecosystem:
            ecosystem = ALIASES[self.ecosystem]
            if ecosystem != 'javascript' and ('/' in self.package or '@' in self.package):
                raise ValueError('invalid_package')
        return self


class OutlineArgs(InfoArgs):
    version: Optional[str] = Field(default=None, min_length=1, max_length=128, pattern=VERSION_PATTERN)

    @model_validator(mode='after')
    def exact_version(self):
        if self.version is not None:
            if self.ecosystem and ALIASES[self.ecosystem] != 'python':
                if not re.fullmatch(SEMVER_PATTERN, self.version):
                    raise ValueError('invalid_version')
            else:
                Version(self.version)
        return self


class OutlinePageArgs(OutlineArgs):
    offset: int = Field(default=0, ge=0, le=10000)
    limit: int = Field(default=100, ge=1, le=100)


class DocsArgs(OutlineArgs):
    section: Optional[str] = Field(default=None, min_length=1, max_length=256)
    max_tokens: int = Field(default=1500, ge=200, le=6000)


class ManifestArgs(EmptyArgs):
    manifest_path: str = Field(min_length=1, max_length=4096)


TOOL_ARGS = {
    'get_package_info': InfoArgs,
    'get_package_docs': DocsArgs,
    'get_docs_outline': OutlinePageArgs,
    'get_project_dependencies': ManifestArgs,
    'cache_stats': EmptyArgs,
}
