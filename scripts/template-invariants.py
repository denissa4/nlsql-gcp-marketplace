#!/usr/bin/env python3
"""Check the deployment template's security and contract invariants, offline.

Usage: template-invariants.py [--template PATH] [--app-repo DIR]

Each check prints one line, ok or the reasons it failed; the exit status is 1
if any failed. Nothing here reaches AWS. The template is read with cfn-lint's
YAML loader (`pip install cfn-lint`), which understands the short intrinsic
tags.

Without --app-repo the checks cover the template alone: the environment the
task gets (no forbidden or secret-shaped names in plain text, the Gemini
settings only with GeminiFeatures Enabled), the load balancer's path allowlist
and the BrowserViewUrl output (the browser view only with BrowserAgent
Enabled, with Gemini or without it), the tables' key schema, the Region list
against RegionMap and the rule's message, the size limit of --template-body, the
pinned ENCRYPTION_KEY block, the task's hardening and the model list's
default. --app-repo DIR names a checkout of the application (ai-employee,
branch aws-marketplace-offer) and adds: LanguageModel's AllowedValues equal
TEMPLATE_MODELS in tests/unit/test_bedrock_models.py, default first, and every
variable the template sets is one the image reads, per the Environment and IAM
contract table of docs/aws-marketplace.md.
"""
import argparse
import ast
import hashlib
import re
import sys
from pathlib import Path

DEFAULT_TEMPLATE = Path(__file__).resolve().parent.parent / "cloudformation" / "nlsql-ai-employee-ecs-fargate.yaml"

# aws cloudformation ... --template-body takes at most 51,200 bytes.
MAX_TEMPLATE_BYTES = 51200

# The EncryptionKeySecret resource, between these marker lines. AWS creates a
# new secret version whenever GenerateSecretString changes, which signs every
# user out, so the block is pinned. Change the hash only together with a
# deliberate key migration.
BLOCK_START = "# >>> invariant: EncryptionKeySecret"
BLOCK_END = "# <<< invariant"
ENCRYPTION_KEY_BLOCK_SHA256 = "40db72d453ee8de9d5a396adf0afe7ccd7de4b30e8fa05a74e1d8917668cf6a2"
ENCRYPTION_KEY_RESOURCE = "EncryptionKeySecret"

# Names the task definition must never set. The per-task keys come from the
# image's startup.sh; code mode, the sandbox, the access-code gate, the
# channel simulation and the Direct Line tools are off in v1; AWS credentials
# come from the task role only; the Azure storage and App Service settings do
# not apply.
FORBIDDEN_ENV = frozenset({
    "VNC_SECRET_KEY", "INTERNAL_AUTH_SECRET",
    "CODER_ENDPOINT", "CODER_TIMEOUT", "SANDBOX_URL", "NLSQL_API_KEY",
    "FORCE_CHANNEL", "CHANNEL_ID", "DIRECT_LINE_SECRET",
    "MODEL_DEPLOYMENT_NAME", "MODEL_API_KEY", "MODEL_ENDPOINT",
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
    "USER_STORAGE_ACCOUNT_NAME", "USER_STORAGE_SAS_TOKEN",
    "GRAPH_API_ID", "GRAPH_API_SECRET", "VNC_SOCKET_DIR", "APP_URL",
})
FORBIDDEN_ENV_PREFIXES = ("DL_", "AWS_ENDPOINT_URL", "AWS_BEARER_TOKEN_", "AWS_CSM_")
# A plain-text Environment entry must not look like a secret.
SECRET_SHAPED = re.compile(r"SECRET|PASSWORD|API_KEY|ENCRYPTION_KEY", re.IGNORECASE)

GEMINI_CONDITION = "GeminiEnabled"
BROWSER_CONDITION = "BrowserAgentEnabled"
GEMINI_ENV = frozenset({"GEMINI_API_KEY", "GEMINI_MODEL"})
# Set always, whatever the parameters (BROWSER_USE_HOST too: the cards'
# images need it; BROWSER_AGENT_ENABLED to true or false).
UNCONDITIONAL_ENV = frozenset({
    "MODEL_NAME", "USER_STORAGE_TABLE_NAME", "TASK_STORAGE_TABLE_NAME",
    "BLOB_CONTAINER_NAME", "CONVERSATION_CONTAINER_NAME",
    "connections__serviceConnection__settings__clientId",
    "connections__serviceConnection__settings__clientSecret",
    "connections__serviceConnection__settings__tenantId",
    "REDIRECT_URI", "BROWSER_USE_HOST", "BROWSER_AGENT_ENABLED", "ENCRYPTION_KEY", "AWS_MARKETPLACE_PRODUCT_CODE",
})
CREDENTIALS_SECRET = "CredentialsSecretArn"
# JSON keys of the buyer's secret, and the condition each is read under
# (None: always). ECS stops a task whose ValueFrom names an absent key.
CREDENTIALS_KEYS = {"AppPassword": None, "GeminiApiKey": GEMINI_CONDITION}

# The load balancer's forward rules: priority -> (paths, condition).
PUBLIC_PATHS = {
    10: (["/api/messages", "/callback", "/health"], None),
    20: (["/static/*"], None),
    30: (["/vnc/*", "/websockify/*", "/api/browser-use/*"], BROWSER_CONDITION),
}
NEVER_PUBLIC = frozenset({"/api/prompt", "/proactive", "/validate-vnc", "/*", "*"})
ORIGIN_HEADER = "X-Origin-Verify"

# helpers/aws_tables.py and bot/tables_storage.js address items by these.
TABLE_ATTRIBUTES = [("PartitionKey", "S"), ("RowKey", "S")]
TABLE_KEYS = [("PartitionKey", "HASH"), ("RowKey", "RANGE")]
TABLE_COUNT = 2

APP_MODELS_FILE = Path("tests") / "unit" / "test_bedrock_models.py"
APP_CONTRACT_FILE = Path("docs") / "aws-marketplace.md"
APP_CONTRACT_SECTION = "## Environment and IAM contract"

NO_VALUE = {"Ref": "AWS::NoValue"}


class TemplateError(Exception):
    """The template cannot be read the way these checks need."""


def load_template(text, path):
    try:
        from cfnlint.decode import cfn_yaml
    except ImportError:
        raise TemplateError("needs cfn-lint's YAML loader: pip install cfn-lint") from None
    try:
        return cfn_yaml.loads(text, str(path))
    except Exception as e:  # cfn-lint raises its own match errors
        raise TemplateError(f"cannot parse {path}: {e}") from None


def resources_of_type(template, kind):
    return {name: res for name, res in template.get("Resources", {}).items() if res.get("Type") == kind}


def container(template):
    tasks = resources_of_type(template, "AWS::ECS::TaskDefinition")
    if len(tasks) != 1:
        raise TemplateError(f"expected one AWS::ECS::TaskDefinition, found {len(tasks)}")
    containers = next(iter(tasks.values()))["Properties"]["ContainerDefinitions"]
    if len(containers) != 1:
        raise TemplateError(f"expected one container, found {len(containers)}")
    return containers[0]


def env_entries(template):
    """Every Environment and Secrets entry as (name, list, condition, entry).
    condition is None for an unconditional entry, else the condition name,
    with '!' in front for an Fn::If's false branch."""
    entries = []
    spec = container(template)
    for kind in ("Environment", "Secrets"):
        for item in spec.get(kind, []):
            if isinstance(item, dict) and "Name" in item:
                entries.append((item["Name"], kind, None, item))
            elif isinstance(item, dict) and list(item) == ["Fn::If"]:
                cond, yes, no = item["Fn::If"]
                for branch, label in ((yes, cond), (no, "!" + cond)):
                    if branch == NO_VALUE:
                        continue
                    if not (isinstance(branch, dict) and "Name" in branch):
                        raise TemplateError(f"{kind}: unrecognised Fn::If branch {branch!r}")
                    entries.append((branch["Name"], kind, label, branch))
            else:
                raise TemplateError(f"{kind}: unrecognised entry {item!r}")
    return entries


def forbidden(name):
    return name in FORBIDDEN_ENV or any(name.startswith(p) for p in FORBIDDEN_ENV_PREFIXES)


# ---------------------------------------------------------------- checks
# Each takes the check context and returns a list of problems.

def check_forbidden_env(ctx):
    problems = []
    for name, kind, _, _ in ctx["env"]:
        if forbidden(name):
            problems.append(f"{kind} sets {name}, which the template must never set")
    return problems


def check_env_placement(ctx):
    problems = []
    seen = {}
    for name, kind, cond, entry in ctx["env"]:
        seen.setdefault(name, []).append(cond)
        if kind == "Environment" and SECRET_SHAPED.search(name):
            problems.append(f"{name} is a secret-shaped name in plain-text Environment; use Secrets")
        if name in GEMINI_ENV and cond != GEMINI_CONDITION:
            problems.append(f"{name} must exist only under Fn::If [{GEMINI_CONDITION}, ..., AWS::NoValue]")
        if kind == "Secrets":
            problems.extend(check_value_from(name, cond, entry.get("ValueFrom")))
    for name, conds in seen.items():
        if len(conds) > 1:
            problems.append(f"{name} is set {len(conds)} times")
    for name in sorted(UNCONDITIONAL_ENV):
        if seen.get(name) != [None]:
            problems.append(f"{name} must be set exactly once, unconditionally")
    for name in sorted(GEMINI_ENV):
        if name not in seen:
            problems.append(f"{name} is missing (under {GEMINI_CONDITION})")
    return problems


def check_value_from(name, cond, value_from):
    if name == "ENCRYPTION_KEY":
        if value_from != {"Ref": ENCRYPTION_KEY_RESOURCE}:
            return [f"ENCRYPTION_KEY must come from !Ref {ENCRYPTION_KEY_RESOURCE}, not {value_from!r}"]
        return []
    sub = value_from.get("Fn::Sub") if isinstance(value_from, dict) else None
    match = re.fullmatch(r"\$\{" + CREDENTIALS_SECRET + r"\}:([A-Za-z0-9]+)::", sub or "")
    if not match:
        return [f"{name}: ValueFrom must be !Sub '${{{CREDENTIALS_SECRET}}}:<JSON key>::', not {value_from!r}"]
    key = match.group(1)
    if key not in CREDENTIALS_KEYS:
        return [f"{name}: ValueFrom names JSON key {key}, which the buyer's secret does not hold"]
    if CREDENTIALS_KEYS[key] != cond:
        want = f"under {CREDENTIALS_KEYS[key]}" if CREDENTIALS_KEYS[key] else "unconditionally"
        return [f"{name}: JSON key {key} may be absent; read it only {want}"]
    return []


def check_paths(ctx):
    template = ctx["template"]
    problems = []
    listeners = resources_of_type(template, "AWS::ElasticLoadBalancingV2::Listener")
    for name, res in listeners.items():
        actions = res["Properties"].get("DefaultActions", [])
        fixed = [a.get("FixedResponseConfig", {}).get("StatusCode") for a in actions]
        if fixed != ["403"] or actions[0].get("Type") != "fixed-response":
            problems.append(f"{name}: the default action must be a fixed 403, not {actions!r}")
    rules = resources_of_type(template, "AWS::ElasticLoadBalancingV2::ListenerRule")
    found = {}
    for name, res in rules.items():
        props = res["Properties"]
        priority = props.get("Priority")
        paths, headers = [], []
        for cond in props.get("Conditions", []):
            if cond.get("Field") == "path-pattern":
                paths.extend(cond.get("PathPatternConfig", {}).get("Values", []))
            elif cond.get("Field") == "http-header":
                headers.append(cond.get("HttpHeaderConfig", {}).get("HttpHeaderName"))
            else:
                problems.append(f"{name}: unexpected condition field {cond.get('Field')!r}")
        for path in paths:
            if path in NEVER_PUBLIC:
                problems.append(f"{name} forwards {path}, which must never be public")
        if headers != [ORIGIN_HEADER]:
            problems.append(f"{name} must require exactly the {ORIGIN_HEADER} header, not {headers!r}")
        if priority not in PUBLIC_PATHS:
            problems.append(f"{name}: priority {priority!r} is not one of {sorted(PUBLIC_PATHS)}")
            continue
        want_paths, want_cond = PUBLIC_PATHS[priority]
        if paths != want_paths:
            problems.append(f"{name} (priority {priority}) forwards {paths!r}, allowed {want_paths!r}")
        if res.get("Condition") != want_cond:
            problems.append(f"{name} (priority {priority}) needs Condition {want_cond!r}, has {res.get('Condition')!r}")
        found[priority] = name
    for priority in sorted(set(PUBLIC_PATHS) - set(found)):
        problems.append(f"no listener rule with priority {priority} {PUBLIC_PATHS[priority][0]!r}")
    outputs = template.get("Outputs", {})
    if "BrowserViewUrl" not in outputs:
        problems.append("the BrowserViewUrl output is missing")
    elif outputs["BrowserViewUrl"].get("Condition") != BROWSER_CONDITION:
        problems.append(f"the BrowserViewUrl output needs Condition {BROWSER_CONDITION}, as the live view's rule")
    return problems


def check_tables(ctx):
    problems = []
    tables = resources_of_type(ctx["template"], "AWS::DynamoDB::Table")
    if len(tables) != TABLE_COUNT:
        problems.append(f"expected {TABLE_COUNT} DynamoDB tables, found {len(tables)}")
    for name, res in tables.items():
        props = res["Properties"]
        attrs = [(a.get("AttributeName"), a.get("AttributeType")) for a in props.get("AttributeDefinitions", [])]
        keys = [(k.get("AttributeName"), k.get("KeyType")) for k in props.get("KeySchema", [])]
        if attrs != TABLE_ATTRIBUTES:
            problems.append(f"{name}: AttributeDefinitions {attrs!r}, must be {TABLE_ATTRIBUTES!r}")
        if keys != TABLE_KEYS:
            problems.append(f"{name}: KeySchema {keys!r}, must be {TABLE_KEYS!r}")
        if props.get("BillingMode") != "PAY_PER_REQUEST":
            problems.append(f"{name}: BillingMode must be PAY_PER_REQUEST")
        if res.get("DeletionPolicy") != "RetainExceptOnCreate":
            problems.append(f"{name}: DeletionPolicy must be RetainExceptOnCreate")
    return problems


def check_regions(ctx):
    template = ctx["template"]
    try:
        assertion = template["Rules"]["SupportedRegion"]["Assertions"][0]
        regions = list(assertion["Assert"]["Fn::Contains"][0])
        message = " ".join(str(assertion["AssertDescription"]).split())
        mapped = list(template["Mappings"]["RegionMap"])
    except (KeyError, IndexError, TypeError) as e:
        return [f"cannot read Rules.SupportedRegion or Mappings.RegionMap ({e!r})"]
    problems = []
    if len(set(regions)) != len(regions):
        problems.append("SupportedRegion lists a Region twice")
    if set(regions) != set(mapped):
        problems.append(f"SupportedRegion and RegionMap differ: only in the rule {sorted(set(regions) - set(mapped))}, "
                        f"only in RegionMap {sorted(set(mapped) - set(regions))}")
    listed = "Use " + ", ".join(regions[:-1]) + " or " + regions[-1] + "."
    if listed not in message:
        problems.append(f"SupportedRegion's AssertDescription does not say {listed!r}")
    return problems


def check_size(ctx):
    size = len(ctx["raw"])
    if size >= MAX_TEMPLATE_BYTES:
        return [f"{size} bytes; --template-body takes less than {MAX_TEMPLATE_BYTES}"]
    return []


def encryption_key_block(text):
    starts = [i for i, line in enumerate(text.splitlines()) if line.strip() == BLOCK_START]
    ends = [i for i, line in enumerate(text.splitlines()) if line.strip() == BLOCK_END]
    if len(starts) != 1 or len(ends) != 1 or ends[0] < starts[0]:
        return None
    lines = text.splitlines(keepends=True)
    return "".join(lines[starts[0] + 1:ends[0]])


def check_encryption_key(ctx):
    problems = []
    block = encryption_key_block(ctx["text"])
    if block is None:
        return [f"needs exactly one '{BLOCK_START}' line followed by one '{BLOCK_END}' line"]
    digest = hashlib.sha256(block.encode("utf-8")).hexdigest()
    if digest != ENCRYPTION_KEY_BLOCK_SHA256:
        problems.append(f"the {ENCRYPTION_KEY_RESOURCE} block changed (sha256 {digest}); a new "
                        "GenerateSecretString signs every user out")
    if not block.lstrip().startswith(ENCRYPTION_KEY_RESOURCE + ":"):
        problems.append(f"the pinned block must hold the {ENCRYPTION_KEY_RESOURCE} resource")
    secrets = sorted(resources_of_type(ctx["template"], "AWS::SecretsManager::Secret"))
    if secrets != [ENCRYPTION_KEY_RESOURCE]:
        problems.append(f"the stack may generate only {ENCRYPTION_KEY_RESOURCE}, it has {secrets!r}")
    return problems


def check_hardening(ctx):
    problems = []
    linux = container(ctx["template"]).get("LinuxParameters", {})
    if linux.get("InitProcessEnabled"):
        problems.append("InitProcessEnabled must stay off: tini is the image's PID 1")
    caps = linux.get("Capabilities", {})
    if caps.get("Drop") != ["ALL"] or caps.get("Add"):
        problems.append(f"Capabilities must be Drop [ALL] with no Add, not {caps!r}")
    for name, res in resources_of_type(ctx["template"], "AWS::ECS::Service").items():
        props = res["Properties"]
        if props.get("EnableExecuteCommand") is not False:
            problems.append(f"{name}: EnableExecuteCommand must be false")
        if props.get("DesiredCount") != 1:
            problems.append(f"{name}: DesiredCount must be 1 (sessions live in one task's memory)")
    return problems


def language_model(template):
    param = template.get("Parameters", {}).get("LanguageModel", {})
    return param.get("Default"), list(param.get("AllowedValues", []))


def check_models(ctx):
    default, allowed = language_model(ctx["template"])
    problems = []
    if not allowed or default != allowed[0]:
        problems.append(f"LanguageModel's Default {default!r} must be its first AllowedValue")
    if len(set(allowed)) != len(allowed):
        problems.append("LanguageModel lists a model twice")
    return problems


def app_models(app_repo):
    path = app_repo / APP_MODELS_FILE
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "TEMPLATE_MODELS" for t in node.targets):
            return list(ast.literal_eval(node.value))
    raise TemplateError(f"{path} has no TEMPLATE_MODELS list")


def check_app_models(ctx):
    _, allowed = language_model(ctx["template"])
    expected = app_models(ctx["app_repo"])
    if allowed != expected:
        return [f"LanguageModel AllowedValues {allowed!r} differ from {APP_MODELS_FILE}'s TEMPLATE_MODELS "
                f"{expected!r} (same models, same order, default first)"]
    return []


def app_env_contract(app_repo):
    """(allowed, refused, values) from the contract table: the names of the
    Required, Optional and Forced rows, the Must not set row (a trailing '*'
    is a prefix), and NAME=value pairs with a literal value."""
    path = app_repo / APP_CONTRACT_FILE
    text = path.read_text(encoding="utf-8")
    if APP_CONTRACT_SECTION not in text:
        raise TemplateError(f"{path} has no '{APP_CONTRACT_SECTION}' section")
    section = text.split(APP_CONTRACT_SECTION, 1)[1].split("\n## ", 1)[0]
    allowed, refused, values = set(), set(), {}
    rows = 0
    for line in section.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) != 2 or not line.lstrip().startswith("|"):
            continue
        group, variables = cells
        tokens = re.findall(r"`([^`]+)`", variables)
        if group.startswith(("Required", "Optional")):
            rows += 1
            for token in tokens:
                name, eq, value = token.partition("=")
                if re.fullmatch(r"[A-Z][A-Z0-9_]*|connections__[A-Za-z_]+", name):
                    allowed.add(name)
                    if eq and re.fullmatch(r"[A-Za-z0-9._-]+", value):
                        values[name] = value
        elif group.startswith("Forced"):
            rows += 1
            for token in tokens:
                name, eq, value = token.partition("=")
                if eq and re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
                    allowed.add(name)
                    values[name] = value
        elif group.startswith("Must not set"):
            rows += 1
            refused.update(t for t in tokens if re.fullmatch(r"[A-Z][A-Z0-9_]*\*?", t))
    if rows < 4 or not allowed:
        raise TemplateError(f"{path}: the contract table's Required, Optional, Forced and Must not set rows "
                            "were not found")
    return allowed, refused, values


def check_app_env(ctx):
    allowed, refused, values = app_env_contract(ctx["app_repo"])
    problems = []
    for name, kind, _, entry in ctx["env"]:
        refuses = [r for r in refused if name == r or (r.endswith("*") and name.startswith(r[:-1]))]
        if refuses:
            problems.append(f"{kind} sets {name}, which {APP_CONTRACT_FILE} says must not be set")
        elif name not in allowed:
            problems.append(f"{kind} sets {name}, which is not in {APP_CONTRACT_FILE}'s contract table")
        value = entry.get("Value")
        if name in values and isinstance(value, str) and value != values[name]:
            problems.append(f"{name} is {value!r}; {APP_CONTRACT_FILE} says {values[name]!r}")
    return problems


CHECKS = [
    ("forbidden environment names", check_forbidden_env),
    ("environment and secrets placement", check_env_placement),
    ("load balancer path allowlist", check_paths),
    ("DynamoDB key schema", check_tables),
    ("Regions: rule, message and RegionMap", check_regions),
    ("template size", check_size),
    ("pinned ENCRYPTION_KEY block", check_encryption_key),
    ("task hardening", check_hardening),
    ("LanguageModel default", check_models),
]
APP_CHECKS = [
    ("LanguageModel against the app's model test", check_app_models),
    ("environment against the app's contract", check_app_env),
]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    parser.add_argument("--app-repo", type=Path, help="checkout of ai-employee, branch aws-marketplace-offer")
    args = parser.parse_args(argv)
    try:
        raw = args.template.read_bytes()
        text = raw.decode("utf-8")
        template = load_template(text, args.template)
        ctx = {"raw": raw, "text": text, "template": template, "env": env_entries(template)}
        checks = list(CHECKS)
        if args.app_repo:
            for rel in (APP_MODELS_FILE, APP_CONTRACT_FILE):
                if not (args.app_repo / rel).is_file():
                    raise TemplateError(f"--app-repo {args.app_repo} has no {rel}")
            ctx["app_repo"] = args.app_repo
            checks += APP_CHECKS
    except (OSError, UnicodeDecodeError, TemplateError) as e:
        print(f"ERROR: {e}")
        return 1
    failed = 0
    for label, check in checks:
        try:
            problems = check(ctx)
        except (KeyError, TypeError, ValueError, AttributeError, SyntaxError, TemplateError) as e:
            problems = [f"could not run: {type(e).__name__}: {e}"]
        if problems:
            failed += 1
            print(f"  {label}: FAILED")
            for problem in problems:
                print(f"    - {problem}")
        else:
            print(f"  {label}: ok")
    if failed:
        print(f"ERROR: {failed} of {len(checks)} template invariants failed in {args.template}")
        return 1
    print(f"  {args.template.name}: all {len(checks)} invariants hold")
    return 0


if __name__ == "__main__":
    sys.exit(main())
