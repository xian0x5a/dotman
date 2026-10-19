from __future__ import annotations

import difflib
import os
import shlex
import stat
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath

import tomlkit

from dotman.atomic_files import write_text_atomic
from dotman.collisions import paths_conflict
from dotman.command_runtime import ArgvCommand, CommandRequest, current_command_runtime
from dotman.manifest import PACKAGE_MANIFEST_FILE_NAME, discover_package_manifests, validate_package_id, validate_target_name
from dotman.toml_utils import load_toml_text


@dataclass(frozen=True)
class LivePathSpec:
    raw_input: str
    resolved_path: Path
    target_kind: str
    target_name_base: str
    source_path: str
    config_path: str
    chmod: str | None


@dataclass(frozen=True)
class AddOperationResult:
    repo_name: str
    package_id: str
    manifest_path: Path
    target_name: str
    target_kind: str
    source_path: str
    config_path: str
    chmod: str | None
    created_package: bool
    before_text: str
    after_text: str

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "mode": "config-only",
            "operation": "add",
            "repo": self.repo_name,
            "package_id": self.package_id,
            "manifest_path": str(self.manifest_path),
            "target": {
                "name": self.target_name,
                "kind": self.target_kind,
                "source": self.source_path,
                "path": self.config_path,
            },
            "created_package": self.created_package,
        }
        if self.chmod is not None:
            payload["target"]["chmod"] = self.chmod
        return payload


@dataclass(frozen=True)
class ExistingTargetMetadata:
    names: set[str]
    paths: set[str]
    # Each declared `source` with the live `path` of the target that declares it.
    source_paths: dict[str, str | None]


@dataclass(frozen=True)
class AddReviewResult:
    exit_code: int
    manifest_text: str


def resolve_live_path_spec(live_path_text: str, *, cwd: Path | None = None, home: Path | None = None) -> LivePathSpec:
    current_dir = (cwd or Path.cwd()).resolve()
    home_dir = (home or Path.home()).resolve()
    candidate_path = Path(os.path.expanduser(live_path_text))
    if not candidate_path.is_absolute():
        candidate_path = current_dir / candidate_path
    if not candidate_path.exists() and not candidate_path.is_symlink():
        raise ValueError(f"live path does not exist: {candidate_path.resolve(strict=False)}")
    if candidate_path.is_symlink():
        raise ValueError(f"live path symlinks are not supported: {candidate_path.resolve(strict=False)}")
    resolved_path = candidate_path.resolve()
    file_mode = stat.S_IMODE(resolved_path.stat().st_mode)
    if resolved_path.is_file():
        target_kind = "file"
        chmod = None if file_mode == 0o644 else f"{file_mode:03o}"
    elif resolved_path.is_dir():
        target_kind = "directory"
        chmod = None if file_mode == 0o755 else f"{file_mode:03o}"
    else:
        raise ValueError(f"unsupported live path type for add: {resolved_path}")
    config_path = _config_path_text(resolved_path, home_dir=home_dir)
    return LivePathSpec(
        raw_input=live_path_text,
        resolved_path=resolved_path,
        target_kind=target_kind,
        target_name_base=build_target_name(config_path=config_path, target_kind=target_kind),
        source_path=build_source_path(resolved_path=resolved_path, home_dir=home_dir),
        config_path=config_path,
        chmod=chmod,
    )


def build_target_name(*, config_path: str, target_kind: str) -> str:
    if target_kind not in {"file", "directory"}:
        raise ValueError(f"unsupported target kind '{target_kind}'")
    normalized_path = config_path
    if normalized_path.startswith("~/"):
        normalized_path = normalized_path[2:]
    elif normalized_path.startswith("/"):
        normalized_path = normalized_path[1:]
    key_body: list[str] = []
    previous_was_separator = False
    for character in normalized_path.lower():
        if character.isalnum():
            key_body.append(character)
            previous_was_separator = False
            continue
        if previous_was_separator:
            continue
        key_body.append("_")
        previous_was_separator = True
    collapsed = "".join(key_body).strip("_")
    prefix = "f" if target_kind == "file" else "d"
    return f"{prefix}_{collapsed}" if collapsed else prefix


def build_source_path(*, resolved_path: Path, home_dir: Path) -> str:
    relative_parts: tuple[str, ...]
    try:
        relative_parts = resolved_path.relative_to(home_dir).parts
    except ValueError:
        relative_parts = tuple(part for part in resolved_path.parts if part != "/")
    normalized_parts = [_strip_component_leading_dots(part) for part in relative_parts]
    kept_parts = [part for part in normalized_parts if part]
    if not kept_parts:
        return "files"
    return "files/" + "/".join(kept_parts)


def package_manifest_path(*, repo_root: Path, package_id: str) -> Path:
    validate_package_id(package_id)
    return repo_root / "packages" / Path(*package_id.split("/")) / PACKAGE_MANIFEST_FILE_NAME


def ensure_package_is_not_inside_another(*, repo_root: Path, repo_name: str, package_id: str) -> None:
    # Discovery stops at the first folder holding a manifest, so a nested package would
    # silently vanish. Checked even when `package.toml` exists at the target path: inside
    # a package folder that file is package content (e.g. a dotfile source), not a manifest.
    packages_root = repo_root / "packages"
    package_parts = package_id.split("/")
    for depth in range(1, len(package_parts)):
        enclosing_package_id = "/".join(package_parts[:depth])
        if (packages_root / enclosing_package_id / PACKAGE_MANIFEST_FILE_NAME).exists():
            raise ValueError(
                f"cannot create package '{repo_name}:{package_id}' inside package "
                f"'{repo_name}:{enclosing_package_id}': packages cannot nest"
            )


def ensure_new_package_holds_no_packages(*, repo_root: Path, repo_name: str, package_id: str) -> None:
    # Giving a namespace folder its own manifest would hide the packages already below it.
    packages_root = repo_root / "packages"
    nested_package_ids = [
        manifest_path.parent.relative_to(packages_root).as_posix()
        for manifest_path in discover_package_manifests(packages_root / package_id)
    ]
    if nested_package_ids:
        nested_text = ", ".join(f"'{repo_name}:{nested_id}'" for nested_id in nested_package_ids)
        raise ValueError(
            f"cannot create package '{repo_name}:{package_id}': its folder already holds "
            f"{nested_text}; packages cannot nest"
        )


def prepare_add_to_package(*, repo_root: Path, repo_name: str, package_id: str, live_path_text: str) -> AddOperationResult:
    live_spec = resolve_live_path_spec(live_path_text)
    manifest_path = package_manifest_path(repo_root=repo_root, package_id=package_id)
    ensure_package_is_not_inside_another(repo_root=repo_root, repo_name=repo_name, package_id=package_id)
    if not manifest_path.exists():
        ensure_new_package_holds_no_packages(repo_root=repo_root, repo_name=repo_name, package_id=package_id)
    before_text = manifest_path.read_text(encoding="utf-8") if manifest_path.exists() else ""
    existing_targets = read_existing_target_metadata(
        before_text,
        manifest_path=manifest_path,
        repo_name=repo_name,
        package_id=package_id,
    )
    if live_spec.config_path in existing_targets.paths:
        raise ValueError(
            f"package '{repo_name}:{package_id}' already declares target path '{live_spec.config_path}'"
        )
    target_name = next_available_target_name(live_spec.target_name_base, existing_targets.names)
    source_path = next_available_source_path(live_spec.source_path, set(existing_targets.source_paths))
    ensure_source_does_not_nest_unrelated_source(
        source_path=source_path,
        config_path=live_spec.config_path,
        existing_source_paths=existing_targets.source_paths,
    )
    target_block = render_target_block(
        target_name=target_name,
        source_path=source_path,
        config_path=live_spec.config_path,
        chmod=live_spec.chmod,
    )
    created_package = not manifest_path.exists()
    after_text = append_target_block(before_text=before_text, target_block=target_block)
    return AddOperationResult(
        repo_name=repo_name,
        package_id=package_id,
        manifest_path=manifest_path,
        target_name=target_name,
        target_kind=live_spec.target_kind,
        source_path=source_path,
        config_path=live_spec.config_path,
        chmod=live_spec.chmod,
        created_package=created_package,
        before_text=before_text,
        after_text=after_text,
    )


def write_add_result(result: AddOperationResult, *, manifest_text: str | None = None) -> AddOperationResult:
    final_manifest_text = manifest_text if manifest_text is not None else result.after_text
    validate_manifest_text(
        final_manifest_text,
        package_id=result.package_id,
        manifest_path=result.manifest_path,
        repo_name=result.repo_name,
    )
    write_text_atomic(result.manifest_path, final_manifest_text)
    return replace(result, after_text=final_manifest_text)


def validate_manifest_text(
    manifest_text: str,
    *,
    package_id: str,
    manifest_path: Path | None = None,
    repo_name: str | None = None,
) -> None:
    payload = load_toml_text(
        manifest_text,
        context=_manifest_context(package_id=package_id, manifest_path=manifest_path),
        path=manifest_path,
        package_repo=repo_name,
        package_id=package_id,
    )
    targets_payload = payload.get("targets")
    if isinstance(targets_payload, dict):
        for target_name in targets_payload:
            validate_target_name(target_name)


def read_existing_target_metadata(
    manifest_text: str,
    *,
    manifest_path: Path | None = None,
    repo_name: str | None = None,
    package_id: str | None = None,
) -> ExistingTargetMetadata:
    if not manifest_text.strip():
        return ExistingTargetMetadata(names=set(), paths=set(), source_paths={})
    payload = load_toml_text(
        manifest_text,
        context=_manifest_context(manifest_path=manifest_path, repo_name=repo_name, package_id=package_id),
        path=manifest_path,
    )
    targets_payload = payload.get("targets")
    if not isinstance(targets_payload, dict):
        return ExistingTargetMetadata(names=set(), paths=set(), source_paths={})
    target_names = set(targets_payload)
    for target_name in target_names:
        validate_target_name(target_name)
    target_payloads = [target_payload for target_payload in targets_payload.values() if isinstance(target_payload, dict)]
    return ExistingTargetMetadata(
        names=target_names,
        paths={target_payload["path"] for target_payload in target_payloads if isinstance(target_payload.get("path"), str)},
        # Normalized so `./files/x` and `files/x/` count as the source `add` derives as `files/x`.
        source_paths={
            PurePosixPath(target_payload["source"]).as_posix(): target_payload.get("path")
            for target_payload in target_payloads
            if isinstance(target_payload.get("source"), str)
        },
    )


def _manifest_context(*, manifest_path: Path | None = None, repo_name: str | None = None, package_id: str | None = None) -> str:
    if repo_name is not None and package_id is not None:
        label = f"{repo_name}:{package_id}"
    elif repo_name is not None:
        label = repo_name
    elif package_id is not None:
        label = package_id
    else:
        label = "package"
    if manifest_path is None:
        return f"package manifest for '{label}'"
    return f"package manifest for '{label}' ({manifest_path})"


def next_available_target_name(base_name: str, existing_names: set[str]) -> str:
    if base_name not in existing_names:
        return base_name
    suffix = 2
    while f"{base_name}_{suffix}" in existing_names:
        suffix += 1
    return f"{base_name}_{suffix}"


def next_available_source_path(base_source: str, existing_sources: set[str]) -> str:
    # Distinct live files can derive one source (`~/.zshrc` and `~/zshrc` both give
    # `files/zshrc`), and two targets sharing a repo file cannot be tracked together.
    # The suffix goes before the extension so the repo file keeps its file type.
    if base_source not in existing_sources:
        return base_source
    base_path = PurePosixPath(base_source)
    suffix = 2
    while (candidate := base_path.with_stem(f"{base_path.stem}_{suffix}").as_posix()) in existing_sources:
        suffix += 1
    return candidate


def ensure_source_does_not_nest_unrelated_source(
    *,
    source_path: str,
    config_path: str,
    existing_source_paths: dict[str, str | None],
) -> None:
    # Dropped leading dots let live paths that do not nest (`~/.config/foo`, `~/config/foo/bar`)
    # derive nested sources, which push and pull reject as incompatible nested targets.
    # Nested live paths may share nested sources; the existing target's ignore rules decide that.
    new_source = PurePosixPath(source_path)
    for existing_source, existing_config_path in existing_source_paths.items():
        existing = PurePosixPath(existing_source)
        if existing not in new_source.parents and new_source not in existing.parents:
            continue
        if isinstance(existing_config_path, str) and paths_conflict(
            Path(os.path.expanduser(config_path)), Path(os.path.expanduser(existing_config_path))
        ):
            continue
        raise ValueError(
            f"cannot add '{config_path}': its source '{source_path}' would nest with the source "
            f"'{existing_source}' of unrelated path '{existing_config_path}', which push and pull "
            "reject; give one of the targets a different source in the package manifest"
        )


def render_target_block(*, target_name: str, source_path: str, config_path: str, chmod: str | None) -> str:
    target = {"source": source_path, "path": config_path}
    if chmod is not None:
        target["chmod"] = chmod
    return tomlkit.dumps({"targets": {target_name: target}})


def append_target_block(*, before_text: str, target_block: str) -> str:
    stripped = before_text.rstrip()
    if not stripped:
        return target_block
    return f"{stripped}\n\n{target_block}"


def add_editor_available() -> bool:
    return bool(os.environ.get("VISUAL") or os.environ.get("EDITOR"))


def review_add_manifest(result: AddOperationResult) -> AddReviewResult | None:
    if not add_editor_available():
        return None
    editor_command = _normalize_editor_command(_resolve_editor_command())
    with tempfile.TemporaryDirectory(prefix="dotman-add-review-") as temp_dir:
        temp_root = Path(temp_dir)
        before_path = temp_root / "package-before.toml"
        after_path = temp_root / "package-after.toml"
        editable_path = temp_root / "package.toml"
        review_path = temp_root / "add-review.md"

        before_path.write_text(result.before_text, encoding="utf-8")
        after_path.write_text(result.after_text, encoding="utf-8")
        editable_path.write_text(result.after_text, encoding="utf-8")
        before_path.chmod(0o444)
        after_path.chmod(0o444)
        review_path.write_text(
            _build_add_review_content(
                result=result,
                manifest_path=result.manifest_path,
                before_path=before_path,
                after_path=after_path,
                editable_path=editable_path,
            ),
            encoding="utf-8",
        )
        review_path.chmod(0o444)

        command_result = current_command_runtime().run(
            CommandRequest(
                command=ArgvCommand((*editor_command, str(review_path), str(editable_path))),
                io="tty",
            )
        )
        return AddReviewResult(
            exit_code=command_result.exit_code,
            manifest_text=editable_path.read_text(encoding="utf-8"),
        )


def _resolve_editor_command() -> list[str]:
    editor_value = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if not editor_value:
        raise ValueError("add review requires $VISUAL or $EDITOR")
    return shlex.split(editor_value)


def _normalize_editor_command(editor_command: list[str]) -> list[str]:
    return [argument for argument in editor_command if argument != "-d"]


def _build_add_review_content(*, result: AddOperationResult, manifest_path: Path, before_path: Path, after_path: Path, editable_path: Path) -> str:
    current_manifest_label = f"current {manifest_path.name}"
    proposed_manifest_label = f"proposed {manifest_path.name}"
    diff_lines = list(
        difflib.unified_diff(
            before_path.read_text(encoding="utf-8", errors="replace").splitlines(),
            after_path.read_text(encoding="utf-8", errors="replace").splitlines(),
            fromfile=current_manifest_label,
            tofile=proposed_manifest_label,
            lineterm="",
        )
    )
    diff_body = "No differences detected.\n" if not diff_lines else "\n".join(diff_lines) + "\n"
    action_text = "create package manifest" if result.created_package else "update package manifest"
    current_manifest_text = str(manifest_path) if manifest_path.exists() else "(new package; no current manifest yet)"
    return (
        "# Dotman Add Review\n\n"
        "Review only. Do not edit this file.\n"
        "Inspect the proposed package manifest diff below, then edit the temporary package.toml copy if needed.\n"
        "Nothing is written back to the repo until dotman asks for confirmation after the editor exits.\n"
        "\n"
        "## Summary\n\n"
        f"- action: {action_text}\n"
        f"- package: {result.repo_name}:{result.package_id}\n"
        "\n"
        "## Review Inputs\n\n"
        f"- package manifest path: {manifest_path}\n"
        f"- current manifest: {current_manifest_text}\n"
        f"- proposed manifest: {proposed_manifest_label}\n"
        f"- editable manifest copy: {editable_path}\n\n"
        "## Diff\n\n"
        "```diff\n"
        f"{diff_body}"
        "```\n"
    )


def _config_path_text(resolved_path: Path, *, home_dir: Path) -> str:
    try:
        relative_path = resolved_path.relative_to(home_dir)
    except ValueError:
        return resolved_path.as_posix()
    if not relative_path.parts:
        return "~"
    return f"~/{relative_path.as_posix()}"


def _strip_component_leading_dots(component: str) -> str:
    return component.lstrip(".")
