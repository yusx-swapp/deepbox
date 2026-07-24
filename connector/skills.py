"""Connector-local Agent Skills management.

A *skill* is a standard Agent Skills package: a directory whose root contains a
``SKILL.md`` file with YAML frontmatter declaring at least ``name`` and
``description``.  This module validates a source package, copies it into a
per-user content-addressed store (the source of truth), and binds it into one
or more runtime destination directories as ``<binding-root>/<skill-name>``.

Design constraints honoured here:

* **No script execution, ever.**  Packages are treated as inert data.
* **Safe YAML parsing** -- ``yaml.safe_load`` accepts standard Agent Skills
  frontmatter, including optional nested metadata, without constructing
  arbitrary Python objects.
* **No import of runtimes** -- callers resolve runtime adapters to concrete
  binding roots and pass them in as :class:`SkillBinding` values.  The final
  directory is always ``<binding-root>/<skill-name>``.
* **Server-safe metadata never contains local paths** -- see
  :meth:`LocalSkill.public_json` and :meth:`SkillManager.inventory`.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping

import yaml

from connector.local_store import LocalProject, LocalProjectStore, LocalSkill

# ---------------------------------------------------------------------------
# Limits and validation constants
# ---------------------------------------------------------------------------

MAX_FILES = 256
MAX_TOTAL_BYTES = 10 * 1024 * 1024  # 10 MiB
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
NAME_MAX = 64
DESCRIPTION_MAX = 1024
SKILL_FILE = "SKILL.md"
# Files whose presence flags the package as containing executable scripts.
_SCRIPT_SUFFIXES = {
    ".sh",
    ".bash",
    ".zsh",
    ".ps1",
    ".psm1",
    ".bat",
    ".cmd",
    ".py",
    ".pyw",
    ".rb",
    ".pl",
    ".js",
    ".mjs",
    ".cjs",
    ".ts",
    ".php",
    ".exe",
    ".com",
    ".scr",
}


class SkillError(Exception):
    """Base error for all skill operations."""


class SkillValidationError(SkillError):
    """The source package is malformed or violates a safety limit."""


class SkillCollisionError(SkillError):
    """A destination is occupied by content this manager does not own."""


class SkillDriftError(SkillError):
    """An installed binding was modified or removed out-of-band."""


# ---------------------------------------------------------------------------
# YAML frontmatter parsing (safe_load; no arbitrary object construction)
# ---------------------------------------------------------------------------


def _split_frontmatter(text: str) -> str:
    """Return the YAML frontmatter block from a ``SKILL.md`` body."""
    # Normalise newlines so CRLF sources parse identically.
    normalised = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalised.split("\n")
    # Skip a leading UTF-8 BOM / blank lines.
    idx = 0
    while idx < len(lines) and lines[idx].strip() == "":
        idx += 1
    if idx >= len(lines) or lines[idx].strip() != "---":
        raise SkillValidationError("SKILL.md must begin with a '---' frontmatter block")
    idx += 1
    block: list[str] = []
    for line in lines[idx:]:
        if line.strip() == "---":
            return "\n".join(block)
        block.append(line)
    raise SkillValidationError("SKILL.md frontmatter is not terminated by '---'")


def parse_frontmatter(text: str) -> dict:
    """Safely parse the YAML mapping at the root of ``SKILL.md``."""
    block = _split_frontmatter(text)
    try:
        fields = yaml.safe_load(block)
    except yaml.YAMLError as exc:
        raise SkillValidationError(f"SKILL.md frontmatter is invalid YAML: {exc}") from exc
    if not isinstance(fields, dict):
        raise SkillValidationError("SKILL.md frontmatter must be a YAML mapping")
    if not all(isinstance(key, str) for key in fields):
        raise SkillValidationError("SKILL.md frontmatter keys must be strings")
    return fields


# ---------------------------------------------------------------------------
# Metadata + safe tree walk
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SkillMetadata:
    name: str
    description: str
    digest: str
    contains_scripts: bool
    file_count: int
    total_bytes: int


@dataclass(frozen=True)
class SkillBinding:
    """A resolved destination for one runtime family.

    ``root`` is the runtime's skills directory; the manager installs into
    ``root/<skill-name>``.  ``family`` labels the target runtime and is what the
    public ``targets`` list reports.
    """

    family: str
    root: str | tuple[str, ...]

    def roots(self) -> tuple[str, ...]:
        values = (self.root,) if isinstance(self.root, str) else self.root
        return tuple(os.path.abspath(value) for value in values)

    def destinations(self, skill_name: str) -> tuple[str, ...]:
        return tuple(os.path.join(root, skill_name) for root in self.roots())

    def destination(self, skill_name: str) -> str:
        """Compatibility helper for adapters with one skills root."""
        destinations = self.destinations(skill_name)
        if len(destinations) != 1:
            raise SkillValidationError(
                f"runtime family {self.family!r} has multiple skills roots")
        return destinations[0]


def _validate_name(name: object) -> str:
    if not name:
        raise SkillValidationError("SKILL.md frontmatter is missing 'name'")
    if not isinstance(name, str):
        raise SkillValidationError("skill name must be a string")
    if len(name) > NAME_MAX or not NAME_RE.match(name):
        raise SkillValidationError(
            "skill name must be 1..64 chars of lowercase alphanumerics and "
            f"single hyphens: {name!r}"
        )
    return name


def _validate_description(description: object) -> str:
    if not description:
        raise SkillValidationError("SKILL.md frontmatter is missing 'description'")
    if not isinstance(description, str):
        raise SkillValidationError("skill description must be a string")
    if len(description) > DESCRIPTION_MAX:
        raise SkillValidationError("skill description must be at most 1024 characters")
    return description


def _is_reparse_or_link(path: Path) -> bool:
    if path.is_symlink():
        return True
    if os.name == "nt":
        try:
            attrs = os.stat(path, follow_symlinks=False).st_file_attributes  # type: ignore[attr-defined]
        except (OSError, AttributeError):
            return False
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        return bool(attrs & reparse)
    return False


def _iter_safe_files(root: Path) -> list[Path]:
    """Walk ``root`` returning regular files, rejecting anything unsafe.

    Rejects symlinks/junctions/reparse points, special files (fifo, device,
    socket) and any entry escaping ``root``.
    """
    root = root.resolve(strict=True)
    files: list[Path] = []
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = sorted(current.iterdir(), key=lambda p: p.name)
        except OSError as exc:  # pragma: no cover - defensive
            raise SkillValidationError(f"cannot read directory: {exc}") from exc
        for entry in entries:
            if _is_reparse_or_link(entry):
                raise SkillValidationError(
                    f"symlink/reparse points are not allowed in skill packages: {entry.name}"
                )
            st = entry.lstat()
            mode = st.st_mode
            if stat.S_ISDIR(mode):
                # Guard against traversal escaping the root.
                resolved = entry.resolve()
                if root not in resolved.parents and resolved != root:
                    raise SkillValidationError(f"path escapes package root: {entry}")
                stack.append(entry)
            elif stat.S_ISREG(mode):
                files.append(entry)
                if len(files) > MAX_FILES:
                    raise SkillValidationError(
                        f"skill package exceeds {MAX_FILES} files"
                    )
            else:
                raise SkillValidationError(
                    f"special files are not allowed in skill packages: {entry.name}"
                )
    return files


def _digest_files(root: Path, files: Iterable[Path]) -> tuple[str, int]:
    """Hash a validated tree without reading beyond the package byte limit."""

    hasher = hashlib.sha256()
    total = 0
    for path in sorted(files, key=lambda item: item.relative_to(root).as_posix()):
        if _is_reparse_or_link(path):
            raise SkillValidationError(
                f"symlink/reparse points are not allowed in skill packages: {path.name}"
            )
        try:
            expected_size = path.stat(follow_symlinks=False).st_size
        except OSError as exc:
            raise SkillValidationError(f"cannot inspect skill file: {path}") from exc
        if expected_size < 0 or total + expected_size > MAX_TOTAL_BYTES:
            raise SkillValidationError(
                f"skill package exceeds {MAX_TOTAL_BYTES} bytes"
            )

        relative = path.relative_to(root).as_posix()
        hasher.update(relative.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(expected_size.to_bytes(8, "big"))

        bytes_read = 0
        try:
            with path.open("rb") as handle:
                while bytes_read <= expected_size:
                    chunk = handle.read(min(1024 * 1024, expected_size - bytes_read + 1))
                    if not chunk:
                        break
                    bytes_read += len(chunk)
                    if bytes_read > expected_size:
                        break
                    hasher.update(chunk)
        except OSError as exc:
            raise SkillValidationError(f"cannot read skill file: {path}") from exc
        if bytes_read != expected_size or _is_reparse_or_link(path):
            raise SkillValidationError(f"skill changed while it was being read: {path}")
        total += bytes_read
    return hasher.hexdigest(), total


def inspect_source(source: str | os.PathLike, *, expected_name: str | None = None) -> SkillMetadata:
    """Validate and hash a source skill package directory.

    ``expected_name`` (defaults to the source directory basename) must equal the
    declared ``name`` in ``SKILL.md``.
    """
    root = Path(source)
    if not root.is_dir():
        raise SkillValidationError(f"skill source is not a directory: {source}")
    if _is_reparse_or_link(root):
        raise SkillValidationError("skill source root must not be a symlink/reparse point")
    root = root.resolve(strict=True)
    skill_md = root / SKILL_FILE
    if not skill_md.is_file():
        raise SkillValidationError(f"skill source is missing {SKILL_FILE}")

    files = _iter_safe_files(root)
    if not any(f.name == SKILL_FILE and f.parent == root for f in files):
        raise SkillValidationError(f"{SKILL_FILE} must be at the package root")
    digest, total = _digest_files(root, files)

    try:
        with skill_md.open("rb") as handle:
            raw_skill_md = handle.read(MAX_TOTAL_BYTES + 1)
    except OSError as exc:
        raise SkillValidationError(f"cannot read {SKILL_FILE}") from exc
    if len(raw_skill_md) > MAX_TOTAL_BYTES:
        raise SkillValidationError(f"skill package exceeds {MAX_TOTAL_BYTES} bytes")
    try:
        text = raw_skill_md.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise SkillValidationError(f"{SKILL_FILE} must be UTF-8") from exc
    fields = parse_frontmatter(text)
    raw_name = fields.get("name", "")
    raw_description = fields.get("description", "")
    name = _validate_name(raw_name.strip() if isinstance(raw_name, str) else raw_name)
    description = _validate_description(
        raw_description.strip() if isinstance(raw_description, str) else raw_description)

    want = (expected_name or root.name).strip()
    if want and want != name:
        raise SkillValidationError(
            f"source directory name {want!r} must match declared skill name {name!r}"
        )
    verified_digest, verified_total = _digest_files(root, files)
    if verified_digest != digest or verified_total != total:
        raise SkillValidationError("skill changed while it was being inspected")

    contains_scripts = any(
        f.suffix.lower() in _SCRIPT_SUFFIXES
        or f.relative_to(root).as_posix().startswith("scripts/")
        for f in files
    )

    return SkillMetadata(
        name=name,
        description=description,
        digest=digest,
        contains_scripts=contains_scripts,
        file_count=len(files),
        total_bytes=total,
    )


# ---------------------------------------------------------------------------
# Copy helpers
# ---------------------------------------------------------------------------


def _stage_tree(src: Path, parent: Path) -> Path:
    """Copy a validated tree to a temporary sibling directory."""
    parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=".skill-tmp-", dir=parent))
    try:
        base = src.resolve(strict=True)
        for item in _iter_safe_files(base):
            rel = item.relative_to(base)
            target = tmp / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(item, target)
        return tmp
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def _copy_tree_atomic(src: Path, dest: Path) -> None:
    """Copy ``src`` into a new ``dest`` via a sibling stage + rename."""
    dest = Path(dest)
    tmp = _stage_tree(src, dest.parent)
    try:
        os.replace(tmp, dest)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def _remove_path(path: Path) -> None:
    """Remove a managed tree, surfacing failures before metadata is changed."""
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.exists():
        shutil.rmtree(path)


def _replace_trees_atomic(src: Path, destinations: Iterable[Path]) -> list[str]:
    """Replace all destination trees as one rollback-capable operation."""
    unique: list[Path] = []
    seen: set[str] = set()
    for value in destinations:
        dest = Path(value)
        key = os.path.normcase(os.path.abspath(dest))
        if key not in seen:
            seen.add(key)
            unique.append(dest)

    staged: list[tuple[Path, Path]] = []
    replacements: list[tuple[Path, Path | None]] = []
    try:
        for dest in unique:
            staged.append((dest, _stage_tree(src, dest.parent)))
        for dest, tmp in staged:
            backup: Path | None = None
            if dest.exists():
                backup = Path(tempfile.mkdtemp(prefix=".skill-bak-", dir=dest.parent))
                backup.rmdir()
                os.replace(dest, backup)
            try:
                os.replace(tmp, dest)
            except Exception:
                if backup is not None and backup.exists():
                    os.replace(backup, dest)
                raise
            replacements.append((dest, backup))
    except Exception:
        for dest, backup in reversed(replacements):
            _remove_path(dest)
            if backup is not None and backup.exists():
                os.replace(backup, dest)
        raise
    finally:
        for _, tmp in staged:
            shutil.rmtree(tmp, ignore_errors=True)

    for _, backup in replacements:
        if backup is not None:
            _remove_path(backup)
    return [str(dest) for dest, _ in replacements]


def _tree_digest(root: Path) -> str | None:
    """Recompute the bounded deterministic digest of an installed tree."""
    root = Path(root)
    if not root.is_dir() or _is_reparse_or_link(root):
        return None
    try:
        base = root.resolve(strict=True)
        files = _iter_safe_files(base)
        digest, _ = _digest_files(base, files)
    except (SkillValidationError, FileNotFoundError, OSError, ValueError):
        return None
    return digest


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------

BindingResolver = Callable[[str], SkillBinding]


@dataclass(frozen=True)
class SkillInstallResult:
    skill: LocalSkill
    changed: bool
    idempotent: bool


class SkillManager:
    """Install, list, inspect and remove connector-local Agent Skills.

    The store of truth lives beside the state DB in
    ``skills/store/<digest>/<name>/``.  Bindings are copies of that store tree
    at ``<binding-root>/<skill-name>``.
    """

    def __init__(self, store: LocalProjectStore, *, state_root: str | None = None):
        self._store = store
        base = state_root or os.path.dirname(store.path)
        self._store_root = Path(base) / "skills" / "store"

    # -- helpers --------------------------------------------------------

    def _scope_for(self, project: LocalProject | None) -> tuple[str, str | None]:
        if project is None:
            return "personal", None
        return "project", project.id

    def _store_dir(self, digest: str, name: str) -> Path:
        return self._store_root / digest / name

    @staticmethod
    def _resolve_bindings(
        targets: Iterable[str],
        bindings: Mapping[str, SkillBinding] | Iterable[SkillBinding] | None,
        resolver: BindingResolver | None,
    ) -> dict[str, SkillBinding]:
        resolved: dict[str, SkillBinding] = {}
        supplied: dict[str, SkillBinding] = {}
        if isinstance(bindings, Mapping):
            supplied = {str(k): v for k, v in bindings.items()}
        elif bindings is not None:
            supplied = {b.family: b for b in bindings}
        for family in targets:
            family = str(family)
            if family in supplied:
                resolved[family] = supplied[family]
            elif resolver is not None:
                resolved[family] = resolver(family)
            else:
                raise SkillValidationError(
                    f"no binding provided for target family {family!r}"
                )
        # Bindings supplied without an explicit target list are also honoured.
        for family, binding in supplied.items():
            resolved.setdefault(family, binding)
        if not resolved:
            raise SkillValidationError("at least one target/binding is required")
        return resolved

    @staticmethod
    def _destination_map(
        name: str, bindings: Mapping[str, SkillBinding]
    ) -> dict[str, str]:
        result: dict[str, str] = {}
        for family, binding in bindings.items():
            destinations = binding.destinations(name)
            for index, destination in enumerate(destinations, start=1):
                key = family if len(destinations) == 1 else f"{family}#{index}"
                result[key] = destination
        return result

    @staticmethod
    def _merge_binding_paths(
        current: Mapping[str, str], previous: Mapping[str, str] | None
    ) -> dict[str, str]:
        """Add newly discovered roots without orphaning older managed copies."""

        merged = dict(current)
        for previous_key, destination in (previous or {}).items():
            family = previous_key.split("#", 1)[0]
            if any(
                key.split("#", 1)[0] == family
                and os.path.normcase(os.path.abspath(value))
                == os.path.normcase(os.path.abspath(destination))
                for key, value in merged.items()
            ):
                continue
            key = previous_key
            if key in merged:
                index = 2
                while f"{family}#{index}" in merged:
                    index += 1
                key = f"{family}#{index}"
            merged[key] = destination
        return merged

    def _gc_store_digest(self, digest: str) -> None:
        if any(skill.digest == digest for skill in self._store.list_skills()):
            return
        _remove_path(self._store_root / digest)

    def _preflight(
        self,
        destinations: Mapping[str, str],
        existing: LocalSkill | None,
        *,
        force: bool,
    ) -> None:
        if existing is not None and self.status(existing) != "installed" and not force:
            raise SkillDriftError(
                "managed skill content is missing or modified; use --force to repair it"
            )
        managed_paths = set((existing.bindings or {}).values()) if existing else set()
        for key, dest in destinations.items():
            if not os.path.lexists(dest):
                continue
            if dest in managed_paths:
                current = _tree_digest(Path(dest))
                if current != existing.digest and not force:  # type: ignore[union-attr]
                    raise SkillDriftError(
                        f"managed binding was modified out-of-band: {key}"
                    )
            elif not force:
                raise SkillCollisionError(
                    f"destination already exists and is not managed: {key}"
                )

    # -- public API -----------------------------------------------------

    def install(
        self,
        source: str | os.PathLike,
        *,
        project: LocalProject | None = None,
        targets: Iterable[str] | None = None,
        bindings: Mapping[str, SkillBinding] | Iterable[SkillBinding] | None = None,
        binding_resolver: BindingResolver | None = None,
        force: bool = False,
    ) -> SkillInstallResult:
        """Validate, store and bind a skill package.

        Provide destinations either as ``bindings`` (mapping or sequence of
        :class:`SkillBinding`) and/or a ``binding_resolver`` callback used for
        any ``targets`` not covered by ``bindings``.
        """
        meta = inspect_source(source)
        scope, project_id = self._scope_for(project)
        resolved = self._resolve_bindings(
            list(targets or []), bindings, binding_resolver
        )
        existing = self._store.get_skill(meta.name, scope, project_id)
        binding_paths = self._merge_binding_paths(
            self._destination_map(meta.name, resolved),
            existing.bindings if existing else None,
        )
        target_families = sorted(
            set(resolved.keys()) | set(existing.targets if existing else [])
        )

        idempotent = (
            existing is not None
            and existing.digest == meta.digest
            and (existing.bindings or {}) == binding_paths
            and sorted(existing.targets) == target_families
        )

        self._preflight(binding_paths, existing, force=force)

        # Populate the content-addressed store. A mismatched tree means the
        # managed store itself was modified and is treated as drift.
        store_dir = self._store_dir(meta.digest, meta.name)
        if store_dir.exists() and _tree_digest(store_dir) != meta.digest:
            if not force:
                raise SkillDriftError(
                    "managed store content was modified; use --force to repair it"
                )
            _remove_path(store_dir)
        created_store = False
        if not store_dir.exists():
            _copy_tree_atomic(Path(source).resolve(strict=True), store_dir)
            created_store = True
        if _tree_digest(store_dir) != meta.digest:
            if created_store:
                _remove_path(store_dir)
            raise SkillValidationError("skill changed while it was being installed")

        pending = [
            Path(dest) for dest in binding_paths.values()
            if _tree_digest(Path(dest)) != meta.digest
        ]
        completed = _replace_trees_atomic(store_dir, pending) if pending else []

        skill = self._store.upsert_skill(
            name=meta.name,
            description=meta.description,
            digest=meta.digest,
            scope=scope,
            project_id=project_id,
            store_path=str(store_dir),
            targets=target_families,
            bindings=binding_paths,
            contains_scripts=meta.contains_scripts,
        )
        if existing is not None and existing.digest != meta.digest:
            self._gc_store_digest(existing.digest)
        changed = not (idempotent and not force and not completed)
        return SkillInstallResult(skill=skill, changed=changed, idempotent=idempotent)

    def list(
        self, project: LocalProject | None = None, *, all_scopes: bool = False
    ) -> list[LocalSkill]:
        if all_scopes:
            return self._store.list_skills()
        scope, project_id = self._scope_for(project)
        return self._store.list_skills(scope=scope, project_id=project_id)

    def status(self, skill: LocalSkill) -> str:
        """Return ``installed`` / ``drifted`` / ``missing`` for a skill."""
        store_dir = self._store_dir(skill.digest, skill.name)
        if not store_dir.exists():
            return "missing"
        if _tree_digest(store_dir) != skill.digest:
            return "drifted"
        if not skill.bindings:
            return "installed"
        state = "installed"
        for dest in skill.bindings.values():
            if not os.path.exists(dest):
                return "missing"
            if _tree_digest(Path(dest)) != skill.digest:
                state = "drifted"
        return state

    def inspect(self, skill: LocalSkill) -> dict:
        """Return a rich, still path-free description of one skill."""
        info = skill.public_json(short_digest=False)
        info["status"] = self.status(skill)
        return info

    def inventory(
        self,
        project: LocalProject | None = None,
        *,
        all_scopes: bool = False,
        short_digest: bool = True,
    ) -> list[dict]:
        """Return sanitized, server-safe metadata for installed skills.

        Contains only name/description/digest/scope/project_id/targets/status/
        contains_scripts.  Never any source, binding or store paths.
        """
        result = []
        for skill in self.list(project, all_scopes=all_scopes):
            entry = skill.public_json(short_digest=short_digest)
            entry["status"] = self.status(skill)
            result.append(entry)
        return result

    def remove(
        self, name: str, *, project: LocalProject | None = None, force: bool = False
    ) -> bool:
        """Remove a skill's bindings and DB record.

        Only the exact binding directories recorded in the DB are deleted.  If a
        binding has drifted, removal is refused unless ``force`` is set.
        """
        scope, project_id = self._scope_for(project)
        skill = self._store.get_skill(name, scope, project_id)
        if skill is None:
            return False
        store_dir = self._store_dir(skill.digest, skill.name)
        store_drifted = store_dir.exists() and _tree_digest(store_dir) != skill.digest
        if not force and (self.status(skill) != "installed" or store_drifted):
            raise SkillDriftError(
                "managed skill content is missing or modified; use --force to remove it"
            )
        removed: set[str] = set()
        for dest in (skill.bindings or {}).values():
            key = os.path.normcase(os.path.abspath(dest))
            if key not in removed:
                removed.add(key)
                _remove_path(Path(dest))
        removed_record = self._store.remove_skill(skill.id)
        if removed_record:
            self._gc_store_digest(skill.digest)
        return removed_record
