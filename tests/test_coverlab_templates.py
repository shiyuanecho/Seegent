import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "seegent/static/coverlab/assets/index-CSbX4Gkb.js"
EXTENSIONS = ROOT / "seegent/static/coverlab/assets/coverlab-extensions.css"
ENTRY = ROOT / "seegent/static/coverlab/index.html"
PARENT = ROOT / "seegent/static/index.html"

NEW_CENTERED_TEMPLATES = {
    "scaleup",
    "scaledown",
    "serifstage",
    "wideair",
    "inverseblocks",
    "outlinepair",
    "colorbeat",
    "glassnote",
    "monostatement",
    "tightstack",
    "softglow",
    "numberstage",
}


class CoverlabTemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = BUNDLE.read_text(encoding="utf-8")
        cls.css = EXTENSIONS.read_text(encoding="utf-8")
        cls.entry = ENTRY.read_text(encoding="utf-8")
        cls.parent = PARENT.read_text(encoding="utf-8")

    def _section(self, start, end):
        start_index = self.bundle.index(start)
        return self.bundle[start_index : self.bundle.index(end, start_index)]

    def test_template_registry_contains_30_unique_entries(self):
        registry = self._section("vg={", "gS={")
        entries = re.findall(r"([a-z]+):\{index:\"([^\"]+)\",name:\"([^\"]+)\"", registry)
        self.assertEqual(30, len(entries))
        self.assertEqual(30, len({key for key, _, _ in entries}))
        self.assertEqual(30, len({label for _, label, _ in entries}))
        self.assertEqual(30, len({name for _, _, name in entries}))
        self.assertIn('tag:"FORM / 30"', self.bundle)

    def test_new_templates_have_defaults_suggestions_and_renderer(self):
        defaults = self._section("mn={", "ps={")
        suggestions = self._section("hS={", "vg={")
        registry = self._section("vg={", "gS={")
        notes = self._section("gS={", "function pS")
        renderer = self._section('["topline"', "].includes(l)")

        for template_id in NEW_CENTERED_TEMPLATES:
            with self.subTest(template=template_id):
                self.assertIn(f"{template_id}:{{title:", defaults)
                self.assertIn(f"{template_id}:[", suggestions)
                self.assertIn(f"{template_id}:{{index:", registry)
                self.assertIn(f"{template_id}:", notes)
                self.assertIn(f'"{template_id}"', renderer)

    def test_new_default_titles_fit_input_limit(self):
        defaults = self._section("mn={", "ps={")
        titles = {
            key: template_title or quoted_title
            for key, template_title, quoted_title in re.findall(
                r'([a-z]+):\{title:(?:`([^`]*)`|"([^"]*)")', defaults
            )
        }
        self.assertTrue(NEW_CENTERED_TEMPLATES.issubset(titles))
        for template_id in NEW_CENTERED_TEMPLATES:
            with self.subTest(template=template_id):
                self.assertLessEqual(len(titles[template_id]), 28)

    def test_new_collection_has_clean_centered_base(self):
        collection = self.css[self.css.index("F–AD · Centered typography collection") :]
        for template_id in NEW_CENTERED_TEMPLATES:
            with self.subTest(template=template_id):
                self.assertIn(f".{template_id}-cover", collection)

        for declaration in (
            "align-content: center;",
            "justify-items: center;",
            "top: 0;",
            "bottom: 0;",
            "text-align: center;",
            "border: 0;",
        ):
            self.assertIn(declaration, collection)

        self.assertNotIn("border-left:", collection)
        self.assertNotIn("border-right:", collection)
        self.assertNotIn("border-top:", collection)
        self.assertNotIn("border-bottom:", collection)

    def test_preview_and_export_use_same_extension_version(self):
        versions = re.findall(r"coverlab-extensions\.css\?v=(\d{8}-\d+)", self.entry)
        self.assertEqual(2, len(versions))
        self.assertEqual(1, len(set(versions)))
        self.assertIn(f"/coverlab/?v={versions[0]}", self.parent)


if __name__ == "__main__":
    unittest.main()
