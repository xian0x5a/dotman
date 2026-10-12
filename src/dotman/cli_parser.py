from __future__ import annotations

import argparse
from collections.abc import Sequence


_EDIT_SUGAR_TOP_LEVEL_OPTIONS_WITH_VALUES = {"--config", "--file-symlink-mode", "--dir-symlink-mode"}


def _command_index(argv: Sequence[str]) -> int | None:
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            return None
        if token in _EDIT_SUGAR_TOP_LEVEL_OPTIONS_WITH_VALUES:
            index += 2
            continue
        if any(token.startswith(f"{option}=") for option in _EDIT_SUGAR_TOP_LEVEL_OPTIONS_WITH_VALUES):
            index += 1
            continue
        if token.startswith("-"):
            index += 1
            continue
        return index
    return None


def normalize_edit_query_argv(argv: Sequence[str]) -> list[str]:
    normalized_argv = list(argv)
    command_index = _command_index(normalized_argv)
    if command_index is None or normalized_argv[command_index] != "edit":
        return normalized_argv
    if command_index + 1 >= len(normalized_argv):
        return normalized_argv
    next_token = normalized_argv[command_index + 1]
    if next_token in {"package", "target", "local", "config", "repo", "query"} or next_token.startswith("-"):
        return normalized_argv
    return [*normalized_argv[: command_index + 1], "query", *normalized_argv[command_index + 1 :]]


def add_track_request_argument(parser: argparse.ArgumentParser, *, required: bool = True) -> None:
    parser.add_argument(
        "binding",
        nargs=None if required else "?",
        metavar="[<repo>:]<selector>[@<profile>]",
        help="Track request; repo may be omitted when unique",
    )


def add_tracked_package_argument(parser: argparse.ArgumentParser, *, required: bool = True) -> None:
    parser.add_argument(
        "binding",
        nargs=None if required else "?",
        metavar="[<repo>:]<package>[@<profile>]",
        help="Tracked package selector; repo may be omitted when unique",
    )


def add_package_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "package",
        metavar="<package>",
        help="Tracked package argument in the form <repo>:<package> or <package>",
    )


def add_target_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "target",
        metavar="<target>",
        help="Tracked target argument in the form [<repo>:]<package>.<target> or <target>",
    )


def add_live_path_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "live_path",
        metavar="<live-path>",
        help="Live file or directory path to adopt into package config",
    )


def add_package_query_argument(parser: argparse.ArgumentParser, *, required: bool = False) -> None:
    parser.add_argument(
        "package_query",
        nargs=None if required else "?",
        metavar="<package-query>",
        help="Package query in the form [<repo>:]<package>",
    )


def add_trackable_query_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "query",
        metavar="<query>",
        help="Package or group selector in the form [<repo>:]<selector>",
    )


def add_variable_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "variable",
        metavar="<var>",
        help="Resolved variable name in dotted form",
    )


def add_snapshot_argument(parser: argparse.ArgumentParser, *, required: bool = True) -> None:
    parser.add_argument(
        "snapshot",
        nargs=None if required else "?",
        metavar="<snapshot>",
        help="Snapshot ID, unique leading snapshot prefix, or 'latest'",
    )


def add_dry_run_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-d",
        "--dry-run",
        action="store_true",
        help="Preview only; review planned changes without applying them",
    )


def add_run_noop_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--run-noop",
        action="store_true",
        dest="run_noop",
        help="Keep push/pull packages with only noop targets alive so hooks rerun",
    )


def add_no_deps_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--no-deps",
        action="store_true",
        dest="no_deps",
        help="Leave out the dependency closure of package scopes",
    )


def add_report_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--report",
        action="store_true",
        help="After execution, list every entry, including unselected, skipped and guard-skipped work",
    )


def add_commit_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Commit Work without the Deck: after a successful --no-confirm run, commit the repository files it wrote (the Deck starts with it on)",
    )


def add_full_path_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--full-path",
        action="store_true",
        default=None,
        dest="full_path",
        help="Show unabridged absolute paths in human-readable selection, review, and execution output",
    )


def add_jinja_context_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--profile",
        metavar="<profile>",
        help="Profile value to expose in template context",
    )
    parser.add_argument(
        "--os",
        dest="template_os",
        metavar="<os>",
        help="OS value to expose in template context",
    )
    parser.add_argument(
        "--cpu-arch",
        dest="template_cpu_arch",
        metavar="<cpu-arch>",
        help="CPU architecture value to expose in template context",
    )
    parser.add_argument(
        "--var",
        action="append",
        default=[],
        metavar="<key=value>",
        help="Additional template var assignment using dotted keys",
    )


def hide_subparser_from_help(subparsers, name: str) -> None:
    # Argparse has no public API for parseable-but-hidden subcommands.
    subparsers._choices_actions = [
        action for action in subparsers._choices_actions if getattr(action, "dest", None) != name
    ]


class _PrintVersionAction(argparse.Action):
    """`--version` that reads package metadata only when asked: importing
    importlib.metadata costs every command ~12ms of startup."""

    def __init__(self, option_strings: Sequence[str], dest: str = argparse.SUPPRESS, help: str | None = None) -> None:
        super().__init__(option_strings, dest, nargs=0, default=argparse.SUPPRESS, help=help or "show program's version number and exit")

    def __call__(self, parser: argparse.ArgumentParser, namespace: argparse.Namespace, values: object, option_string: str | None = None) -> None:
        from importlib.metadata import version

        print(f"{parser.prog} {version('dotman')}")
        parser.exit()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dotman", description="dotman CLI")
    # Keep optional command flags present on every parsed namespace so command
    # dispatch can read a consistent attribute shape.
    parser.set_defaults(full_path=None)
    parser.add_argument("--version", action=_PrintVersionAction)
    parser.add_argument("--config", metavar="<config-path>", help="Path to dotman config.toml")
    parser.add_argument("--json", action="store_true", dest="json_output", help="Emit machine-readable JSON")
    parser.add_argument("--no-confirm", action="store_true", help="Use policy defaults without menus, editors, pagers, or prompts; sudo may still authenticate")
    parser.add_argument(
        "--file-symlink-mode",
        choices=("prompt", "follow"),
        default=None,
        metavar="<mode>",
        help="Override file symlink handling for managed endpoints",
    )
    parser.add_argument(
        "--dir-symlink-mode",
        choices=("fail", "follow"),
        default=None,
        metavar="<mode>",
        help="Override directory symlink handling for managed endpoints",
    )

    subparsers = parser.add_subparsers(dest="command", required=True, title="commands", metavar="<command>")

    elevation_parser = subparsers.add_parser(
        "elevation",
        help=argparse.SUPPRESS,
        description=argparse.SUPPRESS,
    )
    elevation_subparsers = elevation_parser.add_subparsers(
        dest="elevation_command",
        required=True,
        title="elevation commands",
        metavar="<elevation-command>",
    )
    elevation_request_parser = elevation_subparsers.add_parser(
        "request",
        help=argparse.SUPPRESS,
        description=argparse.SUPPRESS,
    )
    elevation_request_parser.add_argument("reason", nargs="?", metavar="<reason>", help=argparse.SUPPRESS)
    hide_subparser_from_help(subparsers, "elevation")

    track_parser = subparsers.add_parser(
        "track",
        help="Track packages in manager state",
        description="Track packages in manager state",
    )
    add_track_request_argument(track_parser)

    search_parser = subparsers.add_parser(
        "search",
        help="Search packages and groups",
        description="Search packages and groups across configured repos",
    )
    search_parser.add_argument(
        "query",
        metavar="<query>",
        help="Search text for package or group selectors",
    )

    add_parser = subparsers.add_parser(
        "add",
        help="Create or update package config from a live path",
        description="Create or update package config from a live path",
    )
    add_live_path_argument(add_parser)
    add_package_query_argument(add_parser, required=False)

    edit_parser = subparsers.add_parser(
        "edit",
        help="Open a tracked package, target, local override, config path, or repo path",
        description="Open a tracked package, target, local override, config path, or repo path",
    )
    edit_subparsers = edit_parser.add_subparsers(
        dest="edit_command",
        required=True,
        title="edit commands",
        metavar="<edit-command>",
    )
    edit_package_parser = edit_subparsers.add_parser(
        "package",
        help="Open a tracked package directory in an editor",
        description="Open a tracked package directory in an editor",
    )
    add_package_argument(edit_package_parser)
    edit_target_parser = edit_subparsers.add_parser(
        "target",
        help="Open a tracked target repo path in an editor",
        description="Open a tracked target repo path in an editor",
    )
    add_target_argument(edit_target_parser)
    edit_local_parser = edit_subparsers.add_parser(
        "local",
        help="Open a per-repo local override file in an editor",
        description="Open a per-repo local override file in an editor",
    )
    edit_local_parser.add_argument(
        "repo",
        nargs="?",
        metavar="<repo>",
        help="Repo name; optional only when config has one repo",
    )
    edit_subparsers.add_parser(
        "config",
        help="Open dotman manager config in an editor",
        description="Open dotman manager config in an editor",
    )
    edit_repo_parser = edit_subparsers.add_parser(
        "repo",
        help="Open a configured repo root in an editor",
        description="Open a configured repo root in an editor",
    )
    edit_repo_parser.add_argument(
        "repo",
        metavar="<repo>",
        help="Configured repo name",
    )
    edit_query_parser = edit_subparsers.add_parser(
        "query",
        help=argparse.SUPPRESS,
        description=argparse.SUPPRESS,
    )
    edit_query_parser.add_argument(
        "query",
        metavar="<query>",
        help=argparse.SUPPRESS,
    )
    # Bare `dotman edit <query>` sugar is wired through this hidden subcommand so
    # explicit edit subcommand help stays canonical.
    hide_subparser_from_help(edit_subparsers, "query")

    sync_parser = subparsers.add_parser(
        "sync",
        help="Review file convergence and select auxiliary work",
        description="Inspect file state, approve Proposals, and select Probe or hook work",
    )
    sync_parser.add_argument(
        "scopes", nargs="*", metavar="<repo:package.target>",
        help="Exact tracked scopes (default: all tracked targets)",
    )
    add_dry_run_argument(sync_parser)
    add_full_path_argument(sync_parser)
    add_report_argument(sync_parser)
    add_commit_argument(sync_parser)
    add_no_deps_argument(sync_parser)
    add_run_noop_argument(sync_parser)

    push_parser = subparsers.add_parser(
        "push",
        help="Push tracked changes from repo to live paths",
        description="Push tracked changes from repo to live paths",
    )
    add_dry_run_argument(push_parser)
    add_full_path_argument(push_parser)
    add_report_argument(push_parser)
    push_parser.add_argument(
        "scopes", nargs="*", metavar="<repo:package.target>",
        help="Exact tracked scopes (default: all tracked targets)",
    )
    add_no_deps_argument(push_parser)
    add_run_noop_argument(push_parser)

    pull_parser = subparsers.add_parser(
        "pull",
        help="Pull live changes back into the repo",
        description="Pull live changes back into the repo",
    )
    add_dry_run_argument(pull_parser)
    add_full_path_argument(pull_parser)
    add_report_argument(pull_parser)
    add_commit_argument(pull_parser)
    pull_parser.add_argument(
        "scopes", nargs="*", metavar="<repo:package.target>",
        help="Exact tracked scopes (default: all tracked targets)",
    )
    add_no_deps_argument(pull_parser)
    add_run_noop_argument(pull_parser)

    subparsers.add_parser(
        "doctor",
        help="Diagnose manager config and tracked package state",
        description="Diagnose manager config and tracked package state",
    )

    restore_parser = subparsers.add_parser(
        "restore",
        help="Restore managed live paths from a recorded snapshot",
        description="Restore managed live paths from a recorded snapshot",
    )
    add_dry_run_argument(restore_parser)
    add_full_path_argument(restore_parser)
    add_snapshot_argument(restore_parser, required=False)

    untrack_parser = subparsers.add_parser(
        "untrack",
        help="Remove a tracked package entry from manager state",
        description="Remove a tracked package entry from manager state",
    )
    add_tracked_package_argument(untrack_parser)

    list_parser = subparsers.add_parser(
        "list",
        help="List tracked package state and catalog items",
        description="List tracked package state and catalog items",
    )
    list_subparsers = list_parser.add_subparsers(
        dest="list_command",
        required=True,
        title="list commands",
        metavar="<list-command>",
    )
    list_subparsers.add_parser(
        "tracked",
        help="List tracked packages",
        description="List tracked packages",
    )
    list_subparsers.add_parser(
        "repo",
        help="List configured repos",
        description="List configured repos",
    )
    list_subparsers.add_parser(
        "trackables",
        help="List trackable packages and groups",
        description="List trackable packages and groups",
    )
    list_subparsers.add_parser(
        "vars",
        help="List resolved variables",
        description="List resolved variables",
    )
    list_subparsers.add_parser(
        "snapshots",
        help="List available snapshots",
        description="List available snapshots",
    )
    list_subparsers.add_parser("sync-bases", help="List currently usable Sync Bases")
    reset_parser = subparsers.add_parser("reset", help="Discard exact Sync Base ancestry")
    reset_subparsers = reset_parser.add_subparsers(dest="reset_command", required=True)
    reset_base_parser = reset_subparsers.add_parser("sync-base", help="Immediately reset one exact Sync Base")
    reset_base_parser.add_argument("sync_unit", metavar="<sync-unit>", help="Exact repo-qualified file target or directory child")
    info_parser = subparsers.add_parser(
        "info",
        help="Show detailed information about tracked package state items",
        description="Show detailed information about tracked package state items",
    )
    info_subparsers = info_parser.add_subparsers(
        dest="info_command",
        required=True,
        title="info commands",
        metavar="<info-command>",
    )
    info_base_parser = info_subparsers.add_parser("sync-base", help="Show exact Sync Base applicability without observing drift")
    info_base_parser.add_argument("sync_unit", metavar="<sync-unit>", help="Exact repo-qualified file target or directory child")
    info_tracked_parser = info_subparsers.add_parser(
        "tracked",
        help="Show tracked package details",
        description="Show tracked package details",
    )
    add_package_argument(info_tracked_parser)
    info_trackable_parser = info_subparsers.add_parser(
        "trackable",
        help="Show package or group details with tracked status",
        description="Show package or group details with tracked status",
    )
    add_trackable_query_argument(info_trackable_parser)
    info_var_parser = info_subparsers.add_parser(
        "var",
        help="Show resolved variable details",
        description="Show resolved variable details",
    )
    add_variable_argument(info_var_parser)
    info_snapshot_parser = info_subparsers.add_parser(
        "snapshot",
        help="Show snapshot details",
        description="Show snapshot details",
    )
    add_full_path_argument(info_snapshot_parser)
    add_snapshot_argument(info_snapshot_parser)
    capture_parser = subparsers.add_parser(
        "capture",
        help="Patch review content back into repo source",
        description="Patch review content back into repo source",
    )
    capture_subparsers = capture_parser.add_subparsers(
        dest="capture_command",
        required=True,
        title="capture commands",
        metavar="<capture-command>",
    )
    capture_patch_parser = capture_subparsers.add_parser(
        "patch",
        help="Patch a rendered/template source file from review content",
        description="Patch a rendered/template source file from review content",
    )
    capture_patch_parser.add_argument(
        "--repo-path",
        required=True,
        metavar="<repo-path>",
        help="Path to the repo source file",
    )
    capture_patch_parser.add_argument(
        "--render",
        required=True,
        metavar="<render-command>",
        help="Forward render to verify against review live bytes, for example jinja or a stdout-producing command",
    )
    capture_patch_parser.add_argument(
        "--review-repo-path",
        metavar="<review-repo-path>",
        help="Prepared repo-side review file path",
    )
    capture_patch_parser.add_argument(
        "--review-live-path",
        metavar="<review-live-path>",
        help="Prepared live-side review file path",
    )
    add_jinja_context_arguments(capture_patch_parser)

    reconcile_parser = subparsers.add_parser(
        "reconcile",
        help="Re-run a reconcile helper subcommand",
        description="Re-run a reconcile helper subcommand",
    )
    reconcile_subparsers = reconcile_parser.add_subparsers(
        dest="reconcile_helper",
        required=True,
        title="reconcile commands",
        metavar="<reconcile-command>",
    )

    reconcile_editor_parser = reconcile_subparsers.add_parser(
        "editor",
        help="Open repo and live files in an editor for reconcile review",
        description="Open repo and live files in an editor for reconcile review",
    )
    reconcile_editor_parser.add_argument(
        "--repo-path",
        required=True,
        metavar="<repo-path>",
        help="Path to the repo copy of the target file",
    )
    reconcile_editor_parser.add_argument(
        "--live-path",
        required=True,
        metavar="<live-path>",
        help="Path to the live copy of the target file",
    )
    reconcile_editor_parser.add_argument(
        "--review-repo-path",
        metavar="<review-repo-path>",
        help="Optional prepared repo-side review file path",
    )
    reconcile_editor_parser.add_argument(
        "--review-live-path",
        metavar="<review-live-path>",
        help="Optional prepared live-side review file path",
    )
    reconcile_editor_parser.add_argument(
        "--additional-source",
        action="append",
        default=[],
        metavar="<source-path>",
        help="Additional repo source file to include in transactional reconcile editing",
    )
    reconcile_editor_parser.add_argument(
        "--editor",
        metavar="<editor-command>",
        help="Editor command to run instead of the default editor",
    )

    reconcile_jinja_parser = reconcile_subparsers.add_parser(
        "jinja",
        help="Reconcile a Jinja source with its recursive template dependencies",
        description="Reconcile a Jinja source with its recursive template dependencies",
    )
    reconcile_jinja_parser.add_argument(
        "--repo-path",
        required=True,
        metavar="<repo-path>",
        help="Path to the repo copy of the target file",
    )
    reconcile_jinja_parser.add_argument(
        "--live-path",
        required=True,
        metavar="<live-path>",
        help="Path to the live copy of the target file",
    )
    reconcile_jinja_parser.add_argument(
        "--review-repo-path",
        metavar="<review-repo-path>",
        help="Optional prepared repo-side review file path",
    )
    reconcile_jinja_parser.add_argument(
        "--review-live-path",
        metavar="<review-live-path>",
        help="Optional prepared live-side review file path",
    )
    reconcile_jinja_parser.add_argument(
        "--editor",
        metavar="<editor-command>",
        help="Editor command to run instead of the default editor",
    )

    render_parser = subparsers.add_parser(
        "render",
        help="Render built-in template helpers",
        description="Render built-in template helpers",
    )
    render_subparsers = render_parser.add_subparsers(
        dest="render_command",
        required=True,
        title="render commands",
        metavar="<render-command>",
    )
    render_jinja_parser = render_subparsers.add_parser(
        "jinja",
        help="Render a file with the built-in Jinja renderer",
        description="Render a file with the built-in Jinja renderer",
    )
    render_jinja_parser.add_argument(
        "source_path",
        metavar="<source-path>",
        help="Path to the Jinja source file",
    )
    add_jinja_context_arguments(render_jinja_parser)

    rewrite_parser = subparsers.add_parser(
        "rewrite",
        help="Run a standalone structure-agnostic text rewrite",
        description="Run a standalone structure-agnostic text rewrite without repository configuration",
    )
    rewrite_subparsers = rewrite_parser.add_subparsers(
        dest="rewrite_name", required=True, title="rewrites", metavar="<rewrite>"
    )
    rewrite_home_parser = rewrite_subparsers.add_parser(
        "home",
        help="Rewrite active home path fragments",
        description="Rewrite between active home path fragments and '~'",
    )
    rewrite_home_subparsers = rewrite_home_parser.add_subparsers(
        dest="rewrite_action", required=True, title="actions", metavar="<action>"
    )
    for action, help_text in (
        ("expand", "Expand standalone '~' fragments to the active $HOME"),
        ("collapse", "Collapse active $HOME fragments to '~'"),
    ):
        action_parser = rewrite_home_subparsers.add_parser(
            action,
            help=help_text,
            description=f"{help_text}; read strict UTF-8 from INPUT or stdin and write to stdout",
        )
        action_parser.add_argument(
            "input_path",
            nargs="?",
            metavar="<input>",
            help="UTF-8 input file; omit or use '-' to read stdin",
        )

    transform_parser = subparsers.add_parser(
        "transform",
        help="Run a standalone structured transform",
        description="Run a standalone structured transform without repository configuration",
    )
    transform_subparsers = transform_parser.add_subparsers(
        dest="transform_format", required=True, title="formats", metavar="<format>"
    )
    json_parser = transform_subparsers.add_parser(
        "json", help="Transform JSON objects", description="Clean up or merge JSON objects"
    )
    from dotman.transforms.cli import configure_parser
    from dotman.transforms.json import JsonTransformEngine

    json_engine = JsonTransformEngine()
    configure_parser(json_parser, json_engine)
    json_parser.set_defaults(transform_parser=json_parser, transform_engine=json_engine)

    toml_parser = transform_subparsers.add_parser(
        "toml", help="Transform TOML documents", description="Clean up or merge TOML documents"
    )
    from dotman.transforms.toml import TomlTransformEngine

    toml_engine = TomlTransformEngine()
    configure_parser(toml_parser, toml_engine)
    toml_parser.set_defaults(transform_parser=toml_parser, transform_engine=toml_engine)

    plist_parser = transform_subparsers.add_parser(
        "plist", help="Transform plist dictionaries", description="Clean up or merge plist dictionaries"
    )
    from dotman.transforms.plist import PlistTransformEngine

    plist_engine = PlistTransformEngine()
    configure_parser(plist_parser, plist_engine)
    plist_parser.set_defaults(transform_parser=plist_parser, transform_engine=plist_engine)

    xml_parser = transform_subparsers.add_parser(
        "xml", help="Transform XML trees", description="Clean up or merge XML trees"
    )
    from dotman.transforms.xml import XmlTransformEngine

    xml_engine = XmlTransformEngine()
    configure_parser(xml_parser, xml_engine)
    xml_parser.set_defaults(transform_parser=xml_parser, transform_engine=xml_engine)

    yaml_parser = transform_subparsers.add_parser(
        "yaml", help="Transform YAML mappings", description="Clean up or merge YAML mappings"
    )
    from dotman.transforms.yaml import YamlTransformEngine

    yaml_engine = YamlTransformEngine()
    configure_parser(yaml_parser, yaml_engine)
    yaml_parser.set_defaults(transform_parser=yaml_parser, transform_engine=yaml_engine)
    return parser
