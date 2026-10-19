"""Planning commands run concurrently, yet results keep target order."""

from tests.engine.test_sync_session import make_engine, open_session


def _rendezvous(signals, mine: str, other: str, then: str) -> str:
    # Announce, then wait up to ~2s for the sibling. Serial planning can never
    # satisfy both sides, so each command fails unless they overlap.
    return (
        f"touch {signals / mine}; i=0; "
        f"while [ ! -e {signals / other} ] && [ $i -lt 100 ]; do sleep 0.02; i=$((i+1)); done; "
        f"[ -e {signals / other} ] && {then}"
    )


def test_comparison_projections_overlap_and_keep_target_order(tmp_path, monkeypatch):
    signals = tmp_path / "signals"
    signals.mkdir()

    def target(name, other):
        render = _rendezvous(signals, name, other, 'cat "$DOTMAN_REPO_PATH"')
        return (name, "both", b"same", b"same",
                f"render = '{render}'\ncompare = {{ repo = \"render\", live = \"raw\" }}")

    engine = make_engine(tmp_path, monkeypatch, [target("first", "second"), target("second", "first")])
    with open_session(engine) as session:
        observations = session.view.observations

    assert [(unit.identity.target_name, unit.state, unit.diagnostics) for unit in observations] == [
        ("first", "directly-in-sync", ()),
        ("second", "directly-in-sync", ()),
    ]


def test_probes_overlap_and_keep_target_order(tmp_path, monkeypatch):
    from tests.engine.test_sync_auxiliary import auxiliary_engine

    signals = tmp_path / "signals"
    signals.mkdir()
    engine = auxiliary_engine(tmp_path, monkeypatch, f"""
[targets.first]
probe = '{_rendezvous(signals, "first", "second", "true")}'
[targets.second]
probe = '{_rendezvous(signals, "second", "first", "true")}'
""")
    with open_session(engine) as session:
        rows = session.view.rows

    assert [(row.kind, row.scope) for row in rows] == [
        ("probe", "main:app.first"),
        ("probe", "main:app.second"),
    ]


def test_failing_probes_report_the_earlier_target(tmp_path, monkeypatch):
    from dotman.sync_session import SessionOpenFailed
    from tests.engine.test_sync_auxiliary import auxiliary_engine

    engine = auxiliary_engine(tmp_path, monkeypatch, """
[targets.first]
probe = "sleep 0.3; exit 3"
[targets.second]
probe = "exit 4"
""")
    opened = engine.open_sync_session(engine.resolve_sync_scope(), preview=True)

    assert isinstance(opened, SessionOpenFailed)
    assert "main:app.first with status 3" in opened.diagnostic.message
