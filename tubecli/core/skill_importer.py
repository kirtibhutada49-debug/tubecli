"""Safe importer for local, Markdown-based SKILL.md files."""

from __future__ import annotations

import re
import shutil
import threading
import uuid
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import unquote, urlsplit

import yaml

from tubecli.config import DATA_DIR
from tubecli.core.skill import Skill


class SkillImportError(ValueError):
    """A local skill cannot be safely converted or registered."""


class FreeOnlyInferenceError(RuntimeError):
    """Imported skill inference has no explicitly approved free model."""


_IMPORT_LOCK = threading.Lock()
_FRONTMATTER_FIELDS = {
    "name", "description", "license", "compatibility", "metadata", "allowed-tools",
}
_INFERENCE_CONFIG_FIELDS = {"provider", "model", "api_key", "base_url"}
_SCRIPT_SUFFIXES = {
    ".bat", ".bash", ".cjs", ".cmd", ".exe", ".go", ".jar", ".js", ".jsx",
    ".lua", ".mjs", ".php", ".pl", ".py", ".rb", ".rs", ".sh", ".ps1",
    ".ts", ".tsx",
}
_MAX_SKILL_BYTES = 5 * 1024 * 1024
_MAX_RESOURCE_COUNT = 100
_MAX_RESOURCE_BYTES = 10 * 1024 * 1024
_MARKDOWN_LINK_RE = re.compile(
    r"!?\[[^\]]*\]\(\s*(?:<([^>]+)>|([^\s)]+))"
)
_RESOURCE_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])((?:scripts|references|assets|resources)/[A-Za-z0-9_./~%+\\-]+)"
)


class _UniqueKeySafeLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(loader: _UniqueKeySafeLoader, node: yaml.Node, deep: bool = False):
    loader.flatten_mapping(node)
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, (str, int, float, bool, type(None))):
            raise SkillImportError("Frontmatter metadata keys must be scalar values.")
        if key in mapping:
            raise SkillImportError(f"Duplicate frontmatter key: {key!r}.")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


def _parse_frontmatter(text: str) -> Tuple[Dict[str, Any], str]:
    if not text.startswith("---\n") and not text.startswith("---\r\n"):
        raise SkillImportError("SKILL.md must begin with YAML frontmatter delimited by '---'.")
    lines = text.splitlines(keepends=True)
    closing_index = next(
        (i for i in range(1, len(lines)) if lines[i].strip() == "---"),
        None,
    )
    if closing_index is None:
        raise SkillImportError("SKILL.md frontmatter is missing its closing '---' delimiter.")

    raw = "".join(lines[1:closing_index])
    try:
        frontmatter = yaml.load(raw, Loader=_UniqueKeySafeLoader)
    except SkillImportError:
        raise
    except yaml.YAMLError as exc:
        raise SkillImportError(f"Invalid SKILL.md frontmatter: {exc}") from exc

    if not isinstance(frontmatter, dict):
        raise SkillImportError("SKILL.md frontmatter must be a YAML mapping.")
    unknown = set(frontmatter) - _FRONTMATTER_FIELDS - _INFERENCE_CONFIG_FIELDS
    if unknown:
        fields = ", ".join(sorted(str(field) for field in unknown))
        raise SkillImportError(f"Unsupported frontmatter field(s): {fields}.")

    for field in _INFERENCE_CONFIG_FIELDS:
        if field in frontmatter:
            raise SkillImportError(
                f"Frontmatter field '{field}' is unsupported; imported skills cannot "
                "select or require an LLM provider or model."
            )

    name = frontmatter.get("name")
    description = frontmatter.get("description")
    if not isinstance(name, str) or not name.strip():
        raise SkillImportError("Frontmatter 'name' must be a non-empty string.")
    if not isinstance(description, str) or not description.strip():
        raise SkillImportError("Frontmatter 'description' must be a non-empty string.")

    for field in ("license", "compatibility"):
        if field in frontmatter and not isinstance(frontmatter[field], str):
            raise SkillImportError(f"Frontmatter '{field}' must be a string.")
    if "license" in frontmatter and not frontmatter["license"].strip():
        raise SkillImportError("Frontmatter 'license' must not be empty.")
    metadata = frontmatter.get("metadata", {})
    if not isinstance(metadata, dict) or any(not isinstance(key, str) for key in metadata):
        raise SkillImportError("Frontmatter 'metadata' must be a mapping with string keys.")
    blocked_metadata = {
        str(key).strip().lower()
        for key in metadata
        if (
            str(key).strip().lower() in _INFERENCE_CONFIG_FIELDS
            or any(marker in str(key).strip().lower().replace("-", "_") for marker in (
                "provider", "model", "api_key", "base_url", "inference",
            ))
            or "paid" in str(key).strip().lower()
            and any(marker in str(key).strip().lower() for marker in ("llm", "api", "model"))
        )
    }
    if blocked_metadata:
        fields = ", ".join(sorted(blocked_metadata))
        raise SkillImportError(
            f"Metadata field(s) {fields} cannot configure inference for imported skills."
        )
    if any(
        not isinstance(value, str)
        and not (
            key == "examples"
            and isinstance(value, list)
            and all(isinstance(item, str) for item in value)
        )
        for key, value in metadata.items()
    ):
        raise SkillImportError("Frontmatter metadata values must be strings.")

    allowed_tools = frontmatter.get("allowed-tools")
    if allowed_tools is not None and not (
        isinstance(allowed_tools, str)
        or isinstance(allowed_tools, list)
        and all(isinstance(item, str) for item in allowed_tools)
    ):
        raise SkillImportError("Frontmatter 'allowed-tools' must be a string or list of strings.")

    body = "".join(lines[closing_index + 1:])
    if not body.strip():
        raise SkillImportError("SKILL.md must contain a Markdown body after its frontmatter.")
    return frontmatter, body


def _resource_references(body: str) -> Iterable[Tuple[str, str]]:
    found = set()
    for match in _MARKDOWN_LINK_RE.finditer(body):
        target = (match.group(1) or match.group(2) or "").strip()
        if target and target not in found:
            found.add(target)
            yield target, match.group(0)
    for match in _RESOURCE_PATH_RE.finditer(body):
        target = match.group(1).rstrip(".,;:!?")
        if target and target not in found:
            found.add(target)
            yield target, match.group(0)


def _safe_resource_path(target: str, source_root: Path) -> Optional[Tuple[Path, str]]:
    decoded = unquote(target)
    if decoded.startswith("#"):
        return None
    parsed = urlsplit(decoded)
    if parsed.scheme or parsed.netloc:
        if parsed.scheme.lower() in {"mailto"}:
            return None
        raise SkillImportError(f"External resource references are unsupported: {target!r}.")

    path_text = parsed.path.replace("\\", "/")
    if not path_text:
        return None
    if (
        PurePosixPath(path_text).is_absolute()
        or PureWindowsPath(path_text).is_absolute()
        or PureWindowsPath(path_text).drive
    ):
        raise SkillImportError(f"Absolute resource paths are not allowed: {target!r}.")
    if any(part == ".." for part in path_text.split("/")):
        raise SkillImportError(f"Path traversal is not allowed in resource paths: {target!r}.")

    relative = PurePosixPath(path_text)
    if any(part in ("", ".") for part in relative.parts):
        relative = PurePosixPath(*(part for part in relative.parts if part not in ("", ".")))
    candidate = source_root.joinpath(*relative.parts)
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError as exc:
        raise SkillImportError(f"Referenced resource does not exist: {target!r}.") from exc
    if not resolved.is_relative_to(source_root):
        raise SkillImportError(f"Resource resolves outside the SKILL.md directory: {target!r}.")
    if not resolved.is_file():
        raise SkillImportError(f"Referenced resource is not a file: {target!r}.")
    return resolved, relative.as_posix()


def is_explicitly_free_model(provider: str, model: str) -> bool:
    """Only the explicitly approved 9Router auto:cheap route is allowed."""
    return (
        str(provider or "").strip().casefold() == "9router"
        and str(model or "").strip() == "auto:cheap"
    )


def call_imported_skill_free_model(
    agent: Dict[str, Any],
    messages: List[Dict[str, str]],
    *,
    temperature: float = 0.7,
) -> str:
    """Call only an explicitly selected free 9Router model; never fail over."""
    provider = str(agent.get("provider") or "").strip()
    model = str(agent.get("model") or "").strip()
    if not is_explicitly_free_model(provider, model):
        raise FreeOnlyInferenceError(
            "Imported skills require the explicitly approved 9Router model 'auto:cheap'. "
            "No inference request was made."
        )

    from tubecli.core.brain import AgentBrain
    from tubecli.core.ninerouter import api_key, base_url

    return AgentBrain._call_openai(
        model, api_key() or "9router", messages,
        base_url=base_url(), temperature=temperature,
    )


def _prepare_skill_from_file(path: str, skill_id: Optional[str] = None) -> Tuple[Skill, Dict[str, Path]]:
    """Validate and convert a local SKILL.md without writing or registering it."""
    try:
        skill_file = Path(path).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SkillImportError(f"Cannot access local skill file: {exc}") from exc
    if not skill_file.is_file() or skill_file.name.casefold() != "skill.md":
        raise SkillImportError("The local file must be named SKILL.md.")
    try:
        if skill_file.stat().st_size > _MAX_SKILL_BYTES:
            raise SkillImportError("SKILL.md exceeds the 5 MiB size limit.")
        text = skill_file.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise SkillImportError("SKILL.md must be valid UTF-8 text.") from exc
    except OSError as exc:
        raise SkillImportError(f"Cannot read SKILL.md: {exc}") from exc

    frontmatter, body = _parse_frontmatter(text)
    source_root = skill_file.parent.resolve(strict=True)

    resources: Dict[str, Path] = {}
    for target, _ in _resource_references(body):
        resolved = _safe_resource_path(target, source_root)
        if resolved:
            resource_file, relative = resolved
            resources[relative] = resource_file
    if len(resources) > _MAX_RESOURCE_COUNT:
        raise SkillImportError(f"SKILL.md references more than {_MAX_RESOURCE_COUNT} local resources.")
    if sum(resource.stat().st_size for resource in resources.values()) > _MAX_RESOURCE_BYTES:
        raise SkillImportError("Referenced local resources exceed the 10 MiB total size limit.")

    metadata = frontmatter.get("metadata", {})
    examples = metadata.get("examples", [])
    if isinstance(examples, str):
        examples = [examples]
    if not isinstance(examples, list) or any(not isinstance(item, str) for item in examples):
        raise SkillImportError("Frontmatter metadata 'examples' must be a string or list of strings.")
    for field in ("input_hint", "when_to_use"):
        if field in metadata and not isinstance(metadata[field], str):
            raise SkillImportError(f"Frontmatter metadata '{field}' must be a string.")

    skill_id = skill_id or str(uuid.uuid4())
    safe_metadata = {
        field: frontmatter[field]
        for field in ("license", "compatibility", "allowed-tools")
        if field in frontmatter
    }
    safe_metadata.update(metadata)
    unsupported_scripts = sorted(
        relative for relative in resources
        if Path(relative).suffix.casefold() in _SCRIPT_SUFFIXES
    )
    skill = Skill(
        id=skill_id,
        name=frontmatter["name"].strip(),
        description=frontmatter["description"].strip(),
        skill_type="Markdown",
        skill_format="markdown",
        commands=[],
        input_hint=metadata.get("input_hint", ""),
        when_to_use=metadata.get("when_to_use", ""),
        examples=examples,
        authored_by="user",
        workflow_data={
            "markdown_content": body,
            "source_metadata": safe_metadata,
            "resources": sorted(resources),
            "unsupported_scripts": unsupported_scripts,
            "_tubecli_importer": {"version": 1, "free_only": True},
            **({"resource_dir": f"imported_skills/{skill_id}"} if resources else {}),
        },
    )
    return skill, resources


def _copy_skill_resources(skill: Skill, resources: Dict[str, Path]) -> Optional[Path]:
    if not resources:
        return None
    data_root = Path(DATA_DIR).resolve()
    resources_root = data_root / "imported_skills"
    resources_root.mkdir(parents=True, exist_ok=True)
    resources_root = resources_root.resolve(strict=True)
    if not resources_root.is_relative_to(data_root):
        raise SkillImportError("Managed resource directory resolves outside TubeCLI data.")

    skill_resource_dir = resources_root / skill.id
    skill_resource_dir.mkdir(mode=0o700)
    try:
        for relative, source in resources.items():
            destination = skill_resource_dir.joinpath(*PurePosixPath(relative).parts)
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            destination.chmod(0o600)
    except Exception:
        shutil.rmtree(skill_resource_dir)
        raise
    return skill_resource_dir


def import_skill_from_file(
    path: str,
    *,
    allow_scripts: bool = False,
    manager=None,
) -> Skill:
    """Convert and register a local SKILL.md as a native TubeCLI Markdown skill.

    Linked local files are copied into data/imported_skills/<skill-id>. Script
    resources are stored as inert files and listed as unsupported; this API
    never executes them. ``allow_scripts=True`` is intentionally unsupported.
    """
    if allow_scripts:
        raise SkillImportError("Script execution/import is unsupported in Phase 1.")
    skill, resources = _prepare_skill_from_file(path)

    if manager is None:
        from tubecli.core.skill import skill_manager
        manager = skill_manager

    from tubecli.core.skill import SkillManager

    normalize = SkillManager._normalize_name
    with _IMPORT_LOCK:
        existing = manager.get_all()
        normalized_name = normalize(skill.name) or skill.name.casefold()
        if any(
            (normalize(getattr(item, "name", "")) or str(getattr(item, "name", "")).casefold())
            == normalized_name
            for item in existing
        ):
            raise SkillImportError(f"A skill named {skill.name!r} is already registered.")

        skill_resource_dir = _copy_skill_resources(skill, resources)
        try:
            imported = manager.create(
                id=skill.id,
                name=skill.name,
                description=skill.description,
                skill_type=skill.skill_type,
                skill_format=skill.skill_format,
                commands=skill.commands,
                input_hint=skill.input_hint,
                when_to_use=skill.when_to_use,
                examples=skill.examples,
                authored_by=skill.authored_by,
                workflow_data=skill.workflow_data,
            )
        except Exception:
            if skill_resource_dir and skill_resource_dir.exists():
                shutil.rmtree(skill_resource_dir)
            raise

    return imported
