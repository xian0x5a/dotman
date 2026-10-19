from __future__ import annotations

from pathlib import Path

import pytest

from dotman.capture import CaptureError, capture_patch
from dotman.command_runtime import current_command_runtime
from dotman.templates import build_template_context, render_template_file, render_template_string


def _build_jinja_projector(*, base_dir: Path, context: dict[str, object]):
    def project(candidate_bytes: bytes) -> bytes:
        candidate_text = candidate_bytes.decode("utf-8")
        return render_template_string(candidate_text, context, base_dir=base_dir).encode("utf-8")

    return project


def _capture_template_edit(
    tmp_path: Path,
    source: bytes,
    edit,
    *,
    variables: dict[str, object] | None = None,
    protect_template_syntax: bool = True,
) -> bytes:
    """Render `source` like a file target, apply `edit` to the rendered bytes, and capture it back."""
    repo_path = tmp_path / "config.txt"
    review_repo_path = tmp_path / "review-repo.txt"
    review_live_path = tmp_path / "review-live.txt"
    repo_path.write_bytes(source)
    context = build_template_context(variables or {}, profile="default", inferred_os="linux")

    def project(candidate_bytes: bytes) -> bytes:
        return render_template_file(repo_path, context, source_bytes=candidate_bytes)[0]

    rendered = project(source)
    review_repo_path.write_bytes(rendered)
    review_live_path.write_bytes(edit(rendered))
    return capture_patch(
        repo_path=repo_path,
        review_repo_path=review_repo_path,
        review_live_path=review_live_path,
        project_repo_bytes=project,
        command_runtime=current_command_runtime(),
        protect_template_syntax=protect_template_syntax,
    )


def _replace(old: bytes, new: bytes):
    return lambda rendered: rendered.replace(old, new, 1)


GIT_CONFIG_TEMPLATE = b"""[core]
editor = {{ vars.editor }}
pager = less
{% if vars.work %}
[user]
email = {{ vars.email }}
{% endif %}
[alias]
st = status
co = checkout
lg = log --graph
"""
GIT_CONFIG_VARS = {"editor": "nvim", "work": True, "email": "me@work"}


def test_capture_patch_keeps_block_template_unchanged_without_live_edits(tmp_path: Path) -> None:
    assert _capture_template_edit(
        tmp_path, GIT_CONFIG_TEMPLATE, lambda rendered: rendered, variables=GIT_CONFIG_VARS,
    ) == GIT_CONFIG_TEMPLATE


def test_capture_patch_transfers_literal_edits_around_template_blocks(tmp_path: Path) -> None:
    def edit(rendered: bytes) -> bytes:
        return (rendered
                .replace(b"[alias]\n", b"[alias]\nci = commit\n")
                .replace(b"co = checkout\n", b"")
                .replace(b"lg = log --graph\n", b"lg = log --oneline\nbr = branch\n"))

    assert _capture_template_edit(tmp_path, GIT_CONFIG_TEMPLATE, edit, variables=GIT_CONFIG_VARS) == (
        GIT_CONFIG_TEMPLATE
        .replace(b"[alias]\n", b"[alias]\nci = commit\n")
        .replace(b"co = checkout\n", b"")
        .replace(b"lg = log --graph\n", b"lg = log --oneline\nbr = branch\n")
    )


@pytest.mark.parametrize(
    ("source", "edit"),
    [
        pytest.param(GIT_CONFIG_TEMPLATE, _replace(b"editor = nvim", b"editor = vim"), id="expression-value"),
        pytest.param(b"x = {{ vars.editor }} # note\ny\nz\n", _replace(b"# note", b"# changed"), id="mixed-line"),
        pytest.param(
            GIT_CONFIG_TEMPLATE,
            lambda rendered: rendered.replace(b"st = status", b"st = status -s").replace(b"editor = nvim", b"editor = vi"),
            id="safe-and-unsafe-hunks",
        ),
        pytest.param(
            b"{% for host in ['a', 'b'] %}\nHost {{ host }}\n  User me\n{% endfor %}\ntail\n",
            _replace(b"User me", b"User root"),
            id="loop-body",
        ),
    ],
)
def test_capture_patch_rejects_live_edits_overlapping_template_output(tmp_path: Path, source: bytes, edit) -> None:
    with pytest.raises(CaptureError, match="template"):
        _capture_template_edit(tmp_path, source, edit, variables=GIT_CONFIG_VARS)


def test_capture_patch_preserves_crlf_template_line_endings(tmp_path: Path) -> None:
    source = b"a = {{ vars.editor }}\r\nb\r\nc\r\nd\r\n"

    assert _capture_template_edit(
        tmp_path, source, _replace(b"d\n", b"D\n"), variables=GIT_CONFIG_VARS,
    ) == b"a = {{ vars.editor }}\r\nb\r\nc\r\nD\r\n"


def test_capture_patch_rejects_mixed_template_line_endings(tmp_path: Path) -> None:
    source = b"a = {{ vars.editor }}\r\nb\nc\nd\n"

    with pytest.raises(CaptureError, match="line endings"):
        _capture_template_edit(tmp_path, source, _replace(b"d\n", b"D\n"), variables=GIT_CONFIG_VARS)


def test_capture_patch_rejects_clean_merge_that_changes_template_syntax(tmp_path: Path) -> None:
    # The expression renders to its own source text, so Git sees an unchanged
    # line and cleanly takes the live replacement, deleting the expression.
    source = b"{{ vars.payload }}\n"
    variables = {"payload": "{% raw %}{{ vars.payload }}{% endraw %}"}

    with pytest.raises(CaptureError, match="template syntax"):
        _capture_template_edit(
            tmp_path, source, lambda _rendered: b"fixed\n", variables=variables,
        )


def test_capture_patch_command_renderer_transfers_literal_edits_without_syntax_check(tmp_path: Path) -> None:
    repo_path = tmp_path / "config.txt"
    review_repo_path = tmp_path / "review-repo.txt"
    review_live_path = tmp_path / "review-live.txt"
    repo_path.write_bytes(b"name = @NAME@\nmode = safe\nlevel = 1\n")
    review_repo_path.write_bytes(b"name = Ada\nmode = safe\nlevel = 1\n")
    review_live_path.write_bytes(b"name = Ada\nmode = safe\nlevel = 2\n")

    result = capture_patch(
        repo_path=repo_path,
        review_repo_path=review_repo_path,
        review_live_path=review_live_path,
        project_repo_bytes=lambda candidate: candidate.replace(b"@NAME@", b"Ada"),
        command_runtime=current_command_runtime(),
        protect_template_syntax=False,
    )

    assert result == b"name = @NAME@\nmode = safe\nlevel = 2\n"


def test_capture_patch_keeps_raw_repo_unchanged_when_review_diff_is_empty(tmp_path: Path) -> None:
    repo_path = tmp_path / "config.txt"
    review_repo_path = tmp_path / "review-repo.txt"
    review_live_path = tmp_path / "review-live.txt"

    repo_path.write_text("greeting = {{ vars.greeting }}\n", encoding="utf-8")
    review_repo_path.write_text("greeting = hello\n", encoding="utf-8")
    review_live_path.write_text("greeting = hello\n", encoding="utf-8")
    context = build_template_context({"greeting": "hello"}, profile="default", inferred_os="linux")

    result = capture_patch(
        repo_path=repo_path,
        review_repo_path=review_repo_path,
        review_live_path=review_live_path,
        project_repo_bytes=_build_jinja_projector(base_dir=repo_path.parent, context=context),
        command_runtime=current_command_runtime(),
        protect_template_syntax=True,
    )

    assert result == repo_path.read_bytes()


def test_capture_patch_requires_review_paths_from_env_when_not_provided(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_path = tmp_path / "config.txt"
    repo_path.write_text("greeting = {{ vars.greeting }}\n", encoding="utf-8")
    monkeypatch.delenv("DOTMAN_REVIEW_REPO_PATH", raising=False)
    monkeypatch.delenv("DOTMAN_REVIEW_LIVE_PATH", raising=False)

    with pytest.raises(CaptureError, match="DOTMAN_REVIEW_REPO_PATH"):
        capture_patch(
            repo_path=repo_path,
            project_repo_bytes=lambda candidate_bytes: candidate_bytes,
            command_runtime=current_command_runtime(),
            protect_template_syntax=False,
        )


def test_capture_patch_reports_projection_mismatch(tmp_path: Path) -> None:
    repo_path = tmp_path / "config.txt"
    review_repo_path = tmp_path / "review-repo.txt"
    review_live_path = tmp_path / "review-live.txt"

    repo_path.write_text("greeting = hello\n", encoding="utf-8")
    review_repo_path.write_text("greeting = hello\n", encoding="utf-8")
    review_live_path.write_text("greeting = world\n", encoding="utf-8")
    context = build_template_context({"greeting": "hello"}, profile="default", inferred_os="linux")

    with pytest.raises(CaptureError, match="captured bytes do not match"):
        capture_patch(
            repo_path=repo_path,
            review_repo_path=review_repo_path,
            review_live_path=review_live_path,
            project_repo_bytes=lambda candidate_bytes: _build_jinja_projector(
                base_dir=repo_path.parent,
                context=context,
            )(candidate_bytes).replace(b"world", b"mismatch"),
            command_runtime=current_command_runtime(),
            protect_template_syntax=True,
        )


def test_capture_patch_wraps_jinja_projection_errors_structurally(tmp_path: Path) -> None:
    repo_path = tmp_path / "config.txt"
    review_repo_path = tmp_path / "review-repo.txt"
    review_live_path = tmp_path / "review-live.txt"

    repo_path.write_text("greeting = hello\n", encoding="utf-8")
    review_repo_path.write_text("greeting = hello\n", encoding="utf-8")
    review_live_path.write_text("greeting = world\n", encoding="utf-8")

    with pytest.raises(CaptureError, match="capture failed") as exc_info:
        capture_patch(
            repo_path=repo_path,
            review_repo_path=review_repo_path,
            review_live_path=review_live_path,
            project_repo_bytes=lambda _candidate_bytes: render_template_string(
                "{{ missing.value }}",
                {},
                base_dir=repo_path.parent,
                source_path=repo_path,
            ).encode("utf-8"),
            command_runtime=current_command_runtime(),
            protect_template_syntax=True,
        )

    assert exc_info.value.path == repo_path
    assert "missing" in exc_info.value.detail

def test_capture_patch_preserves_typed_interruption(tmp_path: Path) -> None:
    path = tmp_path / "config"
    path.write_bytes(b"same\n")
    interruption = InterruptedError("cancelled")
    def project(_content):
        raise interruption
    with pytest.raises(InterruptedError) as caught:
        capture_patch(repo_path=path, review_repo_path=path, review_live_path=path, project_repo_bytes=project,
                      command_runtime=current_command_runtime(), protect_template_syntax=False)
    assert caught.value is interruption
