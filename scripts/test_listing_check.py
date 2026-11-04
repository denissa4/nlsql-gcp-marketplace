#!/usr/bin/env python3
"""Offline tests of scripts/listing-check.py and of the listing texts (standard library only).

Run: python3 -m unittest discover -s scripts -p 'test_*.py'

The real listing must pass every check `make listing-check` runs, and each check
must fail on a file with one planted defect. The listing texts must also keep
AWS's two disclosures, and name the template's Claude models in its order.
"""
import contextlib
import importlib.util
import io
import re
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
spec = importlib.util.spec_from_file_location("listing_check", SCRIPTS / "listing-check.py")
lc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lc)

LISTING = ROOT / "listing"
PRODUCT = LISTING / "product"
TEMPLATE = ROOT / "cloudformation" / "nlsql-ai-employee-ecs-fargate.yaml"
GUIDE = ROOT / "docs" / "aws-deployment-guide.md"
USAGE = LISTING / "usage-instructions.txt"

# What `make listing-check` passes, kept in step with the Makefile.
MAKE_ARGS = [
    f"{USAGE}:4000", f"{LISTING / 'delivery-option-description.txt'}:999",
    *(f"{notes}:29999" for notes in sorted((LISTING / "release-notes").glob("*.md"))),
    f"{PRODUCT / 'title.txt'}:72", f"{PRODUCT / 'short-description.txt'}:1000",
    f"{PRODUCT / 'long-description.txt'}:5000", f"{PRODUCT / 'support-description.txt'}:2000",
    f"{PRODUCT / 'highlight-1.txt'}:250", f"{PRODUCT / 'highlight-2.txt'}:250",
    f"{PRODUCT / 'highlight-3.txt'}:250", f"{PRODUCT / 'keywords.txt'}:keywords",
    f"{USAGE}:regions={TEMPLATE}",
]

ADD_ON = ("This product extends the functionality of Microsoft Teams and without it, this product has "
          "very limited utility. Please note that Microsoft Teams might require its own license for full "
          "functionality with this listing.")
EXTERNAL = ("This product requires an ongoing internet connection. The following ongoing external services "
            "are required to properly function: ")


def run(args):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = lc.main([str(a) for a in args])
    return rc, out.getvalue()


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def write(self, name, text):
        path = self.dir / name
        path.write_text(text, encoding="utf-8")
        return path

    def assertFails(self, arg, expected):
        rc, out = run([arg])
        self.assertEqual(rc, 1, out)
        self.assertIn(expected, out)


class RealListing(Case):
    def test_make_listing_check_passes(self):
        rc, out = run(MAKE_ARGS)
        self.assertEqual(rc, 0, out)
        self.assertNotIn("too long", out)

    def test_every_listing_text_is_checked(self):
        checked = {Path(a.rpartition(":")[0]) for a in MAKE_ARGS}
        texts = {p for p in LISTING.rglob("*") if p.is_file() and p.name != "README.md"}
        self.assertEqual(texts - checked, set(), "a listing text that make listing-check does not check")

    def test_disclosures(self):
        short = (PRODUCT / "short-description.txt").read_text(encoding="utf-8")
        long = (PRODUCT / "long-description.txt").read_text(encoding="utf-8")
        usage = USAGE.read_text(encoding="utf-8")
        self.assertIn(ADD_ON, short)
        self.assertIn(ADD_ON, long)
        for name, text in (("long description", long), ("usage instructions", usage)):
            with self.subTest(name):
                self.assertIn(EXTERNAL, text)
                services = text.split(EXTERNAL, 1)[1].split("\n", 1)[0]
                self.assertIn("Azure Bot Service", services)
                self.assertIn("Microsoft Teams channel", services)
                self.assertIn("any other channel you enable", services)
                self.assertIn("Optional, only", services)
                self.assertIn("with your own key: the Google Gemini API", services)
                self.assertNotIn("Gemini", services.split("Optional, only", 1)[0])

    def test_other_channels_and_optional_gemini_are_stated(self):
        for name in ("short-description.txt", "long-description.txt"):
            with self.subTest(name):
                text = (PRODUCT / name).read_text(encoding="utf-8")
                self.assertRegex(text, r"Other Azure Bot Service channels[^.]*work too, without the Microsoft 365 features")
        long = (PRODUCT / "long-description.txt").read_text(encoding="utf-8")
        self.assertIn("Optional, with your own Google Gemini API key", long)
        self.assertIn("Without a key these features are not offered", long)
        self.assertRegex(USAGE.read_text(encoding="utf-8"),
                         r"Other Bot Service channels work without the Microsoft 365 features")

    def test_models_match_the_template(self):
        # The guide's model table names each LanguageModel value (guide-check
        # compares the table with the template); the listing lists the names in
        # the template's order, the default first.
        values = re.search(r"^  LanguageModel:\n.*?AllowedValues:\n((?:      - \S+\n)+)",
                           TEMPLATE.read_text(encoding="utf-8"), re.M | re.S).group(1)
        values = re.findall(r"- (\S+)", values)
        names = dict(re.findall(r"^\| `(global\.anthropic\.[^`]+)`(?: \(default\))? \| ([^|]+?) \|",
                                GUIDE.read_text(encoding="utf-8"), re.M))
        listed = [names[v] for v in values]
        self.assertTrue(all(n.startswith("Claude ") for n in listed), listed)
        short = [n[len("Claude "):] for n in listed[1:]]
        expected = f"{listed[0]} (default), " + ", ".join(short[:-1]) + " or " + short[-1] + "."
        for path in (PRODUCT / "long-description.txt", LISTING / "release-notes" / "1.0.0.md"):
            with self.subTest(path.name):
                self.assertIn(expected, path.read_text(encoding="utf-8"))


class Limits(Case):
    def test_within_the_limit(self):
        self.assertEqual(run([f"{self.write('a.txt', 'x' * 10 + chr(10))}:10"])[0], 0)

    def test_too_long(self):
        self.assertFails(f"{self.write('a.txt', 'x' * 11)}:10", "too long, limit 10")

    def test_commit_placeholder_counts_as_40(self):
        path = self.write("a.txt", "<commit>")
        self.assertEqual(run([f"{path}:40"])[0], 0)
        self.assertFails(f"{path}:39", "40 of 39 characters")

    def test_non_ascii(self):
        self.assertFails(f"{self.write('a.txt', 'smart ' + chr(0x201C) + 'quote')}:0", "U+201C")

    def test_keywords(self):
        self.assertEqual(run([f"{self.write('k.txt', 'one' + chr(10) + 'two' + chr(10))}:keywords"])[0], 0)
        self.assertFails(f"{self.write('k.txt', chr(10).join(str(i) for i in range(16)))}:keywords",
                         "16 keywords, allowed 1-15")
        self.assertFails(f"{self.write('k.txt', 'x' * 51)}:keywords", "longer than 50 characters")
        self.assertFails(f"{self.write('k.txt', chr(10).join(['y' * 50] * 6))}:keywords",
                         "300 characters combined, limit 250")


class Regions(Case):
    TEMPLATE = ("Rules:\n  SupportedRegion:\n    Assertions:\n      - Assert:\n          Fn::Contains:\n"
                "            - [us-east-1, us-east-2, eu-west-1,\n               ap-south-1]\n")

    def usage(self, line):
        return self.write("usage.txt", f"Title\n\n{line}\n\nMore text naming us-west-2.\n")

    def check(self, line):
        tpl = self.write("template.yaml", self.TEMPLATE)
        return run([f"{self.usage(line)}:regions={tpl}"])

    def test_expand_regions(self):
        self.assertEqual(lc.expand_regions("in us-east-1/2, eu-west-1/2/3 or ap-south-1."),
                         ["us-east-1", "us-east-2", "eu-west-1", "eu-west-2", "eu-west-3", "ap-south-1"])

    def test_same_regions_pass(self):
        rc, out = self.check("Deploy it in us-east-1/2, eu-west-1 or ap-south-1.")
        self.assertEqual(rc, 0, out)

    def test_real_template_and_usage_pass(self):
        rc, out = run([f"{USAGE}:regions={TEMPLATE}"])
        self.assertEqual(rc, 0, out)

    def test_missing_region_fails(self):
        rc, out = self.check("Deploy it in us-east-1/2 or eu-west-1.")
        self.assertEqual(rc, 1, out)
        self.assertIn("lists 3 Regions", out)

    def test_extra_region_fails(self):
        rc, out = self.check("Deploy it in us-east-1/2, eu-west-1/2 or ap-south-1.")
        self.assertEqual(rc, 1, out)

    def test_order_matters(self):
        rc, out = self.check("Deploy it in eu-west-1, us-east-1/2 or ap-south-1.")
        self.assertEqual(rc, 1, out)

    def test_no_deploy_line_fails(self):
        rc, out = self.check("Install it in us-east-1/2, eu-west-1 or ap-south-1.")
        self.assertEqual(rc, 1, out)
        self.assertIn("0 lines start with 'Deploy '", out)

    def test_template_without_rule_fails(self):
        tpl = self.write("template.yaml", "Rules: {}\n")
        rc, out = run([f"{self.usage('Deploy it in us-east-1.')}:regions={tpl}"])
        self.assertEqual(rc, 1, out)
        self.assertIn("no SupportedRegion rule", out)


if __name__ == "__main__":
    unittest.main()
