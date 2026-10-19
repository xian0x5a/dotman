from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

from dotman.models import SnapshotConfig, TargetPlan
from dotman.snapshot import SnapshotRecord, create_push_snapshot
from tests.helpers import make_package_plan

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")

GROUP_AND_OTHER_BITS = stat.S_IRWXG | stat.S_IRWXO


@pytest.fixture
def permissive_umask() -> Iterator[None]:
    # Pin a common permissive umask so the assertions do not depend on the runner's umask.
    original_umask = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(original_umask)


@pytest.fixture
def private_file_snapshot(tmp_path: Path, permissive_umask: None) -> SnapshotRecord:
    live_path = tmp_path / "home" / "credentials.conf"
    live_path.parent.mkdir()
    live_path.write_text("token = secret\n", encoding="utf-8")
    live_path.chmod(0o600)

    # Pre-existing installs may already have a broad snapshots store directory.
    snapshot_store = tmp_path / "snapshots"
    snapshot_store.mkdir()
    snapshot_store.chmod(0o755)

    plan = make_package_plan(
        operation="push",
        repo_name="sandbox",
        package_id="app",
        requested_profile="default",
        target_plans=[
            TargetPlan(
                package_id="app",
                target_name="credentials",
                repo_path=tmp_path / "repo" / "credentials.conf",
                live_path=live_path,
                action="update",
                target_kind="file",
                projection_kind="raw",
            )
        ],
    )
    snapshot = create_push_snapshot(
        [plan],
        SnapshotConfig(enabled=True, path=snapshot_store, max_generations=5),
    )
    assert snapshot is not None
    return snapshot


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_push_snapshot_blob_of_private_file_is_not_group_or_other_accessible(
    private_file_snapshot: SnapshotRecord,
) -> None:
    (entry,) = private_file_snapshot.entries
    assert entry.content_path is not None
    blob_path = private_file_snapshot.root / entry.content_path

    assert oct(_mode(blob_path) & GROUP_AND_OTHER_BITS) == oct(0)


def test_push_snapshot_generation_directory_is_owner_only_inside_broad_store(
    private_file_snapshot: SnapshotRecord,
) -> None:
    assert oct(_mode(private_file_snapshot.root)) == oct(0o700)
