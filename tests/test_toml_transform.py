from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys
import tomllib

import pytest
from tomlkit.toml_document import TOMLDocument

from dotman.transforms import toml as MODULE
from dotman.transforms.framework import SelectorAction, emit_transform_output

REPO_ROOT = Path(__file__).resolve().parents[1]


# File-level drivers: compose the engine's output builders with the shared
# emitter so tests can pass parsed selectors without going through argv.
def write_document_if_changed(
    path: Path | None,
    doc: TOMLDocument,
    mode_reference_path: Path,
    compare_path: Path | None = None,
    stdout: bool = False,
) -> None:
    emit_transform_output(
        path,
        MODULE.build_document_output(
            doc, mode_reference_path=mode_reference_path, line_ending="\n", compare_path=compare_path
        ),
        stdout=stdout,
    )


def strip_keys(
    base_path: Path,
    output_path: Path | None,
    stripped_key_paths: list[tuple[str, ...]],
    stripped_table_regexes: list[re.Pattern[str]],
    compare_path: Path | None = None,
    stdout: bool = False,
) -> None:
    emit_transform_output(
        output_path,
        MODULE.build_stripped_document_output(
            base_path, stripped_key_paths, stripped_table_regexes, compare_path=compare_path
        ),
        stdout=stdout,
    )


def merge_with_selector_action(
    selector_action: SelectorAction,
    base_path: Path,
    output_path: Path | None,
    overlay_path: Path,
    key_paths,
    table_regexes: list[re.Pattern[str]],
    compare_path: Path | None = None,
    stdout: bool = False,
) -> None:
    emit_transform_output(
        output_path,
        MODULE.build_merged_document_output(
            base_path, overlay_path, selector_action, list(key_paths), table_regexes, compare_path=compare_path
        ),
        stdout=stdout,
    )


def merge_keys(*args, **kwargs) -> None:
    merge_with_selector_action(SelectorAction.RETAIN, *args, **kwargs)


def merge_keys_except_stripped(*args, **kwargs) -> None:
    merge_with_selector_action(SelectorAction.REMOVE, *args, **kwargs)


def test_toml_engine_declares_typed_selectors() -> None:
    selector_specs = {spec.name: spec for spec in MODULE.TomlTransformEngine.selector_specs()}

    assert selector_specs["key"].prefix == "exact"
    assert selector_specs["table_regex"].prefix == "re"


def test_main_accepts_typed_selector_flags(tmp_path: Path) -> None:
    live_path = tmp_path / "live.toml"
    repo_path = tmp_path / "repo.toml"
    output_path = tmp_path / "output.toml"

    live_path.write_text(
        """approval_policy = "on-request"
model = "gpt-5.4"

[mcp_servers.playwright.env]
PLAYWRIGHT_MCP_EXTENSION_TOKEN = "secret"
""",
        encoding="utf-8",
    )
    repo_path.write_text(
        """approval_policy = "on-request"

[mcp_servers.context7]
command = "npx"
""",
        encoding="utf-8",
    )

    exit_code = MODULE.main(
        [
            str(live_path),
            str(output_path),
            "--mode",
            "merge",
            "--overlay-file",
            str(repo_path),
            "--selector-type",
            "retain",
            "--selectors",
            "model",
            "re:^mcp_servers\\.playwright\\.env$",
        ]
    )

    assert exit_code == 0
    merged_doc = MODULE.load_document(output_path)[0]
    assert merged_doc["model"] == "gpt-5.4"
    assert (
        merged_doc["mcp_servers"]["playwright"]["env"]["PLAYWRIGHT_MCP_EXTENSION_TOKEN"]
        == "secret"
    )


def test_cleanup_does_not_reuse_compare_file_with_stale_comments(tmp_path: Path) -> None:
    live_path = tmp_path / "live.toml"
    repo_path = tmp_path / "repo.toml"
    output_path = tmp_path / "output.toml"

    live_path.write_text(
        'model = "local-only"\n\n[features]\nhooks = true\n',
        encoding="utf-8",
    )
    repo_path.write_text(
        '# model = "old-local-value"\n\n[features]\nhooks = true\n',
        encoding="utf-8",
    )

    exit_code = MODULE.main(
        [
            str(live_path),
            str(output_path),
            "--mode",
            "cleanup",
            "--compare-file",
            str(repo_path),
            "--selector-type",
            "remove",
            "--selectors",
            "model",
        ]
    )

    assert exit_code == 0
    assert '# model = "old-local-value"' not in output_path.read_text(encoding="utf-8")
    assert MODULE.load_document(output_path)[0].unwrap() == {"features": {"hooks": True}}


def test_cleanup_does_not_reuse_stale_array_of_table_header_comment(
    tmp_path: Path,
) -> None:
    live_path = tmp_path / "live.toml"
    repo_path = tmp_path / "repo.toml"
    output_path = tmp_path / "output.toml"

    live_path.write_text(
        '[[services]] # current\nname = "api"\n',
        encoding="utf-8",
    )
    repo_path.write_text(
        '[[services]] # stale\nname = "api"\n',
        encoding="utf-8",
    )

    exit_code = MODULE.main(
        [
            str(live_path),
            str(output_path),
            "--mode",
            "cleanup",
            "--compare-file",
            str(repo_path),
            "--selectors",
            "services",
        ]
    )

    assert exit_code == 0
    assert "# current" in output_path.read_text(encoding="utf-8")
    assert "# stale" not in output_path.read_text(encoding="utf-8")


def test_cleanup_does_not_reuse_comment_attached_to_different_key(
    tmp_path: Path,
) -> None:
    live_path = tmp_path / "live.toml"
    repo_path = tmp_path / "repo.toml"
    output_path = tmp_path / "output.toml"
    live_text = "a = 1\nb = 2 # note\n"

    live_path.write_text(live_text, encoding="utf-8")
    repo_path.write_text("a = 1 # note\nb = 2\n", encoding="utf-8")

    exit_code = MODULE.main(
        [
            str(live_path),
            str(output_path),
            "--mode",
            "cleanup",
            "--compare-file",
            str(repo_path),
            "--selectors",
            "a",
            "b",
        ]
    )

    assert exit_code == 0
    assert output_path.read_text(encoding="utf-8") == live_text


def test_cleanup_does_not_reuse_stale_multiline_array_comment(
    tmp_path: Path,
) -> None:
    live_path = tmp_path / "live.toml"
    repo_path = tmp_path / "repo.toml"
    output_path = tmp_path / "output.toml"
    live_text = "values = [\n  1, # current\n  2,\n]\n"

    live_path.write_text(live_text, encoding="utf-8")
    repo_path.write_text(
        "values = [\n  1, # stale\n  2,\n]\n",
        encoding="utf-8",
    )

    exit_code = MODULE.main(
        [
            str(live_path),
            str(output_path),
            "--mode",
            "cleanup",
            "--compare-file",
            str(repo_path),
            "--selectors",
            "values",
        ]
    )

    assert exit_code == 0
    assert output_path.read_text(encoding="utf-8") == live_text


def test_cleanup_compare_treats_hash_inside_string_as_value_content(
    tmp_path: Path,
) -> None:
    live_path = tmp_path / "live.toml"
    compare_path = tmp_path / "compare.toml"
    output_path = tmp_path / "output.toml"
    compare_text = "fragment = 'section#details'\n"

    live_path.write_text('fragment = "section#details"\n', encoding="utf-8")
    compare_path.write_text(compare_text, encoding="utf-8")

    exit_code = MODULE.main(
        [
            str(live_path),
            str(output_path),
            "--mode",
            "cleanup",
            "--compare-file",
            str(compare_path),
            "--selectors",
            "fragment",
        ]
    )

    assert exit_code == 0
    assert output_path.read_text(encoding="utf-8") == compare_text


def test_merge_does_not_reuse_stale_live_comment(tmp_path: Path) -> None:
    live_path = tmp_path / "live.toml"
    repo_path = tmp_path / "repo.toml"
    output_path = tmp_path / "output.toml"

    live_path.write_text(
        'model = "local-only"\n\n# stale\nmanaged = true\n',
        encoding="utf-8",
    )
    repo_path.write_text("# current\nmanaged = true\n", encoding="utf-8")

    exit_code = MODULE.main(
        [
            str(live_path),
            str(output_path),
            "--mode",
            "merge",
            "--overlay-file",
            str(repo_path),
            "--compare-file",
            str(live_path),
            "--selectors",
            "model",
        ]
    )

    assert exit_code == 0
    output = output_path.read_text(encoding="utf-8")
    assert "# current" in output
    assert "# stale" not in output


def test_merge_does_not_drop_leading_comment_from_overlay_table(
    tmp_path: Path,
) -> None:
    live_path = tmp_path / "live.toml"
    repo_path = tmp_path / "repo.toml"
    output_path = tmp_path / "output.toml"

    live_path.write_text(
        'local = "yes"\n\n[managed]\nenabled = true\n',
        encoding="utf-8",
    )
    repo_path.write_text(
        "# current\n[managed]\nenabled = true\n",
        encoding="utf-8",
    )

    exit_code = MODULE.main(
        [
            str(live_path),
            str(output_path),
            "--mode",
            "merge",
            "--overlay-file",
            str(repo_path),
            "--compare-file",
            str(live_path),
            "--selectors",
            "local",
        ]
    )

    assert exit_code == 0
    assert "# current\n[managed]" in output_path.read_text(encoding="utf-8")


def test_parse_key_paths_and_table_regexes() -> None:
    key_paths = MODULE.parse_key_paths(["model", "model_reasoning_effort"])
    table_regexes = MODULE.compile_table_regexes(
        ["^projects\\.", "^mcp_servers\\.playwright\\.env$"]
    )

    assert key_paths == [("model",), ("model_reasoning_effort",)]
    assert [pattern.pattern for pattern in table_regexes] == [
        "^projects\\.",
        "^mcp_servers\\.playwright\\.env$",
    ]


def test_retain_matchers_in_strip_mode_keeps_only_selected_content(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo.toml"
    output_path = tmp_path / "output.toml"

    repo_path.write_text(
        """approval_policy = "on-request"
model = "gpt-5.4"

[mcp_servers.context7]
command = "npx"

[projects."/tmp/example"]
trust_level = "trusted"
""",
        encoding="utf-8",
    )

    retained_doc = MODULE.build_document_with_retained_matchers(
        MODULE.load_document(repo_path)[0],
        {("model",)},
        [MODULE.re.compile(r"^projects\.")],
    )
    write_document_if_changed(output_path, retained_doc, mode_reference_path=repo_path)

    output = output_path.read_text(encoding="utf-8")
    assert 'model = "gpt-5.4"' in output
    assert "[projects" in output
    assert "approval_policy" not in output
    assert "mcp_servers" not in output


def test_regex_removes_matching_nested_keys_without_removing_siblings(tmp_path: Path) -> None:
    source_path = tmp_path / "source.toml"
    output_path = tmp_path / "output.toml"

    source_path.write_text(
        """[widget.media]
enabled = true
volume = 75

[widget.clock]
enabled = false
format = "HH:mm"
""",
        encoding="utf-8",
    )

    stripped_doc = MODULE.build_document_with_stripped_matchers(
        MODULE.load_document(source_path)[0],
        [],
        [MODULE.re.compile(r"^widget\.[^.]+\.enabled$")],
    )
    write_document_if_changed(
        output_path,
        stripped_doc,
        mode_reference_path=source_path,
    )

    output_doc = MODULE.load_document(output_path)[0]
    assert "enabled" not in output_doc["widget"]["media"]
    assert output_doc["widget"]["media"]["volume"] == 75
    assert "enabled" not in output_doc["widget"]["clock"]
    assert output_doc["widget"]["clock"]["format"] == "HH:mm"


def test_regex_retain_keeps_matching_nested_keys_only(tmp_path: Path) -> None:
    source_path = tmp_path / "source.toml"

    source_path.write_text(
        """[widget.media]
enabled = true
volume = 75

[widget.clock]
enabled = false
format = "HH:mm"
""",
        encoding="utf-8",
    )

    retained_doc = MODULE.build_document_with_retained_matchers(
        MODULE.load_document(source_path)[0],
        [],
        [MODULE.re.compile(r"^widget\.[^.]+\.enabled$")],
    )

    assert retained_doc.unwrap() == {
        "widget": {
            "media": {"enabled": True},
            "clock": {"enabled": False},
        }
    }


def test_strip_table_preserves_following_commented_tables(tmp_path: Path) -> None:
    live_path = tmp_path / "live.toml"
    output_path = tmp_path / "output.toml"

    live_path.write_text(
        """[mcp_servers.context7]
command = "npx"

[mcp_servers.node_repl]
command = "node_repl"

[mcp_servers.node_repl.env]
CODEX_HOME = "/tmp/codex"

# [mcp_servers.chrome-devtools]
# command = "npx"
# args = ["chrome-devtools-mcp@latest"]

[features]
hooks = true
""",
        encoding="utf-8",
    )

    strip_keys(
        live_path,
        output_path,
        [("mcp_servers", "node_repl")],
        [],
    )

    output = output_path.read_text(encoding="utf-8")
    assert "[mcp_servers]\n" not in output
    assert "[mcp_servers.node_repl]" not in output
    assert "CODEX_HOME" not in output
    assert "# [mcp_servers.chrome-devtools]" in output
    assert '# args = ["chrome-devtools-mcp@latest"]' in output
    assert "[features]" in output


def test_strip_parent_table_preserves_blank_separated_tail_comments(tmp_path: Path) -> None:
    live_path = tmp_path / "live.toml"
    output_path = tmp_path / "output.toml"

    live_path.write_text(
        """[mcp_servers.node_repl]
command = "node_repl"

[mcp_servers.node_repl.env]
CODEX_HOME = "/tmp/codex"

# Keep this note even when the parent table is removed.

[features]
hooks = true
""",
        encoding="utf-8",
    )

    strip_keys(
        live_path,
        output_path,
        [("mcp_servers",)],
        [],
    )

    output = output_path.read_text(encoding="utf-8")
    assert "[mcp_servers" not in output
    assert "# Keep this note even when the parent table is removed." in output
    assert "[features]" in output


def test_strip_table_removes_attached_tail_comments_without_blank_separator(tmp_path: Path) -> None:
    live_path = tmp_path / "live.toml"
    output_path = tmp_path / "output.toml"

    live_path.write_text(
        """[mcp_servers.node_repl]
command = "node_repl"
# Attached to node_repl.

[features]
hooks = true
""",
        encoding="utf-8",
    )

    strip_keys(
        live_path,
        output_path,
        [("mcp_servers", "node_repl")],
        [],
    )

    output = output_path.read_text(encoding="utf-8")
    assert "Attached to node_repl" not in output
    assert "[features]" in output


def test_write_document_with_compare_file_skips_rewrite_for_matching_output(
    tmp_path: Path,
 ) -> None:
    repo_path = tmp_path / "repo.toml"
    output_path = tmp_path / "output.toml"

    repo_path.write_text('model = "gpt-5.4"\n', encoding="utf-8")
    retained_doc = MODULE.load_document(repo_path)[0]
    output_path.write_text(retained_doc.as_string(), encoding="utf-8")
    os.utime(output_path, ns=(1, 1))

    write_document_if_changed(
        output_path,
        retained_doc,
        mode_reference_path=repo_path,
        compare_path=output_path,
    )

    assert output_path.stat().st_mtime_ns == 1


def test_write_document_with_compare_file_skips_rewrite_for_semantic_match(
    tmp_path: Path,
) -> None:
    repo_path = tmp_path / "repo.toml"
    compare_path = tmp_path / "compare.toml"
    output_path = tmp_path / "output.toml"

    repo_path.write_text('model_provider = "openai_http"\n', encoding="utf-8")
    compare_path.write_text(
        'model = "gpt-5.4"\nmodel_provider = "openai_http"\n',
        encoding="utf-8",
    )
    output_path.write_text("stale\n", encoding="utf-8")
    os.utime(output_path, ns=(1, 1))

    merged_doc = MODULE.load_document(repo_path)[0]
    merged_doc["model"] = "gpt-5.4"

    write_document_if_changed(
        output_path,
        merged_doc,
        mode_reference_path=repo_path,
        compare_path=compare_path,
    )

    assert output_path.stat().st_mtime_ns != 1
    assert output_path.read_text(encoding="utf-8") == compare_path.read_text(encoding="utf-8")


def test_write_document_with_compare_file_reuses_existing_text_in_stdout_mode(
    tmp_path: Path,
    capsys,
) -> None:
    repo_path = tmp_path / "repo.toml"
    compare_path = tmp_path / "compare.toml"

    repo_path.write_text(
        '# keep me\nmodel_provider = "openai_http"\n',
        encoding="utf-8",
    )
    compare_path.write_text(
        'model = "gpt-5.4"\n# keep me\nmodel_provider = "openai_http"\n',
        encoding="utf-8",
    )

    merged_doc = MODULE.load_document(repo_path)[0]
    merged_doc["model"] = "gpt-5.4"

    write_document_if_changed(
        None,
        merged_doc,
        mode_reference_path=repo_path,
        compare_path=compare_path,
        stdout=True,
    )

    assert capsys.readouterr().out == compare_path.read_text(encoding="utf-8")


def test_write_document_without_compare_file_rewrites_matching_output(
    tmp_path: Path,
 ) -> None:
    repo_path = tmp_path / "repo.toml"
    output_path = tmp_path / "output.toml"

    repo_path.write_text('model = "gpt-5.4"\n', encoding="utf-8")
    retained_doc = MODULE.load_document(repo_path)[0]
    output_path.write_text(retained_doc.as_string(), encoding="utf-8")
    os.utime(output_path, ns=(1, 1))

    write_document_if_changed(
        output_path,
        retained_doc,
        mode_reference_path=repo_path,
    )

    assert output_path.stat().st_mtime_ns != 1


def test_merge_preserves_selected_live_keys_and_reapplies_repo_content(tmp_path: Path) -> None:
    live_path = tmp_path / "live.toml"
    repo_path = tmp_path / "repo.toml"
    output_path = tmp_path / "output.toml"

    live_path.write_text(
        """approval_policy = "on-request"
model = "gpt-5.4"

[mcp_servers.context7]
command = "npx"

[mcp_servers.playwright]
command = "npx"

[mcp_servers.playwright.env]
PLAYWRIGHT_MCP_EXTENSION_TOKEN = "secret"

[projects."/tmp/example"]
trust_level = "trusted"
""",
        encoding="utf-8",
    )
    repo_path.write_text(
        """approval_policy = "on-request"
web_search = "repo"

[mcp_servers.context7]
command = "npx"
""",
        encoding="utf-8",
    )

    merge_keys(
        live_path,
        output_path,
        repo_path,
        {
            ("model",),
            ("mcp_servers", "playwright", "env", "PLAYWRIGHT_MCP_EXTENSION_TOKEN"),
        },
        [],
    )

    merged_doc = MODULE.load_document(output_path)[0]

    assert merged_doc["model"] == "gpt-5.4"
    assert merged_doc["web_search"] == "repo"
    assert "projects" not in merged_doc
    assert "playwright" in merged_doc["mcp_servers"]
    assert "command" not in merged_doc["mcp_servers"]["playwright"]
    assert (
        merged_doc["mcp_servers"]["playwright"]["env"]["PLAYWRIGHT_MCP_EXTENSION_TOKEN"]
        == "secret"
    )


def test_merge_does_not_restore_omitted_live_table_when_overlay_keeps_a_comment(tmp_path: Path) -> None:
    live_path = tmp_path / "live.toml"
    repo_path = tmp_path / "repo.toml"
    output_path = tmp_path / "output.toml"

    live_path.write_text(
        """[mcp_servers.context7]
command = "npx"

[mcp_servers.node_repl]
command = "node_repl"
""",
        encoding="utf-8",
    )
    repo_path.write_text(
        """# [mcp_servers.context7]
# command = "npx"

[features]
hooks = true
""",
        encoding="utf-8",
    )

    merge_keys(
        live_path,
        output_path,
        repo_path,
        {("mcp_servers", "node_repl")},
        [],
    )

    merged_doc = MODULE.load_document(output_path)[0]

    assert "context7" not in merged_doc["mcp_servers"]
    assert merged_doc["mcp_servers"]["node_repl"]["command"] == "node_repl"
    assert "# [mcp_servers.context7]" in output_path.read_text(encoding="utf-8")


def test_merge_restores_regex_selected_tables(tmp_path: Path) -> None:
    live_path = tmp_path / "live.toml"
    repo_path = tmp_path / "repo.toml"
    output_path = tmp_path / "output.toml"

    live_path.write_text(
        """approval_policy = "on-request"

[mcp_servers.context7]
command = "npx"

[mcp_servers.playwright]
command = "npx"

[mcp_servers.playwright.env]
PLAYWRIGHT_MCP_EXTENSION_TOKEN = "secret"
""",
        encoding="utf-8",
    )
    repo_path.write_text(
        """approval_policy = "on-request"

[mcp_servers.context7]
command = "npx"
""",
        encoding="utf-8",
    )

    merge_keys(
        live_path,
        output_path,
        repo_path,
        set(),
        [MODULE.re.compile(r"^mcp_servers\.playwright\.env$")],
    )

    merged_doc = MODULE.load_document(output_path)[0]

    assert "context7" in merged_doc["mcp_servers"]
    assert "playwright" in merged_doc["mcp_servers"]
    assert "command" not in merged_doc["mcp_servers"]["playwright"]
    assert (
        merged_doc["mcp_servers"]["playwright"]["env"]["PLAYWRIGHT_MCP_EXTENSION_TOKEN"]
        == "secret"
    )


def test_merge_with_compare_file_reuses_semantically_matching_live_bytes(tmp_path: Path) -> None:
    live_path = tmp_path / "live.toml"
    repo_path = tmp_path / "repo.toml"
    output_path = tmp_path / "output.toml"

    live_path.write_text(
        """approval_policy = "on-request"
sandbox_mode = "workspace-write"
web_search = "live"
personality = "pragmatic"
model = "gpt-5.4"
model_reasoning_effort = "high"

model_provider = "openai_http"

[model_providers.openai_http]
name = "OpenAI HTTP only"

[projects."/tmp/example"]
trust_level = "trusted"
""",
        encoding="utf-8",
    )
    repo_path.write_text(
        """approval_policy = "on-request"
sandbox_mode = "workspace-write"
web_search = "live"
personality = "pragmatic"

model_provider = "openai_http"

[model_providers.openai_http]
name = "OpenAI HTTP only"
""",
        encoding="utf-8",
    )

    merge_keys(
        live_path,
        output_path,
        repo_path,
        {
            ("model",),
            ("model_reasoning_effort",),
        },
        [MODULE.re.compile(r"^projects\.")],
        compare_path=live_path,
    )

    assert output_path.read_text(encoding="utf-8") == live_path.read_text(encoding="utf-8")


def test_merge_preserves_top_level_leading_comments_without_compare_file(
    tmp_path: Path,
) -> None:
    live_path = tmp_path / "live.toml"
    repo_path = tmp_path / "repo.toml"
    output_path = tmp_path / "output.toml"

    live_text = """approval_policy = "on-request"
sandbox_mode = "workspace-write"
web_search = "live"
personality = "pragmatic"
model = "gpt-5.4"
model_reasoning_effort = "high"

# model_provider = "openai_http"

[model_providers.openai_http]
name = "OpenAI HTTP only"

[projects."/tmp/example"]
trust_level = "trusted"
"""
    repo_path.write_text(
        """approval_policy = "on-request"
sandbox_mode = "workspace-write"
web_search = "live"
personality = "pragmatic"

# model_provider = "openai_http"

[model_providers.openai_http]
name = "OpenAI HTTP only"
""",
        encoding="utf-8",
    )
    live_path.write_text(live_text, encoding="utf-8")

    merge_keys(
        live_path,
        output_path,
        repo_path,
        {
            ("model",),
            ("model_reasoning_effort",),
        },
        [MODULE.re.compile(r"^projects\.")],
    )

    assert output_path.read_text(encoding="utf-8") == live_text


def test_merge_keeps_independent_comment_at_overlay_position_when_live_key_inserted(
    tmp_path: Path,
) -> None:
    live_path = tmp_path / "live.toml"
    repo_path = tmp_path / "repo.toml"
    output_path = tmp_path / "output.toml"

    live_path.write_text(
        """approval_policy = "on-request"

# model_provider = "openai_http"

notify = ["turn-ended"]

[model_providers.openai_http]
name = "OpenAI HTTP only"
""",
        encoding="utf-8",
    )
    repo_path.write_text(
        """approval_policy = "on-request"

# model_provider = "openai_http"

[model_providers.openai_http]
name = "OpenAI HTTP only"
""",
        encoding="utf-8",
    )

    merge_keys(
        live_path,
        output_path,
        repo_path,
        {("notify",)},
        [],
    )

    assert output_path.read_text(encoding="utf-8") == """approval_policy = "on-request"
notify = ["turn-ended"]

# model_provider = "openai_http"

[model_providers.openai_http]
name = "OpenAI HTTP only"
"""


def test_merge_does_not_add_blank_after_attached_table_leading_comment(
    tmp_path: Path,
) -> None:
    live_path = tmp_path / "live.toml"
    repo_path = tmp_path / "repo.toml"
    output_path = tmp_path / "output.toml"

    live_path.write_text(
        """model = "live"

[profiles.obsidian]
model = "gpt-5.4-mini"

# Extra settings that only apply when `sandbox = "workspace-write"`.
[sandbox_workspace_write]
network_access = true
model = "live"
""",
        encoding="utf-8",
    )
    repo_path.write_text(
        """[profiles.obsidian]
model = "gpt-5.4-mini"

# Extra settings that only apply when `sandbox = "workspace-write"`.
[sandbox_workspace_write]
network_access = true
""",
        encoding="utf-8",
    )

    merge_keys(
        live_path,
        output_path,
        repo_path,
        {("model",)},
        [],
    )

    assert output_path.read_text(encoding="utf-8") == """model = "live"

[profiles.obsidian]
model = "gpt-5.4-mini"

# Extra settings that only apply when `sandbox = "workspace-write"`.
[sandbox_workspace_write]
network_access = true
"""


def test_merge_dedupes_independent_comments_with_different_blank_padding(
    tmp_path: Path,
) -> None:
    live_path = tmp_path / "live.toml"
    repo_path = tmp_path / "repo.toml"
    output_path = tmp_path / "output.toml"

    live_path.write_text(
        """[mcp_servers.context7]
command = "npx"

[mcp_servers.node_repl]
command = "node_repl"

[mcp_servers.node_repl.env]
CODEX_HOME = "/tmp/codex"

# [mcp_servers.chrome-devtools]
# command = "npx"


[notice]
hide_full_access_warning = true
""",
        encoding="utf-8",
    )
    repo_path.write_text(
        """[mcp_servers.context7]
command = "npx"

# [mcp_servers.chrome-devtools]
# command = "npx"

[features]
hooks = true
""",
        encoding="utf-8",
    )

    merge_keys(
        live_path,
        output_path,
        repo_path,
        {
            ("mcp_servers", "node_repl"),
            ("notice",),
        },
        [],
    )

    output = output_path.read_text(encoding="utf-8")
    assert output.count("# [mcp_servers.chrome-devtools]") == 1
    assert output.index("# [mcp_servers.chrome-devtools]") < output.index("[features]")
    assert output.index("[notice]") < output.index("# [mcp_servers.chrome-devtools]")


def test_merge_treats_blank_lines_as_single_section_separators(
    tmp_path: Path,
) -> None:
    live_path = tmp_path / "live.toml"
    repo_path = tmp_path / "repo.toml"
    output_path = tmp_path / "output.toml"

    live_path.write_text(
        """[tui]
status_line = ["current-dir"]

[tui.model_availability_nux]
"gpt-5.5" = 4

[projects."/tmp/example"]
trust_level = "trusted"
""",
        encoding="utf-8",
    )
    repo_path.write_text(
        """[tui]
status_line = ["current-dir"]

[plugins."browser@openai-bundled"]
enabled = true
""",
        encoding="utf-8",
    )

    merge_keys(
        live_path,
        output_path,
        repo_path,
        set(),
        [
            MODULE.re.compile(r"^projects\."),
            MODULE.re.compile(r"^tui\.model_availability_nux$"),
        ],
    )

    assert output_path.read_text(encoding="utf-8") == """[tui]
status_line = ["current-dir"]

[tui.model_availability_nux]
"gpt-5.5" = 4

[projects."/tmp/example"]
trust_level = "trusted"

[plugins."browser@openai-bundled"]
enabled = true
"""


def test_merge_skips_missing_preserved_paths(tmp_path: Path) -> None:
    live_path = tmp_path / "live.toml"
    repo_path = tmp_path / "repo.toml"
    output_path = tmp_path / "output.toml"

    live_path.write_text(
        """approval_policy = "on-request"
""",
        encoding="utf-8",
    )
    repo_path.write_text(
        """approval_policy = "on-request"
""",
        encoding="utf-8",
    )

    merge_keys(
        live_path,
        output_path,
        repo_path,
        {("mcp_servers", "playwright", "env", "PLAYWRIGHT_MCP_EXTENSION_TOKEN")},
        [],
    )

    merged_doc = MODULE.load_document(output_path)[0]

    assert merged_doc["approval_policy"] == "on-request"
    assert "mcp_servers" not in merged_doc


def test_merge_remove_preserves_unselected_live_keys_and_reapplies_repo_content(
    tmp_path: Path,
) -> None:
    live_path = tmp_path / "live.toml"
    repo_path = tmp_path / "repo.toml"
    output_path = tmp_path / "output.toml"

    live_path.write_text(
        """approval_policy = "live"
keep_local = "noise"
model = "live-model"

[mcp_servers.context7]
command = "live-context7"

[mcp_servers.playwright]
command = "live-playwright"

[projects."/tmp/example"]
trust_level = "trusted"
""",
        encoding="utf-8",
    )
    repo_path.write_text(
        """approval_policy = "on-request"
model = "repo-model"

[mcp_servers.context7]
command = "repo-context7"

[projects."/tmp/example"]
trust_level = "repo"
""",
        encoding="utf-8",
    )

    merge_keys_except_stripped(
        live_path,
        output_path,
        repo_path,
        [("model",)],
        [MODULE.re.compile(r"^projects\.")],
    )

    merged_doc = MODULE.load_document(output_path)[0]

    assert merged_doc["approval_policy"] == "on-request"
    assert merged_doc["keep_local"] == "noise"
    assert merged_doc["model"] == "repo-model"
    assert merged_doc["mcp_servers"]["context7"]["command"] == "repo-context7"
    assert merged_doc["mcp_servers"]["playwright"]["command"] == "live-playwright"
    assert merged_doc["projects"]["/tmp/example"]["trust_level"] == "repo"


def test_merge_preserves_overlay_key_order_across_hash_seeds(tmp_path: Path) -> None:
    live_path = tmp_path / "live.toml"
    repo_path = tmp_path / "repo.toml"

    live_path.write_text(
        """approval_policy = "on-request"
model_reasoning_effort = "high"
model = "gpt-5.4"
""",
        encoding="utf-8",
    )
    repo_path.write_text(
        'approval_policy = "on-request"\n',
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
from pathlib import Path

from dotman.transforms import toml as module
from dotman.transforms.framework import SelectorAction, emit_transform_output

repo_path = Path(sys.argv[1])
live_path = Path(sys.argv[1])
repo_path = Path(sys.argv[2])
output_path = Path(sys.argv[3])

emit_transform_output(
    output_path,
    module.build_merged_document_output(
        live_path,
        repo_path,
        SelectorAction.RETAIN,
        list({("model",), ("model_reasoning_effort",)}),
        [],
    ),
)
print(output_path.read_text(encoding="utf-8"))
""",
            str(live_path),
            str(repo_path),
            str(tmp_path / "output.toml"),
        ],
        capture_output=True,
        check=True,
        cwd=REPO_ROOT,
        text=True,
        env={**os.environ, "PYTHONHASHSEED": "1"},
    )

    output = completed.stdout
    assert output.index('model_reasoning_effort = "high"') < output.index('model = "gpt-5.4"')


def run_toml_transform(
    tmp_path: Path,
    base_text: str,
    *selector_args: str,
    overlay_text: str | None = None,
    compare_text: str | None = None,
) -> str:
    """Run the engine CLI and return the output text without newline translation."""
    base_path = tmp_path / "base.toml"
    output_path = tmp_path / "output.toml"
    base_path.write_bytes(base_text.encode("utf-8"))
    argv = [str(base_path), str(output_path)]
    if overlay_text is None:
        argv += ["--mode", "cleanup"]
    else:
        overlay_path = tmp_path / "overlay.toml"
        overlay_path.write_bytes(overlay_text.encode("utf-8"))
        argv += ["--mode", "merge", "--overlay-file", str(overlay_path)]
    if compare_text is not None:
        compare_path = tmp_path / "compare.toml"
        compare_path.write_bytes(compare_text.encode("utf-8"))
        argv += ["--compare-file", str(compare_path)]

    assert MODULE.main([*argv, *selector_args]) == 0
    return output_path.read_bytes().decode("utf-8")


def test_blank_line_collapsing_keeps_multiline_string_content(tmp_path: Path) -> None:
    base_text = 'a = """\nline1\n\n\n\nline5"""\nb = 2\n'
    literal_base_text = "a = '''\nline1\n\n\n\nline5'''\nb = 2\n"

    removed = run_toml_transform(tmp_path, base_text, "--selector-type", "remove", "--selectors", "b")
    literal_removed = run_toml_transform(
        tmp_path, literal_base_text, "--selector-type", "remove", "--selectors", "b"
    )
    merged = run_toml_transform(tmp_path, base_text, "--selectors", "a", overlay_text="c = 1\n")

    assert tomllib.loads(removed) == {"a": "line1\n\n\n\nline5"}
    assert tomllib.loads(literal_removed) == {"a": "line1\n\n\n\nline5"}
    assert tomllib.loads(merged) == {"a": "line1\n\n\n\nline5", "c": 1}


def test_blank_line_collapsing_still_merges_separator_runs(tmp_path: Path) -> None:
    output = run_toml_transform(
        tmp_path,
        "a = 1\n\nb = 2\n\nc = 3\n",
        "--selector-type",
        "remove",
        "--selectors",
        "b",
    )

    assert output == "a = 1\n\nc = 3\n"


def test_crlf_base_keeps_crlf_line_endings(tmp_path: Path) -> None:
    base_text = "a = 1\r\nb = 2\r\n\r\n[t]\r\nx = 1\r\n\r\n[u]\r\ny = 2\r\n"

    retained = run_toml_transform(tmp_path, base_text, "--selectors", "t.x", "a")
    merged = run_toml_transform(
        tmp_path,
        base_text,
        "--selector-type",
        "remove",
        "--selectors",
        "a",
        overlay_text="c = 3\n\n[v]\nq = 1\n",
    )

    merged_with_crlf_overlay = run_toml_transform(
        tmp_path,
        base_text,
        "--selector-type",
        "remove",
        "--selectors",
        "a",
        overlay_text="c = 3\r\n\r\n[v]\r\nq = 1\r\n",
    )

    expected_merge = "b = 2\r\nc = 3\r\n\r\n[t]\r\nx = 1\r\n\r\n[u]\r\ny = 2\r\n\r\n[v]\r\nq = 1\r\n"
    assert retained == "a = 1\r\n\r\n[t]\r\nx = 1\r\n"
    assert merged == expected_merge
    assert merged_with_crlf_overlay == expected_merge



def test_crlf_multiline_string_keeps_its_value_when_moved_into_an_inline_table(
    tmp_path: Path,
) -> None:
    base_text = '[t.c]\r\ns = """line1\r\nline2"""\r\n'

    output = run_toml_transform(
        tmp_path, base_text, "--selectors", "t.c", overlay_text="t = {o = 1}\n"
    )

    assert tomllib.loads(output) == {"t": {"c": {"s": "line1\nline2"}, "o": 1}}

OUT_OF_ORDER_TABLES = "[a]\nx = 1\n[b]\ny = 2\n[a.c]\nz = 3\n"
REPEATED_DOTTED_KEYS = "a.x = 1\nb = 2\na.y = 2\n"


def test_selectors_reach_into_split_tables(tmp_path: Path) -> None:
    def cleanup(base_text: str, *selector_args: str) -> dict:
        return tomllib.loads(run_toml_transform(tmp_path, base_text, *selector_args))

    assert cleanup(OUT_OF_ORDER_TABLES, "--selector-type", "remove", "--selectors", "a.c") == {
        "a": {"x": 1},
        "b": {"y": 2},
    }
    assert cleanup(OUT_OF_ORDER_TABLES, "--selectors", "a.c") == {"a": {"c": {"z": 3}}}
    assert cleanup(OUT_OF_ORDER_TABLES, "--selectors", r"re:^a\.c\.z$") == {"a": {"c": {"z": 3}}}
    assert cleanup(REPEATED_DOTTED_KEYS, "--selector-type", "remove", "--selectors", "a.y") == {
        "a": {"x": 1},
        "b": 2,
    }
    assert cleanup(REPEATED_DOTTED_KEYS, "--selectors", "a.y") == {"a": {"y": 2}}


def test_merge_keeps_every_part_of_split_tables(tmp_path: Path) -> None:
    def merge(base_text: str, overlay_text: str, *selector_args: str) -> dict:
        return tomllib.loads(
            run_toml_transform(tmp_path, base_text, *selector_args, overlay_text=overlay_text)
        )

    assert merge("k = 1\n", OUT_OF_ORDER_TABLES, "--selectors", "zzz") == {
        "a": {"x": 1, "c": {"z": 3}},
        "b": {"y": 2},
    }
    assert merge("k = 1\n", REPEATED_DOTTED_KEYS, "--selectors", "zzz") == {
        "a": {"x": 1, "y": 2},
        "b": 2,
    }
    assert merge(
        OUT_OF_ORDER_TABLES, "[a]\nw = 1\n", "--selector-type", "remove", "--selectors", "b"
    ) == {"a": {"x": 1, "w": 1, "c": {"z": 3}}}
    assert merge(OUT_OF_ORDER_TABLES, "[a]\nw = 1\n", "--selectors", "a.c") == {
        "a": {"w": 1, "c": {"z": 3}}
    }


# The late [a.b.d] extends the last element of [[a.b]].
LATE_ARRAY_ELEMENT_SUBTABLE = "[[a.b]]\nx = 1\n[[a.b]]\nx = 2\n[c]\nq = 1\n[a.b.d]\ny = 1\n"
LATE_ARRAY_OF_TABLES = {"b": [{"x": 1}, {"x": 2, "d": {"y": 1}}]}


def test_late_subtable_of_array_of_tables_element_stays_with_that_element(tmp_path: Path) -> None:
    def transform(base_text: str, *selector_args: str, overlay_text: str | None = None) -> dict:
        return tomllib.loads(
            run_toml_transform(tmp_path, base_text, *selector_args, overlay_text=overlay_text)
        )

    base_text = LATE_ARRAY_ELEMENT_SUBTABLE
    assert transform(base_text, "--selector-type", "remove", "--selectors", "c") == {
        "a": LATE_ARRAY_OF_TABLES
    }
    assert transform(base_text, "--selector-type", "remove", "--selectors", "a.b") == {"c": {"q": 1}}
    assert transform(base_text, "--selectors", "a.b") == {"a": LATE_ARRAY_OF_TABLES}
    assert transform("k = 1\n", "--selectors", "zzz", overlay_text=base_text) == {
        "a": LATE_ARRAY_OF_TABLES,
        "c": {"q": 1},
    }
    assert transform(base_text, "--selectors", "a", overlay_text="[c]\nq = 2\n") == {
        "a": LATE_ARRAY_OF_TABLES,
        "c": {"q": 2},
    }


def test_selectors_reach_into_inline_tables(tmp_path: Path) -> None:
    base_text = "a = {b = 1, c = 2}\nz = 3\n"

    retained = run_toml_transform(tmp_path, base_text, "--selectors", "a.b")
    removed = run_toml_transform(tmp_path, base_text, "--selector-type", "remove", "--selectors", "a.b")
    regex_removed = run_toml_transform(
        tmp_path, base_text, "--selector-type", "remove", "--selectors", r"re:^a\.c$"
    )

    middle_removed = run_toml_transform(
        tmp_path,
        "a = {b = 1, c = 2, e = 3}  # note\n",
        "--selector-type",
        "remove",
        "--selectors",
        "a.c",
    )

    assert retained == "a = {b = 1}\n"
    assert removed == "a = {c = 2}\nz = 3\n"
    assert regex_removed == "a = {b = 1}\nz = 3\n"
    assert middle_removed == "a = {b = 1, e = 3}  # note\n"


@pytest.mark.parametrize("base_text", ["x = {c = 2, a.b = 1}\n", "x = {a.b = 1, c = 2}\n"])
@pytest.mark.parametrize(
    ("selector_args", "expected"),
    [
        (("--selector-type", "remove", "--selectors", "x.a.b"), {"x": {"c": 2}}),
        (("--selectors", "x.a"), {"x": {"a": {"b": 1}}}),
        (("--selectors", "x.a.b"), {"x": {"a": {"b": 1}}}),
    ],
)
def test_selectors_reach_dotted_keys_inside_inline_tables(
    tmp_path: Path, base_text: str, selector_args: tuple[str, ...], expected: dict
) -> None:
    output = run_toml_transform(tmp_path, base_text, *selector_args)

    assert tomllib.loads(output) == expected


def test_merge_retains_live_keys_beside_dotted_keys_inside_inline_tables(tmp_path: Path) -> None:
    output = run_toml_transform(tmp_path, "x = {a = {b = 1}}\n", "--selectors", "x.a.b", overlay_text="x = {a.e = 5}\n")

    assert output == "x = {a = {b = 1, e = 5}}\n"


def test_merge_keeps_retained_live_keys_across_inline_and_table_styles(tmp_path: Path) -> None:
    inline_overlay = run_toml_transform(
        tmp_path,
        "[a]\nx = 1\nlive = 9\n\n[a.sub]\nq = 1\n",
        "--selectors",
        "a.live",
        "a.sub",
        overlay_text="a = {x = 2}\n",
    )
    table_overlay = run_toml_transform(
        tmp_path,
        "a = {x = 1, live = 9}\n",
        "--selectors",
        "a.live",
        overlay_text="[a]\nx = 2\n",
    )

    commented_inline_overlay = run_toml_transform(
        tmp_path,
        "a = {x = 1, live = 9}\n",
        "--selectors",
        "a.live",
        overlay_text="a = {x = 2}  # repo note\n",
    )

    assert tomllib.loads(inline_overlay) == {"a": {"x": 2, "live": 9, "sub": {"q": 1}}}
    assert commented_inline_overlay == "a = {x = 2, live = 9}  # repo note\n"
    assert inline_overlay.startswith("a = {")
    assert tomllib.loads(table_overlay) == {"a": {"x": 2, "live": 9}}
    assert table_overlay.startswith("[a]\n")


def test_retain_mixing_regex_and_exact_selectors_keeps_document_order(tmp_path: Path) -> None:
    output = run_toml_transform(tmp_path, "[t]\nx = 1\n[u]\ny = 1\n", "--selectors", "re:^u$", "t")

    assert output == "[t]\nx = 1\n\n[u]\ny = 1\n"


def test_compare_file_reuse_requires_values_of_the_same_type(tmp_path: Path) -> None:
    def cleanup_with_compare(base_text: str, compare_text: str) -> str:
        return run_toml_transform(
            tmp_path,
            base_text,
            "--selector-type",
            "remove",
            "--selectors",
            "unused",
            compare_text=compare_text,
        )

    stale_lookalikes = [
        ("a = true\n", "a = 1\n"),
        ("a = 1\n", "a = 1.0\n"),
        ("a = [1, 2]\n", "a = [1.0, 2]\n"),
        ("a = {b = 0.0}\n", "a = {b = -0.0}\n"),
        ("t = 1979-05-27T07:32:00Z\n", "t = 1979-05-27T00:32:00-07:00\n"),
    ]
    for base_text, compare_text in stale_lookalikes:
        assert cleanup_with_compare(base_text, compare_text) == base_text

    reusable_compare_text = "a  =  nan\nt = 1979-05-27T07:32:00Z\n"
    assert (
        cleanup_with_compare("a = nan\nt = 1979-05-27T07:32:00Z\n", reusable_compare_text)
        == reusable_compare_text
    )


def test_quoted_selector_segments_follow_toml_basic_string_rules() -> None:
    assert MODULE.parse_key_paths(
        ['"é"', r'"a\tb"', '" a"', 'x."a.b"', r'"q\"t"', r'"é"', 'a.""', " a . b "]
    ) == [
        ("é",),
        ("a\tb",),
        (" a",),
        ("x", "a.b"),
        ('q"t',),
        ("é",),
        ("a", ""),
        ("a", "b"),
    ]


def test_single_quoted_selector_segments_are_toml_literal_strings() -> None:
    assert MODULE.parse_key_paths(["x.'a.b'", r"'C:\dir'", "''", "'say \"hi\"'.k"]) == [
        ("x", "a.b"),
        ("C:\\dir",),
        ("",),
        ('say "hi"', "k"),
    ]


@pytest.mark.parametrize("raw_key", ["a..b", "a.", ".a", "a. .b"])
def test_empty_unquoted_selector_segment_is_rejected(raw_key: str) -> None:
    with pytest.raises(ValueError, match="empty"):
        MODULE.parse_key_paths([raw_key])


def test_remove_quoted_empty_key_keeps_its_table(tmp_path: Path) -> None:
    output = run_toml_transform(
        tmp_path,
        '[a]\n"" = 1\nk = 2\n',
        "--selector-type",
        "remove",
        "--selectors",
        'a.""',
    )

    assert output == "[a]\nk = 2\n"


def test_remove_key_drops_its_attached_leading_comment(tmp_path: Path) -> None:
    def remove(base_text: str, *selectors: str) -> str:
        return run_toml_transform(
            tmp_path, base_text, "--selector-type", "remove", "--selectors", *selectors
        )

    assert remove("# about b\nb = 2\n# about c\nc = 3\n", "b") == "# about c\nc = 3\n"
    assert remove("[t]\n# about k\n# more\nk = 1\nm = 2\n", "t.k") == "[t]\nm = 2\n"
    assert remove("# about b\nb = 2\nc = 3\n", r"re:^b$") == "c = 3\n"
    assert remove("# independent\n\nb = 2\nc = 3\n", "b") == "# independent\n\nc = 3\n"


@pytest.mark.parametrize("value", ["true", "1", '"x"'])
def test_retain_keeps_the_attached_leading_comment(tmp_path: Path, value: str) -> None:
    output = run_toml_transform(
        tmp_path, f"# about b\nb = {value}\nc = 3\n", "--selectors", "b"
    )

    assert output == f"# about b\nb = {value}\n"


def test_retain_keeps_dotted_keys_and_their_comments(tmp_path: Path) -> None:
    output = run_toml_transform(
        tmp_path, "x = 1\n# about b\nb.e = true\nb.a = 2\n", "--selectors", "b"
    )

    assert output == "# about b\nb.e = true\nb.a = 2\n"


def test_merge_moves_a_dotted_key_table_into_an_overlay_inline_table(tmp_path: Path) -> None:
    output = run_toml_transform(
        tmp_path, "[e]\na.x = 1\n", "--selectors", "e.a", overlay_text="e = {y = 2}\n"
    )

    assert tomllib.loads(output) == {"e": {"a": {"x": 1}, "y": 2}}


def test_retained_split_table_keeps_the_comment_above_its_header(tmp_path: Path) -> None:
    output = run_toml_transform(
        tmp_path, "[e.f]\nx = 1\n\n# about e\n[e]\ny = 2\n", "--selectors", "e"
    )

    assert output == "# about e\n[e]\ny = 2\n\n[e.f]\nx = 1\n"


def test_comment_directly_above_a_table_header_belongs_to_that_table(tmp_path: Path) -> None:
    # tomlkit parses `# about b` into the end of the previous table's body.
    def remove(base_text: str, *selectors: str) -> str:
        return run_toml_transform(
            tmp_path, base_text, "--selector-type", "remove", "--selectors", *selectors
        )

    base = "# about a\n[a]\nx = 1\n# about b\n[b]\ny = 1\n"
    assert remove(base, "b") == "# about a\n[a]\nx = 1\n"
    assert remove(base, "a") == "# about b\n[b]\ny = 1\n"

    nested = "[a]\nx = 1\n[a.c]\nz = 1\n# about b\n[b]\ny = 1\n"
    assert remove(nested, "a") == "# about b\n[b]\ny = 1\n"

    array_of_tables = "[[a]]\nx = 1\n# about b\n[b]\ny = 1\n"
    assert remove(array_of_tables, "a") == "# about b\n[b]\ny = 1\n"


def test_merge_does_not_duplicate_a_comment_directly_above_an_overlay_table(
    tmp_path: Path,
) -> None:
    output = run_toml_transform(
        tmp_path,
        "# about a\n[a]\nx = 1\n# about b\n[b]\ny = 1\n",
        "--selector-type", "remove", "--selectors", "b",
        overlay_text="# about b\n[b]\ny = 2\n",
    )

    assert output == "# about a\n[a]\nx = 1\n# about b\n[b]\ny = 2\n"


def test_merge_takes_nested_table_header_comments_from_the_overlay(tmp_path: Path) -> None:
    output = run_toml_transform(
        tmp_path,
        "[t]\nk = 1\n# live note\n[t.u]\nv = 1\n",
        "--selectors", "t.u.v",
        overlay_text="[t]\nk = 1\n# repo note\n[t.u]\nv = 2\n",
    )

    assert output == "[t]\nk = 1\n# repo note\n[t.u]\nv = 2\n"


def test_merge_keeps_layout_around_a_commented_inline_table(tmp_path: Path) -> None:
    live = "b = 1\n\n# about t\nt = {f = 0, g = 1, h = false}\n"

    output = run_toml_transform(
        tmp_path,
        live,
        "--selectors", "t.f",
        overlay_text="b = 1\n\n# about t\nt = {g = 1, h = false}\n",
    )

    assert output == live


def test_merge_sets_off_a_kept_live_table_that_has_an_attached_comment(tmp_path: Path) -> None:
    live = "[d]\nx = 1\n\n# about c\n[c]\ny = 1\n"

    output = run_toml_transform(tmp_path, live, "--selectors", "c", overlay_text="[d]\nx = 1\n")

    assert output == live


def test_output_does_not_end_with_blank_lines(tmp_path: Path) -> None:
    live = "x = 1\n\n[[a]]\nn = 1\n"

    captured = run_toml_transform(
        tmp_path, live, "--selector-type", "remove", "--selectors", "a"
    )
    rendered = run_toml_transform(tmp_path, live, "--selectors", "a", overlay_text="x = 1\n\n")

    assert captured == "x = 1\n"
    assert rendered == live


@pytest.mark.parametrize("selector", ["a", "a.x"])
def test_removing_the_first_table_leaves_no_leading_blank_line(
    tmp_path: Path, selector: str
) -> None:
    live = "[a]\nx = 1\n\n[b]\nz = 1\n"

    output = run_toml_transform(
        tmp_path, live, "--selector-type", "remove", "--selectors", selector
    )

    assert output == "[b]\nz = 1\n"


@pytest.mark.parametrize(
    ("live", "expected"),
    [
        ("x = 1\n  \n\t\n \ny = 2\n", "x = 1\n\ny = 2\n"),
        ("\n\n\nx = 1\n", "x = 1\n"),
        ("x = 1\n\n  \n", "x = 1\n"),
    ],
    ids=["whitespace-only", "leading", "trailing-whitespace-only"],
)
def test_blank_line_runs_collapse_even_when_blank_lines_hold_spaces(
    live: str, expected: str, tmp_path: Path
) -> None:
    assert run_toml_transform(
        tmp_path, live, "--selector-type", "remove", "--selectors", "absent"
    ) == expected


@pytest.mark.parametrize(
    "source",
    [
        "# top\nk = 1\n# about b\n[b]\nx = 1\n# tail\n\n# independent\n\n# about c\n[c]\ny = 1\n",
        "# about b.k\n[b.k]\nx = 1\n# about b.j\n[b.j]\ny = 1\n\n# independent\n\n[z]\n",
        "[a]\nx = 1\n[a.b]\ny = 1\n# about d\n[d]\n",
        "[[t]]\nx = 1\n# about t2\n[[t]]\nx = 2\n\n# between\n\n# about u\n[u]\n",
        "x = 1\n  # indented\n  y = 2\n\n# end\n",
        "[a]\nx = 1\n[b]\ny = 2\n[a.c]\nz = 3\n# tail of a.c\n",
    ],
)
def test_removing_nothing_keeps_the_source_text(tmp_path: Path, source: str) -> None:
    output = run_toml_transform(
        tmp_path, source, "--selector-type", "remove", "--selectors", "absent"
    )

    assert output == source


def test_retained_nested_dotted_tables_keep_their_full_path(tmp_path: Path) -> None:
    base = 'tui.theme.name = "x"\ntui.theme.dark.bg = "y"\n'

    output = run_toml_transform(tmp_path, base, "--selectors", "tui")

    assert tomllib.loads(output) == tomllib.loads(base)


def test_merge_keeps_nested_dotted_tables_at_their_full_path(tmp_path: Path) -> None:
    base = 'tui.theme.name = "x"\ntui.theme.dark.bg = "y"\n'

    output = run_toml_transform(
        tmp_path,
        base,
        "--selector-type",
        "remove",
        "--selectors",
        "absent",
        overlay_text="other = 2\n",
    )

    assert tomllib.loads(output) == {**tomllib.loads(base), "other": 2}


def test_merge_of_inline_and_standard_tables_keeps_boolean_values(tmp_path: Path) -> None:
    for base, overlay in (
        ("f = {a = true, c = 2}\n", "[f]\nb = 1\n"),
        ("[f]\na = true\nc = 2\n", "f = {b = 1}\n"),
    ):
        output = run_toml_transform(
            tmp_path,
            base,
            "--selector-type",
            "remove",
            "--selectors",
            "absent",
            overlay_text=overlay,
        )

        assert tomllib.loads(output)["f"] == {**tomllib.loads(base)["f"], "b": 1}


def test_regex_removal_of_nested_dotted_keys_removes_everything_matched(tmp_path: Path) -> None:
    base = "x.s.e = 1\nx.s.b.b = 1\nx.s.b.c = 2\nkeep = 3\n"

    output = run_toml_transform(tmp_path, base, "--selector-type", "remove", "--selectors", "re:^x")

    assert tomllib.loads(output) == {"keep": 3}


def test_regex_retain_matching_a_table_and_its_nested_keys_retains_the_table(tmp_path: Path) -> None:
    base = "[t]\ne.a.x = 1\ne.b.k = 2\nk = 3\n\n[u]\nv = 4\n"

    output = run_toml_transform(tmp_path, base, "--selectors", "re:^t")

    assert tomllib.loads(output) == {"t": tomllib.loads(base)["t"]}


def test_merge_of_overlay_without_final_newline_keeps_keys_on_separate_lines(
    tmp_path: Path,
) -> None:
    # The overlay's last key has no line break and is placed before the
    # dotted table, so it must gain one to stay on its own line.
    output = run_toml_transform(
        tmp_path,
        "\nt = 1\n",
        "--selectors",
        "absent",
        overlay_text="\n\nt.x = 1\n\nlast = 2",
    )

    assert tomllib.loads(output) == {"t": {"x": 1}, "last": 2}


@pytest.mark.parametrize(
    ("q_header", "removed_path", "expected"),
    [
        ("[b.q]", "b.q", {"b": {"k": {}, "x": 1}}),
        ("[b.q]", "b.k", {"b": {"q": {}, "x": 1}}),
        ("[[b.q]]", "b.k", {"b": {"q": [{}], "x": 1}}),
    ],
)
def test_comment_between_implicit_parent_subtables_keeps_output_valid(
    tmp_path: Path, q_header: str, removed_path: str, expected: dict
) -> None:
    base_text = f"[b.k]\n\n# about q\n{q_header}\n[b]\nx = 1\n"

    output = run_toml_transform(
        tmp_path, base_text, "--selector-type", "remove", "--selectors", removed_path
    )

    assert tomllib.loads(output) == expected
    assert ("# about q" in output) == (removed_path != "b.q")


@pytest.mark.parametrize(
    ("selector_args", "keeps_trailing_comment"),
    [(("--selectors", "a"), False), (("--selector-type", "remove", "--selectors", "drop"), True)],
)
def test_merge_keeps_base_trailing_comments_only_when_cleanup_would(
    tmp_path: Path, selector_args: tuple[str, ...], keeps_trailing_comment: bool
) -> None:
    base_text = "a = 1\n\n[drop]\nx = 1\n\n# trailing note\n"

    cleaned = run_toml_transform(tmp_path, base_text, *selector_args)
    merged = run_toml_transform(tmp_path, base_text, *selector_args, overlay_text="b = 2\n")

    assert ("# trailing note" in cleaned) == keeps_trailing_comment
    assert ("# trailing note" in merged) == keeps_trailing_comment


def test_removing_the_last_key_of_a_split_table_part_keeps_its_header(tmp_path: Path) -> None:
    live = "# about a\n[a]\nx = 1\n[b]\ny = 2\n[a.c]\nz = 3\n"

    # Like an emptied table that is not split, the header and its comment stay.
    assert run_toml_transform(
        tmp_path, live, "--selector-type", "remove", "--selectors", "a.x"
    ) == "# about a\n[a]\n[b]\ny = 2\n[a.c]\nz = 3\n"


def test_compare_file_reuse_follows_which_array_element_a_comment_belongs_to(tmp_path: Path) -> None:
    def cleanup_with_compare(base_text: str, compare_text: str) -> str:
        return run_toml_transform(
            tmp_path, base_text, "--selector-type", "remove", "--selectors", "unused", compare_text=compare_text
        )

    live = "a = [\n  [1, # one\n   2],\n  # before three\n  3, # three\n]\n"
    reformatted = "a = [\n    [1,  # one\n     2],\n    # before three\n    3,  # three\n]\n"
    moved_out_of_nested_array = "a = [\n  [1,\n   2], # one\n  # before three\n  3, # three\n]\n"
    moved_to_previous_element = "a = [\n  [1, # one\n   2], # before three\n  3, # three\n]\n"

    assert cleanup_with_compare(live, reformatted) == reformatted
    assert cleanup_with_compare(live, moved_out_of_nested_array) == live
    assert cleanup_with_compare(live, moved_to_previous_element) == live


def test_compare_file_reuse_reads_array_comments_past_strings_holding_toml_syntax(tmp_path: Path) -> None:
    live = "a = [\n  \"x] # not a comment\",\n  2, # c\n]\n"
    moved_to_previous_element = "a = [\n  \"x] # not a comment\", # c\n  2,\n]\n"

    output = run_toml_transform(
        tmp_path, live, "--selector-type", "remove", "--selectors", "unused", compare_text=moved_to_previous_element
    )

    assert output == live


def test_not_selector_keeps_a_key_at_any_depth_out_of_the_synced_region(tmp_path: Path) -> None:
    selectors = ("--selectors", "settings", r"not:re:(^|\.)cache$")
    live_text = (
        "state = 1\n\n[settings]\ntheme = \"dark\"\ncache = \"L0\"\n\n"
        "[settings.a]\nx = 1\ncache = \"L1\"\n"
    )

    captured = run_toml_transform(tmp_path, live_text, *selectors)
    assert tomllib.loads(captured) == {"settings": {"theme": "dark", "a": {"x": 1}}}

    rendered = run_toml_transform(
        tmp_path,
        live_text,
        "--selector-type",
        "remove",
        *selectors,
        overlay_text='[settings]\ntheme = "light"\n\n[settings.a]\nx = 9\n',
    )
    assert tomllib.loads(rendered) == {
        "state": 1,
        "settings": {"theme": "light", "cache": "L0", "a": {"x": 9, "cache": "L1"}},
    }


def test_capture_reproduces_a_repo_that_deleted_a_table_holding_live_only_keys(
    tmp_path: Path,
) -> None:
    # Issue #99: the repo deleted table `skills.b`, whose `hash` is live-only.
    selectors = ("--selectors", r"re:^skills\..+\.hash$")
    live_text = '[skills.a]\nsource = "x"\nhash = "1"\n\n[skills.b]\nsource = "y"\nhash = "2"\n'
    repo_text = '[skills.a]\nsource = "x"\n'

    rendered = run_toml_transform(
        tmp_path, live_text, "--selector-type", "retain", *selectors, overlay_text=repo_text
    )
    # A live-only key survives Render even where the repo lacks its table.
    assert tomllib.loads(rendered) == {
        "skills": {"a": {"source": "x", "hash": "1"}, "b": {"hash": "2"}}
    }

    captured = run_toml_transform(tmp_path, rendered, "--selector-type", "remove", *selectors)
    assert captured == repo_text


def test_remove_drops_only_tables_the_removal_empties(tmp_path: Path) -> None:
    base_text = "[empty]\n\n[emptied.nested]\ncache = 1\n\n[kept]\ncache = 1\nx = 2\n"

    removed = run_toml_transform(
        tmp_path, base_text, "--selector-type", "remove", "--selectors", r"re:(^|\.)cache$"
    )

    assert tomllib.loads(removed) == {"empty": {}, "kept": {"x": 2}}


@pytest.mark.parametrize("base_text", ["[a]\n\n[b]\nz = 1\n", "a = {}\nb = 1\n"])
def test_remove_keeps_an_empty_table_a_selector_names_a_missing_key_in(
    tmp_path: Path, base_text: str
) -> None:
    removed = run_toml_transform(
        tmp_path, base_text, "--selector-type", "remove", "--selectors", "a.x"
    )

    assert tomllib.loads(removed) == tomllib.loads(base_text)


def test_merge_remove_keeps_live_key_order_in_a_table_the_removal_empties(tmp_path: Path) -> None:
    merged = run_toml_transform(
        tmp_path,
        "c = 1\n\n[a]\nx = 1\ny = 2\n",
        "--selector-type", "remove", "--selectors", "a.x", "a.y",
        overlay_text="[a]\ny = 20\nx = 10\n",
    )

    assert list(tomllib.loads(merged)["a"].items()) == [("x", 10), ("y", 20)]


def test_merge_retain_keeps_live_key_order_in_a_table_no_selector_reaches(tmp_path: Path) -> None:
    merged = run_toml_transform(
        tmp_path,
        "live = 1\n\n[a]\nx = 1\ny = 2\n",
        "--selector-type", "retain", "--selectors", "live",
        overlay_text="[a]\ny = 20\nx = 10\n",
    )

    assert list(tomllib.loads(merged)["a"].items()) == [("x", 10), ("y", 20)]


@pytest.mark.parametrize("selector_type", ["remove", "retain"])
def test_merge_keeps_the_repo_layout_of_a_whole_selected_table(
    tmp_path: Path, selector_type: str
) -> None:
    # A table selected whole is one value, so the repo's copy replaces it: an
    # allowlist (remove) syncs it, and a denylist (retain) republishes it.
    overlay_text = "[a]\ny = 20 # repo\nx = 10\n"

    merged = run_toml_transform(
        tmp_path,
        "[a]\nx = 1\ny = 2\n",
        "--selector-type", selector_type, "--selectors", "a",
        overlay_text=overlay_text,
    )

    assert merged == overlay_text


def test_merge_remove_drops_a_table_the_removal_empties_and_the_repo_deleted(tmp_path: Path) -> None:
    merged = run_toml_transform(
        tmp_path,
        "b = 2\n\n[empty]\n\n[a]\nx = 1\n",
        "--selector-type", "remove", "--selectors", "a.x", "b",
        overlay_text="b = 3\n",
    )

    assert tomllib.loads(merged) == {"b": 3, "empty": {}}
