"""Read bounded installed type stubs without importing target packages."""

from __future__ import annotations

import ast
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal


@dataclass(frozen=True)
class LocalStubExcerpt:
    package: str
    ecosystem: str
    version: str
    relative_path: str
    content: str
    content_bytes: int
    source: Literal["local_stub"] = "local_stub"
    version_binding: Literal["local_installed_artifact"] = "local_installed_artifact"


def _read_confined(root_fd: int, parts: list[str]) -> bytes | None:
    current = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            nxt = os.open(
                part,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=current,
            )
            os.close(current)
            current = nxt
        fd = os.open(
            parts[-1], os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=current
        )
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
                return None
            data = os.read(fd, 65537)
            return data if len(data) <= 65536 else None
        finally:
            os.close(fd)
    except (OSError, ValueError):
        return None
    finally:
        os.close(current)


def _walk(
    root_fd: int,
    prefix: list[str],
    suffixes: tuple[str, ...],
    limit: int = 8,
    *,
    symbols: set[str] | None = None,
) -> list[list[str]]:
    found: list[list[str]] = []
    symbol_tokens = {
        tok
        for sym in (symbols or set())
        for tok in re.findall(r"[a-z0-9]+", sym.lower())
        if len(tok) >= 3
    }
    queue: list[tuple[int, list[str]]] = [(0, prefix)]
    visited_dirs = 0
    while queue and visited_dirs < 32:
        depth, parts = queue.pop(0)
        visited_dirs += 1
        current = os.dup(root_fd)
        try:
            for part in parts:
                nxt = os.open(
                    part,
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=current,
                )
                os.close(current)
                current = nxt
            names = os.listdir(current)
        except OSError:
            continue
        finally:
            os.close(current)
        for name in sorted(names):
            if name.endswith(suffixes):
                found.append(parts + [name])
            elif (
                depth < 2
                and "." not in name
                and not name.startswith("_")
                and name not in {"tests", "test", "__pycache__", "node_modules"}
            ):
                queue.append((depth + 1, parts + [name]))

    def _rank(path: list[str]) -> tuple[int, int, int, int, str]:
        filename = path[-1]
        stem = filename.rsplit(".", 1)[0].lower()
        is_private = 1 if stem.startswith("_") and stem != "__init__" else 0
        is_symbol_hit = (
            0 if (symbol_tokens and any(tok in stem for tok in symbol_tokens)) else 1
        )
        is_init = 0 if stem == "__init__" and not symbol_tokens else 1
        is_pyi = 0 if filename.endswith((".pyi", ".d.ts")) else 1
        return (is_private, is_symbol_hit, is_init, is_pyi, "/".join(path))

    found.sort(key=_rank)
    return found[:limit]


def _doc(node: ast.AST) -> str:
    doc = ast.get_docstring(node, clean=True)
    return "\n".join(doc.splitlines()[:3]) if doc else ""


def _python_excerpt(data: bytes, symbols: set[str], include_public: bool) -> str:
    try:
        tree = ast.parse(data.decode("utf-8"))
    except (UnicodeDecodeError, SyntaxError):
        return ""
    output = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if node.name.startswith("_") and not include_public:
            continue
        if symbols and node.name not in symbols:
            continue
        header = ast.unparse(node)
        # Keep only declaration header; class methods are handled separately.
        if isinstance(node, ast.ClassDef):
            bases = (
                f"({', '.join(ast.unparse(x) for x in node.bases)})"
                if node.bases
                else ""
            )
            header = f"class {node.name}{bases}:"
            output.append(header)
            if _doc(node):
                output.append('    """' + _doc(node) + '"""')
            for child in node.body:
                if isinstance(
                    child, (ast.FunctionDef, ast.AsyncFunctionDef)
                ) and not child.name.startswith("_"):
                    output.append("    " + ast.unparse(child).splitlines()[0])
                    if _doc(child):
                        output.append("    " + repr(_doc(child)))
        else:
            output.append(header.splitlines()[0])
            if _doc(node):
                output.append('"""' + _doc(node) + '"""')
    return "\n".join(output)


def extract_local_stubs(
    project_root: Path,
    package: str,
    ecosystem: str,
    version: str,
    *,
    symbols: Iterable[str] = (),
    max_bytes: int = 4096,
) -> LocalStubExcerpt | None:
    if (
        not isinstance(package, str)
        or not package
        or any(x in package for x in ("..", "\\", "\x00"))
        or max_bytes < 1
    ):
        return None
    if ecosystem not in ("python", "javascript", "typescript"):
        return None
    if ecosystem == "python" and "/" in package:
        return None
    try:
        root_fd = os.open(
            Path(project_root),
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError:
        return None
    requested = set(symbols)
    try:
        candidates: list[tuple[list[str], bytes]] = []
        if ecosystem == "python":
            import_name = package.replace("-", "_")
            roots = []
            for env in (".venv", "venv"):
                try:
                    env_fd = os.open(
                        env,
                        os.O_RDONLY
                        | getattr(os, "O_DIRECTORY", 0)
                        | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=root_fd,
                    )
                    lib_fd = os.open(
                        "lib",
                        os.O_RDONLY
                        | getattr(os, "O_DIRECTORY", 0)
                        | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=env_fd,
                    )
                    versions = sorted(
                        x for x in os.listdir(lib_fd) if x.startswith("python")
                    )
                    os.close(lib_fd)
                    os.close(env_fd)
                except OSError:
                    continue
                roots.extend(
                    [
                        [env, "lib", py, "site-packages", directory]
                        for py in versions
                        for directory in (import_name + "-stubs", import_name)
                    ]
                )
            for base in roots:
                if len(candidates) >= 8:
                    break
                files = _walk(
                    root_fd,
                    base,
                    (".pyi", ".py"),
                    8 - len(candidates),
                    symbols=requested,
                )
                candidates.extend(
                    (p, data)
                    for p in files
                    if (data := _read_confined(root_fd, p)) is not None
                )
        else:
            package_parts = package.split("/")
            if (
                len(package_parts) > 2
                or (len(package_parts) == 2 and not package_parts[0].startswith("@"))
                or any(
                    not re.fullmatch(r"[A-Za-z0-9_.@-]+", p) or p in (".", "..")
                    for p in package_parts
                )
            ):
                return None
            base = ["node_modules", *package_parts]
            for path in _walk(root_fd, base, (".d.ts",), 8, symbols=requested):
                data = _read_confined(root_fd, path)
                if data is not None:
                    candidates.append((path, data))
        excerpts: list[tuple[str, str]] = []
        for path, data in candidates[:8]:
            relative = "/".join(path)
            if ecosystem == "python":
                excerpt = _python_excerpt(
                    data,
                    requested,
                    include_public=not requested
                    and path[-1] in ("__init__.py", "__init__.pyi"),
                )
            else:
                text = data.decode("utf-8", errors="ignore")
                declarations = re.finditer(
                    r"(?m)^\s*export\s+(?:declare\s+)?(?:interface|type|function|class|const|enum)\s+(\w+)[^;{]*(?:\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\})?;?",
                    text,
                )
                excerpt = "\n".join(
                    m.group(0).strip()
                    for m in declarations
                    if not requested or m.group(1) in requested
                )
            if excerpt:
                excerpts.append((relative, excerpt))
        if not excerpts:
            return None
        first_relative = excerpts[0][0]
        header = (
            f"UNTRUSTED LOCAL STUB DATA\n"
            f"Exact installed version: {version}\n"
            f"Path: {first_relative}\n"
        )
        body = "\n\n".join(
            excerpt if idx == 0 else f"# {rel}\n{excerpt}"
            for idx, (rel, excerpt) in enumerate(excerpts)
        )
        content = (
            (header + body).encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")
        )
        if len(content.encode("utf-8")) <= len(header.encode("utf-8")):
            return None
        return LocalStubExcerpt(
            package,
            ecosystem,
            version,
            first_relative,
            content,
            len(content.encode("utf-8")),
        )
    finally:
        os.close(root_fd)
