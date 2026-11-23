"""The Sources panel's pure rules (#354), in both languages.

`static/source_href.js` decides in the browser which links the panel may frame, and
`source_panel.is_source_path` decides on the server which citations get a card; they are one
rule, so one table of cases runs against both. `static/panel_state.js` is the panel's states
and the events between them, run under Node as `markers.js` is.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import cast

import pytest

from compliance_rag.api.sign_in import STATIC_DIR
from compliance_rag.api.source_panel import is_source_path

NODE = shutil.which("node")
NEEDS_NODE = pytest.mark.skipif(
    NODE is None and not os.environ.get("CI"),  # in CI a missing node fails, it does not skip
    reason="node is not installed",
)

OK = "/v1/sources/privacy-act-1988/C2026C00227#clause-sch1-app11-1"

# (href, whether the panel may frame it)
CASES = [
    # valid: a law's, a policy's (its instrument id holds a slash), a dot inside a segment
    (OK, True),
    ("/v1/sources/acme/privacy-policy/v1#clause-3-1", True),
    ("/v1/sources/acme/doc/v1#clause-p3", True),
    ("/v1/sources/iso-27001/v1.0#clause-a-8-3", True),
    ("/v1/sources/x/v1?q=1#clause-a", True),
    # dot segments, literal and encoded in every case, alone or mixed: the browser resolves
    # these before it asks, so `/v1/sources/../../logout` is `/logout`
    ("/v1/sources/../../logout", False),
    ("/v1/sources/x/../../../logout", False),
    ("/v1/sources/./x/v1", False),
    ("/v1/sources/x/./v1", False),
    ("/v1/sources/x/v1/..", False),
    ("/v1/sources/%2e%2e/%2e%2e/logout", False),
    ("/v1/sources/%2E%2E/logout", False),
    ("/v1/sources/%2e./logout", False),
    ("/v1/sources/.%2E/logout", False),
    ("/v1/sources/%2e/x/v1", False),
    ("/v1/sources/x/%2E/v1", False),
    ("/v1/sources/..?x=1", False),
    ("/v1/sources/x/..#clause-a", False),
    # empty segments
    ("/v1/sources//logout", False),
    ("/v1/sources/x//v1", False),
    ("/v1/sources/x/v1/", False),
    ("/v1/sources/", False),
    # a slash or a backslash hidden by encoding, or not hidden
    ("/v1/sources/x%2f..%2flogout/v1", False),
    ("/v1/sources/x%5c..%5clogout/v1", False),
    ("/v1/sources/x\\..\\y", False),
    # control characters
    ("/v1/sources/x\n/v1", False),
    ("/v1/sources/x\r/v1", False),
    ("/v1/sources/x\t/v1", False),
    ("/v1/sources/x\x00/v1", False),
    ("/v1/sources/x\x7f/v1", False),
    # not the source route at all
    ("/checks/abc#clause-p3", False),
    ("https://evil.example/v1/sources/x/v1", False),
    ("//evil.example/v1/sources/x/v1", False),
    ("javascript:alert(1)", False),
    ("/v1/answers", False),
    ("v1/sources/x/v1", False),
    ("", False),
]


@pytest.mark.parametrize(("href", "allowed"), CASES)
def test_the_server_gives_a_card_only_to_a_path_under_the_source_route(
    href: str, allowed: bool
) -> None:
    assert is_source_path(href) is allowed


def run_node(script: str) -> object:
    assert NODE is not None
    done = subprocess.run(
        [NODE, "--input-type=module", "-e", script], capture_output=True, text=True, check=True
    )
    return json.loads(done.stdout)


def source_url(data_href: str | None, href: str | None) -> str | None:
    """`source_href.js`'s `sourceUrl`, run under Node."""
    module = (STATIC_DIR / "source_href.js").as_uri()
    result = run_node(
        f"import {{ sourceUrl }} from {json.dumps(module)};"
        f"console.log(JSON.stringify(sourceUrl({json.dumps(data_href)}, {json.dumps(href)})));"
    )
    assert result is None or isinstance(result, str)
    return result


@NEEDS_NODE
@pytest.mark.parametrize(("href", "allowed"), CASES)
def test_the_browser_frames_only_a_path_under_the_source_route(href: str, allowed: bool) -> None:
    assert source_url(None, href) == (href if allowed else None)


@NEEDS_NODE
def test_a_claims_own_source_href_wins_and_is_held_to_the_same_rule() -> None:
    assert source_url("/v1/sources/acme/doc/v1#clause-p3", "/checks/abc#clause-p3") == (
        "/v1/sources/acme/doc/v1#clause-p3"
    )
    assert source_url("", OK) == OK  # an empty one is no one
    assert source_url("/v1/answers", OK) is None  # it wins, and is refused
    assert source_url("/v1/sources/../logout", OK) is None
    assert source_url(None, None) is None


# ---- the panel's states ----------------------------------------------------------------------

LIST = {"view": "list", "collapsed": False}
SOURCE = {"view": "source", "collapsed": False}
FOLDED_LIST = {"view": "list", "collapsed": True}
FOLDED_SOURCE = {"view": "source", "collapsed": True}


def step(state: dict[str, object], event: str) -> dict[str, object]:
    """`panel_state.js`'s `next`, run under Node."""
    module = (STATIC_DIR / "panel_state.js").as_uri()
    result = run_node(
        f"import {{ next }} from {json.dumps(module)};"
        f"console.log(JSON.stringify(next({json.dumps(state)}, {json.dumps(event)})));"
    )
    assert isinstance(result, dict)
    return cast("dict[str, object]", result)


@NEEDS_NODE
@pytest.mark.parametrize(
    ("before", "event", "after"),
    [
        # open a source: it is shown, and the panel is unfolded
        (LIST, "open", SOURCE),
        (SOURCE, "open", SOURCE),  # another link, while one is open
        (FOLDED_LIST, "open", SOURCE),
        (FOLDED_SOURCE, "open", SOURCE),
        # back to the sources: the list; folding is the reader's, and back does not undo it
        (SOURCE, "back", LIST),
        (FOLDED_SOURCE, "back", FOLDED_LIST),
        (LIST, "back", LIST),
        # Escape: back from a source, and nothing otherwise
        (SOURCE, "escape", LIST),
        (FOLDED_SOURCE, "escape", FOLDED_LIST),
        (LIST, "escape", LIST),
        (FOLDED_LIST, "escape", FOLDED_LIST),
        # collapse and expand, in either view
        (LIST, "toggle", FOLDED_LIST),
        (FOLDED_LIST, "toggle", LIST),
        (SOURCE, "toggle", FOLDED_SOURCE),
        (FOLDED_SOURCE, "toggle", SOURCE),
        # a finding's link to a card: the list, unfolded
        (SOURCE, "card", LIST),
        (FOLDED_LIST, "card", LIST),
        (FOLDED_SOURCE, "card", LIST),
        (LIST, "card", LIST),
        # an event it does not know changes nothing
        (SOURCE, "nonsense", SOURCE),
    ],
)
def test_the_panels_state_follows_its_events(
    before: dict[str, object], event: str, after: dict[str, object]
) -> None:
    assert step(before, event) == after


@NEEDS_NODE
def test_the_panel_starts_on_the_list_unfolded_and_a_state_is_never_changed() -> None:
    module = (STATIC_DIR / "panel_state.js").as_uri()
    result = run_node(
        f"import {{ INITIAL, next }} from {json.dumps(module)};"
        "const before = JSON.stringify(INITIAL);"
        'next(INITIAL, "open");'
        "const frozen = Object.isFrozen(INITIAL);"
        "console.log(JSON.stringify([INITIAL, JSON.stringify(INITIAL) === before, frozen]));"
    )
    assert result == [LIST, True, True]


def test_the_panel_script_is_a_thin_layer_over_the_pure_pieces() -> None:
    script = (STATIC_DIR / "sources.js").read_text()
    assert 'from "./panel_state.js"' in script and 'from "./source_href.js"' in script
    # one click listener for the page's links, not one per kind of link
    assert script.count('document.addEventListener("click"') == 1
    for forbidden in ("innerHTML", "insertAdjacentHTML", "outerHTML", "eval(", "document.write"):
        for name in ("sources.js", "panel_state.js", "source_href.js"):
            assert forbidden not in (STATIC_DIR / name).read_text(), (name, forbidden)
