from types import SimpleNamespace

from dotman.cli_style import render_sync_term
from dotman.sync_deck_command import sync_document
from dotman.sync_session import SetApproval, SetResolutionIntent
from tests.engine.test_sync_convergence import command
from tests.engine.test_sync_session import make_engine, open_session


def test_failure_document_preserves_partial_unit_and_unattempted_effects(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, monkeypatch, [
        ("unit", "both", b"old", b"live",
         'render = "sed s/live/published/ $DOTMAN_SOURCE"\ncompare = { repo = "raw", live = "raw" }\n'
         '[targets.unit.hooks]\npre_push = "exit 7"'),
    ])
    with open_session(engine, preview=False) as session:
        command(session, SetResolutionIntent, "main:app.unit", "use-live")
        command(session, SetApproval, "main:app.unit", True)
        result = session.execute().result
        document = sync_document(SimpleNamespace(dry_run=False, no_confirm=False, scopes=[]), session, result)
    assert document["sync_units"][0]["result"] == "not-converged"
    assert [(step["action"], step["status"]) for step in document["stages"]] == [
        ("update", "ok"), ("pre_push", "failed"), ("write", "unattempted"), ("complete", "unattempted"),
    ]
    assert all(step["scope_identity"] == "main:app.unit" for step in document["stages"])
    assert all(step["skip_reason"] == "earlier-failure" for step in document["stages"]
               if step["status"] == "unattempted")


def test_failure_terms_use_shared_warning_and_skipped_styles():
    assert render_sync_term("not-converged", use_color=True) != "not-converged"
    assert render_sync_term("unattempted", use_color=True) == render_sync_term(
        "skipped", use_color=True).replace("skipped", "unattempted")
