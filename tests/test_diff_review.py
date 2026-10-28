from __future__ import annotations

import stat
from pathlib import Path

import pytest

from dotman.command_runtime import (
    CommandResult,
    MemoryCommandRuntime,
    command_runtime_session,
)
from dotman.diff_review import (
    DEFAULT_REVIEW_PAGER,
    ReviewItem,
    _review_display_path,
    _select_review_pager_command,
    display_review_path,
    run_review_item_diff,
)
from dotman.models import UiConfig
from dotman.ui_context import ui_config_scope


def test_run_review_item_diff_invokes_git_diff(monkeypatch) -> None:
    repo_path = Path.home() / ".config" / "repo-file"
    live_path = Path.home() / ".local" / "share" / "live-file"
    review_item = ReviewItem(
        selection_label="example:git@basic",
        package_id="git",
        target_name="gitconfig",
        action="update",
        operation="push",
        repo_path=repo_path,
        live_path=live_path,
        source_path="/repo-file",
        destination_path="/live-file",
        before_bytes=b"before\n",
        after_bytes=b"after\n",
    )
    recorded: dict[str, object] = {}

    monkeypatch.setattr("sys.stdout.isatty", lambda: False)
    monkeypatch.setattr("dotman.diff_review._select_review_pager_command", lambda: None)

    def fake_run(request):
        recorded["request"] = request
        assert request.cwd is not None
        assert Path(request.cwd, "live", "~", "...", "share", "live-file").read_text(encoding="utf-8") == "before\n"
        assert Path(request.cwd, "repo", "~", ".config", "repo-file").read_text(encoding="utf-8") == "after\n"
        return CommandResult(exit_code=1)

    runtime = MemoryCommandRuntime([fake_run])
    with command_runtime_session(runtime):
        run_review_item_diff(review_item)

    request = recorded["request"]
    assert request.env == {}
    assert request.command.arguments[:5] == ("git", "diff", "--no-index", "--color=auto", "--")
    assert request.command.arguments[5:] == ("live/~/.../share/live-file", "repo/~/.config/repo-file")
    assert request.io == "tty"


def test_run_review_item_diff_materializes_executable_bit_change(monkeypatch, capsys) -> None:
    repo_path = Path.home() / ".config" / "repo-file"
    live_path = Path.home() / ".local" / "share" / "live-file"
    review_item = ReviewItem(
        selection_label="example:scripts@basic",
        package_id="scripts",
        target_name="bin",
        action="update",
        operation="push",
        repo_path=repo_path,
        live_path=live_path,
        source_path="/repo-file",
        destination_path="/live-file",
        before_bytes=b"same\n",
        after_bytes=b"same\n",
        before_mode=0o644,
        after_mode=0o755,
    )

    monkeypatch.setattr("sys.stdout.isatty", lambda: False)
    monkeypatch.setattr("dotman.diff_review._select_review_pager_command", lambda: None)

    def fake_run(request):
        assert request.cwd is not None
        assert stat.S_IMODE(Path(request.cwd, "live", "~", "...", "share", "live-file").stat().st_mode) == 0o644
        assert stat.S_IMODE(Path(request.cwd, "repo", "~", ".config", "repo-file").stat().st_mode) == 0o755
        return CommandResult(exit_code=1)

    with command_runtime_session(MemoryCommandRuntime([fake_run])):
        run_review_item_diff(review_item)

    assert "file mode:" not in capsys.readouterr().out


def test_run_review_item_diff_materializes_absolute_paths_under_temp_root(monkeypatch) -> None:
    repo_path = Path("/etc/sddm.conf.d/kde_settings.conf")
    live_path = Path("/var/lib/sddm.conf.d/kde_settings.conf")
    review_item = ReviewItem(
        selection_label="main:sddm@basic",
        package_id="sddm",
        target_name="kde_settings.conf",
        action="update",
        operation="push",
        repo_path=repo_path,
        live_path=live_path,
        source_path=str(repo_path),
        destination_path=str(live_path),
        before_bytes=b"before\n",
        after_bytes=b"after\n",
    )
    recorded: dict[str, object] = {}

    monkeypatch.setattr("sys.stdout.isatty", lambda: False)
    monkeypatch.setattr("dotman.diff_review._select_review_pager_command", lambda: None)

    def fake_run(request):
        recorded["request"] = request
        assert request.cwd is not None
        assert Path(request.cwd, "live", "var", "...", "sddm.conf.d", "kde_settings.conf").read_text(encoding="utf-8") == "before\n"
        assert Path(request.cwd, "repo", "etc", "sddm.conf.d", "kde_settings.conf").read_text(encoding="utf-8") == "after\n"
        return CommandResult(exit_code=1)

    with command_runtime_session(MemoryCommandRuntime([fake_run])):
        run_review_item_diff(review_item)

    assert recorded["request"].command.arguments[5:] == ("live/var/.../sddm.conf.d/kde_settings.conf", "repo/etc/sddm.conf.d/kde_settings.conf")




def test_run_review_item_diff_uses_explicit_pager_when_stdout_is_tty(monkeypatch) -> None:
    repo_path = Path.home() / ".config" / "repo-file"
    live_path = Path.home() / ".local" / "share" / "live-file"
    review_item = ReviewItem(
        selection_label="example:git@basic",
        package_id="git",
        target_name="gitconfig",
        action="update",
        operation="push",
        repo_path=repo_path,
        live_path=live_path,
        source_path="/repo-file",
        destination_path="/live-file",
        before_bytes=b"before\n",
        after_bytes=b"after\n",
    )

    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr("dotman.diff_review._select_review_pager_command", lambda: DEFAULT_REVIEW_PAGER)

    runtime = MemoryCommandRuntime([CommandResult(exit_code=1)])
    with command_runtime_session(runtime):
        run_review_item_diff(review_item)

    request = runtime.requests[0]
    assert request.command.arguments[:6] == ("git", "--paginate", "diff", "--no-index", "--color=auto", "--")
    assert request.command.arguments[6:] == ("live/~/.../share/live-file", "repo/~/.config/repo-file")
    assert request.env["GIT_PAGER"] == DEFAULT_REVIEW_PAGER


def test_run_review_item_diff_preserves_interruption(monkeypatch) -> None:
    review_item = ReviewItem(
        selection_label="example:git@basic",
        package_id="git",
        target_name="gitconfig",
        action="update",
        operation="push",
        repo_path=Path("/repo-file"),
        live_path=Path("/live-file"),
        source_path="/repo-file",
        destination_path="/live-file",
        before_bytes=b"before\n",
        after_bytes=b"after\n",
    )
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)

    with command_runtime_session(MemoryCommandRuntime([CommandResult(exit_code=130)])):
        with pytest.raises(KeyboardInterrupt):
            run_review_item_diff(review_item)


def test_review_display_path_uses_tilde_for_home_prefix() -> None:
    assert _review_display_path(Path.home() / ".config" / "nvim" / "init.lua") == Path("~/.../nvim/init.lua")


def test_review_display_path_compacts_long_home_relative_path() -> None:
    assert _review_display_path(Path.home() / ".local" / "share" / "nvim" / "init.lua") == Path("~/.../nvim/init.lua")


def test_review_display_path_uses_configured_tail_segments() -> None:
    assert _review_display_path(Path.home() / ".local" / "share" / "nvim" / "init.lua", tail_segments=3) == Path("~/.../share/nvim/init.lua")


def test_review_display_path_uses_ui_context_tail_segments() -> None:
    with ui_config_scope(UiConfig(compact_path_tail_segments=3)):
        assert _review_display_path(Path.home() / ".local" / "share" / "nvim" / "init.lua") == Path("~/.../share/nvim/init.lua")


def test_review_display_path_rejects_invalid_tail_segments() -> None:
    with pytest.raises(ValueError, match="tail segments"):
        _review_display_path(Path.home() / ".local" / "share" / "nvim" / "init.lua", tail_segments=0)


def test_review_display_path_keeps_absolute_path_with_root_prefix() -> None:
    assert _review_display_path(Path("/etc/gitconfig")) == Path("/etc/gitconfig")


def test_review_display_path_keeps_short_absolute_system_path() -> None:
    assert _review_display_path(Path("/etc/sddm.conf.d/kde_settings.conf")) == Path("/etc/sddm.conf.d/kde_settings.conf")


def test_review_display_path_compacts_long_absolute_path() -> None:
    assert _review_display_path(Path("/etc/xdg/nvim/init.lua")) == Path("/etc/.../nvim/init.lua")


def test_display_review_path_can_disable_compaction_and_home_collapse() -> None:
    full_path = Path.home() / ".config" / "nvim" / "init.lua"

    assert display_review_path(full_path, compact=False) == str(full_path)


def test_select_review_pager_command_uses_git_pager_before_git_config_and_pager(monkeypatch) -> None:
    monkeypatch.setenv("GIT_PAGER", "delta")
    monkeypatch.setenv("PAGER", "less")
    monkeypatch.setattr("dotman.diff_review._git_configured_pager_command", lambda: "diff-so-fancy")

    assert _select_review_pager_command() == "delta"


def test_select_review_pager_command_uses_git_config_before_pager_env(monkeypatch) -> None:
    monkeypatch.delenv("GIT_PAGER", raising=False)
    monkeypatch.setenv("PAGER", "less")
    monkeypatch.setattr("dotman.diff_review._git_configured_pager_command", lambda: "delta")

    assert _select_review_pager_command() == "delta"


def test_select_review_pager_command_uses_pager_env_after_git_config(monkeypatch) -> None:
    monkeypatch.delenv("GIT_PAGER", raising=False)
    monkeypatch.setenv("PAGER", "less")
    monkeypatch.setattr("dotman.diff_review._git_configured_pager_command", lambda: None)

    assert _select_review_pager_command() == "less"


def test_select_review_pager_command_treats_pager_cat_as_disabled(monkeypatch) -> None:
    monkeypatch.delenv("GIT_PAGER", raising=False)
    monkeypatch.setenv("PAGER", "cat")
    monkeypatch.setattr("dotman.diff_review._git_configured_pager_command", lambda: None)
    monkeypatch.setattr("dotman.diff_review.shutil.which", lambda name: "/usr/bin/less" if name == "less" else None)

    assert _select_review_pager_command() is None


def test_select_review_pager_command_treats_git_pager_cat_as_disabled(monkeypatch) -> None:
    monkeypatch.setenv("GIT_PAGER", "cat")
    monkeypatch.setattr("dotman.diff_review.shutil.which", lambda name: "/usr/bin/less" if name == "less" else None)

    assert _select_review_pager_command() is None











