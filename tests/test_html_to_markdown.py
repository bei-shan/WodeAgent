"""HTML to Markdown extraction used by WebFetch."""
from __future__ import annotations

from core.runtime.html_to_markdown import html_to_markdown


def test_headings_links_lists_and_code_survive():
    html = """
    <html><head><title>Guide</title></head><body>
      <h2>Install</h2>
      <p>Run <code>pip install x</code> then see <a href="/docs">the docs</a>.</p>
      <ul><li>one</li><li>two</li></ul>
      <pre>print("hi")\nprint("bye")</pre>
    </body></html>
    """
    md = html_to_markdown(html, base_url="https://example.com/guide")
    assert md.startswith("# Guide")
    assert "## Install" in md
    assert "`pip install x`" in md
    assert "[the docs](https://example.com/docs)" in md
    assert "- one" in md and "- two" in md
    assert "```" in md and 'print("hi")' in md


def test_main_region_wins_over_navigation():
    html = """
    <html><body>
      <nav><a href="/">Home</a> Nav junk</nav>
      <main><h1>Real content</h1><p>Body text.</p></main>
      <footer>Footer junk</footer>
    </body></html>
    """
    md = html_to_markdown(html, base_url="https://example.com/")
    assert "Real content" in md and "Body text." in md
    assert "Nav junk" not in md
    assert "Footer junk" not in md


def test_scripts_and_styles_are_dropped():
    html = "<html><body><script>alert(1)</script><style>.a{}</style><p>Keep me</p></body></html>"
    md = html_to_markdown(html)
    assert "Keep me" in md
    assert "alert(1)" not in md
    assert ".a{}" not in md


def test_tables_become_markdown_tables():
    html = """
    <table>
      <tr><th>Name</th><th>Value</th></tr>
      <tr><td>alpha</td><td>1</td></tr>
    </table>
    """
    md = html_to_markdown(html)
    assert "| Name | Value |" in md
    assert "| --- | --- |" in md
    assert "| alpha | 1 |" in md


def test_empty_and_malformed_input_do_not_raise():
    assert html_to_markdown("") == ""
    assert html_to_markdown("<p>unclosed") .strip().startswith("unclosed")


def test_relative_links_resolve_against_base():
    md = html_to_markdown('<a href="sub/page">x</a>', base_url="https://example.com/a/b")
    assert "[x](https://example.com/a/sub/page)" in md
