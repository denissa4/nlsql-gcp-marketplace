#!/usr/bin/env python3
"""Offline tests of scripts/template-invariants.py (needs cfn-lint installed).

Run: python3 -m unittest discover -s scripts -p 'test_*.py'

The real template must pass every check. Each check must fail on a copy of
the template with one planted defect. The app-repo checks run against a fake
checkout built here; APP_REPO=<checkout of ai-employee> also runs them against
the real one.
"""
import contextlib
import importlib.util
import io
import os
import re
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("template_invariants", SCRIPTS / "template-invariants.py")
inv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inv)

TEMPLATE = inv.DEFAULT_TEMPLATE.read_text(encoding="utf-8")

# The env lines of the real template that the defects below replace.
GEMINI_MODEL_ENTRY = """            - !If
              - GeminiEnabled
              - {Name: GEMINI_MODEL, Value: !Ref GeminiModel}
              - !Ref AWS::NoValue
"""
GEMINI_KEY_ENTRY = """            - !If
              - GeminiEnabled
              - {Name: GEMINI_API_KEY, ValueFrom: !Sub '${CredentialsSecretArn}:GeminiApiKey::'}
              - !Ref AWS::NoValue
"""
DEBUG_ENTRY = "            - {Name: DEBUG, Value: 'false'}\n"
ENCRYPTION_ENTRY = """            - Name: ENCRYPTION_KEY
              ValueFrom: !Ref EncryptionKeySecret
"""


def env_line(name, value="x"):
    return f"            - {{Name: {name}, Value: '{value}'}}\n"


def secret_line(name, key):
    return f"            - {{Name: {name}, ValueFrom: !Sub '${{CredentialsSecretArn}}:{key}::'}}\n"


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def mutate(self, old, new, text=None):
        text = TEMPLATE if text is None else text
        self.assertEqual(text.count(old), 1, f"defect anchor not unique in the template: {old!r}")
        return text.replace(old, new)

    def run_checks(self, text=None, app_repo=None):
        path = self.dir / "template.yaml"
        path.write_text(TEMPLATE if text is None else text, encoding="utf-8")
        argv = ["--template", str(path)]
        if app_repo:
            argv += ["--app-repo", str(app_repo)]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = inv.main(argv)
        return rc, out.getvalue()

    def assertFails(self, text, check, message, app_repo=None):
        rc, out = self.run_checks(text, app_repo)
        self.assertEqual(rc, 1, out)
        self.assertIn(f"  {check}: FAILED", out)
        self.assertIn(message, out)
        failed = re.findall(r"^  (.+): FAILED$", out, re.MULTILINE)
        self.assertEqual(failed, [check], f"the defect should fail only {check!r}:\n{out}")

    def fake_app(self, models=None, must_not="", name="app"):
        """A checkout with the two files --app-repo reads, agreeing with the template."""
        app = self.dir / name
        (app / "tests" / "unit").mkdir(parents=True)
        (app / "docs").mkdir()
        template = inv.load_template(TEMPLATE, "template")
        _, allowed = inv.language_model(template)
        models = allowed if models is None else models
        (app / "tests" / "unit" / "test_bedrock_models.py").write_text(
            "OTHER = ['x']\nTEMPLATE_MODELS = [\n" + "".join(f"    {m!r},\n" for m in models) + "]\n")
        names = sorted({name for name, *_ in inv.env_entries(template)} - {"MODEL_PROVIDER", "CLOUD_PROVIDER"})
        required = ", ".join(f"`{n}`" for n in names if n != "AWS_MARKETPLACE_PRODUCT_CODE")
        (app / "docs" / "aws-marketplace.md").write_text(
            "# App\n\n## Environment and IAM contract\n\n"
            "| Group | Variables |\n|---|---|\n"
            f"| Required (template) | {required}, `AWS_MARKETPLACE_PRODUCT_CODE=f5jcvx1rl6wb66fhzy5qycpnr` |\n"
            "| Optional (template) | `MODEL_VERSION` (a region) |\n"
            "| Forced on ECS | `MODEL_PROVIDER=aws`, `CLOUD_PROVIDER=aws` |\n"
            f"| Must not set (v1) | `CODER_ENDPOINT`, `DL_TOOL_*`{must_not} |\n"
            "\n## Exact strings\n\n| `NOT_IN_TABLE` | x |\n")
        return app


class RealTemplate(Case):
    def test_the_template_passes(self):
        rc, out = self.run_checks()
        self.assertEqual(rc, 0, out)
        self.assertIn(f"all {len(inv.CHECKS)} invariants hold", out)

    def test_the_template_passes_against_a_matching_app(self):
        rc, out = self.run_checks(app_repo=self.fake_app())
        self.assertEqual(rc, 0, out)

    @unittest.skipUnless(os.environ.get("APP_REPO"), "set APP_REPO to a checkout of ai-employee")
    def test_the_template_passes_against_the_real_app(self):
        rc, out = self.run_checks(app_repo=Path(os.environ["APP_REPO"]))
        self.assertEqual(rc, 0, out)

    def test_the_pinned_hash_is_the_blocks(self):
        block = inv.encryption_key_block(TEMPLATE)
        self.assertIn("ExcludeCharacters: ghijklmnopqrstuvwxyz", block)
        self.assertRegex(inv.ENCRYPTION_KEY_BLOCK_SHA256, r"^[0-9a-f]{64}$")


class ForbiddenEnv(Case):
    CHECK = "forbidden environment names"

    def test_each_forbidden_name_fails(self):
        names = sorted(inv.FORBIDDEN_ENV) + ["DL_TOOL_SALES", "DL_SECRET_SALES", "AWS_ENDPOINT_URL",
                                             "AWS_ENDPOINT_URL_S3", "AWS_BEARER_TOKEN_BEDROCK", "AWS_CSM_HOST"]
        for name in names:
            with self.subTest(name=name):
                text = self.mutate(DEBUG_ENTRY, DEBUG_ENTRY + env_line(name))
                rc, out = self.run_checks(text)
                self.assertEqual(rc, 1, out)
                self.assertIn(f"Environment sets {name}, which the template must never set", out)

    def test_a_forbidden_secret_fails(self):
        text = self.mutate(ENCRYPTION_ENTRY, ENCRYPTION_ENTRY + "            - {Name: VNC_SECRET_KEY, ValueFrom: !Ref EncryptionKeySecret}\n")
        rc, out = self.run_checks(text)
        self.assertEqual(rc, 1, out)
        self.assertIn("Secrets sets VNC_SECRET_KEY", out)


class EnvPlacement(Case):
    CHECK = "environment and secrets placement"

    def test_a_secret_shaped_plain_variable_fails(self):
        text = self.mutate(DEBUG_ENTRY, DEBUG_ENTRY + env_line("TEAMS_PASSWORD"))
        self.assertFails(text, self.CHECK, "TEAMS_PASSWORD is a secret-shaped name in plain-text Environment")

    def test_an_unconditional_gemini_model_fails(self):
        text = self.mutate(GEMINI_MODEL_ENTRY, "            - {Name: GEMINI_MODEL, Value: !Ref GeminiModel}\n")
        self.assertFails(text, self.CHECK, "GEMINI_MODEL must exist only under Fn::If [GeminiEnabled")

    def test_an_unconditional_gemini_key_fails(self):
        text = self.mutate(GEMINI_KEY_ENTRY, secret_line("GEMINI_API_KEY", "GeminiApiKey"))
        self.assertFails(text, self.CHECK, "JSON key GeminiApiKey may be absent; read it only under GeminiEnabled")

    def test_an_absent_json_key_fails(self):
        text = self.mutate(GEMINI_KEY_ENTRY, GEMINI_KEY_ENTRY + secret_line("DB_PASS", "DbPassword"))
        self.assertFails(text, self.CHECK, "names JSON key DbPassword, which the buyer's secret does not hold")

    def test_a_missing_browser_use_host_fails(self):
        text = self.mutate("            - {Name: BROWSER_USE_HOST, Value: !Sub 'https://${Distribution.DomainName}'}\n", "")
        self.assertFails(text, self.CHECK, "BROWSER_USE_HOST must be set exactly once, unconditionally")

    def test_a_conditional_browser_use_host_fails(self):
        old = "            - {Name: BROWSER_USE_HOST, Value: !Sub 'https://${Distribution.DomainName}'}\n"
        new = ("            - !If\n              - GeminiEnabled\n"
               "              - {Name: BROWSER_USE_HOST, Value: !Sub 'https://${Distribution.DomainName}'}\n"
               "              - !Ref AWS::NoValue\n")
        self.assertFails(self.mutate(old, new), self.CHECK, "BROWSER_USE_HOST must be set exactly once")

    def test_an_encryption_key_from_elsewhere_fails(self):
        text = self.mutate(ENCRYPTION_ENTRY, secret_line("ENCRYPTION_KEY", "AppPassword"))
        self.assertFails(text, self.CHECK, "ENCRYPTION_KEY must come from !Ref EncryptionKeySecret")

    def test_a_duplicate_fails(self):
        text = self.mutate(DEBUG_ENTRY, DEBUG_ENTRY + DEBUG_ENTRY.replace("'false'", "'true'"))
        self.assertFails(text, self.CHECK, "DEBUG is set 2 times")


class Paths(Case):
    CHECK = "load balancer path allowlist"

    def test_an_internal_path_fails(self):
        for path in sorted(inv.NEVER_PUBLIC):
            with self.subTest(path=path):
                text = self.mutate("Values: [/static/*]", f"Values: [/static/*, '{path}']")
                self.assertFails(text, self.CHECK, f"ListenerRuleAssets forwards {path}, which must never be public")

    def test_an_extra_core_path_fails(self):
        text = self.mutate("Values: [/api/messages, /callback, /health]", "Values: [/api/messages, /callback, /api/*]")
        self.assertFails(text, self.CHECK, "ListenerRuleCore (priority 10) forwards")

    def test_a_browser_view_without_gemini_fails(self):
        text = self.mutate("    Condition: GeminiEnabled\n    Properties:\n      ListenerArn", "    Properties:\n      ListenerArn")
        self.assertFails(text, self.CHECK, "ListenerRuleBrowserView (priority 30) needs Condition 'GeminiEnabled'")

    def test_a_rule_without_the_origin_header_fails(self):
        old = ("      Priority: 20\n      Conditions:\n        - Field: http-header\n          HttpHeaderConfig:\n"
               "            HttpHeaderName: X-Origin-Verify\n            Values:\n"
               "              - !Select [2, !Split ['/', !Ref AWS::StackId]]\n")
        text = self.mutate(old, "      Priority: 20\n      Conditions:\n")
        self.assertFails(text, self.CHECK, "ListenerRuleAssets must require exactly the X-Origin-Verify header")

    def test_a_forwarding_default_action_fails(self):
        text = self.mutate("            StatusCode: '403'", "            StatusCode: '200'")
        self.assertFails(text, self.CHECK, "Listener: the default action must be a fixed 403")

    def test_an_unknown_priority_fails(self):
        text = self.mutate("      Priority: 20\n", "      Priority: 25\n")
        self.assertFails(text, self.CHECK, "ListenerRuleAssets: priority 25 is not one of")

    def test_an_unconditional_browser_view_output_fails(self):
        text = self.mutate("    Condition: GeminiEnabled\n    Description: The live browser", "    Description: The live browser")
        self.assertFails(text, self.CHECK, "the BrowserViewUrl output needs Condition GeminiEnabled")


class Tables(Case):
    CHECK = "DynamoDB key schema"

    def test_a_number_key_fails(self):
        # The first occurrence is UsersTable's.
        text = TEMPLATE.replace("{AttributeName: RowKey, AttributeType: S}", "{AttributeName: RowKey, AttributeType: N}", 1)
        self.assertFails(text, self.CHECK, "UsersTable: AttributeDefinitions")

    def test_a_renamed_key_fails(self):
        text = TEMPLATE.replace("{AttributeName: RowKey, KeyType: RANGE}", "{AttributeName: SortKey, KeyType: RANGE}", 1)
        text = text.replace("{AttributeName: RowKey, AttributeType: S}", "{AttributeName: SortKey, AttributeType: S}", 1)
        self.assertFails(text, self.CHECK, "KeySchema [('PartitionKey', 'HASH'), ('SortKey', 'RANGE')]")

    def test_a_third_table_fails(self):
        extra = ("  ExtraTable:\n    Type: AWS::DynamoDB::Table\n    DeletionPolicy: RetainExceptOnCreate\n"
                 "    UpdateReplacePolicy: Retain\n    Properties:\n      BillingMode: PAY_PER_REQUEST\n"
                 "      AttributeDefinitions:\n        - {AttributeName: PartitionKey, AttributeType: S}\n"
                 "        - {AttributeName: RowKey, AttributeType: S}\n      KeySchema:\n"
                 "        - {AttributeName: PartitionKey, KeyType: HASH}\n        - {AttributeName: RowKey, KeyType: RANGE}\n\n")
        text = self.mutate("  # Users' files and memory", extra + "  # Users' files and memory")
        self.assertFails(text, self.CHECK, "expected 2 DynamoDB tables, found 3")


class Regions(Case):
    CHECK = "Regions: rule, message and RegionMap"

    def test_a_region_missing_from_the_map_fails(self):
        text = self.mutate("    eu-north-1:     {CloudFrontPrefixList: pl-fab65393}\n", "")
        self.assertFails(text, self.CHECK, "only in the rule ['eu-north-1']")

    def test_a_region_missing_from_the_message_fails(self):
        text = self.mutate("eu-west-3, eu-central-1, eu-north-1, ap-south-1,\n          ap-northeast-1",
                           "eu-west-3, eu-central-1, ap-south-1,\n          ap-northeast-1")
        self.assertFails(text, self.CHECK, "AssertDescription does not say")

    def test_a_region_missing_from_the_rule_fails(self):
        text = self.mutate("eu-west-1, eu-west-2, eu-west-3, eu-central-1, eu-north-1,\n",
                           "eu-west-1, eu-west-2, eu-west-3, eu-central-1,\n")
        rc, out = self.run_checks(text)
        self.assertEqual(rc, 1, out)
        self.assertIn("only in RegionMap ['eu-north-1']", out)


class Size(Case):
    def test_a_template_at_the_limit_fails(self):
        padding = "#" * (inv.MAX_TEMPLATE_BYTES - len(TEMPLATE.encode()) + 10) + "\n"
        self.assertFails(TEMPLATE + padding, "template size", "--template-body takes less than 51200")


class EncryptionKey(Case):
    CHECK = "pinned ENCRYPTION_KEY block"

    def test_a_changed_generator_fails(self):
        for old, new in [("PasswordLength: 64", "PasswordLength: 32"),
                         ("ExcludeCharacters: ghijklmnopqrstuvwxyz", "ExcludeCharacters: ghijklmnopqrstuvwxy"),
                         ("    DeletionPolicy: Delete\n    UpdateReplacePolicy: Retain\n    Properties:\n      Description: !Sub 'NLSQL",
                          "    DeletionPolicy: Retain\n    UpdateReplacePolicy: Retain\n    Properties:\n      Description: !Sub 'NLSQL")]:
            with self.subTest(old=old):
                self.assertFails(self.mutate(old, new), self.CHECK, "the EncryptionKeySecret block changed")

    def test_missing_markers_fail(self):
        text = self.mutate("  # <<< invariant\n", "")
        self.assertFails(text, self.CHECK, "needs exactly one '# >>> invariant: EncryptionKeySecret' line")

    def test_another_generated_secret_fails(self):
        extra = ("  VncSecretKeySecret:\n    Type: AWS::SecretsManager::Secret\n    Properties:\n"
                 "      GenerateSecretString: {PasswordLength: 64, ExcludePunctuation: true}\n\n")
        text = self.mutate("  # Pulls the image,", extra + "  # Pulls the image,")
        self.assertFails(text, self.CHECK, "the stack may generate only EncryptionKeySecret")


class Hardening(Case):
    CHECK = "task hardening"

    def test_an_init_process_fails(self):
        text = self.mutate("          LinuxParameters:\n", "          LinuxParameters:\n            InitProcessEnabled: true\n")
        self.assertFails(text, self.CHECK, "InitProcessEnabled must stay off")

    def test_an_added_capability_fails(self):
        text = self.mutate("              Drop: [ALL]\n", "              Drop: [ALL]\n              Add: [SYS_PTRACE]\n")
        self.assertFails(text, self.CHECK, "Capabilities must be Drop [ALL] with no Add")

    def test_ecs_exec_fails(self):
        text = self.mutate("      EnableExecuteCommand: false", "      EnableExecuteCommand: true")
        self.assertFails(text, self.CHECK, "EnableExecuteCommand must be false")

    def test_two_tasks_fail(self):
        text = self.mutate("      DesiredCount: 1", "      DesiredCount: 2")
        self.assertFails(text, self.CHECK, "DesiredCount must be 1")


class Models(Case):
    def test_a_default_that_is_not_first_fails(self):
        text = self.mutate("    Default: global.anthropic.claude-sonnet-5-5", "    Default: global.anthropic.claude-opus-5")
        self.assertFails(text, "LanguageModel default", "must be its first AllowedValue")

    def test_a_list_unlike_the_apps_fails(self):
        template = inv.load_template(TEMPLATE, "template")
        _, allowed = inv.language_model(template)
        variants = [allowed[1:], allowed[:1] + allowed[2:] + allowed[1:2], allowed + ["global.anthropic.claude-x"]]
        for i, models in enumerate(variants):
            with self.subTest(models=models):
                app = self.fake_app(models=models, name=f"app{i}")
                self.assertFails(None, "LanguageModel against the app's model test",
                                 "differ from tests/unit/test_bedrock_models.py", app)


class AppEnv(Case):
    CHECK = "environment against the app's contract"

    def test_a_name_the_app_does_not_read_fails(self):
        text = self.mutate(DEBUG_ENTRY, DEBUG_ENTRY + env_line("LOG_LEVEL"))
        self.assertFails(text, self.CHECK, "sets LOG_LEVEL, which is not in docs/aws-marketplace.md's contract table",
                         self.fake_app())

    def test_a_name_the_app_refuses_fails(self):
        text = self.mutate(DEBUG_ENTRY, DEBUG_ENTRY + env_line("MODEL_VERSION"))
        app = self.fake_app(must_not=", `MODEL_VERSION`")
        self.assertFails(text, self.CHECK, "sets MODEL_VERSION, which docs/aws-marketplace.md says must not be set", app)

    def test_a_wrong_product_code_fails(self):
        text = self.mutate("Value: f5jcvx1rl6wb66fhzy5qycpnr}", "Value: notthisproductscode000000}")
        self.assertFails(text, self.CHECK, "AWS_MARKETPLACE_PRODUCT_CODE is 'notthisproductscode000000'",
                         self.fake_app())

    def test_an_app_without_the_files_is_an_error(self):
        rc, out = self.run_checks(app_repo=self.dir / "missing")
        self.assertEqual(rc, 1, out)
        self.assertIn("has no tests/unit/test_bedrock_models.py", out)


if __name__ == "__main__":
    unittest.main()
