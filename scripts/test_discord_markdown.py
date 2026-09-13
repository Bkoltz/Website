#!/usr/bin/env python3
"""
Tests for the Discord markdown renderer.

The renderer is the security boundary between untrusted Discord text and
webodm.org, so the bulk of these cases are adversarial.

Run: python -m unittest discover -s scripts -p 'test_*.py'
"""

import unittest

from discord_markdown import RenderContext, plain_text, render


class TestEscaping(unittest.TestCase):
    """Nothing user-supplied may reach the page as live markup."""

    def test_script_tag_is_escaped(self):
        out = render("<script>alert(1)</script>")
        self.assertNotIn("<script>", out)
        self.assertIn("&lt;script&gt;", out)

    def test_img_onerror_is_escaped(self):
        out = render('<img src=x onerror=alert(1)>')
        self.assertNotIn("<img src=x", out)
        self.assertIn("&lt;img", out)

    def test_attribute_breakout_is_escaped(self):
        out = render('" onmouseover="alert(1)')
        self.assertNotIn('onmouseover="alert', out)
        self.assertIn("&quot;", out)

    def test_javascript_url_is_not_linked(self):
        out = render("[click](javascript:alert(1))")
        self.assertNotIn("<a", out)
        self.assertNotIn("javascript:", out.lower().replace("&#x27;", ""))

    def test_data_url_is_not_linked(self):
        out = render("[click](data:text/html,<script>alert(1)</script>)")
        self.assertNotIn("<a", out)

    def test_vbscript_url_is_not_linked(self):
        self.assertNotIn("<a", render("[x](vbscript:msgbox(1))"))

    def test_html_inside_code_fence_stays_inert(self):
        out = render("```\n<script>alert(1)</script>\n```")
        self.assertIn("<pre><code>", out)
        self.assertIn("&lt;script&gt;", out)
        self.assertNotIn("<script>", out)

    def test_zola_shortcode_syntax_is_escaped(self):
        # Zola interpolates {{ }} in page bodies; front matter is safe, but the
        # rendered HTML must not carry live template syntax regardless.
        out = render("{{ config.base_url }} and {% if true %}x{% endif %}")
        self.assertIn("{{ config.base_url }}", out)
        self.assertNotIn("<", out.replace("<p>", "").replace("</p>", ""))

    def test_close_script_sequence_is_escaped(self):
        self.assertNotIn("</script>", render("</script><script>alert(1)</script>"))


class TestFormatting(unittest.TestCase):
    """Ordinary Discord markdown renders the way people expect."""

    def test_bold_italic_strike(self):
        self.assertIn("<strong>b</strong>", render("**b**"))
        self.assertIn("<em>i</em>", render("*i*"))
        self.assertIn("<del>s</del>", render("~~s~~"))
        self.assertIn("<u>u</u>", render("__u__"))

    def test_bold_italic_combined(self):
        self.assertIn("<strong><em>x</em></strong>", render("***x***"))

    def test_spoiler(self):
        self.assertIn('<span class="d-spoiler" tabindex="0">hidden</span>', render("||hidden||"))

    def test_fenced_code_with_language(self):
        out = render("```python\nprint(1)\n```")
        self.assertIn('<pre><code class="language-python">print(1)</code></pre>', out)

    def test_unknown_language_is_not_echoed(self):
        out = render("```evil\"onload=x\nbody\n```")
        # No language class is emitted, and the stray text stays escaped inside
        # the code element rather than becoming an attribute.
        self.assertIn("<pre><code>", out)
        self.assertNotIn('class="language-', out)
        self.assertNotIn('"onload', out)
        self.assertIn("&quot;onload", out)

    def test_inline_code(self):
        self.assertIn("<code>--min-num-features</code>", render("`--min-num-features`"))

    def test_blockquote(self):
        self.assertIn("<blockquote>", render("> quoted"))

    def test_lists(self):
        self.assertIn("<ul>", render("- one\n- two"))
        self.assertIn("<ol>", render("1. one\n2. two"))

    def test_headings_never_emit_h1_or_h2(self):
        out = render("# Big\n\n## Mid\n\n### Small")
        self.assertNotIn("<h1", out)
        self.assertNotIn("<h2", out)
        self.assertIn("<h3>Big</h3>", out)

    def test_paragraphs_and_line_breaks(self):
        out = render("one\ntwo\n\nthree")
        self.assertIn("one<br>two", out)
        self.assertEqual(out.count("<p>"), 2)

    def test_fenced_block_is_not_wrapped_in_a_paragraph(self):
        out = render("before\n\n```python\nprint(1)\n```\n\nafter")
        self.assertNotIn("<p><pre>", out)
        self.assertIn("</p>\n<pre>", out)

    def test_unterminated_fence_does_not_crash(self):
        self.assertIsInstance(render("```python\nprint(1)"), str)


class TestLinks(unittest.TestCase):
    """User-posted links must never pass PageRank."""

    def test_external_link_is_nofollow_ugc(self):
        out = render("https://example.com/page")
        self.assertIn('rel="nofollow ugc noopener"', out)
        self.assertIn('target="_blank"', out)

    def test_internal_link_is_followed(self):
        out = render("https://docs.webodm.org/guide")
        self.assertIn('<a href="https://docs.webodm.org/guide">', out)
        self.assertNotIn("nofollow", out)

    def test_markdown_link_label_is_escaped(self):
        out = render("[<b>x</b>](https://example.com)")
        self.assertIn("&lt;b&gt;", out)
        self.assertNotIn("<b>", out)

    def test_trailing_punctuation_not_swallowed(self):
        out = render("see https://example.com/a.")
        self.assertIn('href="https://example.com/a"', out)


class TestDiscordEntities(unittest.TestCase):
    """Ids are resolved to names; raw snowflakes never reach the page."""

    def setUp(self):
        self.ctx = RenderContext(
            users={"111": "jane.doe"},
            channels={"222": "help"},
            roles={"333": "maintainer"},
            emoji={"444": "/images/help/emoji/444.webp"},
        )

    def test_user_mention_resolved(self):
        out = render("<@111> hi", self.ctx)
        self.assertIn("@jane.doe", out)
        self.assertNotIn("111", out)

    def test_unknown_mention_falls_back(self):
        self.assertIn("@user", render("<@999>", self.ctx))

    def test_channel_and_role_mentions(self):
        self.assertIn("#help", render("<#222>", self.ctx))
        self.assertIn("@maintainer", render("<@&333>", self.ctx))

    def test_custom_emoji_mirrored(self):
        out = render("<:smile:444>", self.ctx)
        self.assertIn('src="/images/help/emoji/444.webp"', out)

    def test_unmirrored_emoji_degrades_to_text(self):
        self.assertIn(":smile:", render("<:smile:555>", self.ctx))

    def test_timestamp_becomes_time_element(self):
        out = render("<t:1712912504:f>", self.ctx)
        self.assertIn("<time datetime=", out)

    def test_mass_ping_is_defused(self):
        out = render("@everyone please help")
        self.assertNotIn("@everyone", out)
        self.assertIn("everyone", out)


class TestRedaction(unittest.TestCase):
    """People paste secrets into help channels constantly."""

    def test_email_removed(self):
        self.assertNotIn("someone@example.com", render("mail someone@example.com"))

    def test_aws_key_redacted(self):
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", render("key AKIAIOSFODNN7EXAMPLE"))

    def test_github_token_redacted(self):
        out = render("ghp_" + "a" * 36)
        self.assertNotIn("ghp_" + "a" * 36, out)

    def test_password_assignment_redacted(self):
        self.assertIn("[redacted]", render("password=hunter2000"))

    def test_foreign_invite_removed_but_ours_kept(self):
        self.assertIn("[invite link removed]", render("join discord.gg/abcd1234"))
        self.assertIn("RxHPXCSMBS", render("join discord.gg/RxHPXCSMBS"))


class TestRobustness(unittest.TestCase):
    """Weird input must degrade, never raise."""

    def test_empty_and_none(self):
        self.assertEqual(render(""), "")
        self.assertEqual(render(None), "")

    def test_bidi_override_stripped(self):
        self.assertNotIn("‮", render("safe‮gnp.exe"))

    def test_zero_width_stripped(self):
        self.assertNotIn("​", render("a​b"))

    def test_very_long_line(self):
        self.assertIsInstance(render("x" * 20000), str)

    def test_toml_delimiters_are_escaped(self):
        # Quotes are HTML-escaped, so rendered output can never contain a raw
        # TOML multi-line delimiter to break out of the front matter with.
        out = render("text with ''' and \"\"\" inside")
        self.assertNotIn("'''", out)
        self.assertNotIn('"""', out)
        self.assertIn("&#x27;", out)


class TestPlainText(unittest.TestCase):
    """plain_text feeds meta descriptions and JSON-LD, so it must carry no markup."""

    def test_strips_markup_and_code(self):
        out = plain_text("**bold** `code` ```py\nx=1\n``` https://example.com tail")
        self.assertNotIn("**", out)
        self.assertNotIn("https://", out)
        self.assertIn("bold", out)

    def test_no_angle_brackets(self):
        self.assertNotIn("<", plain_text("<script>alert(1)</script>"))

    def test_truncates_on_word_boundary(self):
        out = plain_text("alpha beta gamma delta epsilon zeta", limit=20)
        self.assertLessEqual(len(out), 21)
        self.assertTrue(out.endswith("…"))

    def test_intra_word_underscores_survive(self):
        # base_url and --min-num-features are content, not emphasis markers.
        out = plain_text("set base_url and pass --min-num-features 12000")
        self.assertIn("base_url", out)
        self.assertIn("--min-num-features", out)

    def test_emphasis_underscores_are_stripped(self):
        self.assertEqual(plain_text("this is __bold__ here"), "this is bold here")

    def test_redaction_applies(self):
        self.assertNotIn("a@b.com", plain_text("mail a@b.com"))


if __name__ == "__main__":
    unittest.main()
