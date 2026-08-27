"""app/email_templates.py -- 2026-07-09: support for MACROS in
the templates was added, building on VARIABLE support already present, so
cancel_email.html for instance can DEFINE how the final email is
assembled -- see that module's own docstring for the full design
(variables and macros are the same substitution mechanism; only the
ASSEMBLY ORDER moves into the template file, not what each piece
renders)."""
import tempfile
import unittest
from pathlib import Path

from app.email_templates import load_email_template, render_template

from .helpers import make_settings


class RenderTemplateTest(unittest.TestCase):
    def test_substitutes_a_single_placeholder(self):
        self.assertEqual(render_template("Hello {{$name}}!", name="Alice"), "Hello Alice!")

    def test_substitutes_multiple_placeholders_in_any_order(self):
        rendered = render_template("{{$b}}{{$a}}{{$b}}", a="A", b="B")
        self.assertEqual(rendered, "BAB")

    def test_missing_context_value_raises_key_error_naming_the_placeholder(self):
        with self.assertRaises(KeyError) as ctx:
            render_template("Hello {{$typo}}!", name="Alice")
        self.assertIn("typo", str(ctx.exception))

    def test_a_values_own_literal_braces_are_not_re_scanned(self):
        # A guest-typed message containing literal "{{...}}" text must not
        # be treated as a second round of template substitution -- this is
        # a single, one-pass replacement, not a recursive one.
        rendered = render_template("Message: {{$message}}", message="see {{$tag}} in my notes")
        self.assertEqual(rendered, "Message: see {{$tag}} in my notes")

    def test_no_placeholders_at_all_returns_the_text_unchanged(self):
        self.assertEqual(render_template("plain text, no macros here"), "plain text, no macros here")


class LoadEmailTemplateTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def test_uses_the_built_in_template_when_no_folder_is_configured(self):
        settings = make_settings(email_templates_folder="")
        text = load_email_template(settings, "cancel_email.txt")
        self.assertIn("{{$intro}}", text)

    def test_uses_the_built_in_template_when_folder_is_configured_but_file_is_missing_there(self):
        # Partial customization: a folder with only SOME templates
        # overridden must still resolve the others to the built-in copy,
        # not error out.
        settings = make_settings(email_templates_folder=str(self.dir))
        text = load_email_template(settings, "cancel_email.txt")
        self.assertIn("{{$intro}}", text)

    def test_a_file_in_the_configured_folder_overrides_the_built_in_one(self):
        (self.dir / "cancel_email.txt").write_text("Custom wording: {{$intro}}")
        settings = make_settings(email_templates_folder=str(self.dir))
        text = load_email_template(settings, "cancel_email.txt")
        self.assertEqual(text, "Custom wording: {{$intro}}")

    def test_raises_file_not_found_when_neither_place_has_it(self):
        settings = make_settings(email_templates_folder=str(self.dir))
        with self.assertRaises(FileNotFoundError):
            load_email_template(settings, "no-such-template.txt")


if __name__ == "__main__":
    unittest.main()
