#!/usr/bin/env python3
"""Offline tests of scripts/deploy-test.sh and scripts/destroy-test.sh.

Run: python3 -m unittest discover -s scripts -p 'test_*.py'

Each script runs with a PATH of its own: links to the few tools it uses and a
fake `aws` that records its arguments (and the secret and template files it
is handed) and answers from a canned description of the seller account, so
no test can reach AWS. The environment holds no profile and only dummy keys
for a closed local port. All IDs, names and secrets are fake.
"""
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEPLOY = REPO / "scripts" / "deploy-test.sh"
DESTROY = REPO / "scripts" / "destroy-test.sh"
TEMPLATE = REPO / "cloudformation" / "nlsql-ai-employee-ecs-fargate.yaml"

ACCOUNT = "111122223333"
APP_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeee1"
TENANT_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeee2"
STACK = "nlsql-ai-employee-test"
SECRET = f"{STACK}/credentials"
SECRET_ARN = f"arn:aws:secretsmanager:us-east-1:{ACCOUNT}:secret:{SECRET}-AbCdEf"
TEST_IMAGE = f"{ACCOUNT}.dkr.ecr.us-east-1.amazonaws.com/nlsql-ai-employee-test:1.0.0-rc1"
MARKETPLACE = r"709825985650\.dkr\.ecr\.us-east-1\.amazonaws\.com/nlsql/ai-employee:[0-9]+\.[0-9]+\.[0-9]+"
TEST_REPO = r"[0-9]{12}\.dkr\.ecr\.us-east-1\.amazonaws\.com/nlsql-ai-employee-test:.+"
KEPT = {"UsersTableName": "fake-users", "TasksTableName": "fake-tasks",
        "UploadsBucketName": "fake-uploads", "ConversationsBucketName": "fake-conversations"}
TOOLS = ["bash", "python3", "sed", "grep", "cat", "mktemp", "chmod", "rm", "dirname", "basename", "tr"]

FAKE_AWS = r'''#!/usr/bin/env python3
import json, os, sys
state = json.load(open(os.environ["FAKE_AWS_STATE"]))
args = sys.argv[1:]
entry = {"args": args, "region": os.environ.get("AWS_REGION")}
for i, arg in enumerate(args[:-1]):
    if arg == "--secret-string" and args[i + 1].startswith("file://"):
        path = args[i + 1][len("file://"):]
        entry["secret"] = json.load(open(path))
        entry["secret_mode"] = os.stat(path).st_mode & 0o777
    if arg == "--template-file":
        entry["template"] = open(args[i + 1]).read()
with open(os.environ["FAKE_AWS_LOG"], "a") as log:
    log.write(json.dumps(entry) + "\n")

def opt(name):
    return args[args.index(name) + 1] if name in args else ""

def fail(code, message):
    sys.stderr.write(f"\nAn error occurred ({code}) when calling the operation: {message}\n")
    sys.exit(254)

def text(value):
    print("None" if value is None else value)

call, query = tuple(args[:2]), opt("--query")
stack, secret = state.get("stack"), state.get("secret")
if tuple(state.get("deny") or ()) == call:
    fail("AccessDeniedException", "not authorized")
if call == ("sts", "get-caller-identity"):
    text(state["account"])
elif call == ("cloudformation", "describe-stacks"):
    if stack is None:
        fail("ValidationError", f"Stack with id {opt('--stack-name')} does not exist")
    if "Tags[" in query:
        text(stack.get("tag"))
    elif "contains(" in query:
        print("\n".join(f"{k}\t{v}" for k, v in stack["outputs"].items()))
    else:
        print("| outputs table |")
elif call == ("cloudformation", "deploy"):
    if state.get("deploy_fails"):
        sys.exit(255)
    state["stack"] = stack or {"tag": "test", "outputs": {}}
    json.dump(state, open(os.environ["FAKE_AWS_STATE"], "w"))
elif call in (("cloudformation", "describe-stack-events"), ("cloudformation", "delete-stack"),
              ("cloudformation", "wait"), ("dynamodb", "wait")):
    pass
elif call == ("secretsmanager", "describe-secret"):
    if secret is None:
        fail("ResourceNotFoundException", "Secrets Manager can't find the specified secret.")
    if query.startswith("[DeletedDate"):
        print(f"{secret.get('deleted') or 'None'}\t{secret.get('tag') or 'None'}")
    elif query == "ARN":
        text(state["secret_arn"])
    else:
        text(secret.get("tag"))
elif call[0] == "secretsmanager" and call[1] in ("create-secret", "put-secret-value", "restore-secret",
                                                   "delete-secret"):
    if call[1] == "create-secret":
        state["secret"] = {"tag": "test"}
        json.dump(state, open(os.environ["FAKE_AWS_STATE"], "w"))
    print("{}")
elif call == ("ec2", "describe-vpcs"):
    text("vpc-0fake")
elif call == ("ec2", "describe-subnets"):
    print("subnet-0fakea\tsubnet-0fakeb")
elif call == ("dynamodb", "describe-table"):
    name = opt("--table-name")
    if name not in state["tables"]:
        fail("ResourceNotFoundException", f"Requested resource not found: Table: {name} not found")
    print(f"arn:aws:dynamodb:us-east-1:{state['account']}:table/{name}")
elif call == ("dynamodb", "list-tags-of-resource"):
    text(state["tables"][opt("--resource-arn").rsplit("/", 1)[1]])
elif call == ("dynamodb", "delete-table"):
    print("{}")
elif call == ("s3api", "get-bucket-tagging"):
    name = opt("--bucket")
    if name not in state["buckets"]:
        fail("NoSuchBucket", "The specified bucket does not exist")
    if state["buckets"][name] is None:
        fail("NoSuchTagSet", "The TagSet does not exist")
    text(state["buckets"][name])
elif call in (("s3", "rm"), ("s3api", "delete-bucket")):
    pass
else:
    sys.stderr.write("unexpected aws call: " + " ".join(args) + "\n")
    sys.exit(99)
'''


def env_text(**extra):
    values = {"connections__serviceConnection__settings__clientId": APP_ID,
              "connections__serviceConnection__settings__clientSecret": "verify-not-a-real-secret",
              "connections__serviceConnection__settings__tenantId": TENANT_ID}
    values.update(extra)
    return "".join(f"{k}={v}\n" for k, v in values.items() if v is not None)


class StackScriptsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.home = self.dir / "home"
        self.home.mkdir()
        self.scratch = self.dir / "tmp"
        self.scratch.mkdir()
        self.log = self.dir / "aws-calls"
        self.state_file = self.dir / "aws-state.json"
        self.bin = self.dir / "bin"
        self.bin.mkdir()
        for tool in TOOLS + (["git"] if shutil.which("git") else []):
            found = shutil.which(tool)
            self.assertIsNotNone(found, f"{tool} is not installed")
            (self.bin / tool).symlink_to(found)
        fake = self.bin / "aws"
        fake.write_text(FAKE_AWS, encoding="utf-8")
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        self.env_dir = self.dir / "envs"
        self.env_dir.mkdir()

    def state(self, stack=None, secret=None, tables=None, buckets=None, **extra):
        state = {"account": ACCOUNT, "secret_arn": SECRET_ARN, "stack": stack, "secret": secret,
                 "tables": tables or {}, "buckets": buckets or {}}
        state.update(extra)
        return state

    def run_script(self, script, state, *args, env=None):
        if script.name == DEPLOY.name and not shutil.which("git"):
            self.skipTest("deploy-test.sh needs git, to refuse a tracked env file")
        self.state_file.write_text(json.dumps(state), encoding="utf-8")
        environ = {"PATH": str(self.bin), "HOME": str(self.home), "TMPDIR": str(self.scratch),
                   "AWS_CONFIG_FILE": "/dev/null", "AWS_SHARED_CREDENTIALS_FILE": "/dev/null",
                   "AWS_EC2_METADATA_DISABLED": "true", "AWS_ACCESS_KEY_ID": "AKIDDUMMY",
                   "AWS_SECRET_ACCESS_KEY": "dummy", "AWS_ENDPOINT_URL": "http://127.0.0.1:9",
                   "FAKE_AWS_STATE": str(self.state_file), "FAKE_AWS_LOG": str(self.log), "LC_ALL": "C"}
        environ.update(env or {})
        result = subprocess.run([str(self.bin / "bash"), str(script), *args], env=environ,
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(list(self.scratch.iterdir()), [], "the private work directory must be removed")
        return result

    def env_file(self, text, name="test.env"):
        path = self.env_dir / name
        path.write_text(text, encoding="utf-8")
        return path

    def deploy(self, state, env_text_=None, env=None):
        return self.run_script(DEPLOY, state, str(self.env_file(env_text_ or env_text())), env=env)

    def calls(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def ops(self):
        return [tuple(c["args"][:2]) for c in self.calls()]

    def only(self, op):
        found = [c for c in self.calls() if tuple(c["args"][:2]) == op]
        self.assertEqual(len(found), 1, f"{op} called {len(found)} times: {self.ops()}")
        return found[0]

    def deploy_params(self):
        args = self.only(("cloudformation", "deploy"))["args"]
        params = args[args.index("--parameter-overrides") + 1:]
        return dict(p.split("=", 1) for p in params)

    def assertDeployed(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        call = self.only(("cloudformation", "deploy"))
        args = call["args"]
        self.assertEqual(args[args.index("--stack-name") + 1], STACK)
        self.assertEqual(args[args.index("--tags") + 1], "nlsql:purpose=test")
        self.assertEqual(args[args.index("--capabilities") + 1], "CAPABILITY_IAM")
        self.assertEqual(call["region"], "us-east-1")
        original = TEMPLATE.read_text(encoding="utf-8")
        old = f"AllowedPattern: '^{MARKETPLACE}$'"
        self.assertEqual(original.count(old), 1, "the template's ImageUri pattern changed: update deploy-test.sh")
        self.assertEqual(call["template"], original.replace(old, f"AllowedPattern: '^({MARKETPLACE}|{TEST_REPO})$'"))
        for secret in ("verify-not-a-real-secret", "verify-not-a-real-key"):
            self.assertNotIn(secret, result.stdout + result.stderr)
        self.assertIn("OAuthRedirectUri", result.stdout)
        return self.deploy_params()

    def assertRefusedBeforeAws(self, result, needle, code=1):
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        self.assertIn(needle, result.stderr)
        self.assertEqual(self.calls(), [])

    # deploy-test.sh

    def test_deploy_gemini_off_creates_the_secret_and_the_stack(self):
        result = self.deploy(self.state(), env_text(GEMINI_API_KEY="verify-not-a-real-key"),
                             env={"GEMINI": "off"})
        params = self.assertDeployed(result)
        create = self.only(("secretsmanager", "create-secret"))
        self.assertEqual(create["secret"], {"AppPassword": "verify-not-a-real-secret"})
        self.assertEqual(create["secret_mode"], 0o600)
        args = create["args"]
        self.assertEqual(args[args.index("--name") + 1], SECRET)
        self.assertEqual(args[args.index("--tags") + 1], "Key=nlsql:purpose,Value=test")
        self.assertNotIn(("secretsmanager", "put-secret-value"), self.ops())
        self.assertEqual(params, {
            "VpcId": "vpc-0fake", "SubnetIds": "subnet-0fakea,subnet-0fakeb", "ImageUri": TEST_IMAGE,
            "CredentialsSecretArn": SECRET_ARN, "MicrosoftAppId": APP_ID, "MicrosoftAppTenantId": TENANT_ID,
            "LanguageModel": "global.anthropic.claude-sonnet-5-5", "Streaming": "false",
            "ConversationMemoryTokens": "8000", "AdditionalInstructions": "", "GeminiModel": "gemini-3.8-flash",
            "GeminiFeatures": "Disabled"})
        self.assertIn("Gemini off", result.stdout)

    def test_deploy_gemini_on_updates_the_test_secret_and_stack(self):
        state = self.state(stack={"tag": "test", "outputs": KEPT}, secret={"tag": "test"})
        text = env_text(GEMINI_API_KEY="verify-not-a-real-key", GEMINI_MODEL="gemini-3.6-flash",
                        MODEL_NAME="global.anthropic.claude-opus-5", STREAMING="True", MODEL_MEMORY_TOKENS="16000",
                        SYSTEM_PROMPT='"Answer briefly, and name the source = always."')
        params = self.assertDeployed(self.deploy(state, text, env={"GEMINI": "on"}))
        put = self.only(("secretsmanager", "put-secret-value"))
        self.assertEqual(put["secret"], {"AppPassword": "verify-not-a-real-secret",
                                         "GeminiApiKey": "verify-not-a-real-key"})
        self.assertEqual(put["secret_mode"], 0o600)
        self.assertNotIn(("secretsmanager", "create-secret"), self.ops())
        self.assertNotIn(("secretsmanager", "restore-secret"), self.ops())
        for key, value in {"GeminiFeatures": "Enabled", "GeminiModel": "gemini-3.6-flash",
                           "LanguageModel": "global.anthropic.claude-opus-5", "Streaming": "true",
                           "ConversationMemoryTokens": "16000",
                           "AdditionalInstructions": "Answer briefly, and name the source = always."}.items():
            self.assertEqual(params[key], value, key)

    def test_gemini_defaults_to_the_env_files_key(self):
        params = self.assertDeployed(self.deploy(self.state(), env_text(GEMINI_API_KEY="verify-not-a-real-key")))
        self.assertEqual(params["GeminiFeatures"], "Enabled")
        self.assertIn("GeminiApiKey", self.only(("secretsmanager", "create-secret"))["secret"])
        self.log.unlink()
        params = self.assertDeployed(self.deploy(self.state()))
        self.assertEqual(params["GeminiFeatures"], "Disabled")

    def test_overrides_win_over_the_env_file(self):
        text = env_text(GEMINI_API_KEY="verify-not-a-real-key", GEMINI_MODEL="gemini-3.6-flash",
                        MODEL_NAME="global.anthropic.claude-opus-5")
        env = {"LANGUAGE_MODEL": "global.anthropic.claude-haiku-4-5-20251001-v1:0",
               "GEMINI_MODEL": "gemini-3.5-flash-lite", "STACK_NAME": "other-test", "VERSION": "2.0.0",
               "VPC_ID": "vpc-0given", "SUBNET_IDS": "subnet-1,subnet-2"}
        result = self.deploy(self.state(), text, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        params = self.deploy_params()
        self.assertEqual(params["LanguageModel"], "global.anthropic.claude-haiku-4-5-20251001-v1:0")
        self.assertEqual(params["GeminiModel"], "gemini-3.5-flash-lite")
        self.assertEqual(params["ImageUri"], TEST_IMAGE.replace("1.0.0-rc1", "2.0.0-rc1"))
        self.assertEqual((params["VpcId"], params["SubnetIds"]), ("vpc-0given", "subnet-1,subnet-2"))
        self.assertNotIn(("ec2", "describe-vpcs"), self.ops())
        args = self.only(("secretsmanager", "create-secret"))["args"]
        self.assertEqual(args[args.index("--name") + 1], "other-test/credentials")

    def test_a_secret_scheduled_for_deletion_is_restored(self):
        state = self.state(secret={"tag": "test", "deleted": "2026-11-01T10:00:00+00:00"})
        self.assertDeployed(self.deploy(state))
        ops = self.ops()
        self.assertLess(ops.index(("secretsmanager", "restore-secret")), ops.index(("secretsmanager", "put-secret-value")))

    def test_deploy_refuses_a_stack_without_the_test_tag(self):
        for tag in (None, "prod"):
            with self.subTest(tag=tag):
                self.log.unlink(missing_ok=True)
                result = self.deploy(self.state(stack={"tag": tag, "outputs": KEPT}, secret={"tag": "test"}))
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("is not tagged nlsql:purpose=test; refusing to update it", result.stderr)
                ops = self.ops()
                for op in (("cloudformation", "deploy"), ("secretsmanager", "put-secret-value"),
                           ("secretsmanager", "create-secret")):
                    self.assertNotIn(op, ops)

    def test_deploy_refuses_a_secret_without_the_test_tag(self):
        result = self.deploy(self.state(secret={"tag": None}))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(f"secret {SECRET} is not tagged nlsql:purpose=test", result.stderr)
        self.assertNotIn(("secretsmanager", "put-secret-value"), self.ops())
        self.assertNotIn(("cloudformation", "deploy"), self.ops())

    @unittest.skipUnless(shutil.which("git"), "git is not installed")
    def test_deploy_refuses_a_tracked_env_file(self):
        path = self.env_file(env_text(), name=".env.test")
        subprocess.run(["git", "init", "-q", str(self.env_dir)], check=True, capture_output=True,
                       env={"PATH": str(self.bin), "HOME": str(self.home)})
        subprocess.run(["git", "-C", str(self.env_dir), "add", "-f", path.name], check=True, capture_output=True,
                       env={"PATH": str(self.bin), "HOME": str(self.home)})
        result = self.run_script(DEPLOY, self.state(), str(path))
        self.assertRefusedBeforeAws(result, "is tracked by git; refusing to read it")
        # Untracked in the same repository: read.
        other = self.env_file(env_text(), name="local.env")
        self.assertDeployed(self.run_script(DEPLOY, self.state(), str(other)))

    def test_deploy_refuses_bad_input_before_any_aws_call(self):
        cases = [
            ({"GEMINI": "on"}, env_text(), "lacks GEMINI_API_KEY (GEMINI=on)", 1),
            ({"GEMINI": "yes"}, env_text(), "GEMINI must be on or off", 2),
            ({}, env_text(connections__serviceConnection__settings__clientSecret=None), "lacks clientSecret", 1),
            ({}, env_text(MODEL_MEMORY_TOKENS="12000"), "MODEL_MEMORY_TOKENS '12000' is not one of", 1),
            ({}, env_text(MODEL_NAME="anthropic.claude-v2"), "LANGUAGE_MODEL or MODEL_NAME 'anthropic.claude-v2'", 1),
            ({"LANGUAGE_MODEL": "x"}, env_text(), "LANGUAGE_MODEL or MODEL_NAME 'x' is not one of", 1),
            ({"GEMINI_MODEL": "gemini-2.5-flash"}, env_text(), "GEMINI_MODEL 'gemini-2.5-flash' is not one of", 1),
            ({}, env_text(STREAMING="yes"), "STREAMING 'yes' is not one of", 1),
            ({}, env_text(SYSTEM_PROMPT="x" * 4001), "SYSTEM_PROMPT is longer than the template's 4000", 1),
        ]
        for env, text, needle, code in cases:
            with self.subTest(needle=needle):
                self.log.unlink(missing_ok=True)
                self.assertRefusedBeforeAws(self.deploy(self.state(), text, env=env), needle, code)
        self.assertEqual(self.run_script(DEPLOY, self.state()).returncode, 2)
        self.assertRefusedBeforeAws(self.run_script(DEPLOY, self.state(), str(self.dir / "missing.env")),
                                    "not found", 2)

    def test_deploy_refuses_the_marketplace_image(self):
        image = "709825985650.dkr.ecr.us-east-1.amazonaws.com/nlsql/ai-employee:1.0.0"
        result = self.deploy(self.state(), env={"IMAGE_URI": image})
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("never the Marketplace one", result.stderr)
        self.assertEqual(self.ops(), [("sts", "get-caller-identity")])

    def test_deploy_stops_on_a_changed_image_pattern(self):
        repo = self.dir / "repo"
        (repo / "scripts").mkdir(parents=True)
        (repo / "cloudformation").mkdir()
        shutil.copy(DEPLOY, repo / "scripts")
        text = TEMPLATE.read_text(encoding="utf-8").replace("nlsql/ai-employee:[0-9]+", "nlsql/ai-employee:v[0-9]+")
        (repo / "cloudformation" / TEMPLATE.name).write_text(text, encoding="utf-8")
        result = self.run_script(repo / "scripts" / DEPLOY.name, self.state(), str(self.env_file(env_text())))
        self.assertRefusedBeforeAws(result, "the ImageUri AllowedPattern in the template is not the expected one")

    def test_deploy_reports_a_failed_deployment(self):
        result = self.deploy(self.state(deploy_fails=True))
        self.assertEqual(result.returncode, 1)
        self.assertIn("Deployment failed", result.stderr)
        self.assertIn(("cloudformation", "describe-stack-events"), self.ops())

    def test_deploy_stops_on_an_unexpected_aws_error(self):
        result = self.deploy(self.state(deny=["cloudformation", "describe-stacks"]))
        self.assertEqual(result.returncode, 1)
        self.assertIn("AccessDeniedException", result.stderr)
        self.assertNotIn(("secretsmanager", "create-secret"), self.ops())
        self.assertNotIn(("cloudformation", "deploy"), self.ops())

    # destroy-test.sh

    def destroy(self, state, env=None):
        return self.run_script(DESTROY, state, env=env)

    def test_destroy_keeps_the_data_without_purge(self):
        result = self.destroy(self.state(stack={"tag": "test", "outputs": KEPT}, secret={"tag": "test"}))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        ops = self.ops()
        self.assertLess(ops.index(("cloudformation", "delete-stack")), ops.index(("cloudformation", "wait")))
        self.assertEqual(self.only(("cloudformation", "wait"))["args"][2], "stack-delete-complete")
        args = self.only(("secretsmanager", "delete-secret"))["args"]
        self.assertIn("--recovery-window-in-days", args)
        self.assertFalse([op for op in ops if op[0] in ("dynamodb", "s3", "s3api")], ops)
        for name in ("fake-users", "fake-tasks"):
            self.assertIn(f"Kept table {name} (retained by the stack). PURGE_DATA=1 deletes it.", result.stdout)
        for name in ("fake-uploads", "fake-conversations"):
            self.assertIn(f"Kept bucket {name} (retained by the stack). PURGE_DATA=1 deletes it.", result.stdout)

    def test_destroy_with_purge_data_deletes_the_tagged_tables_and_buckets(self):
        state = self.state(stack={"tag": "test", "outputs": KEPT}, secret={"tag": "test"},
                           tables={"fake-users": "test", "fake-tasks": "test"},
                           buckets={"fake-uploads": "test", "fake-conversations": "test"})
        result = self.destroy(state, env={"PURGE_DATA": "1"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = [c["args"] for c in self.calls()]
        stack_gone = calls.index(["cloudformation", "wait", "stack-delete-complete", "--stack-name", STACK])
        for name in ("fake-users", "fake-tasks"):
            deleted = calls.index(["dynamodb", "delete-table", "--table-name", name])
            self.assertLess(stack_gone, deleted)
            self.assertIn(["dynamodb", "wait", "table-not-exists", "--table-name", name], calls[deleted:])
            self.assertIn(f"Deleted table {name}", result.stdout)
        for name in ("fake-uploads", "fake-conversations"):
            emptied = calls.index(["s3", "rm", f"s3://{name}", "--recursive", "--only-show-errors"])
            self.assertLess(stack_gone, emptied)
            self.assertIn(["s3api", "delete-bucket", "--bucket", name], calls[emptied:])
            self.assertIn(f"Emptied and deleted bucket {name}", result.stdout)

    def test_purge_data_leaves_untagged_and_missing_data_alone(self):
        state = self.state(stack={"tag": "test", "outputs": KEPT}, secret={"tag": "test"},
                           tables={"fake-users": None}, buckets={"fake-uploads": None, "fake-conversations": "prod"})
        result = self.destroy(state, env={"PURGE_DATA": "1"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        ops = self.ops()
        for op in (("dynamodb", "delete-table"), ("s3", "rm"), ("s3api", "delete-bucket")):
            self.assertNotIn(op, ops)
        self.assertIn("Table fake-users is not tagged nlsql:purpose=test; left in place", result.stdout)
        self.assertIn("Table fake-tasks does not exist", result.stdout)
        self.assertIn("Bucket fake-uploads is not tagged nlsql:purpose=test; left in place", result.stdout)
        self.assertIn("Bucket fake-conversations is not tagged nlsql:purpose=test; left in place", result.stdout)

    def test_destroy_refuses_a_stack_without_the_test_tag(self):
        for tag in (None, "prod"):
            with self.subTest(tag=tag):
                self.log.unlink(missing_ok=True)
                state = self.state(stack={"tag": tag, "outputs": KEPT}, secret={"tag": "test"},
                                   tables={"fake-users": "test"}, buckets={"fake-uploads": "test"})
                result = self.destroy(state, env={"PURGE_DATA": "1"})
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("is not tagged nlsql:purpose=test; refusing to delete it", result.stderr)
                self.assertEqual(self.ops(), [("sts", "get-caller-identity"), ("cloudformation", "describe-stacks")])

    def test_destroy_without_a_stack_still_removes_the_test_secret(self):
        result = self.destroy(self.state(secret={"tag": "test"}), env={"FORCE_DELETE_SECRET": "1", "PURGE_DATA": "1"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"Stack {STACK} does not exist", result.stdout)
        self.assertIn("--force-delete-without-recovery", self.only(("secretsmanager", "delete-secret"))["args"])
        self.assertNotIn(("cloudformation", "delete-stack"), self.ops())
        self.assertIn("no kept tables or buckets are known", result.stdout)

    def test_destroy_secret_rules(self):
        cases = [({"KEEP_SECRET": "1"}, {"tag": "test"}, None),
                 ({}, {"tag": None}, "is not tagged nlsql:purpose=test; left in place"),
                 ({}, None, "does not exist")]
        for env, secret, needle in cases:
            with self.subTest(env=env, secret=secret):
                self.log.unlink(missing_ok=True)
                result = self.destroy(self.state(stack={"tag": "test", "outputs": KEPT}, secret=secret), env=env)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertNotIn(("secretsmanager", "delete-secret"), self.ops())
                if needle:
                    self.assertIn(f"Secret {SECRET} {needle}", result.stdout)
                else:
                    self.assertNotIn(("secretsmanager", "describe-secret"), self.ops())

    def test_destroy_stops_on_an_unexpected_aws_error(self):
        result = self.destroy(self.state(stack={"tag": "test", "outputs": KEPT},
                                         deny=["cloudformation", "describe-stacks"]))
        self.assertEqual(result.returncode, 1)
        self.assertIn("AccessDeniedException", result.stderr)
        self.assertNotIn(("cloudformation", "delete-stack"), self.ops())

    def test_destroy_takes_no_arguments(self):
        self.assertRefusedBeforeAws(self.run_script(DESTROY, self.state(), "some.env"), "Usage:", 2)


@unittest.skipUnless(shutil.which("make"), "make is not installed")
class MakeTargetsTest(unittest.TestCase):
    def make(self, *args):
        return subprocess.run(["make", "--no-print-directory", "-n", "-C", str(REPO), *args],
                              capture_output=True, text=True, timeout=60,
                              env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": "/nonexistent"})

    def test_deploy_test_passes_the_switches(self):
        result = self.make("deploy-test", "ENV_FILE=local.env", "GEMINI=on", "LANGUAGE_MODEL=m1", "GEMINI_MODEL=g1")
        self.assertEqual(result.returncode, 0, result.stderr)
        for needle in ('GEMINI="on"', 'LANGUAGE_MODEL="m1"', 'GEMINI_MODEL="g1"', "TEST_TAG=1.0.0-rc1",
                       'STACK_NAME="nlsql-ai-employee-test"', 'scripts/deploy-test.sh "local.env"'):
            self.assertIn(needle, result.stdout)

    def test_destroy_test_passes_purge_data(self):
        result = self.make("destroy-test", "PURGE_DATA=1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('PURGE_DATA="1" scripts/destroy-test.sh', result.stdout)


if __name__ == "__main__":
    unittest.main()
