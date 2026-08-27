"""app/macros.py -- the three macro kinds, expansion, and the sanitizer."""
import unittest

from app import macros
from app.cancellation import html_to_text


class NameRulesTest(unittest.TestCase):
    def test_a_plain_name_is_accepted(self):
        macros.validate_name("studio")
        macros.validate_name("_leading_underscore")
        macros.validate_name("lux_gym2")

    def test_a_name_longer_than_twenty_characters_is_refused(self):
        macros.validate_name("a" * 20)
        with self.assertRaises(macros.MacroError):
            macros.validate_name("a" * 21)

    def test_a_name_cannot_start_with_a_digit_or_hold_punctuation(self):
        for bad in ("2nd", "with-hyphen", "with space", "dot.dot", ""):
            with self.assertRaises(macros.MacroError):
                macros.validate_name(bad)

    def test_a_name_cannot_start_with_a_sigil(self):
        # The sigils mark names the SYSTEM owns; one of the operator's
        # own can never begin with them, or {{!x}} would be ambiguous.
        for bad in ("!system", "$dynamic"):
            with self.assertRaises(macros.MacroError):
                macros.validate_name(bad)


class KindTest(unittest.TestCase):
    def test_the_sigil_decides_the_kind(self):
        self.assertEqual(macros.kind_of("{{studio}}"), macros.USER)
        self.assertEqual(macros.kind_of("{{!retention_months}}"), macros.SYSTEM)
        self.assertEqual(macros.kind_of("{{$name}}"), macros.DYNAMIC)

    def test_names_used_can_be_limited_to_one_kind(self):
        text = "{{studio}} {{!r}} {{$name}} {{studio}}"
        self.assertEqual(macros.names_used(text), ["studio", "r", "name"])
        self.assertEqual(macros.names_used(text, macros.SYSTEM), ["r"])
        self.assertEqual(macros.names_used(text, macros.USER), ["studio"])


class ExpandTest(unittest.TestCase):
    def test_each_kind_resolves_from_its_own_table(self):
        out = macros.expand(
            "{{studio}} / {{!retention_months}} / {{$name}}",
            user={"studio": "Studio"}, system={"retention_months": "24"},
            dynamic={"name": "Ada"}, rich=True,
        )
        self.assertEqual(out, "Studio / 24 / Ada")

    def test_a_name_from_the_wrong_namespace_does_not_resolve(self):
        # The whole point of the sigils: the app owning "name" cannot
        # collide with an operator macro also called "name".
        with self.assertRaises(macros.MacroError):
            macros.expand("{{name}}", dynamic={"name": "Ada"}, rich=True)

    def test_an_unknown_macro_raises_rather_than_vanishing(self):
        with self.assertRaises(macros.MacroError) as caught:
            macros.expand("{{nope}}", user={"studio": "S"}, rich=True)
        self.assertIn("studio", str(caught.exception))

    def test_expansion_is_one_pass_so_a_value_cannot_drive_it(self):
        out = macros.expand("{{a}}", user={"a": "{{b}}", "b": "never"}, rich=True)
        self.assertEqual(out, "{{b}}")

    def test_a_plain_context_reduces_markup_to_its_text(self):
        # Not refused for being rich -- reduced, so a macro works in
        # every field and there is no rule to learn about where it may go.
        out = macros.expand(
            "Cancel: {{hint}}", rich=False, to_text=html_to_text,
            user={"hint": 'under <a href="https://x/my">x/my</a>'},
        )
        self.assertNotIn("<a", out)
        self.assertIn("x/my", out)


class SanitizeTest(unittest.TestCase):
    def test_allowed_markup_survives(self):
        result = macros.sanitize('<p><b>Bold</b> and <a href="https://x">a link</a></p>')
        self.assertEqual(result.html, '<p><b>Bold</b> and <a href="https://x">a link</a></p>')
        self.assertEqual(result.dropped, [])

    def test_a_script_is_dropped_with_its_content(self):
        result = macros.sanitize("before<script>alert(1)</script>after")
        self.assertEqual(result.html, "beforeafter")
        self.assertIn("<script>", result.dropped)

    def test_event_handlers_and_styles_are_dropped(self):
        result = macros.sanitize('<b onclick="steal()" style="x">hi</b>')
        self.assertEqual(result.html, "<b>hi</b>")
        self.assertIn("b[onclick]", result.dropped)
        self.assertIn("b[style]", result.dropped)

    def test_a_javascript_url_is_dropped_but_the_link_text_stays(self):
        result = macros.sanitize('<a href="javascript:alert(1)">click</a>')
        self.assertEqual(result.html, "<a>click</a>")
        self.assertIn("a[href]", result.dropped)

    def test_a_data_url_is_dropped_too(self):
        result = macros.sanitize('<a href="data:text/html;base64,PHNjcmlwdD4=">x</a>')
        self.assertIn("a[href]", result.dropped)

    def test_ordinary_urls_are_kept(self):
        for url in ("https://x/y", "http://x", "mailto:a@b.c", "tel:+352", "/my", "#top"):
            with self.subTest(url=url):
                self.assertIn(url, macros.sanitize(f'<a href="{url}">l</a>').html)

    def test_target_blank_gains_noopener(self):
        result = macros.sanitize('<a href="https://x" target="_blank">l</a>')
        self.assertIn('rel="noopener noreferrer"', result.html)

    def test_an_iframe_is_dropped_with_its_content(self):
        result = macros.sanitize("<iframe src='https://evil'>fallback</iframe>tail")
        self.assertEqual(result.html, "tail")

    def test_text_is_escaped_so_it_cannot_become_markup(self):
        self.assertEqual(macros.sanitize("a < b & c").html, "a &lt; b &amp; c")

    def test_a_comment_is_not_kept(self):
        self.assertEqual(macros.sanitize("a<!-- hidden -->b").html, "ab")


if __name__ == "__main__":
    unittest.main()
