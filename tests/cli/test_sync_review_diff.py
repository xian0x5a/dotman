from rich.console import Console

from dotman.sync_base_store import FilePresent
from dotman.sync_deck import REVIEW_CHANGE_KEY, ReviewDocument, ReviewSection, _review_difference


def review_of(before: bytes, after: bytes) -> ReviewDocument:
    diff = _review_difference(FilePresent(before), FilePresent(after), before_label="repo", after_label="live",
                              description="file", use_color=True)
    return ReviewDocument("Review", "subject", (ReviewSection("Diff", tuple(diff)),), use_color=True)


def change_stop_lines(document: ReviewDocument) -> list[str]:
    console = Console(width=80, color_system=None)
    rows = console.render_lines(document.renderable(), console.options, pad=False)
    return ["".join(segment.text for segment in row).strip() for row in rows
            if any(segment.style and segment.style.meta.get(REVIEW_CHANGE_KEY) for segment in row)]


def test_content_lines_starting_with_dashes_or_pluses_render_as_changes_not_headers():
    # Removing "-- note" and adding "++count" yields "--- note" / "+++count" diff lines.
    document = review_of(b"keep\n-- note\n", b"keep\n++count\n")
    lines = [line.strip() for line in document.text().splitlines()]
    styled = lambda code, marker, content: f"\x1b[{code}m{marker}\x1b[0m\x1b[{code}m{content}\x1b[0m"
    assert styled(31, "-", "-- note") in lines and styled(32, "+", "++count") in lines
    # The real file headers stay bold.
    assert styled(1, "-", "-- repo") in lines and styled(1, "+", "++ live") in lines
    # The change block made only of such lines still gets a navigation stop.
    assert change_stop_lines(document) == ["--- note"]
