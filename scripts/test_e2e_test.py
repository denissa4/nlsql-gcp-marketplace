#!/usr/bin/env python3
"""Offline tests of scripts/e2e-test.py's pure parts.

Run: python3 -m unittest discover -s scripts -p 'test_*.py'

Nothing here reaches a network: the activity builders, the reply matching,
the JWT, token and OAuth-state helpers (with fake keys made here), the
log-hygiene patterns, the test images, the capture function's handler (run
in-process), the WebSocket and RFB client (against a fake server on a local
socket pair), the /health classification and the command line. The boto3
calls are tested with botocore's Stubber when botocore is installed.
APP_REPO=<checkout of ai-employee on aws-marketplace-offer> also compares the
exact strings, the capability sentences, the sign-in scopes and the
attachment refusal reasons with the application. All IDs and secrets are
fake.
"""
import base64
import contextlib
import datetime
import hashlib
import hmac
import importlib.util
import io
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import types
import unittest
import unittest.mock
import urllib.parse
import zlib
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
REPO = SCRIPTS.parent
spec = importlib.util.spec_from_file_location("e2e_test", SCRIPTS / "e2e-test.py")
e2e = importlib.util.module_from_spec(spec)
spec.loader.exec_module(e2e)

try:
    import botocore.session
    from botocore.stub import ANY, Stubber
except ImportError:
    botocore = None

APP_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeee1"
TENANT = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeee2"
SERVICE_URL = "https://fake-capture.lambda-url.us-east-1.on.aws/"
ACCOUNT = "111122223333"


def fake_jwt(claims, key):
    def part(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")
    head = part({"alg": "HS256", "typ": "JWT"}) + "." + part(claims)
    sig = hmac.new(key, head.encode(), hashlib.sha256).digest()
    return head + "." + base64.urlsafe_b64encode(sig).decode().rstrip("=")


def itsdangerous_like(payload, key):
    """A token shaped as itsdangerous' URLSafeTimedSerializer makes it."""
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    stamp = base64.urlsafe_b64encode(struct.pack(">I", 1_800_000_000)).decode().rstrip("=")
    sig = hmac.new(key, f"{body}.{stamp}".encode(), hashlib.sha256).digest()
    return f"{body}.{stamp}.{base64.urlsafe_b64encode(sig).decode().rstrip('=')}"


def record(method, path, activity=None, returned_id="", ts=0, host="h", auth=False):
    return {"ts": ts, "method": method, "path": path, "host": host, "auth_header_present": auth,
            "returned_id": returned_id, "activity": activity or {}}


class CommandLineTest(unittest.TestCase):
    def test_defaults(self):
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            args = e2e.parse_args(["local.env"])
        self.assertEqual((args.stack, args.region, args.ws_idle), ("nlsql-ai-employee-test", "us-east-1", 900))
        self.assertFalse(args.skip_schedule or args.restart_probe or args.keep_capture or args.teardown)
        self.assertEqual(args.login_url, "")

    def test_stack_and_region_from_the_environment(self):
        with unittest.mock.patch.dict(os.environ, {"STACK_NAME": "other-test", "AWS_REGION": "us-west-2"},
                                      clear=True):
            args = e2e.parse_args(["local.env"])
        self.assertEqual((args.stack, args.region), ("other-test", "us-west-2"))

    def test_login_url_from_the_environment(self):
        url = "https://login.microsoftonline.com/t/oauth2/v2.0/authorize?client_id=a&state=fake.state.sig"
        with unittest.mock.patch.dict(os.environ, {"LOGIN_URL": url}, clear=True):
            self.assertEqual(e2e.parse_args(["local.env"]).login_url, url)
            self.assertEqual(e2e.parse_args(["local.env", "--login-url", "https://x/"]).login_url, "https://x/")

    def test_flags(self):
        args = e2e.parse_args(["local.env", "--skip-schedule", "--restart-probe", "--ws-idle", "120",
                               "--login-url", "https://x/", "--keep-capture", "--stack", "s"])
        self.assertTrue(args.skip_schedule and args.restart_probe and args.keep_capture)
        self.assertEqual((args.ws_idle, args.login_url, args.stack), (120, "https://x/", "s"))

    def test_teardown_needs_no_env_file(self):
        self.assertTrue(e2e.parse_args(["--teardown"]).teardown)

    def test_refusals(self):
        for argv in ([], ["local.env", "--ws-idle", "0"], ["local.env", "--ws-idle", "x"]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as cm:
                    e2e.parse_args(argv)
                self.assertEqual(cm.exception.code, 2)

    def test_help_runs_without_boto3_or_aws(self):
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": tempfile.gettempdir(),
               "AWS_CONFIG_FILE": "/dev/null", "AWS_SHARED_CREDENTIALS_FILE": "/dev/null",
               "AWS_EC2_METADATA_DISABLED": "true", "PYTHONDONTWRITEBYTECODE": "1"}
        out = subprocess.run([sys.executable, "-S", str(SCRIPTS / "e2e-test.py"), "--help"],
                             capture_output=True, text=True, timeout=60, env=env)
        self.assertEqual(out.returncode, 0, out.stderr)
        for flag in ("--skip-schedule", "--restart-probe", "--ws-idle", "--login-url", "--keep-capture",
                     "--teardown"):
            self.assertIn(flag, out.stdout)

    def test_main_without_boto3_says_so(self):
        with unittest.mock.patch.object(e2e, "boto3", None):
            with self.assertRaises(SystemExit) as cm:
                e2e.main(["local.env"])
        self.assertIn("needs boto3", str(cm.exception.code))

    def test_env_file_parsing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "local.env")
            value = "fake" + "-value-" + "x" * 12
            path.write_text("# comment\n\nexport A=1\nB='two words'\nC=\"q\"\nD\n"
                            f"connections__serviceConnection__settings__clientSecret={value}\n")
            env = e2e.read_env(path)
        self.assertEqual(env["A"], "1")
        self.assertEqual(env["B"], "two words")
        self.assertEqual(env["C"], "q")
        self.assertNotIn("D", env)
        self.assertEqual(e2e.secret_values(env), [value])


class ActivityTest(unittest.TestCase):
    def test_teams_message(self):
        act = e2e.activity("message", "a:conv", SERVICE_URL, APP_ID, TENANT, "29:e2e-x", text="hi")
        self.assertEqual(act["channelId"], "msteams")
        self.assertEqual(act["serviceUrl"], SERVICE_URL)
        self.assertEqual(act["from"], {"id": "29:e2e-x", "name": "E2E Tester", "aadObjectId": e2e.TEST_OID})
        self.assertEqual(act["recipient"]["id"], f"28:{APP_ID}")
        self.assertEqual(act["conversation"], {"id": "a:conv", "conversationType": "personal", "tenantId": TENANT})
        self.assertEqual(act["channelData"], {"tenant": {"id": TENANT}})
        self.assertEqual((act["text"], act["textFormat"]), ("hi", "plain"))
        self.assertNotIn("attachments", act)

    def test_direct_line_reuses_the_teams_users_id(self):
        act = e2e.activity("message", "dl-conv", SERVICE_URL, APP_ID, TENANT, "29:e2e-x", channel="directline",
                           text="hi")
        self.assertEqual(act["channelId"], "directline")
        self.assertEqual(act["from"]["id"], "29:e2e-x")
        self.assertEqual(act["conversation"], {"id": "dl-conv"})
        self.assertNotIn("channelData", act)

    def test_conversation_update_and_invoke(self):
        act = e2e.activity("conversationUpdate", "a:conv", SERVICE_URL, APP_ID, TENANT, "29:e2e-x",
                           members_added=[{"id": "29:e2e-x"}])
        self.assertEqual(act["membersAdded"], [{"id": "29:e2e-x"}])
        inv = e2e.open_live_browser_invoke("a:conv", SERVICE_URL, APP_ID, TENANT, "29:e2e-x")
        self.assertEqual((inv["type"], inv["name"]), ("invoke", "task/fetch"))
        self.assertEqual(inv["value"]["data"], {"msteams": {"type": "task/fetch"}, "action": "open_live_browser"})

    def test_attachments(self):
        att = e2e.file_attachment("https://h/files/a.txt", "a.txt")
        self.assertEqual(att["content"]["downloadUrl"], "https://h/files/a.txt")
        self.assertEqual(att["content"]["fileType"], "txt")
        self.assertEqual(e2e.html_part("q")["contentType"], "text/html")


class RepliesTest(unittest.TestCase):
    def card(self, text, actions=()):
        return {"type": "message", "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive",
                "content": {"type": "AdaptiveCard", "body": [{"type": "TextBlock", "text": text}],
                            "actions": list(actions)}}]}

    def test_edits_typing_and_other_conversations(self):
        conv, act_id = "a:conv", "act-1"
        base = f"/v3/conversations/{urllib.parse.quote(conv, safe='')}/activities"
        records = [
            record("POST", f"{base}/{act_id}", {"type": "typing", "replyToId": act_id}, "r0"),
            record("POST", f"{base}/{act_id}", dict(self.card("Thinking..."), replyToId=act_id), "r1"),
            record("PUT", f"{base}/r1", self.card("the answer is 391"), "r2"),
            record("POST", "/v3/conversations/other/activities/act-1", self.card("elsewhere"), "r3"),
        ]
        got = e2e.replies_to(records, conv, act_id)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["edits"], 1)
        self.assertTrue(e2e.is_answer(got[0]))
        self.assertIn("391", e2e.activity_text(got[0]["final"]))
        self.assertFalse(e2e.is_answer({"final": self.card("Thinking...")}))

    def test_proactive_messages_without_their_question(self):
        conv = "a:conv"
        base = f"/v3/conversations/{urllib.parse.quote(conv, safe='')}/activities"
        records = [record("POST", f"{base}/q1", dict(self.card("Scheduled: PLUM"), replyToId="q1"), "r1"),
                   record("POST", f"{base}/q1", dict(self.card("PLUM-HARBOR-31"), replyToId="q1"), "r2")]
        msgs = e2e.conversation_messages(records, conv, exclude_ids={"r1"})
        self.assertEqual([m["returned_id"] for m in msgs], ["r2"])

    def test_live_browser_button(self):
        action = {"type": "Action.Submit", "title": "Open live browser",
                  "data": {"msteams": {"type": "task/fetch"}, "action": "open_live_browser"}}
        self.assertTrue(e2e.has_live_browser_button(self.card("Your live browser is ready.", [action])))
        self.assertFalse(e2e.has_live_browser_button(self.card("x", [dict(action, title="Open")])))
        self.assertFalse(e2e.has_live_browser_button(self.card("x", [{"type": "Action.OpenUrl", "title": "Open live browser",
                                                                      "url": "https://h/vnc/?t=x"}])))
        self.assertIn("Open live browser", e2e.activity_text(self.card("y", [action])))

    def test_capture_records_are_parsed_from_log_lines(self):
        body = json.dumps({"type": "message", "text": "hi"})
        line = json.dumps({"capture": True, "method": "POST", "path": "/v3/x", "host": "h",
                           "auth_header_present": True, "returned_id": "r", "body": body})
        events = [(5, "START RequestId: abc"), (6, line), (7, "not json {"), (8, json.dumps({"other": 1}))]
        got = e2e.parse_capture(events)
        self.assertEqual(len(got), 1)
        self.assertEqual((got[0]["ts"], got[0]["activity"]["text"], got[0]["auth_header_present"]), (6, "hi", True))


class TokensTest(unittest.TestCase):
    KEY = b"fake-signing-key-made-in-this-test"

    def test_jwt_claims_of_the_bots_own_token(self):
        token = fake_jwt({"appid": APP_ID, "tid": TENANT, "aud": APP_ID}, self.KEY)
        claims = e2e.jwt_claims(token)
        self.assertEqual(claims["appid"], APP_ID)
        self.assertTrue(e2e.token_is_for_app(claims, APP_ID, TENANT))
        self.assertTrue(e2e.token_is_for_app({"azp": APP_ID, "tid": TENANT}, APP_ID, TENANT))
        self.assertFalse(e2e.token_is_for_app({"appid": "another-app", "tid": TENANT}, APP_ID, TENANT))
        self.assertFalse(e2e.token_is_for_app({"appid": APP_ID, "tid": "another-tenant"}, APP_ID, TENANT))
        self.assertEqual(e2e.jwt_claims("not-a-jwt"), {})
        self.assertEqual(e2e.jwt_claims("a.!!!.c"), {})

    def test_tampered_tokens_differ_where_it_counts(self):
        token = itsdangerous_like({"s": "x" * 22, "u": "29:e2e-x"}, self.KEY)
        tampered = e2e.tamper_token(token)
        self.assertNotEqual(tampered, token)
        self.assertEqual(tampered.rsplit(".", 1)[0], token.rsplit(".", 1)[0])
        sig, bad = token.rsplit(".", 1)[1], tampered.rsplit(".", 1)[1]
        self.assertEqual(len(sig), len(bad))
        self.assertNotEqual(sig[:-1], bad[:-1])  # not only the last character, whose low bits may not count
        self.assertTrue(re.fullmatch(r"[A-Za-z0-9_.-]+", tampered))
        self.assertEqual(e2e.tamper_token("AAAA"), "AABA")

    def test_states(self):
        signed = itsdangerous_like({"u": "29:e2e-x", "c": "msteams", "a": e2e.TEST_OID, "n": "n"}, self.KEY)
        self.assertTrue(e2e.state_is_signed(signed))
        plain = base64.urlsafe_b64encode(json.dumps({"u": "29:e2e-x"}).encode()).decode().rstrip("=")
        self.assertFalse(e2e.state_is_signed(plain))
        self.assertFalse(e2e.state_is_signed(""))
        self.assertFalse(e2e.state_is_signed("a.b"))
        # itsdangerous' compressed payloads start with '.'
        self.assertTrue(e2e.state_is_signed("." + signed))
        self.assertFalse(e2e.state_is_signed(".." + signed))
        forged = e2e.forged_state("29:e2e-x")
        self.assertTrue(e2e.state_is_signed(forged))
        self.assertNotEqual(forged, e2e.forged_state("29:e2e-x"))
        self.assertEqual(e2e.b64url_json(forged.split(".")[0])["u"], "29:e2e-x")

    def login_url(self, **overrides):
        params = {"client_id": APP_ID, "response_type": "code", "redirect_uri": "https://d.example/callback",
                  "response_mode": "query", "scope": " ".join(e2e.SCOPES),
                  "state": itsdangerous_like({"u": "29:e2e-x"}, self.KEY)}
        params.update(overrides)
        return f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/authorize?{urllib.parse.urlencode(params)}"

    def test_login_url(self):
        rows = e2e.analyse_login_url(self.login_url(), APP_ID, TENANT, "https://d.example/callback")
        self.assertEqual(len(rows), 6)
        self.assertTrue(all(ok for _, ok, _ in rows), rows)

    def test_login_url_defects(self):
        cases = {
            "client_id": self.login_url(client_id="another-app"),
            "redirect_uri": self.login_url(redirect_uri="https://other.example/callback"),
            "7 scopes": self.login_url(scope=" ".join(e2e.SCOPES[:-1])),
            "state": self.login_url(state=base64.urlsafe_b64encode(b'{"u": "x"}').decode()),
            "tenant": self.login_url().replace(TENANT, "common"),
            "response_type": self.login_url(response_mode="fragment"),
        }
        for broken, url in cases.items():
            with self.subTest(broken=broken):
                rows = e2e.analyse_login_url(url, APP_ID, TENANT, "https://d.example/callback")
                failed = [name for name, ok, _ in rows if not ok]
                self.assertEqual(len(failed), 1, rows)
        extra = self.login_url(scope=" ".join(e2e.SCOPES + ["Sites.Read.All"]))
        self.assertIn("login URL: exactly the 7 scopes",
                      [n for n, ok, _ in e2e.analyse_login_url(extra, APP_ID, TENANT, "https://d.example/callback")
                       if not ok])


class HygieneTest(unittest.TestCase):
    CLEAN = [
        '10.0.1.2 - - "GET /callback HTTP/1.1" 400 512',
        "GET /callback?code=[redacted]&state=[redacted] refused",
        "websockify path /websockify/abc?token=[redacted]",
        "viewer /vnc/?t=[redacted]",
        "Authorization: Bearer [redacted]",
        "AWS Marketplace: entitlement verified for NLSQL AI Employee (product x, region us-east-1).",
        "the state of the task is fine; t = 5",
        "Bedrock probe failed (AccessDeniedException)",
    ]

    def test_clean_lines(self):
        self.assertEqual(e2e.hygiene_problems(self.CLEAN), {})

    def test_each_leak(self):
        jwt = fake_jwt({"appid": APP_ID}, b"fake-key-for-this-test")
        secret = "fake-secret-" + "z" * 16
        cases = {
            "a code=, state=, token= or t= value": [
                "GET /callback?code=0.AXYZfakecode&state=abc", "proxy /websockify/s?token=abc.def.ghi",
                "open /vnc/?t=abc.def", "x&state=abc", "u;token=v"],
            "a /v2/credentials/ path with its id": [
                'botocore.exceptions.EndpointConnectionError: Could not connect to the endpoint URL: '
                '"http://169.254.170.2/v2/credentials/1a2b3c4d-0000-4000-8000-00000000abcd"'],
            "a /v2/credentials/ path (redacted)": [
                'Could not connect to the endpoint URL: "http://169.254.170.2/v2/credentials/[redacted]"'],
            "a JWT": [f"token {jwt}"],
            "a Bearer value": ["Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123"],
            "an AccessKeyId or access key": ['{"AccessKeyId": "x"}', "key " + "AKIA" + "A" * 16],
            "a secret value": [f"oops {secret} logged"],
        }
        for name, lines in cases.items():
            for line in lines:
                with self.subTest(line=line):
                    self.assertIn(name, e2e.hygiene_problems([line], [secret]))

    def test_short_values_are_not_searched(self):
        self.assertEqual(e2e.hygiene_problems(["true false"], ["true", ""]), {})

    def test_secret_values_from_the_env_file(self):
        env = {"connections__serviceConnection__settings__clientSecret": "c" * 20, "GEMINI_API_KEY": "g" * 20,
               "MicrosoftAppPassword": "p" * 20, "SOME_TOKEN": "t" * 20, "ENCRYPTION_KEY": "e" * 20,
               "MODEL_NAME": "global.anthropic.claude-sonnet-5-5", "DL_SECRET_X": "short"}
        self.assertEqual(e2e.secret_values(env, ["x" * 9]),
                         sorted(["c" * 20, "g" * 20, "p" * 20, "t" * 20, "e" * 20, "x" * 9]))


class ExactStringsTest(unittest.TestCase):
    def test_capability_sentences(self):
        self.assertEqual(e2e.capability_sentence(False),
                         "I can browse the web while you watch, read documents and images, and run tasks on a "
                         "schedule.")
        self.assertEqual(e2e.capability_sentence(False, browser=False),
                         "I can read documents and images, and run tasks on a schedule.")
        self.assertEqual(e2e.capability_sentence(True, True),
                         "I can read and send your email, manage your calendar, browse the web while you watch, "
                         "read documents and images, write code, and run tasks on a schedule.")

    def test_template_parts(self):
        self.assertEqual(e2e.template_parts("[agent] tool: <name>"), ["[agent] tool: "])
        self.assertEqual(e2e.template_parts("a <x> b <y>."), ["a ", " b ", "."])

    def test_error_lines_are_not_the_expected_ones(self):
        for line in e2e.ERROR_LINES + e2e.BOOT_ERROR_LINES:
            for expected in (e2e.STRINGS["entitlement_verified"], e2e.STRINGS["bedrock_probe_ok"],
                             e2e.STRINGS["coder_off"], e2e.STRINGS["gemini_unset"]):
                self.assertNotIn(line, expected)

    @unittest.skipUnless(os.environ.get("APP_REPO"), "set APP_REPO to a checkout of ai-employee")
    def test_strings_match_the_applications_table(self):
        doc = (Path(os.environ["APP_REPO"]) / "docs" / "aws-marketplace.md").read_text(encoding="utf-8")
        section = doc.split("## Exact strings", 1)[1].split("\n## ", 1)[0]
        cells = set()
        for line in section.splitlines():
            parts = [p.strip() for p in re.split(r"(?<!\\)\|", line)]
            if len(parts) >= 4 and parts[2].startswith("`") and parts[2].endswith("`"):
                cells.add(parts[2][1:-1])
        self.assertGreater(len(cells), 50)
        for key, value in e2e.STRINGS.items():
            with self.subTest(key=key):
                self.assertIn(value, cells)
        sentences = re.findall(r"^\| (yes|no) \| (yes|no) \| (yes|no) \| `([^`]+)` \|$", section, re.M)
        self.assertEqual(len(sentences), 8)
        for m365, browser, coder, sentence in sentences:
            self.assertEqual(e2e.capability_sentence(m365 == "yes", coder == "yes", browser == "yes"), sentence)

    @unittest.skipUnless(os.environ.get("APP_REPO"), "set APP_REPO to a checkout of ai-employee")
    def test_user_references_live_in_the_tasks_table(self):
        app = Path(os.environ["APP_REPO"])
        bot = (app / "bot" / "tables_storage.js").read_text(encoding="utf-8")
        self.assertIn('const TABLE_NAME = process.env.TASK_STORAGE_TABLE_NAME || "scheduledtasks";', bot)
        self.assertIn('PartitionKey: "UserReferences"', bot)
        sched = (app / "agent" / "tools" / "schedule_task.py").read_text(encoding="utf-8")
        self.assertIn('TABLE_NAME = os.getenv("TASK_STORAGE_TABLE_NAME", "scheduledtasks")', sched)
        self.assertIn('partition_key="UserReferences"', sched)
        template = (REPO / "cloudformation" / "nlsql-ai-employee-ecs-fargate.yaml").read_text(encoding="utf-8")
        self.assertRegex(template, r"\{Name: TASK_STORAGE_TABLE_NAME, +Value: !Ref TasksTable\}")

    @unittest.skipUnless(os.environ.get("APP_REPO"), "set APP_REPO to a checkout of ai-employee")
    def test_scopes_and_refusals_match_the_code(self):
        app = Path(os.environ["APP_REPO"])
        auth = (app / "helpers" / "auth_handler.py").read_text(encoding="utf-8")
        block = re.search(r"^SCOPES = \[(.*?)\]", auth, re.S | re.M).group(1)
        self.assertEqual(re.findall(r'"([^"]+)"', block), e2e.SCOPES)
        fetch = (app / "helpers" / "safe_fetch.py").read_text(encoding="utf-8")
        self.assertIn(f'FetchRejected("{e2e.REFUSED_HTTP}")', fetch)
        self.assertIn(e2e.REFUSED_PRIVATE + '")', fetch)
        bot = (app / "bot" / "live_browser.js").read_text(encoding="utf-8")
        self.assertIn("action: 'open_live_browser'", bot)
        self.assertIn("type: 'task/fetch'", bot)
        self.assertIn('"active": True, "path": f"websockify/{data[\'s\']}?token={token}"',
                      (app / "server.py").read_text(encoding="utf-8"))


class ImagesTest(unittest.TestCase):
    def test_words_use_known_glyphs(self):
        for word in e2e.IMAGE_WORDS.values():
            self.assertTrue(set(word) <= set(e2e.FONT), word)
        rows = e2e.render_word("FIG")
        self.assertEqual((len(rows), len(rows[0])), ((7 + 4) * 16, (3 * 6 - 1 + 4) * 16))
        self.assertFalse(rows[0][0])
        self.assertTrue(rows[2 * 16][2 * 16])  # F's top-left pixel

    def test_png(self):
        data = e2e.png_rgb(e2e.render_word("KIWI"))
        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))
        pos, chunks = 8, {}
        while pos < len(data):
            n = struct.unpack(">I", data[pos:pos + 4])[0]
            kind, body = data[pos + 4:pos + 8], data[pos + 8:pos + 8 + n]
            crc = struct.unpack(">I", data[pos + 8 + n:pos + 12 + n])[0]
            self.assertEqual(crc, zlib.crc32(kind + body) & 0xFFFFFFFF)
            chunks.setdefault(kind, b"")
            chunks[kind] += body
            pos += 12 + n
        width, height, depth, colour = struct.unpack(">IIBB", chunks[b"IHDR"][:10])
        self.assertEqual((depth, colour), (8, 2))
        raw = zlib.decompress(chunks[b"IDAT"])
        self.assertEqual(len(raw), height * (1 + 3 * width))
        row = raw[(2 * 16) * (1 + 3 * width):][:1 + 3 * width]
        self.assertEqual(row[1 + 3 * 2 * 16:4 + 3 * 2 * 16], bytes((20, 40, 160)))

    def test_tiff(self):
        rows = e2e.render_word("LIME")
        data = e2e.tiff_grey16(rows)
        self.assertEqual(data[:4], b"II*\x00")
        ifd = struct.unpack("<I", data[4:8])[0]
        count = struct.unpack("<H", data[ifd:ifd + 2])[0]
        tags = {}
        for i in range(count):
            tag, kind, _, value = struct.unpack("<HHII", data[ifd + 2 + 12 * i:ifd + 14 + 12 * i])
            tags[tag] = value & 0xFFFF if kind == 3 else value
        self.assertEqual((tags[256], tags[257]), (len(rows[0]), len(rows)))
        self.assertEqual((tags[258], tags[259], tags[262], tags[277]), (16, 1, 1, 1))
        pixels = data[tags[273]:tags[273] + tags[279]]
        self.assertEqual(len(pixels), 2 * len(rows) * len(rows[0]))
        self.assertEqual(struct.unpack("<H", pixels[:2])[0], 60000)

    def test_jpeg(self):
        data = e2e.jpeg_cmyk(e2e.render_word("FIG"))
        self.assertEqual((data[:2], data[-2:]), (b"\xff\xd8", b"\xff\xd9"))
        markers, pos = {}, 2
        while data[pos] == 0xFF and data[pos + 1] != 0xDA:
            n = struct.unpack(">H", data[pos + 2:pos + 4])[0]
            markers.setdefault(data[pos + 1], []).append(data[pos + 4:pos + 2 + n])
            pos += 2 + n
        self.assertEqual(markers[0xEE][0][:5], b"Adobe")
        self.assertEqual(markers[0xEE][0][-1], 0)       # transform 0: CMYK, not YCCK
        self.assertEqual(markers[0xC0][0][5], 4)        # four components
        self.assertEqual(len(markers[0xC4]), 2)
        with self.assertRaises(ValueError):
            e2e.jpeg_cmyk([[False] * 9] * 8)

    def test_huffman_codes_are_canonical(self):
        codes = e2e._huffman_codes(e2e.DC_BITS, e2e.DC_VALUES)
        self.assertEqual(codes[0], (0b00, 2))
        self.assertEqual(codes[1], (0b010, 3))
        self.assertEqual(codes[11], (0b111111110, 9))

    def test_images_decode_with_pillow(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow is not installed")
        modes = {"e2e-image.png": "RGB", "e2e-image.tif": "I;16", "e2e-image.jpg": "CMYK"}
        for name, data in e2e.test_images().items():
            with self.subTest(name=name):
                image = Image.open(io.BytesIO(data))
                image.load()
                self.assertEqual(image.mode, modes[name])
                rgb = image.convert("RGB") if image.mode != "I;16" else None
                if rgb:
                    self.assertEqual(rgb.getpixel((1, 1)), (255, 255, 255))
                    self.assertNotEqual(rgb.getpixel((2 * 16 + 1, 2 * 16 + 1)), (255, 255, 255))
                else:
                    self.assertEqual((image.getpixel((1, 1)), image.getpixel((33, 33))), (60000, 3000))


class CaptureFunctionTest(unittest.TestCase):
    def setUp(self):
        self.module = types.ModuleType("index")
        exec(compile(e2e.capture_code(), "index.py", "exec"), self.module.__dict__)
        self.context = types.SimpleNamespace(aws_request_id="0123456789abcdef")

    def call(self, method, path, headers=None, body=None, host="alt.lambda-url.us-east-1.on.aws"):
        event = {"rawPath": path, "headers": headers or {}, "body": body,
                 "requestContext": {"domainName": host, "http": {"method": method}}}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            resp = self.module.handler(event, self.context)
        return resp, [json.loads(line) for line in out.getvalue().splitlines()]

    def test_serves_the_files_and_logs_only_whether_auth_came(self):
        secret = "Bearer " + "fake" * 8
        resp, logged = self.call("GET", "/trafficmanager/smba.trafficmanager.net/files/" + e2e.LEAK_NAME,
                                 {"Authorization": secret})
        self.assertEqual(resp["statusCode"], 200)
        self.assertTrue(resp["isBase64Encoded"])
        self.assertIn("5843", base64.b64decode(resp["body"]).decode())
        self.assertEqual(logged[0]["auth_header_present"], True)
        self.assertEqual(logged[0]["host"], "alt.lambda-url.us-east-1.on.aws")
        self.assertNotIn("fake" * 8, json.dumps(logged))
        resp, logged = self.call("GET", "/files/e2e-image.jpg")
        self.assertEqual(base64.b64decode(resp["body"])[:2], b"\xff\xd8")
        self.assertEqual(resp["headers"]["Content-Type"], "image/jpeg")
        self.assertIs(logged[0]["auth_header_present"], False)
        resp, logged = self.call("GET", "/files/e2e-http.txt")
        self.assertEqual((resp["statusCode"], logged[0]["status"]), (404, 404))

    def test_answers_replies_like_the_bot_connector(self):
        resp, logged = self.call("POST", "/v3/conversations/a%3Ac/activities/x",
                                 {"authorization": "Bearer " + "fake" * 8}, json.dumps({"type": "message"}))
        rid = json.loads(resp["body"])["id"]
        self.assertTrue(rid.startswith("capture-"))
        self.assertEqual((logged[0]["returned_id"], json.loads(logged[0]["body"])), (rid, {"type": "message"}))
        self.assertNotIn("fake" * 8, json.dumps(logged))
        records = e2e.parse_capture([(1, "INFO " + json.dumps(logged[0]))])
        self.assertEqual(records[0]["activity"], {"type": "message"})


class FakeVncServer(threading.Thread):
    """The far end of a socket pair: an HTTP upgrade, then RFB 3.8 with
    pings, as nginx, websockify and x11vnc answer through CloudFront."""

    def __init__(self, sock, status=101, bad_accept=False, close_after=None):
        super().__init__(daemon=True)
        self.sock, self.status, self.bad_accept, self.close_after = sock, status, bad_accept, close_after
        self.client_data, self.pongs, self.request = b"", 0, b""

    def send(self, payload, opcode=0x2):
        self.sock.sendall(e2e.ws_frame(opcode, payload, mask=False))

    def run(self):
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += self.sock.recv(4096)
        self.request, buf = buf.split(b"\r\n\r\n", 1)
        key = re.search(rb"Sec-WebSocket-Key: (\S+)", self.request).group(1).decode()
        if self.status != 101:
            self.sock.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            return
        accept = "x" if self.bad_accept else e2e.ws_accept(key)
        self.sock.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                           f"Sec-WebSocket-Accept: {accept}\r\nSec-WebSocket-Protocol: binary\r\n\r\n").encode())
        if self.bad_accept:
            return
        self.send(b"ping-1", 0x9)
        self.send(b"RFB 003")
        self.send(b".008\n\x01\x01")   # greeting, then the security types: one, None
        ws = e2e.WebSocket(self.sock)
        ws.buf = buf

        def client(n):
            while len(self.client_data) < n:
                frame = ws._frame()
                if frame is None:
                    ws.buf += self.sock.recv(4096)
                    continue
                opcode, payload = frame
                if opcode == 0xA:
                    self.pongs += 1
                elif opcode == 0x2:
                    self.client_data += payload
                elif opcode == 0x8:
                    raise ConnectionError("closed")
            return self.client_data

        client(12)
        client(13)                                 # the chosen security type
        self.send(struct.pack(">I", 0))
        client(14)                                 # ClientInit
        name = b"fake display"
        self.send(struct.pack(">HH", 1280, 800) + bytes(16) + struct.pack(">I", len(name)) + name)
        for i in range(3):
            self.send(b"ping-%d" % i, 0x9)
        if self.close_after is not None:
            self.send(struct.pack(">H", 1006) + b"gone", 0x8)
            return
        try:
            client(10 ** 9)
        except (ConnectionError, OSError):
            pass


class WebSocketTest(unittest.TestCase):
    def test_accept_key_of_rfc_6455(self):
        self.assertEqual(e2e.ws_accept("dGhlIHNhbXBsZSBub25jZQ=="), "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")

    def test_frames(self):
        frame = e2e.ws_frame(0x2, b"abc", mask_key=b"\x01\x02\x03\x04")
        self.assertEqual(frame, bytes([0x82, 0x83, 1, 2, 3, 4]) + bytes(b ^ k for b, k in zip(b"abc", b"\x01\x02\x03")))
        self.assertEqual(e2e.ws_frame(0x2, b"x" * 200, mask=False)[:4], bytes([0x82, 126, 0, 200]))
        self.assertEqual(e2e.ws_frame(0x2, b"x" * 70000, mask=False)[:2], bytes([0x82, 127]))

    def pair(self, **kwargs):
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        server = FakeVncServer(b, **kwargs)
        server.start()
        return e2e.WebSocket(a), server

    def test_handshake_rfb_and_idle(self):
        ws, server = self.pair()
        status, headers = ws.handshake("d.example", "/websockify/" + "s" * 22 + "?token=t", "https://d.example")
        self.assertEqual((status, headers["sec-websocket-protocol"]), (101, "binary"))
        for needle in (b"GET /websockify/" + b"s" * 22 + b"?token=t HTTP/1.1", b"Upgrade: websocket",
                       b"Sec-WebSocket-Version: 13", b"Sec-WebSocket-Protocol: binary",
                       b"Origin: https://d.example", b"Host: d.example"):
            self.assertIn(needle, server.request)
        rfb = e2e.rfb_handshake(ws)
        self.assertEqual(rfb, {"version": "RFB 003.008", "width": 1280, "height": 800, "name": "fake display"})
        self.assertEqual(server.client_data, b"RFB 003.008\n\x01\x01")
        ticks = iter(range(0, 10 ** 6, 1))
        stayed, elapsed, reason = e2e.ws_idle(ws, 5, clock=lambda: next(ticks), poll=0.2)
        self.assertTrue(stayed, reason)
        self.assertGreaterEqual(ws.pings, 4)
        ws.close()
        server.join(5)
        self.assertGreaterEqual(server.pongs, 1)

    def test_a_close_during_the_idle_is_reported(self):
        ws, server = self.pair(close_after=0)
        ws.handshake("d.example", "/websockify/x", "https://d.example")
        e2e.rfb_handshake(ws)
        stayed, _, reason = e2e.ws_idle(ws, 3600, poll=1)
        self.assertFalse(stayed)
        self.assertIn("1006", reason)

    def test_403_and_a_wrong_accept(self):
        ws, _ = self.pair(status=403)
        self.assertEqual(ws.handshake("d.example", "/websockify/x", "https://d.example")[0], 403)
        ws, _ = self.pair(bad_accept=True)
        with self.assertRaises(ValueError):
            ws.handshake("d.example", "/websockify/x", "https://d.example")

    def test_rfb_refusals(self):
        class Feed:
            def __init__(self, data):
                self.data, self.sent = data, b""

            def recv_exact(self, n):
                out, self.data = self.data[:n], self.data[n:]
                return out

            def send(self, payload):
                self.sent += payload

        with self.assertRaises(ValueError):
            e2e.rfb_handshake(Feed(b"HTTP/1.1 200"))
        with self.assertRaises(ValueError):
            e2e.rfb_handshake(Feed(b"RFB 003.008\n\x01\x02"))          # VNC password only
        with self.assertRaises(ValueError):
            e2e.rfb_handshake(Feed(b"RFB 003.008\n\x00" + struct.pack(">I", 2) + b"no"))


class HealthTest(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(e2e.classify_health(200, b'{"status":"UP"}'), "UP")
        self.assertEqual(e2e.classify_health(503, b'{"status":"STARTING"}'), "STARTING")
        self.assertEqual(e2e.classify_health(503, b"<html>503 Service Temporarily Unavailable</html>"), "EDGE")
        self.assertEqual(e2e.classify_health(502, b"<html>Bad Gateway</html>"), "EDGE")
        self.assertEqual(e2e.classify_health(None, b"timeout"), "EDGE")
        self.assertEqual(e2e.classify_health(503, b'{"status": "waiting_for_entitlement"}'), "OTHER 503")
        self.assertEqual(e2e.classify_health(200, b'{"status":"ok"}'), "OTHER 200")

    def test_judge_restart(self):
        good = ["UP", "UP", "EDGE", "EDGE", "STARTING", "STARTING", "UP", "UP", "UP"]
        self.assertTrue(e2e.judge_restart(good)[0])
        # The load balancer's 5xx then UP is what an ungated task gives too:
        # it passes only with a health check of the new target that got 503
        ok, detail = e2e.judge_restart(["UP", "EDGE", "EDGE", "UP", "UP", "UP"])
        self.assertFalse(ok)
        self.assertIn("the gate was never seen", detail)
        self.assertFalse(e2e.judge_restart(["EDGE", "UP"])[0])
        self.assertTrue(e2e.judge_restart(["EDGE", "UP"], gate_checks=2)[0])
        self.assertFalse(e2e.judge_restart(["EDGE", "OTHER 200", "UP"], gate_checks=2)[0])
        for bad in (["UP", "UP"], ["UP", "EDGE", "STARTING"], ["EDGE", "OTHER 200", "UP"],
                    ["STARTING", "UP", "STARTING", "UP"], ["UP", "OTHER 503", "UP"]):
            with self.subTest(sequence=bad):
                self.assertFalse(e2e.judge_restart(bad)[0])


    def test_gate_503_checks(self):
        obs = [("10.0.1.5", "unhealthy", "Target.ResponseCodeMismatch", "Health checks failed with these codes: [503]"),
               ("10.0.1.5", "initial", "Elb.RegistrationInProgress", "Target registration is in progress"),
               ("10.0.1.9", "unhealthy", "Target.ResponseCodeMismatch", "Health checks failed with these codes: [503]"),
               ("10.0.1.5", "unhealthy", "Target.ResponseCodeMismatch", "Health checks failed with these codes: [502]"),
               ("10.0.1.5", "unhealthy", "Target.Timeout", "Request timed out"),
               ("10.0.1.5", "healthy", "", "")]
        # 10.0.1.9 is the old task: its 503s say nothing about the new one
        self.assertEqual(e2e.gate_503_checks(obs, {"10.0.1.9"}), 1)
        self.assertEqual(e2e.gate_503_checks(obs[1:], {"10.0.1.9"}), 0)

    def test_entitled_before_up(self):
        self.assertTrue(e2e.judge_entitled_before_up(1_000_000, 1_030_000)[0])
        self.assertTrue(e2e.judge_entitled_before_up(1_000_000, 997_000)[0])  # clock skew
        for entitled, up in ((1_000_000, 900_000), (None, 1_000_000), (1_000_000, None)):
            with self.subTest(entitled=entitled, up=up):
                self.assertFalse(e2e.judge_entitled_before_up(entitled, up)[0])

    def test_startup_since_is_the_running_tasks_creation(self):
        # A stack switched from Gemini off to on keeps the old task's
        # 'browser agent ... off' line in its log group: only the running
        # task's start-up counts
        utc = datetime.timezone.utc
        tasks = [{"createdAt": datetime.datetime(2026, 11, 4, 19, 3, 30, tzinfo=utc)},
                 {"createdAt": datetime.datetime(2026, 11, 4, 19, 1, 0, tzinfo=utc)}, {}]
        self.assertEqual(e2e.startup_since(tasks, 5),
                         int(datetime.datetime(2026, 11, 4, 19, 1, 0, tzinfo=utc).timestamp() * 1000))
        self.assertEqual(e2e.startup_since([], 5), 5)
        self.assertEqual(e2e.startup_since([{}], 5), 5)


class ReadImageTest(unittest.TestCase):
    SAVED_TIF = "Annotated image saved as image_0a1b2c3d.tif."

    def test_whole_words(self):
        for word, text in (("FIG", "FIG"), ("FIG", "The word is **FIG**."), ("LIME", "lime"),
                           ("KIWI", "KIWI\nAnnotated image saved as image_0a1b2c3d.png.")):
            with self.subTest(text=text):
                self.assertTrue(e2e.word_answered(word, text))
        for word, text in (("FIG", "I could not read the figure."), ("FIG", "check your configuration"),
                           ("LIME", "SUBLIME"), ("KIWI", "KIWIS"), ("FIG", "FIG2")):
            with self.subTest(text=text):
                self.assertFalse(e2e.word_answered(word, text))

    def rows(self, *args, **kwargs):
        return {name.split(": ", 1)[1]: ok for name, ok, _ in e2e.judge_image(*args, **kwargs)}

    def test_a_good_answer(self):
        rows = self.rows("e2e-image.tif", "LIME", ["LIME\n" + self.SAVED_TIF])
        self.assertTrue(all(rows.values()), rows)
        # quoted with the name in backticks, or found only in the snapshot
        self.assertTrue(all(self.rows("e2e-image.tif", "LIME",
                                      ["LIME\nAnnotated image saved as `image_0a1b2c3d.tif`."]).values()))
        self.assertTrue(all(self.rows("e2e-image.jpg", "FIG", ["FIG"], "",
                                      '{"content": "... Annotated image saved as image_99aa88bb.jpg."}').values()))
        # B13's fallback: PNG when Pillow cannot write the image's format
        self.assertTrue(all(self.rows("e2e-image.jpg", "FIG",
                                      ["FIG\nAnnotated image saved as image_0a1b2c3d.png."]).values()))

    def test_failures(self):
        cases = {
            "a figure is not FIG": (["I could not read the figure. " + self.SAVED_TIF.replace("tif", "jpg")],
                                    "answers FIG"),
            "configuration is not FIG": (["check your configuration"], "answers FIG"),
            "no save sentence": (["FIG"], "the annotated image was saved as image_<8 hex>.jpg/png"),
            "another format": (["FIG\n" + self.SAVED_TIF], "the annotated image was saved as image_<8 hex>.jpg/png"),
            "could not be saved": (["FIG\n" + e2e.STRINGS["annotated_not_saved"]],
                                   "no '" + e2e.STRINGS["annotated_not_saved"] + "'"),
        }
        for case, (texts, failing) in cases.items():
            with self.subTest(case=case):
                rows = self.rows("e2e-image.jpg", "FIG", texts)
                self.assertFalse(rows[failing], rows)

    def test_the_snapshot_counts_only_what_it_gained(self):
        before = '{"m": "Annotated image saved as image_11112222.jpg."}'
        rows = self.rows("e2e-image.jpg", "FIG", ["FIG"], before, before)
        self.assertFalse(rows["the annotated image was saved as image_<8 hex>.jpg/png"])
        after = before + '{"m": "' + e2e.STRINGS["annotated_not_saved"] + '"}'
        rows = self.rows("e2e-image.jpg", "FIG", ["FIG"], before, after)
        self.assertFalse(rows["no '" + e2e.STRINGS["annotated_not_saved"] + "'"])

    def test_the_prompt_asks_for_the_word_and_the_tools_sentence(self):
        self.assertIn("read_image", e2e.IMAGE_PROMPT)
        self.assertIn("last sentence word for word", e2e.IMAGE_PROMPT)

    def test_summaries_mask_secret_shapes(self):
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4eHh4eHgifQ.c2lnbmF0dXJlc2ln"
        key = "AK" + "IA" + "ABCDEFGHIJKLMNOP"  # built here, so no-secrets sees no key-shaped literal
        text = (f"Bearer {jwt} key {key} and https://d.example/websockify/x?token=abcdef123 "
                '{"SecretAccessKey": "fakeSecretValue", "Token": "fakeSessionToken"}')
        out = e2e.summary([text])
        for leak in (jwt, key, "abcdef123", "fakeSecretValue", "fakeSessionToken"):
            self.assertNotIn(leak, out)
        self.assertEqual(e2e.summary(["The word is FIG."]), "The word is FIG.")

    def test_summaries_mask_the_runs_secret_values(self):
        value = "fake-" + "q" * 20  # no shape regex knows it
        self.addCleanup(e2e.MASKED_VALUES.discard, value)
        e2e.mask_values([value, "short"])
        self.assertNotIn("short", e2e.MASKED_VALUES)
        self.assertEqual(e2e.summary([f"the key is {value}."]), "the key is [masked].")


@unittest.skipUnless(botocore, "botocore is not installed")
class AwsCallsTest(unittest.TestCase):
    """The boto3 calls, against botocore's Stubber: nothing is sent."""

    def client(self, name):
        client = botocore.session.get_session().create_client(
            name, region_name="us-east-1", endpoint_url="http://127.0.0.1:9",
            aws_access_key_id="testing", aws_secret_access_key="testing")
        stubber = Stubber(client)
        stubber.activate()
        self.addCleanup(stubber.deactivate)
        return client, stubber

    def session(self, **clients):
        return types.SimpleNamespace(client=lambda name: clients[name])

    def test_teardown_deletes_only_tagged_resources(self):
        lam, ls = self.client("lambda")
        iam, iss = self.client("iam")
        logs, lgs = self.client("logs")
        arn = f"arn:aws:lambda:us-east-1:{ACCOUNT}:function:st-capture"
        ls.add_response("get_function", {"Configuration": {"FunctionArn": arn}}, {"FunctionName": "st-capture"})
        ls.add_response("list_tags", {"Tags": {"nlsql:purpose": "test"}}, {"Resource": arn})
        ls.add_response("delete_function", {}, {"FunctionName": "st-capture"})
        lgs.add_response("delete_log_group", {}, {"logGroupName": "/aws/lambda/st-capture"})
        iss.add_response("list_role_tags", {"Tags": [{"Key": "nlsql:purpose", "Value": "test"}]},
                         {"RoleName": "st-capture-role"})
        policy = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
        iss.add_response("list_attached_role_policies", {"AttachedPolicies": [{"PolicyArn": policy}]},
                         {"RoleName": "st-capture-role"})
        iss.add_response("detach_role_policy", {}, {"RoleName": "st-capture-role", "PolicyArn": policy})
        iss.add_response("delete_role", {}, {"RoleName": "st-capture-role"})
        with contextlib.redirect_stdout(io.StringIO()):
            e2e.teardown(self.session(**{"lambda": lam, "iam": iam, "logs": logs}), "st")
        for stub in (ls, iss, lgs):
            stub.assert_no_pending_responses()

    def test_teardown_refuses_an_untagged_function(self):
        lam, ls = self.client("lambda")
        iam, _ = self.client("iam")
        logs, _ = self.client("logs")
        arn = f"arn:aws:lambda:us-east-1:{ACCOUNT}:function:st-capture"
        ls.add_response("get_function", {"Configuration": {"FunctionArn": arn}}, {"FunctionName": "st-capture"})
        ls.add_response("list_tags", {"Tags": {}}, {"Resource": arn})
        with self.assertRaises(SystemExit) as cm:
            e2e.teardown(self.session(**{"lambda": lam, "iam": iam, "logs": logs}), "st")
        self.assertIn("not tagged nlsql:purpose=test", str(cm.exception.code))
        ls.assert_no_pending_responses()

    def test_teardown_with_nothing_left(self):
        lam, ls = self.client("lambda")
        iam, iss = self.client("iam")
        logs, _ = self.client("logs")
        ls.add_client_error("get_function", "ResourceNotFoundException", http_status_code=404)
        iss.add_client_error("list_role_tags", "NoSuchEntity", http_status_code=404)
        with contextlib.redirect_stdout(io.StringIO()):
            e2e.teardown(self.session(**{"lambda": lam, "iam": iam, "logs": logs}), "st")
        ls.assert_no_pending_responses()
        iss.assert_no_pending_responses()

    def test_log_events_follow_next_token(self):
        logs, stub = self.client("logs")
        stub.add_response("filter_log_events", {"events": [{"timestamp": 1, "message": "a"}], "nextToken": "n"},
                          {"logGroupName": "/ecs/st", "startTime": 5, "filterPattern": '"capture"'})
        stub.add_response("filter_log_events", {"events": [{"timestamp": 2, "message": "b"}]},
                          {"logGroupName": "/ecs/st", "startTime": 5, "filterPattern": '"capture"', "nextToken": "n"})
        got = e2e.log_events(self.session(logs=logs), "/ecs/st", 5, '"capture"')
        self.assertEqual(got, [(1, "a"), (2, "b")])
        stub.assert_no_pending_responses()

    def test_log_events_of_a_missing_group(self):
        logs, stub = self.client("logs")
        stub.add_client_error("filter_log_events", "ResourceNotFoundException", http_status_code=400)
        self.assertEqual(e2e.log_events(self.session(logs=logs), "/aws/lambda/none", 5), [])

    def test_active_tasks_and_references(self):
        ddb, stub = self.client("dynamodb")
        run = e2e.Run.__new__(e2e.Run)
        run.session = self.session(dynamodb=ddb)
        run.outputs = {"TasksTableName": "fake-tasks", "UsersTableName": "fake-users"}
        run.user = "29:e2e-x"
        stub.add_response("query", {"Items": [{"RowKey": {"S": "r1"}}]}, {
            "TableName": "fake-tasks", "ConsistentRead": True, "KeyConditionExpression": "PartitionKey = :p",
            "FilterExpression": "UserID = :u",
            "ExpressionAttributeValues": {":p": {"S": "ActiveTasks"}, ":u": {"S": "29:e2e-x"}}})
        # The bot keeps UserReferences in the tasks table, not the users table
        stub.add_response("get_item", {"Item": {"ConversationReferenceJson": {"S": '{"channelId": "msteams"}'}}}, {
            "TableName": "fake-tasks", "ConsistentRead": True,
            "Key": {"PartitionKey": {"S": "UserReferences"}, "RowKey": {"S": "directline:29:e2e-x"}}})
        stub.add_response("get_item", {}, {
            "TableName": "fake-tasks", "ConsistentRead": True,
            "Key": {"PartitionKey": {"S": "UserReferences"}, "RowKey": {"S": "29:e2e-x"}}})
        self.assertEqual(run.active_tasks(), [{"RowKey": {"S": "r1"}}])
        self.assertEqual(run.reference("directline:29:e2e-x")["channelId"], "msteams")
        self.assertEqual(run.reference(run.user), {})
        stub.assert_no_pending_responses()

    FN_ARN = f"arn:aws:lambda:us-east-1:{ACCOUNT}:function:st-capture"
    ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/st-capture-role"

    def existing_role(self, iss, tags):
        iss.add_response("get_role", {"Role": {
            "Path": "/", "RoleName": "st-capture-role", "RoleId": "AROAFAKEFAKEFAKEFAKE1", "Arn": self.ROLE_ARN,
            "CreateDate": "2026-01-01T00:00:00Z"}}, {"RoleName": "st-capture-role"})
        iss.add_response("list_role_tags", {"Tags": [{"Key": k, "Value": v} for k, v in tags.items()]},
                         {"RoleName": "st-capture-role"})

    def existing_function(self, ls, tags):
        ls.add_response("get_function", {"Configuration": {"FunctionArn": self.FN_ARN}}, {"FunctionName": "st-capture"})
        ls.add_response("list_tags", {"Tags": tags}, {"Resource": self.FN_ARN})

    def test_ensure_capture_refuses_an_untagged_role(self):
        lam, ls = self.client("lambda")
        iam, iss = self.client("iam")
        self.existing_role(iss, {"owner": "someone"})
        with self.assertRaises(SystemExit) as cm:
            e2e.ensure_capture(self.session(**{"lambda": lam, "iam": iam}), "st")
        self.assertEqual(str(cm.exception.code),
                         "refusing to change st-capture-role: it is not tagged nlsql:purpose=test")
        # Nothing else was asked: a call not stubbed here would raise
        iss.assert_no_pending_responses()
        ls.assert_no_pending_responses()

    def test_ensure_capture_refuses_an_untagged_function(self):
        lam, ls = self.client("lambda")
        iam, iss = self.client("iam")
        self.existing_role(iss, {"nlsql:purpose": "test"})
        self.existing_function(ls, {"nlsql:purpose": "prod"})
        with self.assertRaises(SystemExit) as cm:
            e2e.ensure_capture(self.session(**{"lambda": lam, "iam": iam}), "st")
        self.assertEqual(str(cm.exception.code),
                         "refusing to change st-capture: it is not tagged nlsql:purpose=test")
        # no update_function_code, alias, create_function_url_config or add_permission
        ls.assert_no_pending_responses()
        iss.assert_no_pending_responses()

    def test_ensure_capture_refuses_an_untagged_function_before_making_a_role(self):
        lam, ls = self.client("lambda")
        iam, iss = self.client("iam")
        iss.add_client_error("get_role", "NoSuchEntity", http_status_code=404)
        self.existing_function(ls, {})
        with self.assertRaises(SystemExit):
            e2e.ensure_capture(self.session(**{"lambda": lam, "iam": iam}), "st")
        ls.assert_no_pending_responses()
        iss.assert_no_pending_responses()  # no create_role

    def test_ensure_capture_updates_a_tagged_function(self):
        lam, ls = self.client("lambda")
        iam, iss = self.client("iam")
        self.existing_role(iss, {"nlsql:purpose": "test"})
        self.existing_function(ls, {"nlsql:purpose": "test"})
        ls.add_response("update_function_code", {"FunctionArn": self.FN_ARN},
                        {"FunctionName": "st-capture", "ZipFile": ANY})
        ls.add_response("get_function", {"Configuration": {"State": "Active"}}, {"FunctionName": "st-capture"})
        ls.add_response("get_function", {"Configuration": {"LastUpdateStatus": "Successful"}},
                        {"FunctionName": "st-capture"})
        ls.add_response("publish_version", {"Version": "7"}, {"FunctionName": "st-capture"})
        ls.add_response("update_alias", {}, {"FunctionName": "st-capture", "Name": e2e.CAPTURE_ALIAS,
                                             "FunctionVersion": "7"})
        hosts = {None: "https://fake-a.lambda-url.us-east-1.on.aws/",
                 e2e.CAPTURE_ALIAS: "https://fake-b.lambda-url.us-east-1.on.aws/"}
        for qualifier, url in hosts.items():
            q = {"Qualifier": qualifier} if qualifier else {}
            ls.add_response("get_function_url_config", {
                "FunctionUrl": url, "FunctionArn": self.FN_ARN, "AuthType": "NONE",
                "CreationTime": "2026-01-01T00:00:00Z", "LastModifiedTime": "2026-01-01T00:00:00Z"},
                {"FunctionName": "st-capture", **q})
            ls.add_response("add_permission", {"Statement": "{}"}, {
                "FunctionName": "st-capture", "StatementId": "public-url", "Principal": "*",
                "Action": "lambda:InvokeFunctionUrl", "FunctionUrlAuthType": "NONE", **q})
            ls.add_response("add_permission", {"Statement": "{}"}, {
                "FunctionName": "st-capture", "StatementId": "public-url-invoke", "Principal": "*",
                "Action": "lambda:InvokeFunction", "InvokedViaFunctionUrl": True, **q})
        with contextlib.redirect_stdout(io.StringIO()):
            got = e2e.ensure_capture(self.session(**{"lambda": lam, "iam": iam}), "st")
        self.assertEqual(got, (hosts[None], hosts[e2e.CAPTURE_ALIAS], "/aws/lambda/st-capture"))
        ls.assert_no_pending_responses()
        iss.assert_no_pending_responses()

    def test_main_does_not_tear_down_after_a_refusal(self):
        args = e2e.parse_args(["local.env"])
        fake = "fake-" + "x" * 20  # built here, so no-secrets sees no secret-shaped literal
        with unittest.mock.patch.object(e2e, "parse_args", return_value=args), \
                unittest.mock.patch.object(e2e, "boto3", types.SimpleNamespace(Session=lambda **_: None)), \
                unittest.mock.patch.object(e2e, "read_env", return_value={
                    "connections__serviceConnection__settings__clientId": APP_ID,
                    "connections__serviceConnection__settings__clientSecret": fake,
                    "connections__serviceConnection__settings__tenantId": TENANT}), \
                unittest.mock.patch.object(e2e, "run_checks",
                                           side_effect=e2e.NotTestTagged("refusing to change st-capture")), \
                unittest.mock.patch.object(e2e, "teardown") as teardown:
            with self.assertRaises(SystemExit) as cm:
                e2e.main([])
        self.assertEqual(str(cm.exception.code), "refusing to change st-capture")
        teardown.assert_not_called()


@unittest.skipUnless(shutil.which("make"), "make is not installed")
class MakeTargetsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = tmp.name

    def make(self, *args):
        return subprocess.run(["make", "--no-print-directory", "-n", "-C", str(REPO), *args],
                              capture_output=True, text=True, timeout=60,
                              env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": self.home})

    def test_e2e_test_passes_its_arguments(self):
        result = self.make("e2e-test", "ENV_FILE=local.env", "E2E_ARGS=--skip-schedule --ws-idle 120")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('python3 scripts/e2e-test.py "local.env" --stack "nlsql-ai-employee-test" --region us-east-1 '
                      "--skip-schedule --ws-idle 120", result.stdout)

    def test_a_login_url_reaches_the_script_whole_and_is_not_echoed(self):
        state = "fakepayload.fakestamp." + "s" * 40
        url = ("https://login.microsoftonline.com/t/oauth2/v2.0/authorize?client_id=a&response_type=code"
               f"&scope=openid%20User.Read&state={state}")
        echoed = self.make("e2e-test", "ENV_FILE=local.env", f"LOGIN_URL={url}")
        self.assertEqual(echoed.returncode, 0, echoed.stderr)
        self.assertNotIn(state, echoed.stdout + echoed.stderr)
        # Run for real with a python3 on PATH that only records what it got
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "got.json"
            fake = Path(tmp) / "python3"
            fake.write_text(f"#!{sys.executable}\nimport json, os, sys\n"
                            f"json.dump({{'argv': sys.argv[1:], 'login_url': os.environ.get('LOGIN_URL')}}, "
                            f"open({str(out)!r}, 'w'))\n")
            fake.chmod(0o755)
            result = subprocess.run(["make", "--no-print-directory", "-C", str(REPO), "e2e-test",
                                     "ENV_FILE=local.env", f"LOGIN_URL={url}"],
                                    capture_output=True, text=True, timeout=60,
                                    env={"PATH": f"{tmp}:{os.environ.get('PATH', '/usr/bin:/bin')}",
                                         "HOME": self.home})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn(state, result.stdout + result.stderr)
            got = json.loads(out.read_text())
        self.assertEqual(got["argv"][0], "scripts/e2e-test.py")
        with unittest.mock.patch.dict(os.environ, {"LOGIN_URL": got["login_url"]}, clear=True):
            self.assertEqual(e2e.parse_args(got["argv"][1:]).login_url, url)

    def test_e2e_test_needs_an_env_file(self):
        self.assertIn("test -n \"\"", self.make("e2e-test").stdout)

    def test_destroy_test_removes_the_capture_function_first(self):
        out = self.make("destroy-test").stdout.splitlines()
        teardown = [i for i, line in enumerate(out) if "scripts/e2e-test.py --teardown" in line]
        destroy = [i for i, line in enumerate(out) if "scripts/destroy-test.sh" in line]
        self.assertTrue(teardown and destroy and teardown[0] < destroy[0], out)


if __name__ == "__main__":
    unittest.main()
