"""Installed-parser regressions for ordered inline table evidence."""

from __future__ import annotations

import pytest

from keystone_agents.tools.website_extraction_tool import extract_website_content_from_html


def _extract(body: str) -> str:
    html = (
        "<article><h1>Synthetic interface documentation</h1>"
        "<p>This report describes the available fields and their qualifications. "
        "Independent review is required before operational use.</p>"
        + body
        + "<p>Interpret values only with the accompanying limitations.</p></article>"
    )
    return extract_website_content_from_html(
        html, url="https://example.org/reference", company_name="Synthetic interface"
    ).text_or_markdown


def test_table_keeps_nested_code_links_and_all_following_tails_in_the_same_row():
    text = _extract("""
    <table><tr><th>Field</th><th>Meaning</th></tr>
    <tr><td><code>scopeMode</code></td><td>
      <p><code>integer (<a href="/format">unsigned</a> format)</code></p>
      <p>Include records from <code>ACTIVE</code> and <code>ARCHIVED</code> only after review.</p>
      <p>Use <a href="/query"><code>owner:current</code> query syntax</a>;
      this parameter is unavailable in the <code>minimal</code> scope.</p>
    </td></tr>
    <tr><td><code>recordKey</code></td><td><p>Returns <code>id</code> and
      <code>groupId</code>; neither implies full content.</p></td></tr>
    </table>""")
    rows = text.splitlines()
    scope = next(row for row in rows if "scopeMode" in row)
    assert "integer ([unsigned](https://example.org/format) format)" in scope
    assert "Include records from `ACTIVE` and `ARCHIVED` only after review." in scope
    assert "[`owner:current` query syntax](https://example.org/query)" in scope
    assert "unavailable in the `minimal` scope." in scope
    assert scope.index("only after review") < scope.index("Use ") < scope.index("unavailable")
    record = next(row for row in rows if "recordKey" in row)
    assert "Returns `id` and `groupId`; neither implies full content." in record
    assert "recordKey" not in scope and "scopeMode" not in record


@pytest.mark.parametrize("deleted_tag", ["del", "s", "strike"])
def test_table_revision_semantics_survive_nested_markup_and_multiple_paragraphs(deleted_tag):
    table = """<table><tr><th>Decision</th><th>Revision</th></tr>
    <tr><td>Release</td><td><p>
    <{deleted_tag}>Approved for <code>external</code> use.</{deleted_tag}>
    <ins>NOT approved; <strong>review required</strong>.</ins></p>
    <p>See <a href="/review"><span>independent</span> review</a> before proceeding.</p>
    <p>Current terms override the removed statement.</p></td></tr></table>"""
    text = _extract(table.format(deleted_tag=deleted_tag))
    row = next(line for line in text.splitlines() if "Release" in line)
    assert "~~Approved for `external` use.~~" in row
    assert "NOT approved; **review required**." in row
    assert "[independent review](https://example.org/review) before proceeding." in row
    assert "Current terms override the removed statement." in row
    opposite = (
        table.format(deleted_tag=deleted_tag)
        .replace(f"<{deleted_tag}>", "<ins>")
        .replace(f"</{deleted_tag}>", "</ins>")
        .replace("<ins>NOT approved;", f"<{deleted_tag}>NOT approved;")
        .replace("review required</strong>.</ins>", f"review required</strong>.</{deleted_tag}>")
    )
    opposite_text = _extract(opposite)
    assert opposite_text != text
    assert "~~NOT approved; **review required**.~~" in opposite_text
    assert "~~Approved for `external` use.~~" not in opposite_text


def test_table_inline_literals_footnote_and_subscript_preserve_meaning():
    text = _extract("""<table><tr><th>Metric</th><th>Value</th></tr>
    <tr><td>Threshold</td><td>10<sup>2</sup> units; CO<sub>2</sub> is excluded.
    <code>left|right</code><sup><a href="#note">1</a></sup></td></tr>
    </table><p id="note">Footnote: the threshold does NOT establish approval.</p>""")
    row = next(line for line in text.splitlines() if "Threshold" in line)
    assert "10^{2} units; CO_{2} is excluded." in row
    assert r"`left\|right`" in row
    assert "[1](https://example.org/reference#note)" in row
    assert "threshold does NOT establish approval" in text
