#!/usr/bin/env python3
"""Offline tests of scripts/guide-check.py (standard library only).

Run: python3 -m unittest discover -s scripts -p 'test_*.py'

The real guide must pass against the real template. Each check must fail on a
copy of the guide with one planted defect. The exact-strings check runs
against a fake application checkout built here; APP_REPO=<checkout of
ai-employee> also runs it against the real one.
"""
import contextlib
import importlib.util
import io
import os
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("guide_check", SCRIPTS / "guide-check.py")
gc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gc)

GUIDE = gc.DEFAULT_GUIDE.read_text(encoding="utf-8")

# A fake app document: a few rows in the real table's shape, plus every row the
# real guide quotes, taken from the real document when it is there.
FAKE_ROWS = [
    ("Container's last line after a denial (exit 78)", "NLSQL AI Employee stopped: <reason>"),
    ("Denial (server and bot): a configuration refusal", "configuration error: <reason>"),
    ("Configuration reason", "<NAME> is missing or shorter than 32 characters"),
    ("Log: the entitlement check denies the task", "AWS Marketplace: NLSQL AI Employee cannot start: <reason> (exit <n>)."),
    ("Denial: not subscribed (exit 10)", "this AWS account is not subscribed to NLSQL AI Employee. "
                                         "Subscribe on AWS Marketplace, then start the task again"),
    ("Reply to `refresh`", "Chat history has been refreshed."),
]


def app_doc(rows):
    table = "\n".join(f"| {where} | `{string}` | `server.py` |" for where, string in rows)
    return ("# App\n\n## Exact strings\n\nIntro.\n\n| Where | String | Source |\n|---|---|---|\n"
            f"{table}\n\n## Sizing\n\n| `Not a table string at all here` |\n")


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def run_checks(self, guide=GUIDE, app=None, template=None):
        path = self.dir / "guide.md"
        path.write_text(guide, encoding="utf-8")
        argv = ["--guide", str(path)]
        if template is not None:
            tpl = self.dir / "template.yaml"
            tpl.write_text(template, encoding="utf-8")
            argv += ["--template", str(tpl)]
        if app is not None:
            repo = self.dir / "app"
            (repo / "docs").mkdir(parents=True, exist_ok=True)
            (repo / "docs" / "aws-marketplace.md").write_text(app, encoding="utf-8")
            argv += ["--app-repo", str(repo)]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = gc.main(argv)
        return rc, out.getvalue()

    def mutate(self, old, new, text=GUIDE):
        self.assertEqual(text.count(old), 1, f"defect anchor not unique: {old!r}")
        return text.replace(old, new)

    def assertFails(self, label, guide=GUIDE, **kw):
        rc, out = self.run_checks(guide, **kw)
        self.assertEqual(rc, 1, out)
        self.assertIn(f"{label}: FAILED", out)
        return out


class RealFiles(Case):
    def test_the_guide_passes_against_the_template(self):
        rc, out = self.run_checks()
        self.assertEqual(rc, 0, out)
        self.assertIn("all 5 checks hold", out)

    @unittest.skipUnless(os.environ.get("APP_REPO"), "set APP_REPO to a checkout of ai-employee")
    def test_the_guide_passes_against_the_real_app(self):
        app = (Path(os.environ["APP_REPO"]) / gc.APP_STRINGS_FILE).read_text(encoding="utf-8")
        rc, out = self.run_checks(app=app)
        self.assertEqual(rc, 0, out)
        self.assertIn("all 6 checks hold", out)


class TemplateChecks(Case):
    def test_a_missing_region_fails(self):
        guide = self.mutate("eu-north-1, ap-south-1,", "ap-south-1,")
        self.assertIn("15 Regions", self.assertFails("Regions equal the template's SupportedRegion", guide))

    def test_a_reordered_region_list_fails(self):
        guide = self.mutate("The template accepts us-east-1, us-east-2,", "The template accepts us-east-2, us-east-1,")
        self.assertFails("Regions equal the template's SupportedRegion", guide)

    def test_the_registry_host_is_not_a_listed_region(self):
        # The paragraph names us-east-1 again in the image path; that must not count.
        rc, out = self.run_checks()
        self.assertNotIn("17 Regions", out)

    def test_a_missing_model_fails(self):
        guide = self.mutate("| `global.anthropic.claude-opus-4-8` | Claude Opus 4.8 | `anthropic.claude-opus-4-8` |\n", "")
        self.assertFails("model table equals LanguageModel, default marked", guide)

    def test_another_default_fails(self):
        guide = self.mutate("`global.anthropic.claude-sonnet-5-5` (default)", "`global.anthropic.claude-sonnet-5-5`")
        guide = self.mutate("| `global.anthropic.claude-opus-5` |", "| `global.anthropic.claude-opus-5` (default) |", guide)
        out = self.assertFails("model table equals LanguageModel, default marked", guide)
        self.assertIn("marks ['global.anthropic.claude-opus-5']", out)

    def test_a_missing_parameter_fails(self):
        start = GUIDE.index("| `KmsKeyArn` |")
        line = GUIDE[start:GUIDE.index("\n", start) + 1]
        out = self.assertFails("parameter table names every template parameter", self.mutate(line, ""))
        self.assertIn("lacks ['KmsKeyArn']", out)

    def test_an_unknown_parameter_fails(self):
        guide = self.mutate("| `ImageUri` |", "| `ImageTag` | no | — | x |\n| `ImageUri` |")
        self.assertIn("'ImageTag'", self.assertFails("parameter table names every template parameter", guide))

    def test_a_template_parameter_the_guide_lacks_fails(self):
        template = gc.DEFAULT_TEMPLATE.read_text(encoding="utf-8").replace(
            "  ImageUri:\n", "  ExtraKnob:\n    Type: String\n    Default: ''\n\n  ImageUri:\n", 1)
        out = self.assertFails("parameter table names every template parameter", template=template)
        self.assertIn("lacks ['ExtraKnob']", out)

    def test_a_missing_output_fails(self):
        guide = self.mutate("| `ClusterName`, `ServiceName` |", "| `ClusterName` |")
        self.assertIn("lacks ['ServiceName']", self.assertFails("output table names every template output", guide))

    def test_a_broken_anchor_fails(self):
        guide = self.mutate("[Scaling](#scaling))", "[Scaling](#scale))")
        self.assertIn("#scale", self.assertFails("in-page links resolve", guide))

    def test_slugs_follow_github(self):
        self.assertEqual(gc.slug("Step 1 — Store your credentials"), "step-1--store-your-credentials")
        self.assertEqual(gc.slug("1. What gets deployed"), "1-what-gets-deployed")
        self.assertEqual(gc.slug("Where your sensitive data lives, and how it is encrypted"),
                         "where-your-sensitive-data-lives-and-how-it-is-encrypted")

    def test_headings_in_code_blocks_are_not_anchors(self):
        guide = GUIDE + "\n```\n# Not a heading\n```\n\nSee [it](#not-a-heading).\n"
        self.assertFails("in-page links resolve", guide)


class ExactStrings(Case):
    LABEL = "quoted messages come from the Exact strings table"

    def guide_with(self, line):
        return "# G\n\n" + line + "\n"

    def run_strings(self, line, rows=FAKE_ROWS):
        # Only the exact-strings check matters here; the template checks fail
        # on a one-line guide, so read that check's own line.
        rc, out = self.run_checks(self.guide_with(line), app=app_doc(rows))
        return out

    def assertStringOk(self, line):
        out = self.run_strings(line)
        self.assertIn(f"{self.LABEL}: ok", out)

    def assertStringFails(self, line):
        out = self.run_strings(line)
        self.assertIn(f"{self.LABEL}: FAILED", out)
        return out

    def test_an_exact_row_passes(self):
        self.assertStringOk("The reply is `Chat history has been refreshed.`")

    def test_a_changed_message_fails(self):
        out = self.assertStringFails("The reply is `Chat history was refreshed.`")
        self.assertIn("NOT_PRODUCT_MESSAGES", out)

    def test_the_placeholder_itself_passes(self):
        self.assertStringOk("The last line is `NLSQL AI Employee stopped: <reason>`")

    def test_a_reason_filled_with_rows_passes(self):
        self.assertStringOk("`NLSQL AI Employee stopped: configuration error: ENCRYPTION_KEY is missing or "
                            "shorter than 32 characters`")
        self.assertStringOk("`AWS Marketplace: NLSQL AI Employee cannot start: this AWS account is not "
                            "subscribed to NLSQL AI Employee. Subscribe on AWS Marketplace, then start the "
                            "task again (exit 10).`")

    def test_a_reason_filled_with_free_text_fails(self):
        self.assertStringFails("`NLSQL AI Employee stopped: something went wrong somewhere`")

    def test_an_elided_reason_passes(self):
        self.assertStringOk("`NLSQL AI Employee stopped: ...`")

    def test_a_json_status_is_a_message(self):
        self.assertStringFails('`{"status":"BOOTING"}`')

    def test_a_filter_pattern_must_be_part_of_a_row(self):
        self.assertStringOk('Search for `"cannot start"`.')
        out = self.assertStringFails('Search for `"cannot begin"`.')
        self.assertIn("filter pattern", out)

    def test_commands_and_short_spans_are_not_messages(self):
        self.assertStringOk("Run `aws ecs update-service --force-new-deployment now`, "
                            "`/schedule email me a joke every Friday`, `--capabilities CAPABILITY_IAM x`, "
                            "or `two words`.")

    def test_fenced_blocks_are_not_quoted(self):
        self.assertStringOk("```\n`This is not a quoted message at all`\n```")

    def test_not_product_messages_and_template_text_pass(self):
        self.assertStringOk("`ECS Deployment Circuit Breaker was triggered`, and the template's "
                            "`NLSQL AI Employee is not supported in this Region.`")

    def test_a_span_across_a_line_break_is_squashed(self):
        self.assertStringOk("`Chat history has\nbeen refreshed.`")

    def test_rows_outside_the_section_do_not_count(self):
        self.assertStringFails("`Not a table string at all here`")

    def test_a_missing_app_document_fails(self):
        repo = self.dir / "empty"
        repo.mkdir()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = gc.main(["--app-repo", str(repo)])
        self.assertEqual(rc, 1)
        self.assertIn("ERROR", out.getvalue())


if __name__ == "__main__":
    unittest.main()
