#!/usr/bin/env python3
"""End-to-end test of a seller-account test stack (scripts/deploy-test.sh). Not for buyers.

Usage:
    scripts/e2e-test.py <env-file> [--stack nlsql-ai-agent-test] [--region us-east-1]
                        [--sql-question "..."] [--docs-question "..." --docs-expect "..."]
    scripts/e2e-test.py --teardown [--stack ...]      remove the capture function and its role

It drives the deployed bot the way Microsoft Teams does, without Teams:

1. A temporary AWS Lambda function URL stands in for the Bot Connector. Its URL
   is sent as the activities' serviceUrl, so the bot's replies (which the SDK
   POSTs to <serviceUrl>/v3/conversations/...) land in its CloudWatch log. It
   also serves a small text file for the attachment test.
2. An Entra ID client-credentials token for the bot's own app ID (from the env
   file) authorises each activity: the Agents SDK accepts tokens whose audience
   is the app ID from the app's own tenant, and replies with a token for the
   same app, so no real Teams tenant or channel is involved.
3. Teams-shaped activities are POSTed to the stack's MessagingEndpoint through
   CloudFront, and the captured replies are checked.

Checks: /health and /ready; the load balancer refuses requests that bypass
CloudFront; every activity is acknowledged within ACK_LIMIT seconds (turns are
answered proactively); the welcome message; a document answer; an attachment
answer; in DocsAndSql mode an NLSQL answer, whose chart link must be served
with no-store; exactly one reply per question; and no secret value from the env
file appears anywhere in the task's log group. Secret values are never printed.
"""

import argparse
import base64
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile

import boto3
from botocore.exceptions import ClientError

ACK_LIMIT = 8.0            # seconds for /api/messages to answer 200/202
REPLY_TIMEOUT = 300        # seconds to wait for a proactive answer
TEST_TAG = {"nlsql:purpose": "test"}
ATTACHMENT_NAME = "e2e-attachment.txt"
ATTACHMENT_TEXT = ("NLSQL AI Agent end-to-end test attachment.\n"
                   "The secret number in this attached file is 7319.\n")

# The capture function. It logs one JSON line per request (never the headers,
# which carry the bot's bearer token) and answers like the Bot Connector does.
CAPTURE_CODE = r'''
import base64, json, time

ATTACHMENT = %(attachment)r

def handler(event, context):
    http = event.get("requestContext", {}).get("http", {})
    method, path = http.get("method", ""), event.get("rawPath", "")
    if method == "GET" and path.endswith("/files/%(name)s"):
        return {"statusCode": 200, "headers": {"Content-Type": "text/plain"}, "body": ATTACHMENT}
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8", "replace")
    rid = "capture-%%d-%%s" %% (int(time.time() * 1000), context.aws_request_id[:8])
    print(json.dumps({"capture": True, "method": method, "path": path, "returned_id": rid,
                      "body": body[:60000]}))
    return {"statusCode": 200, "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"id": rid})}
''' % {"attachment": ATTACHMENT_TEXT, "name": ATTACHMENT_NAME}


def log(msg):
    print(msg, flush=True)


def read_env(path):
    env = {}
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            env[key.strip()] = value
    return env


def http(method, url, body=None, headers=None, timeout=30):
    data = None
    headers = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode()
        headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read()


class Result:
    def __init__(self):
        self.rows = []

    def add(self, name, ok, detail=""):
        self.rows.append((name, ok, detail))
        log(f"  {'PASS' if ok else 'FAIL'}  {name}{': ' + detail if detail else ''}")

    @property
    def failed(self):
        return [r for r in self.rows if not r[1]]


# --- capture function -------------------------------------------------------

def capture_names(stack):
    return f"{stack}-capture", f"{stack}-capture-role"


def ensure_capture(session, stack):
    fn_name, role_name = capture_names(stack)
    iam, lam = session.client("iam"), session.client("lambda")
    try:
        role_arn = iam.get_role(RoleName=role_name)["Role"]["Arn"]
    except iam.exceptions.NoSuchEntityException:
        role_arn = iam.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps({
                "Version": "2012-10-17",
                "Statement": [{"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"},
                               "Action": "sts:AssumeRole"}]}),
            Description="NLSQL AI Agent e2e test capture function (temporary)",
            Tags=[{"Key": k, "Value": v} for k, v in TEST_TAG.items()],
        )["Role"]["Arn"]
        iam.attach_role_policy(RoleName=role_name,
                               PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole")
        log(f"  created role {role_name}; waiting for IAM to propagate")
        time.sleep(12)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("index.py", CAPTURE_CODE)
    code = buf.getvalue()
    try:
        lam.get_function(FunctionName=fn_name)
        lam.update_function_code(FunctionName=fn_name, ZipFile=code)
    except lam.exceptions.ResourceNotFoundException:
        for attempt in range(6):
            try:
                lam.create_function(FunctionName=fn_name, Runtime="python3.13", Role=role_arn,
                                    Handler="index.handler", Code={"ZipFile": code}, Timeout=10,
                                    MemorySize=128, Tags=TEST_TAG,
                                    Description="NLSQL AI Agent e2e test: captures bot replies (temporary)")
                break
            except ClientError as exc:
                if "role" in str(exc).lower() and attempt < 5:
                    time.sleep(8)
                    continue
                raise
    lam.get_waiter("function_active_v2").wait(FunctionName=fn_name)
    lam.get_waiter("function_updated_v2").wait(FunctionName=fn_name)

    try:
        url = lam.get_function_url_config(FunctionName=fn_name)["FunctionUrl"]
    except lam.exceptions.ResourceNotFoundException:
        url = lam.create_function_url_config(FunctionName=fn_name, AuthType="NONE")["FunctionUrl"]
    for sid, kwargs in (
        ("public-url", {"Action": "lambda:InvokeFunctionUrl", "FunctionUrlAuthType": "NONE"}),
        ("public-url-invoke", {"Action": "lambda:InvokeFunction", "InvokedViaFunctionUrl": True}),
    ):
        try:
            lam.add_permission(FunctionName=fn_name, StatementId=sid, Principal="*", **kwargs)
        except lam.exceptions.ResourceConflictException:
            pass
        except Exception as exc:  # older SDKs lack InvokedViaFunctionUrl
            log(f"  note: permission {sid} not added ({type(exc).__name__})")
    return url.rstrip("/") + "/", f"/aws/lambda/{fn_name}"


def teardown(session, stack):
    fn_name, role_name = capture_names(stack)
    lam, iam, logs = session.client("lambda"), session.client("iam"), session.client("logs")
    try:
        tags = lam.list_tags(Resource=lam.get_function(FunctionName=fn_name)["Configuration"]["FunctionArn"])["Tags"]
        if tags.get("nlsql:purpose") != "test":
            sys.exit(f"refusing to delete {fn_name}: it is not tagged nlsql:purpose=test")
        lam.delete_function(FunctionName=fn_name)
        log(f"deleted function {fn_name}")
    except lam.exceptions.ResourceNotFoundException:
        pass
    try:
        logs.delete_log_group(logGroupName=f"/aws/lambda/{fn_name}")
    except logs.exceptions.ResourceNotFoundException:
        pass
    try:
        for p in iam.list_attached_role_policies(RoleName=role_name)["AttachedPolicies"]:
            iam.detach_role_policy(RoleName=role_name, PolicyArn=p["PolicyArn"])
        iam.delete_role(RoleName=role_name)
        log(f"deleted role {role_name}")
    except iam.exceptions.NoSuchEntityException:
        pass


def captured(session, log_group, since_ms):
    """All capture records since since_ms: dicts with method, path, returned_id, activity."""
    logs = session.client("logs")
    out, token = [], None
    while True:
        kwargs = {"logGroupName": log_group, "startTime": since_ms, "filterPattern": '"capture"'}
        if token:
            kwargs["nextToken"] = token
        try:
            resp = logs.filter_log_events(**kwargs)
        except logs.exceptions.ResourceNotFoundException:
            return out
        for ev in resp.get("events", []):
            msg = ev["message"]
            start = msg.find("{")
            if start < 0:
                continue
            try:
                rec = json.loads(msg[start:])
                activity = json.loads(rec.get("body") or "{}")
            except ValueError:
                continue
            out.append({"method": rec.get("method", ""), "path": rec.get("path", ""),
                        "returned_id": rec.get("returned_id", ""), "activity": activity})
        token = resp.get("nextToken")
        if not token:
            return out


def task_log(session, log_group, since_ms, pattern):
    logs = session.client("logs")
    out, token = [], None
    while True:
        kwargs = {"logGroupName": log_group, "startTime": since_ms, "filterPattern": pattern}
        if token:
            kwargs["nextToken"] = token
        resp = logs.filter_log_events(**kwargs)
        out.extend(ev["message"] for ev in resp.get("events", []))
        token = resp.get("nextToken")
        if not token:
            return out


def fallback_reasons(session, log_group, since_ms):
    """Reason categories the bot logged for both-mode fallbacks since since_ms (never questions)."""
    reasons = []
    for line in task_log(session, log_group, since_ms, '"both mode: NLSQL could not answer"'):
        m = re.search(r"could not answer \((.*?)\); asking Talk to Docs", line)
        if m:
            reasons.append(m.group(1))
    return reasons


# --- the bot ------------------------------------------------------------------

def entra_token(tenant, client_id, secret):
    form = urllib.parse.urlencode([
        ("grant_type", "client_credentials"),
        ("client_id", client_id),
        ("client_secret", secret),
        ("scope", f"{client_id}/.default"),
    ]).encode()
    req = urllib.request.Request(f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token", data=form)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)["access_token"]


def activity(kind, conv_id, service_url, app_id, tenant, text=None, attachments=None, members_added=None):
    act = {
        "type": kind,
        "id": str(uuid.uuid4()),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
        "channelId": "msteams",
        "serviceUrl": service_url,
        "from": {"id": "29:e2e-test-user", "name": "E2E Tester",
                 "aadObjectId": "00000000-0000-0000-0000-00000000e2e0"},
        "conversation": {"id": conv_id, "conversationType": "personal", "tenantId": tenant},
        "recipient": {"id": f"28:{app_id}", "name": "NLSQL AI Agent"},
        "locale": "en-US",
        "channelData": {"tenant": {"id": tenant}},
    }
    if text is not None:
        act["text"] = text
        act["textFormat"] = "plain"
    if attachments:
        act["attachments"] = attachments
    if members_added:
        act["membersAdded"] = members_added
    return act


def post_activity(endpoint, token, act):
    t0 = time.monotonic()
    status, _, body = http("POST", endpoint, act, {"Authorization": f"Bearer {token}"}, timeout=90)
    return status, time.monotonic() - t0, body[:200]


def replies_to(records, conv_id, activity_id):
    """Messages the bot created for one inbound activity, each with its final content.

    In Teams the bot posts a "Processing your request..." card and then edits it
    in place (PUT .../activities/<id>), so an edit is not a second reply: it is
    linked to its card through the id the capture function handed back.
    """
    def in_conv(r):
        path = r["path"]
        return urllib.parse.quote(conv_id, safe="") in path or conv_id in urllib.parse.unquote(path)

    records = [r for r in records if in_conv(r)]
    posts = [r for r in records
             if r["method"] == "POST" and r["activity"].get("type") != "typing"
             and (r["activity"].get("replyToId") == activity_id
                  or r["path"].rstrip("/").endswith("/" + activity_id))]
    out = []
    for post in posts:
        edits = [r for r in records if r["method"] == "PUT" and post["returned_id"]
                 and r["path"].rstrip("/").endswith("/" + post["returned_id"])]
        out.append({"initial": post["activity"], "final": (edits[-1] if edits else post)["activity"],
                    "edits": len(edits)})
    return out


# Without Streaming the bot posts this status message before the answer card
# (master's behaviour); it is not an answer.
STATUS_MESSAGE = "Processing your request..."


def _card_texts(node):
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("text", "title", "value") and isinstance(value, str):
                yield value
            else:
                yield from _card_texts(value)
    elif isinstance(node, list):
        for item in node:
            yield from _card_texts(item)


def activity_text(act):
    """The readable text of a reply: its text plus every text field of its cards."""
    parts = [act.get("text") or ""]
    for att in act.get("attachments") or []:
        parts.extend(_card_texts(att.get("content", "")))
    return "\n".join(p for p in parts if p)


def is_answer(reply):
    return activity_text(reply["final"]).strip() != STATUS_MESSAGE


def wait_reply(session, log_group, since_ms, conv_id, activity_id, timeout=REPLY_TIMEOUT, want=None):
    deadline = time.monotonic() + timeout
    got = []
    while time.monotonic() < deadline:
        got = replies_to(captured(session, log_group, since_ms), conv_id, activity_id)
        answers = [r for r in got if is_answer(r)]
        if answers and (want is None or any(want(activity_text(r["final"])) for r in answers)):
            time.sleep(10)  # give a duplicate the chance to show up
            return replies_to(captured(session, log_group, since_ms), conv_id, activity_id)
        time.sleep(5)
    return got


# --- main ---------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("env_file", nargs="?")
    ap.add_argument("--stack", default="nlsql-ai-agent-test")
    ap.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    ap.add_argument("--docs-question", default="What is the code word for this test deployment?")
    ap.add_argument("--docs-expect", default="AMBER-FALCON-42")
    ap.add_argument("--sql-question", default="",
                    help="DocsAndSql: a question your NLSQL KPIs can answer (default: skip the NLSQL answer check)")
    ap.add_argument("--sql-expect", default="",
                    help="DocsAndSql: text the NLSQL answer must contain (makes the database check strict)")
    ap.add_argument("--teardown", action="store_true", help="remove the capture function and role, then exit")
    ap.add_argument("--keep-capture", action="store_true", help="leave the capture function in place")
    args = ap.parse_args()

    session = boto3.Session(region_name=args.region)
    if args.teardown:
        teardown(session, args.stack)
        return 0
    if not args.env_file:
        ap.error("env_file is required")
    env = read_env(args.env_file)
    app_id = env.get("connections__serviceConnection__settings__clientId") or env.get("MicrosoftAppId", "")
    secret = env.get("connections__serviceConnection__settings__clientSecret") or env.get("MicrosoftAppPassword", "")
    tenant = env.get("connections__serviceConnection__settings__tenantId") or env.get("MicrosoftAppTenantId", "")
    if not (app_id and secret and tenant):
        sys.exit("the env file lacks the bot's clientId, clientSecret or tenantId")

    cfn = session.client("cloudformation")
    stack = cfn.describe_stacks(StackName=args.stack)["Stacks"][0]
    outputs = {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}
    mode = {p["ParameterKey"]: p.get("ParameterValue", "") for p in stack.get("Parameters", [])}.get("Mode", "")
    endpoint = outputs["MessagingEndpoint"]
    base = endpoint.rsplit("/api/messages", 1)[0]
    res = Result()
    log(f"Stack {args.stack} ({mode}), endpoint {endpoint}")

    # 1. health and readiness
    log("health:")
    deadline = time.monotonic() + 900
    ready_body = b""
    while time.monotonic() < deadline:
        h, _, _ = http("GET", base + "/health")
        r, _, ready_body = http("GET", base + "/ready")
        if h == 200 and r == 200:
            break
        time.sleep(15)
    res.add("/health 200 (entitlement verified)", h == 200, f"got {h}")
    res.add("/ready 200 (documents indexed)", r == 200, f"got {r} {ready_body[:80]!r}")
    try:
        status_json = json.loads(ready_body or b"{}")
    except ValueError:
        status_json = {}
    res.add("/ready exposes status only", set(status_json) <= {"status"}, f"keys {sorted(status_json)}")

    # 2. the load balancer only answers CloudFront
    lb_arn = next(r["PhysicalResourceId"] for r in
                  cfn.describe_stack_resources(StackName=args.stack)["StackResources"]
                  if r["LogicalResourceId"] == "LoadBalancer")
    lb_dns = session.client("elbv2").describe_load_balancers(LoadBalancerArns=[lb_arn])["LoadBalancers"][0]["DNSName"]
    d, _, _ = http("GET", f"http://{lb_dns}/health")
    res.add("load balancer refuses requests without the CloudFront header", d == 403, f"got {d}")

    # 3. conversations through CloudFront
    log("capture function:")
    service_url, capture_log = ensure_capture(session, args.stack)
    log(f"  serviceUrl {service_url}")
    token = entra_token(tenant, app_id, secret)
    since = int(time.time() * 1000) - 5000
    conv = f"a:e2e-{uuid.uuid4().hex}"
    log("conversation:")

    welcome = activity("conversationUpdate", conv, service_url, app_id, tenant,
                       members_added=[{"id": "29:e2e-test-user", "name": "E2E Tester"}])
    st, took, body = post_activity(endpoint, token, welcome)
    res.add("conversationUpdate acknowledged", st in (200, 201, 202) and took < ACK_LIMIT,
            f"HTTP {st} in {took:.1f}s {'' if st in (200, 201, 202) else body!r}")
    got = wait_reply(session, capture_log, since, conv, welcome["id"], timeout=60)
    res.add("welcome message", any("NLSQL AI Agent" in activity_text(r["final"]) for r in got),
            f"{len(got)} reply(ies)")

    both = mode == "DocsAndSql"
    checks = [("document answer", args.docs_question, None,
               lambda t: args.docs_expect.lower() in t.lower(), args.docs_expect)]
    file_url = service_url + "files/" + ATTACHMENT_NAME
    attachments = [
        {"contentType": "application/vnd.microsoft.teams.file.download.info",
         "contentUrl": file_url, "name": ATTACHMENT_NAME,
         "content": {"downloadUrl": file_url, "uniqueId": str(uuid.uuid4()), "fileType": "txt"}},
        {"contentType": "text/html", "content": "<p>What is the secret number in the attached file?</p>"},
    ]
    checks.append(("attachment answer", "What is the secret number in the attached file?", attachments,
                   lambda t: "7319" in t, "7319"))
    if both and args.sql_question:
        if args.sql_expect:
            checks.append(("NLSQL answer", args.sql_question, None,
                           lambda t: args.sql_expect.lower() in t.lower(), args.sql_expect))
        else:
            checks.append(("NLSQL answer", args.sql_question, None, lambda t: bool(t.strip()), "any answer"))

    sql_reply = None
    for name, text, atts, want, expect in checks:
        act = activity("message", conv, service_url, app_id, tenant, text=text, attachments=atts)
        q_since = int(time.time() * 1000) - 2000
        st, took, body = post_activity(endpoint, token, act)
        res.add(f"{name}: acknowledged at once", st in (200, 201, 202) and took < ACK_LIMIT,
                f"HTTP {st} in {took:.1f}s {'' if st in (200, 201, 202) else body!r}")
        got = wait_reply(session, capture_log, since, conv, act["id"], want=want)
        answers = [r for r in got if is_answer(r)]
        texts = [activity_text(r["final"]) for r in answers]
        res.add(f"{name}: contains {expect}", any(want(t) for t in texts),
                (" | ".join(t[:300].replace("\n", " / ") for t in texts) if texts else "no answer"))
        res.add(f"{name}: exactly one answer", len(answers) == 1,
                f"{len(answers)} answer(s), {len(got) - len(answers)} status message(s), "
                f"{sum(r['edits'] for r in got)} in-place edit(s)")
        if name == "NLSQL answer" and answers:
            sql_reply = json.dumps(answers[0]["final"], ensure_ascii=False)
        if both:
            time.sleep(3)  # CloudWatch Logs delivery
            reasons = fallback_reasons(session, outputs["LogGroupName"], q_since)
            if name == "document answer":
                res.add("document question: routed to the documents because NLSQL wrote no SQL",
                        reasons == ["not a question about the data"], f"logged reasons {reasons}")
            elif name == "attachment answer":
                res.add("file question: sent straight to the documents (NLSQL not asked)",
                        reasons == [], f"logged reasons {reasons}")
            elif name == "NLSQL answer":
                db_errors = task_log(session, outputs["LogGroupName"], q_since, '"cannot connect to the"')
                if not reasons:
                    log("  info  NLSQL answered from the database")
                else:
                    detail = re.sub(r"'[^']*'", "'<...>'", db_errors[0])[-160:] if db_errors else ""
                    log(f"  info  NLSQL fell back to the documents ({reasons[0]}){': ' + detail if detail else ''}")
                if args.sql_expect:
                    res.add("NLSQL question: answered from the database", not reasons, f"logged reasons {reasons}")

    if sql_reply is not None:
        links = re.findall(r"https://[^\s\"'<>)]+/bot/static/[^\s\"'<>)\\]+", sql_reply)
        if links:
            s, headers, _ = http("GET", links[0])
            cc = {k.lower(): v for k, v in headers.items()}.get("cache-control", "")
            res.add("NLSQL chart or export link served", s == 200, f"HTTP {s}")
            res.add("chart link is not cacheable", "no-store" in cc, f"Cache-Control: {cc}")
        else:
            log("  note: the NLSQL answer carried no chart or export link")

    # 4. no secret value in the task's logs
    log("logs:")
    secrets = [v for k, v in env.items()
               if k in ("connections__serviceConnection__settings__clientSecret", "ApiToken", "DbPassword")
               and len(v) >= 8]
    leaks = 0
    logs = session.client("logs")
    kwargs = {"logGroupName": outputs["LogGroupName"], "startTime": int(time.time() * 1000) - 6 * 3600 * 1000}
    scanned = 0
    while True:
        resp = logs.filter_log_events(**kwargs)
        for ev in resp.get("events", []):
            scanned += 1
            leaks += sum(1 for s in secrets if s in ev["message"])
        if not resp.get("nextToken"):
            break
        kwargs["nextToken"] = resp["nextToken"]
    res.add("no secret value in the task's log group", leaks == 0,
            f"{scanned} events scanned for {len(secrets)} secret values")

    if not args.keep_capture:
        teardown(session, args.stack)

    log("")
    log(f"summary: {len(res.rows) - len(res.failed)} passed, {len(res.failed)} failed")
    return 1 if res.failed else 0


if __name__ == "__main__":
    sys.exit(main())
