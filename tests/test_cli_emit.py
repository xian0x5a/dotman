from __future__ import annotations

from pathlib import Path

from dotman.capture import CaptureError
from dotman.cli_emit import emit_error
from dotman.models import ResolvedPackageIdentity
from dotman.planning import TrackedPackageProfileConflictError
from dotman.templates import JinjaRenderError


class StructuredGenericError(ValueError):
    def __init__(self, path: Path, detail: str) -> None:
        self.path = path
        self.detail = detail
        super().__init__(detail)


def test_emit_error_includes_path_for_structured_generic_errors(capsys) -> None:
    error = StructuredGenericError(path=Path("/tmp/example.txt"), detail="boom")

    emit_error(error, use_color=False)

    error_output = capsys.readouterr().err
    assert ":: StructuredGenericError" in error_output
    assert "path:" in error_output
    assert "detail:" in error_output
    assert "/tmp/example.txt" in error_output
    assert "boom" in error_output


def test_emit_error_keeps_the_layout_of_a_detail_but_escapes_its_unprintable_characters(capsys) -> None:
    """A detail is free text that may span lines on purpose."""
    error = StructuredGenericError(path=Path("/tmp/example.txt"), detail="first line\n\tsecond line\x1b[31m")

    emit_error(error, use_color=False)

    error_output = capsys.readouterr().err
    assert "detail: first line\n\tsecond line\\u001b[31m\n" in error_output


def test_emit_error_escapes_every_unprintable_character_in_a_path_field(capsys) -> None:
    """A path is one field of one line: a line break in it would start a line the error never printed."""
    error = StructuredGenericError(path=Path("/tmp/a\nforged: line\x1b[31m"), detail="boom")

    emit_error(error, use_color=False)

    error_output = capsys.readouterr().err
    assert "path: /tmp/a\\u000aforged: line\\u001b[31m\n" in error_output


def test_emit_error_styles_capture_errors_with_capture_header(capsys) -> None:
    error = CaptureError(path=Path("/tmp/review.txt"), detail="broken projection")

    emit_error(error, use_color=False)

    error_output = capsys.readouterr().err
    assert ":: CaptureError" in error_output
    assert "path:" in error_output
    assert "detail:" in error_output
    assert "/tmp/review.txt" in error_output
    assert "broken projection" in error_output


def test_emit_error_uses_error_type_name_for_jinja_errors(capsys) -> None:
    error = JinjaRenderError(path=Path("/tmp/template.txt"), detail="missing value")

    emit_error(error, use_color=False)

    error_output = capsys.readouterr().err
    assert ":: JinjaRenderError" in error_output
    assert "path:" in error_output
    assert "detail:" in error_output
    assert "/tmp/template.txt" in error_output
    assert "missing value" in error_output


def test_emit_error_styles_profile_conflict_selectors_and_package_identity(capsys) -> None:
    error = TrackedPackageProfileConflictError(
        package_identity=ResolvedPackageIdentity(repo="fixture", package_id="shared", bound_profile=None),
        conflict_kind="ambiguous_implicit",
        contenders=(
            "fixture:shared@basic required by fixture:meta-a@basic",
            "fixture:shared@work required by fixture:meta-b@work",
        ),
    )

    emit_error(error, use_color=True)

    error_output = capsys.readouterr().err
    assert "ambiguous implicit profile contexts for \033[2;34mfixture\033[0m\033[2m:\033[0m\033[1mshared\033[0m:" in error_output
    assert "\033[2;34mfixture\033[0m\033[2m:\033[0m\033[1mshared\033[0m\033[2m@basic\033[0m" in error_output
    assert " required by " in error_output
    assert "\033[2mrequired by\033[0m" not in error_output
    assert "\033[2;34mfixture\033[0m\033[2m:\033[0m\033[1mmeta-b\033[0m\033[2m@work\033[0m" in error_output
