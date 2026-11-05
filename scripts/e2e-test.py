#!/usr/bin/env python3
"""End-to-end test of a seller-account test stack (scripts/deploy-test.sh). Not for buyers.

Usage:
    scripts/e2e-test.py <env-file> [--stack nlsql-ai-employee-test] [--region us-east-1]
                        [--skip-schedule] [--restart-probe] [--ws-idle SECONDS]
                        [--login-url URL] [--keep-capture] [--gemini-fallback]
    scripts/e2e-test.py --teardown [--stack ...]      remove the capture function and its role

--stack defaults to $STACK_NAME, as in deploy-test.sh, else nlsql-ai-employee-test.
--login-url defaults to $LOGIN_URL (make e2e-test LOGIN_URL='<url>'), which
keeps the link's signed state out of the command line and make's echo.
The modes are read from the stack: Gemini on or off from its GeminiFeatures
parameter, and the browser agent from its BrowserAgent parameter, which the
BrowserViewUrl output must agree with. With Gemini on, --gemini-fallback says
the stack's Gemini key fails (no credits, say): the browser agent and
read_image must then go on with Bedrock and log why.

It drives the deployed bot the way Microsoft Teams does, without Teams:

1. A temporary AWS Lambda function URL stands in for the Bot Connector. Its URL
   is sent as the activities' serviceUrl, so the bot's replies (which the SDK
   POSTs to <serviceUrl>/v3/conversations/...) land in its CloudWatch log. It
   also serves the attachments (a text file with a secret number, and three
   images with a word in them). A second function URL, on an
   alias, gives a second host for the token-leak test. It logs whether a
   request carried an Authorization header, never the header itself.
2. An Entra ID client-credentials token for the bot's own app ID (from the env
   file) authorises each activity: besides the Bot Connector's own tokens, the
   bot accepts only tokens issued to its own app ID.
3. Teams-shaped activities (and one Direct Line activity) are POSTed to the
   stack's MessagingEndpoint through CloudFront, and the captured replies, the
   stack's tables and buckets and the task's log group are checked.

The serviceUrl is not a Teams host, so the bot treats the test user as a Teams
user without Microsoft 365 (that needs a real Teams turn): the welcome and
/help must not offer sign-in, and `login` must answer with the Teams-only
sentence. --login-url checks the sign-in link copied from a real Teams login
card instead (tenant, client_id, redirect_uri, the 7 scopes, a signed state).

Checks: /health (and with --restart-probe the 503 sequence while the task is
replaced); the edge (the load balancer refuses requests that bypass
CloudFront; /api/prompt, /proactive, /validate-vnc and every other path get
the ALB's 403, also through encoded traversal; /callback refuses a forged
state); every activity is acknowledged within ACK_LIMIT seconds; the welcome
and /help match the stack's features and the channel; exactly one answer per
question; the storage round trip (DynamoDB reference, S3 snapshot, refresh);
login; attachments (the secret number, no token to another host, http and
link-local URLs refused); /schedule; identity (a Direct Line sender reusing
the Teams user's ID gets a separate, empty conversation, and the Teams user's
proactive delivery still reaches Teams); with BrowserAgent Disabled, no
browser tool, no browsing in the welcome and no live-view route; otherwise the
live browser, on Gemini or on Bedrock, through the Teams dialog (task/fetch), the session lookup, a WSS handshake and RFB
through CloudFront kept idle for --ws-idle seconds (default 900, S4), 403 for
a missing, tampered or stale token, close-session, and read_image on an RGB
PNG, a 16-bit greyscale TIFF and a CMYK JPEG; Gemini's fallback lines, which
only --gemini-fallback expects; and log hygiene since the stack
was created (no secret value, no code=, state=, token= or t= value, no
/v2/credentials/ path, no JWT; the expected start-up lines). Secret values
are never printed. The capture function is removed at the end, also when the
run fails, unless --keep-capture is given.
"""

import argparse
import base64
import hashlib
import io
import json
import os
import re
import secrets
import socket
import ssl
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
import zlib

try:  # only the AWS calls need boto3: --help and the unit tests do not
    import boto3
    from botocore.exceptions import ClientError
except ImportError:  # pragma: no cover - depends on the machine
    boto3 = None
    ClientError = Exception

ACK_LIMIT = 8.0            # seconds for /api/messages to answer 200/202
REPLY_TIMEOUT = 300        # seconds to wait for a proactive answer
HEALTH_TIMEOUT = 900       # seconds for /health to answer UP
WS_IDLE_DEFAULT = 900      # seconds the live view's WebSocket stays idle (S4: at least 15 minutes)
KEEPALIVE_INTERVAL = 600   # under the image's BROWSER_USER_REPLY_TIMEOUT (900 s)
TEST_TAG = {"nlsql:purpose": "test"}
PRODUCT_CODE = "f5jcvx1rl6wb66fhzy5qycpnr"
TEST_OID = "00000000-0000-0000-0000-00000000e2e0"
CAPTURE_ALIAS = "alt"

# The application's exact strings (docs/aws-marketplace.md on the app branch,
# 'Exact strings'; scripts/test_e2e_test.py compares them with that table).
# <name> marks a placeholder, as in the table.
STRINGS = {
    "welcome": "**Welcome! I'm NLSQL AI Employee.**",
    "storage_unavailable": "Sorry, I can't reach my storage right now. Please try again in a minute.",
    "teams_only_sign_in": "Microsoft 365 sign-in is available in Microsoft Teams only.",
    "sign_in_link_invalid": ("This sign-in link has expired or is not valid. "
                             "Go back to the chat and type login to get a new one."),
    "refreshed": "Chat history has been refreshed.",
    "live_browser_ready": "Your live browser is ready.",
    "open_live_browser": "Open live browser",
    "live_browser_title": "Live browser",
    "no_live_browser": "There is no live browser session right now.",
    "help_request_teams": "Select **Open live browser** below to take over the browser.",
    "starting": '{"status":"STARTING"}',
    "up": '{"status":"UP"}',
    "attachment_refused": "Attachment download refused: <reason>",
    "coder_off": "Features: code mode is off (no CODER_ENDPOINT).",
    "browser_off": "Features: the browser agent is off (BROWSER_AGENT_ENABLED).",
    "gemini_unset": ("Features: no GEMINI_API_KEY or GEMINI_MODEL; the browser agent and image recognition use the "
                     "main model."),
    "gemini_browser_failed": "Browser agent: Gemini failed (<error>); the main model (<model>) goes on with the task.",
    "gemini_image_failed": "Image recognition: Gemini failed (<error>); the main model reads the image.",
    "entitlement_verified": ("AWS Marketplace: entitlement verified for NLSQL AI Employee "
                             "(product <code>, region <region>)."),
    "bedrock_probe_ok": "NLSQL AI Employee: Bedrock probe for <model> succeeded.",
    "tool_marker": "[agent] tool: <name>",
    "annotated_saved": "Annotated image saved as <name>.",
    "annotated_not_saved": "The annotated image could not be saved.",
    "prompt_rejected": "/api/prompt rejected a request without the internal secret",
    "caller_rejected": "[auth] rejected a request from an unexpected caller (iss=<iss>, appid=<appid>)",
    "redacted": "[redacted]",
}
# helpers/safe_fetch.py's reasons, after STRINGS["attachment_refused"]'s prefix
REFUSED_HTTP = "only https URLs are allowed"
REFUSED_PRIVATE = "resolves to a non-public address"
# The Microsoft 365 sign-in's scopes (helpers/auth_handler.py SCOPES)
SCOPES = ["openid", "User.Read", "Mail.ReadWrite", "Mail.Send", "offline_access",
          "Files.ReadWrite", "Calendars.ReadWrite"]
# Lines that must not appear since the test began: each means a broken role,
# table or bucket, or a caller the image refused.
ERROR_LINES = ["AccessDenied", "Failed to initialize DynamoDB table", "Failed to upsert reference",
               "Failed to create table", "Failed to persist conversation",
               "Failed to save conversation snapshot", "/api/prompt rejected a request",
               "[auth] rejected a request from an unexpected caller"]
# Start-up lines that must not appear since the stack was created
BOOT_ERROR_LINES = ["Cannot describe DynamoDB table", "Cannot check S3 bucket", "Cannot create DynamoDB table",
                    "cannot start:"]

ATTACHMENT_NAME = "e2e-attachment.txt"
ATTACHMENT_TEXT = ("NLSQL AI Employee end-to-end test attachment.\n"
                   "The secret number in this attached file is 7319.\n")
LEAK_NAME = "e2e-attachment-2.txt"
LEAK_TEXT = ("NLSQL AI Employee end-to-end test attachment from a second host.\n"
             "The secret number in this attached file is 5843.\n")
SCHEDULE_WORD = "PLUM-HARBOR-31"
# The word in each image, one per format, so each answer proves its own image was read
IMAGE_WORDS = {"e2e-image.png": "KIWI", "e2e-image.tif": "LIME", "e2e-image.jpg": "FIG"}
IMAGE_TYPES = {"e2e-image.png": "image/png", "e2e-image.tif": "image/tiff", "e2e-image.jpg": "image/jpeg"}
IMAGE_PROMPT = ("Use the read_image tool on the attached image. What single word is written in it? Draw a box "
                "around the word. Answer with the word alone in capital letters on the first line, then quote "
                "the read_image tool's last sentence word for word on the second line.")
BROWSE_PROMPT = ("Use the browser agent for this task: open https://example.com, then use ask_user_for_help "
                 "to ask me exactly this question: 'E2E checkpoint: reply done when you have finished "
                 "watching.' If my reply is anything other than done, ask the same question again. "
                 "Do nothing else.")
KEEPALIVE_REPLY = "not yet"


def log(msg):
    print(msg, flush=True)


def capability_sentence(microsoft_365, coder=False, browser=True):
    """What the welcome and /help say the user can ask for (bot/features.js),
    with Gemini or without it."""
    things = ["read and send your email", "manage your calendar"] if microsoft_365 else []
    if browser:
        things.append("browse the web while you watch")
    things.append("read documents and images")
    if coder:
        things.append("write code")
    things.append("run tasks on a schedule")
    return f"I can {', '.join(things[:-1])}, and {things[-1]}."


def template_parts(template):
    """The fixed pieces of an exact string around its <placeholders>."""
    return [p for p in re.split(r"<[A-Za-z]+>", template) if p]


# --- test images (stdlib only) -----------------------------------------------

# 5x7 glyphs of the letters the image words use
FONT = {
    "K": ["10001", "10010", "10100", "11000", "10100", "10010", "10001"],
    "I": ["01110", "00100", "00100", "00100", "00100", "00100", "01110"],
    "W": ["10001", "10001", "10001", "10101", "10101", "10101", "01010"],
    "L": ["10000", "10000", "10000", "10000", "10000", "10000", "11111"],
    "M": ["10001", "11011", "10101", "10101", "10001", "10001", "10001"],
    "E": ["11111", "10000", "10000", "11110", "10000", "10000", "11111"],
    "F": ["11111", "10000", "10000", "11110", "10000", "10000", "10000"],
    "G": ["01110", "10001", "10000", "10111", "10001", "10001", "01111"],
}


def render_word(word, scale=16, margin=2):
    """The word as rows of booleans (True = ink): each glyph pixel is a
    scale x scale square, so with a scale that is a multiple of 8 every JPEG
    block is one colour."""
    cols = []
    for i, letter in enumerate(word):
        if i:
            cols.append("0" * 7)
        glyph = FONT[letter]
        cols += ["".join(glyph[r][c] for r in range(7)) for c in range(5)]
    grid_w, grid_h = len(cols) + 2 * margin, 7 + 2 * margin
    rows = []
    for gy in range(grid_h):
        line = []
        for gx in range(grid_w):
            c, r = gx - margin, gy - margin
            line.append(0 <= c < len(cols) and 0 <= r < 7 and cols[c][r] == "1")
        rows.extend([[px for px in line for _ in range(scale)]] * scale)
    return rows


def png_rgb(rows, ink=(20, 40, 160), paper=(255, 255, 255)):
    """An 8-bit RGB PNG."""
    height, width = len(rows), len(rows[0])
    raw = b"".join(b"\x00" + b"".join(bytes(ink if px else paper) for px in row) for row in rows)

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def tiff_grey16(rows, ink=3000, paper=60000):
    """An uncompressed 16-bit greyscale (BlackIsZero) little-endian TIFF, which
    Pillow opens in mode I;16."""
    height, width = len(rows), len(rows[0])
    pixels = b"".join(struct.pack(f"<{width}H", *(ink if px else paper for px in row)) for row in rows)
    entries = [(256, 4, width), (257, 4, height), (258, 3, 16), (259, 3, 1), (262, 3, 1),
               (273, 4, None), (277, 3, 1), (278, 4, height), (279, 4, len(pixels)), (284, 3, 1)]
    ifd_size = 2 + 12 * len(entries) + 4
    data_offset = 8 + ifd_size
    ifd = struct.pack("<H", len(entries))
    for tag, kind, value in entries:
        value = data_offset if value is None else value
        if kind == 3:
            ifd += struct.pack("<HHIHH", tag, kind, 1, value, 0)
        else:
            ifd += struct.pack("<HHII", tag, kind, 1, value)
    ifd += struct.pack("<I", 0)
    return b"II" + struct.pack("<HI", 42, 8) + ifd + pixels


def _huffman_codes(bits, values):
    """{symbol: (code, length)} of a JPEG Huffman table (BITS and HUFFVAL)."""
    codes, code, k = {}, 0, 0
    for length in range(1, 17):
        for _ in range(bits[length - 1]):
            codes[values[k]] = (code, length)
            code += 1
            k += 1
        code <<= 1
    return codes


DC_BITS = [0, 1, 5, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0]   # the standard luminance DC table
DC_VALUES = list(range(12))
AC_BITS = [1] + [0] * 15                                     # one code: end of block
AC_VALUES = [0x00]


def jpeg_cmyk(rows, ink=(20, 255, 230, 40)):
    """A baseline CMYK JPEG with an Adobe marker (transform 0, inverted
    samples, as Photoshop writes them). Every 8x8 block is one colour, so
    each block is only a DC coefficient: no DCT is needed."""
    height, width = len(rows), len(rows[0])
    if height % 8 or width % 8:
        raise ValueError("the image must be a whole number of 8x8 blocks")
    dc = _huffman_codes(DC_BITS, DC_VALUES)
    eob = _huffman_codes(AC_BITS, AC_VALUES)[0x00]
    out, acc, nbits = bytearray(), 0, 0

    def put(code, length):
        nonlocal acc, nbits
        acc, nbits = (acc << length) | code, nbits + length
        while nbits >= 8:
            nbits -= 8
            byte = (acc >> nbits) & 0xFF
            out.append(byte)
            if byte == 0xFF:
                out.append(0x00)
        acc &= (1 << nbits) - 1

    predictions = [0, 0, 0, 0]
    for by in range(height // 8):
        for bx in range(width // 8):
            inked = rows[by * 8][bx * 8]
            for comp in range(4):
                stored = 255 - (ink[comp] if inked else 0)   # Adobe CMYK is stored inverted
                coefficient = 8 * (stored - 128)             # the DC of a flat block, quantiser 1
                diff = coefficient - predictions[comp]
                predictions[comp] = coefficient
                size = abs(diff).bit_length()
                put(*dc[size])
                if size:
                    put(diff if diff > 0 else diff + (1 << size) - 1, size)
                put(*eob)
    if nbits:
        put((1 << (8 - nbits)) - 1, 8 - nbits)

    def segment(marker, payload):
        return struct.pack(">BBH", 0xFF, marker, len(payload) + 2) + payload

    header = b"\xff\xd8"
    header += segment(0xEE, b"Adobe" + struct.pack(">HHHB", 100, 0, 0, 0))
    header += segment(0xDB, b"\x00" + bytes([1] * 64))
    header += segment(0xC0, struct.pack(">BHHB", 8, height, width, 4)
                      + b"".join(struct.pack(">BBB", i, 0x11, 0) for i in range(1, 5)))
    header += segment(0xC4, b"\x00" + bytes(DC_BITS) + bytes(DC_VALUES))
    header += segment(0xC4, b"\x10" + bytes(AC_BITS) + bytes(AC_VALUES))
    header += segment(0xDA, b"\x04" + b"".join(struct.pack(">BB", i, 0x00) for i in range(1, 5))
                      + b"\x00\x3f\x00")
    return header + bytes(out) + b"\xff\xd9"


def test_images():
    """{file name: bytes} of the three read_image test images."""
    return {
        "e2e-image.png": png_rgb(render_word(IMAGE_WORDS["e2e-image.png"])),
        "e2e-image.tif": tiff_grey16(render_word(IMAGE_WORDS["e2e-image.tif"])),
        "e2e-image.jpg": jpeg_cmyk(render_word(IMAGE_WORDS["e2e-image.jpg"])),
    }


# --- capture function -------------------------------------------------------

# It logs one JSON line per request: the method, path, host and whether an
# Authorization header came, never the headers themselves (the bot's replies
# carry its bearer token), and answers like the Bot Connector does. GET
# .../files/<name> serves the test files.
CAPTURE_CODE = r'''
import base64, json, time

FILES = __FILES__

def handler(event, context):
    ctx = event.get("requestContext", {})
    http = ctx.get("http", {})
    method, path, host = http.get("method", ""), event.get("rawPath", ""), ctx.get("domainName", "")
    auth = any(k.lower() == "authorization" for k in (event.get("headers") or {}))
    record = {"capture": True, "method": method, "path": path, "host": host, "auth_header_present": auth}
    if method == "GET":
        name = path.rsplit("/", 1)[-1]
        found = "/files/" in path and name in FILES
        record["status"] = 200 if found else 404
        print(json.dumps(record))
        if not found:
            return {"statusCode": 404, "headers": {"Content-Type": "text/plain"}, "body": "not found"}
        content_type, data = FILES[name]
        return {"statusCode": 200, "headers": {"Content-Type": content_type}, "body": data,
                "isBase64Encoded": True}
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8", "replace")
    rid = "capture-%d-%s" % (int(time.time() * 1000), context.aws_request_id[:8])
    record.update({"returned_id": rid, "body": body[:60000]})
    print(json.dumps(record))
    return {"statusCode": 200, "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"id": rid})}
'''


def capture_code():
    files = {ATTACHMENT_NAME: ("text/plain", base64.b64encode(ATTACHMENT_TEXT.encode()).decode()),
             LEAK_NAME: ("text/plain", base64.b64encode(LEAK_TEXT.encode()).decode())}
    # The images go also with Gemini off: the image question must then reach no tool
    for name, data in test_images().items():
        files[name] = (IMAGE_TYPES[name], base64.b64encode(data).decode())
    return CAPTURE_CODE.replace("__FILES__", json.dumps(files))


def capture_names(stack):
    return f"{stack}-capture", f"{stack}-capture-role"


def read_env(path):
    env = {}
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export "):]
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


def try_http(method, url, **kwargs):
    """http(), with (None, {}, reason) when there is no answer at all."""
    try:
        return http(method, url, **kwargs)
    except OSError as exc:
        return None, {}, type(exc).__name__.encode()


class Result:
    def __init__(self):
        self.rows = []

    def add(self, name, ok, detail=""):
        self.rows.append((name, ok, detail))
        log(f"  {'PASS' if ok else 'FAIL'}  {name}{': ' + detail if detail else ''}")

    @property
    def failed(self):
        return [r for r in self.rows if not r[1]]


class NotTestTagged(SystemExit):
    """A capture function or role of this name exists but is not tagged
    nlsql:purpose=test: the script did not make it, so it changes and
    deletes nothing of it."""


def require_test_tag(action, name, tags):
    if tags.get("nlsql:purpose") != "test":
        raise NotTestTagged(f"refusing to {action} {name}: it is not tagged nlsql:purpose=test")


def ensure_capture(session, stack):
    """The capture function, its public URL and a second URL on an alias (a
    second host): (service_url, alt_base_url, log_group). A function or role
    of that name that is not tagged nlsql:purpose=test is refused before
    anything is created or changed: the run would replace its code and make
    it publicly invokable, and teardown would then refuse to remove it."""
    fn_name, role_name = capture_names(stack)
    iam, lam = session.client("iam"), session.client("lambda")
    try:
        role_arn = iam.get_role(RoleName=role_name)["Role"]["Arn"]
        require_test_tag("change", role_name,
                         {t["Key"]: t["Value"] for t in iam.list_role_tags(RoleName=role_name)["Tags"]})
    except iam.exceptions.NoSuchEntityException:
        role_arn = None
    try:
        fn_arn = lam.get_function(FunctionName=fn_name)["Configuration"]["FunctionArn"]
        require_test_tag("change", fn_name, lam.list_tags(Resource=fn_arn)["Tags"])
    except lam.exceptions.ResourceNotFoundException:
        fn_arn = None

    if role_arn is None:
        role_arn = iam.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps({
                "Version": "2012-10-17",
                "Statement": [{"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"},
                               "Action": "sts:AssumeRole"}]}),
            Description="NLSQL AI Employee e2e test capture function (temporary)",
            Tags=[{"Key": k, "Value": v} for k, v in TEST_TAG.items()],
        )["Role"]["Arn"]
        iam.attach_role_policy(RoleName=role_name,
                               PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole")
        log(f"  created role {role_name}; waiting for IAM to propagate")
        time.sleep(12)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("index.py", capture_code())
    code = buf.getvalue()
    if fn_arn is not None:
        lam.update_function_code(FunctionName=fn_name, ZipFile=code)
    else:
        for attempt in range(6):
            try:
                lam.create_function(FunctionName=fn_name, Runtime="python3.13", Role=role_arn,
                                    Handler="index.handler", Code={"ZipFile": code}, Timeout=10,
                                    MemorySize=128, Tags=TEST_TAG,
                                    Description="NLSQL AI Employee e2e test: captures bot replies (temporary)")
                break
            except ClientError as exc:
                if "role" in str(exc).lower() and attempt < 5:
                    time.sleep(8)
                    continue
                raise
    lam.get_waiter("function_active_v2").wait(FunctionName=fn_name)
    lam.get_waiter("function_updated_v2").wait(FunctionName=fn_name)

    # The alias points at the code just deployed, and its URL is a second host.
    version = lam.publish_version(FunctionName=fn_name)["Version"]
    try:
        lam.update_alias(FunctionName=fn_name, Name=CAPTURE_ALIAS, FunctionVersion=version)
    except lam.exceptions.ResourceNotFoundException:
        lam.create_alias(FunctionName=fn_name, Name=CAPTURE_ALIAS, FunctionVersion=version)

    urls = []
    for qualifier in (None, CAPTURE_ALIAS):
        q = {"Qualifier": qualifier} if qualifier else {}
        try:
            url = lam.get_function_url_config(FunctionName=fn_name, **q)["FunctionUrl"]
        except lam.exceptions.ResourceNotFoundException:
            url = lam.create_function_url_config(FunctionName=fn_name, AuthType="NONE", **q)["FunctionUrl"]
        urls.append(url.rstrip("/") + "/")
        for sid, kwargs in (
            ("public-url", {"Action": "lambda:InvokeFunctionUrl", "FunctionUrlAuthType": "NONE"}),
            ("public-url-invoke", {"Action": "lambda:InvokeFunction", "InvokedViaFunctionUrl": True}),
        ):
            try:
                lam.add_permission(FunctionName=fn_name, StatementId=sid, Principal="*", **q, **kwargs)
            except lam.exceptions.ResourceConflictException:
                pass
            except Exception as exc:  # older SDKs lack InvokedViaFunctionUrl
                log(f"  note: permission {sid} not added ({type(exc).__name__})")
    return urls[0], urls[1], f"/aws/lambda/{fn_name}"


def teardown(session, stack):
    """Deletes the capture function (its alias and URLs go with it), its log
    group and its role, each only when tagged nlsql:purpose=test."""
    fn_name, role_name = capture_names(stack)
    lam, iam, logs = session.client("lambda"), session.client("iam"), session.client("logs")
    deleted_function = False
    try:
        tags = lam.list_tags(Resource=lam.get_function(FunctionName=fn_name)["Configuration"]["FunctionArn"])["Tags"]
        require_test_tag("delete", fn_name, tags)
        lam.delete_function(FunctionName=fn_name)
        log(f"deleted function {fn_name}")
        deleted_function = True
    except lam.exceptions.ResourceNotFoundException:
        pass
    # Lambda creates this log group untagged, so it goes only with the tagged
    # function it belongs to.
    if deleted_function:
        try:
            logs.delete_log_group(logGroupName=f"/aws/lambda/{fn_name}")
        except logs.exceptions.ResourceNotFoundException:
            pass
    try:
        tags = {t["Key"]: t["Value"] for t in iam.list_role_tags(RoleName=role_name)["Tags"]}
        require_test_tag("delete", role_name, tags)
        for p in iam.list_attached_role_policies(RoleName=role_name)["AttachedPolicies"]:
            iam.detach_role_policy(RoleName=role_name, PolicyArn=p["PolicyArn"])
        iam.delete_role(RoleName=role_name)
        log(f"deleted role {role_name}")
    except iam.exceptions.NoSuchEntityException:
        pass


def log_events(session, log_group, since_ms, pattern=""):
    """(timestamp_ms, message) of a log group's events since since_ms."""
    logs = session.client("logs")
    out, token = [], None
    while True:
        kwargs = {"logGroupName": log_group, "startTime": since_ms}
        if pattern:
            kwargs["filterPattern"] = pattern
        if token:
            kwargs["nextToken"] = token
        try:
            resp = logs.filter_log_events(**kwargs)
        except logs.exceptions.ResourceNotFoundException:
            return out
        out.extend((ev["timestamp"], ev["message"]) for ev in resp.get("events", []))
        token = resp.get("nextToken")
        if not token:
            return out


def parse_capture(events):
    """The capture records among (timestamp, message) events: dicts with ts,
    method, path, host, auth_header_present, returned_id and activity."""
    out = []
    for ts, msg in events:
        start = msg.find("{")
        if start < 0:
            continue
        try:
            rec = json.loads(msg[start:])
        except ValueError:
            continue
        if not isinstance(rec, dict) or not rec.get("capture"):
            continue
        try:
            activity = json.loads(rec.get("body") or "{}")
        except ValueError:
            activity = {}
        out.append({"ts": ts, "method": rec.get("method", ""), "path": rec.get("path", ""),
                    "host": rec.get("host", ""), "auth_header_present": rec.get("auth_header_present"),
                    "returned_id": rec.get("returned_id", ""),
                    "activity": activity if isinstance(activity, dict) else {}})
    return out


def captured(session, log_group, since_ms):
    return parse_capture(log_events(session, log_group, since_ms, '"capture"'))


# --- log hygiene ----------------------------------------------------------------

# The values of code, state, token and t as query parameters, unless the image
# redacted them (helpers/log_redaction.py writes [redacted] in their place)
QUERY_VALUE = re.compile(r"(?<=[?&;])(?:code|state|token|t)=(?!\[redacted\])[^\s&\"'<>]+", re.IGNORECASE)
# The ECS credentials path, with its id (unredacted) or without (redacted)
CREDENTIALS_PATH = re.compile(r"/v2/credentials/(\[redacted\])?")
JWT_SHAPE = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
BEARER_VALUE = re.compile(r"Bearer\s+(?!\[redacted\])[A-Za-z0-9._~+/=-]{16,}")
ACCESS_KEY = re.compile(r"AccessKeyId|\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")
# What mask() hides in printed answers: a regression that leaks a token or a
# key into a reply must not print it in the FAIL line
SECRET_SHAPES = [JWT_SHAPE, BEARER_VALUE, re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
                 re.compile(r"(?<=[?&;])(?:code|state|token|t)=[^\s&\"'<>]+", re.IGNORECASE),
                 re.compile(r"(?<=\"SecretAccessKey\": \")[^\"]+|(?<=\"Token\": \")[^\"]+")]


# The run's actual secret values (the env file's, the generated ones), which
# no shape regex would catch if a reply quoted one; filled by mask_values()
MASKED_VALUES = set()


def mask_values(values):
    MASKED_VALUES.update(v for v in values if v and len(v) >= 8)


def mask(text):
    for value in sorted(MASKED_VALUES, key=len, reverse=True):
        text = text.replace(value, "[masked]")
    for shape in SECRET_SHAPES:
        text = shape.sub("[masked]", text)
    return text


def hygiene_problems(messages, secret_values=()):
    """{problem: count} for log lines that quote what the image must never log."""
    found = {}

    def hit(name):
        found[name] = found.get(name, 0) + 1

    secret_values = [s for s in secret_values if s and len(s) >= 8]
    for msg in messages:
        if QUERY_VALUE.search(msg):
            hit("a code=, state=, token= or t= value")
        for m in CREDENTIALS_PATH.finditer(msg):
            hit("a /v2/credentials/ path (redacted)" if m.group(1) else "a /v2/credentials/ path with its id")
        if JWT_SHAPE.search(msg):
            hit("a JWT")
        if BEARER_VALUE.search(msg):
            hit("a Bearer value")
        if ACCESS_KEY.search(msg):
            hit("an AccessKeyId or access key")
        for value in secret_values:
            if value in msg:
                hit("a secret value")
    return found


def secret_values(env, extra=()):
    """The secret values to look for: every env file key naming a password,
    secret, token or API key, plus extra (generated in the run or read from
    the stack)."""
    values = {v for k, v in env.items()
              if re.search(r"password|secret|token|api_?key|encryption_key", k, re.IGNORECASE)}
    values |= set(extra)
    return sorted(v for v in values if v and len(v) >= 8)


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


def jwt_claims(token):
    """The claims of a JWT, unverified (the bot verifies it), or {}."""
    try:
        payload = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        return {}


def token_is_for_app(claims, app_id, tenant):
    """Whether a client-credentials token was issued to the bot's own app in its tenant."""
    return (claims.get("appid") == app_id or claims.get("azp") == app_id) and claims.get("tid") == tenant


def activity(kind, conv_id, service_url, app_id, tenant, user_id, channel="msteams", text=None,
             attachments=None, members_added=None, name=None, value=None):
    """A Bot Framework activity shaped as the channel sends it. Off Teams it
    still names the Teams test user's object id: the bot must ignore it."""
    act = {
        "type": kind,
        "id": str(uuid.uuid4()),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
        "channelId": channel,
        "serviceUrl": service_url,
        "from": {"id": user_id, "name": "E2E Tester", "aadObjectId": TEST_OID},
        "recipient": {"id": f"28:{app_id}" if channel == "msteams" else app_id, "name": "NLSQL AI Employee"},
        "locale": "en-US",
    }
    if channel == "msteams":
        act["conversation"] = {"id": conv_id, "conversationType": "personal", "tenantId": tenant}
        act["channelData"] = {"tenant": {"id": tenant}}
    else:
        act["conversation"] = {"id": conv_id}
    if text is not None:
        act["text"] = text
        act["textFormat"] = "plain"
    if attachments:
        act["attachments"] = attachments
    if members_added:
        act["membersAdded"] = members_added
    if name:
        act["name"] = name
    if value is not None:
        act["value"] = value
    return act


def open_live_browser_invoke(conv_id, service_url, app_id, tenant, user_id):
    """What Teams sends when the user selects 'Open live browser' (bot/live_browser.js)."""
    return activity("invoke", conv_id, service_url, app_id, tenant, user_id, name="task/fetch",
                    value={"data": {"msteams": {"type": "task/fetch"}, "action": "open_live_browser"},
                           "context": {"theme": "default"}})


def file_attachment(url, name):
    """A Teams file attachment (pre-authenticated downloadUrl)."""
    return {"contentType": "application/vnd.microsoft.teams.file.download.info",
            "contentUrl": url, "name": name,
            "content": {"downloadUrl": url, "uniqueId": str(uuid.uuid4()), "fileType": name.rsplit(".", 1)[-1]}}


def html_part(text):
    return {"contentType": "text/html", "content": f"<p>{text}</p>"}


def post_activity(endpoint, token, act):
    t0 = time.monotonic()
    status, _, body = http("POST", endpoint, act, {"Authorization": f"Bearer {token}"}, timeout=90)
    return status, time.monotonic() - t0, body


def in_conversation(record, conv_id):
    path = record["path"]
    return urllib.parse.quote(conv_id, safe="") in path or conv_id in urllib.parse.unquote(path)


def replies_to(records, conv_id, activity_id):
    """Messages the bot created for one inbound activity, each with its final content.

    A card the bot edits in place (PUT .../activities/<id>) is not a second
    reply: it is linked to its card through the id the capture function
    handed back.
    """
    records = [r for r in records if in_conversation(r, conv_id)]
    posts = [r for r in records
             if r["method"] == "POST" and r["activity"].get("type") != "typing"
             and (r["activity"].get("replyToId") == activity_id
                  or r["path"].rstrip("/").endswith("/" + activity_id))]
    out = []
    for post in posts:
        edits = [r for r in records if r["method"] == "PUT" and post["returned_id"]
                 and r["path"].rstrip("/").endswith("/" + post["returned_id"])]
        out.append({"initial": post["activity"], "final": (edits[-1] if edits else post)["activity"],
                    "edits": len(edits), "returned_id": post["returned_id"], "ts": post["ts"]})
    return out


def conversation_messages(records, conv_id, exclude_ids=()):
    """Every message the bot created in a conversation, with its final
    content, except those whose capture id is in exclude_ids: proactive
    messages carry no replyToId of their own question."""
    records = [r for r in records if in_conversation(r, conv_id)]
    out = []
    for post in records:
        if post["method"] != "POST" or post["activity"].get("type") == "typing":
            continue
        if post["returned_id"] in exclude_ids:
            continue
        edits = [r for r in records if r["method"] == "PUT" and post["returned_id"]
                 and r["path"].rstrip("/").endswith("/" + post["returned_id"])]
        out.append({"initial": post["activity"], "final": (edits[-1] if edits else post)["activity"],
                    "edits": len(edits), "returned_id": post["returned_id"], "ts": post["ts"]})
    return out


# A streamed answer starts as this card and is edited in place; it is not an answer.
STATUS_MESSAGES = {"Thinking..."}


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


def card_actions(act):
    """Every Action.* of an activity's cards."""
    found = []

    def walk(node):
        if isinstance(node, dict):
            if str(node.get("type", "")).startswith("Action."):
                found.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    for att in act.get("attachments") or []:
        walk(att.get("content"))
    return found


def has_live_browser_button(act):
    return any(a.get("type") == "Action.Submit" and a.get("title") == STRINGS["open_live_browser"]
               and (a.get("data") or {}).get("action") == "open_live_browser"
               and ((a.get("data") or {}).get("msteams") or {}).get("type") == "task/fetch"
               for a in card_actions(act))


def is_answer(reply):
    return activity_text(reply["final"]).strip() not in STATUS_MESSAGES


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


def wait_text(session, log_group, since_ms, conv_id, want, timeout=REPLY_TIMEOUT, exclude_ids=()):
    """The first message in the conversation (not in exclude_ids) whose text
    satisfies want, or None: for proactive messages."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for msg in conversation_messages(captured(session, log_group, since_ms), conv_id, exclude_ids):
            if want(activity_text(msg["final"])):
                return msg
        time.sleep(5)
    return None


def summary(texts):
    """The answers for a FAIL line, with anything secret-shaped masked."""
    return " | ".join(mask(t[:300]).replace("\n", " / ") for t in texts) if texts else "no answer"


# --- read_image ---------------------------------------------------------------

# analyze_image.py's sentence (STRINGS["annotated_saved"]) with its file name;
# the model may set the name in backticks when it quotes it
ANNOTATED_SAVED = re.compile(r"Annotated image saved as `?(image_[0-9a-f]{8}\.([A-Za-z0-9]+))`?\.")


def word_answered(word, text):
    """Whether text has the word as a whole word (so FIG is not FIGURE or
    CONFIGURATION, and LIME is not SUBLIME)."""
    return re.search(rf"(?<![A-Z0-9]){re.escape(word.upper())}(?![A-Z0-9])", text.upper()) is not None


def annotated_names(text):
    """{file name: extension} of every annotated image text says was saved."""
    return {m.group(1): m.group(2).lower() for m in ANNOTATED_SAVED.finditer(text or "")}


def judge_image(name, word, texts, snapshot_before="", snapshot_after=""):
    """[(check, ok, detail)] for one read_image answer. The saved image is
    looked for in the answers and in what the conversation snapshot gained
    (it keeps the tool's own output): B13 saves it in the image's format
    (.tif, .jpg, .png), or as PNG when Pillow cannot write that format."""
    allowed = {name.rsplit(".", 1)[-1].lower(), "png"}
    new = {**annotated_names("\n".join(texts))}
    before = annotated_names(snapshot_before)
    new.update({k: v for k, v in annotated_names(snapshot_after).items() if k not in before})
    not_saved = (any(STRINGS["annotated_not_saved"] in t for t in texts)
                 or snapshot_after.count(STRINGS["annotated_not_saved"])
                 > snapshot_before.count(STRINGS["annotated_not_saved"]))
    saved = sorted(n for n, ext in new.items() if ext in allowed)
    return [
        (f"read_image {name}: answers {word}", any(word_answered(word, t) for t in texts), summary(texts)),
        (f"read_image {name}: the annotated image was saved as image_<8 hex>.{'/'.join(sorted(allowed))}",
         bool(saved), ", ".join(saved) or ", ".join(sorted(new)) or "no 'Annotated image saved as' sentence"),
        (f"read_image {name}: no '{STRINGS['annotated_not_saved']}'", not not_saved, ""),
    ]


# --- sign-in links ------------------------------------------------------------

def b64url_json(text):
    """The JSON a base64url string decodes to, or None."""
    try:
        return json.loads(base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)))
    except (ValueError, TypeError):
        return None


def state_is_signed(state):
    """Whether an OAuth state is signed (itsdangerous: payload.timestamp.signature)
    rather than plain base64 JSON anyone could write."""
    if not state or b64url_json(state) is not None:
        return False
    # itsdangerous marks a compressed payload with a leading '.'
    parts = (state[1:] if state.startswith(".") else state).split(".")
    return len(parts) == 3 and all(re.fullmatch(r"[A-Za-z0-9_-]+", p) for p in parts) and len(parts[2]) >= 32


def forged_state(user_id, channel="msteams", oid=TEST_OID):
    """A state shaped like the image's, signed with a random key."""
    payload = base64.urlsafe_b64encode(json.dumps(
        {"u": user_id, "c": channel, "a": oid, "n": secrets.token_urlsafe(8)}, separators=(",", ":")
    ).encode()).decode().rstrip("=")
    stamp = base64.urlsafe_b64encode(struct.pack(">I", int(time.time()))).decode().rstrip("=")
    return f"{payload}.{stamp}.{base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip('=')}"


def tamper_token(token):
    """The token with one character of its signature (the last part) changed,
    away from the last character, whose low bits base64 may ignore."""
    head, _, sig = token.rpartition(".")
    if not sig:
        head, sig = "", token
    i = len(sig) // 2
    sig = sig[:i] + ("B" if sig[i] == "A" else "A") + sig[i + 1:]
    return f"{head}.{sig}" if head else sig


def analyse_login_url(url, app_id, tenant, redirect_uri):
    """[(check, ok, detail)] for a sign-in link from a Teams login card."""
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)

    def one(name):
        values = query.get(name) or [""]
        return values[0] if len(values) == 1 else ""

    scopes = one("scope").split()
    state = one("state")
    return [
        ("login URL: the test tenant's authorize endpoint",
         parsed.scheme == "https" and parsed.netloc == "login.microsoftonline.com"
         and parsed.path == f"/{tenant}/oauth2/v2.0/authorize", f"{parsed.netloc}{parsed.path}"),
        ("login URL: client_id is the bot's app", one("client_id") == app_id, ""),
        ("login URL: redirect_uri is OAuthRedirectUri", one("redirect_uri") == redirect_uri, one("redirect_uri")),
        ("login URL: response_type code, response_mode query",
         one("response_type") == "code" and one("response_mode") == "query", ""),
        ("login URL: exactly the 7 scopes", sorted(scopes) == sorted(SCOPES) and len(scopes) == 7,
         " ".join(scopes)),
        ("login URL: the state is signed, not plain base64 JSON", state_is_signed(state),
         f"{len(state.split('.'))} part(s)"),
    ]


# --- the live view's WebSocket --------------------------------------------------

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def ws_accept(key):
    return base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()


def ws_frame(opcode, payload=b"", mask=True, mask_key=None):
    """One final WebSocket frame; a client's frames are masked."""
    first = 0x80 | opcode
    n = len(payload)
    bit = 0x80 if mask else 0
    if n < 126:
        header = bytes([first, bit | n])
    elif n < 65536:
        header = bytes([first, bit | 126]) + struct.pack(">H", n)
    else:
        header = bytes([first, bit | 127]) + struct.pack(">Q", n)
    if not mask:
        return header + payload
    key = mask_key or os.urandom(4)
    return header + key + bytes(b ^ key[i % 4] for i, b in enumerate(payload))


class WebSocketClosed(Exception):
    def __init__(self, code=None, reason=""):
        super().__init__(f"closed ({code}{', ' + reason if reason else ''})")
        self.code, self.reason = code, reason


class WebSocket:
    """A minimal RFC 6455 client over a connected (TLS) socket: answers pings,
    collects data frames, and never consumes a frame until it is whole, so a
    socket timeout loses nothing."""

    def __init__(self, sock):
        self.sock = sock
        self.buf = b""
        self.data = b""
        self.pings = 0

    def handshake(self, host, path, origin, protocol="binary"):
        """(status, headers); raises ValueError when a 101 is not a valid upgrade."""
        key = base64.b64encode(os.urandom(16)).decode()
        request = (f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                   f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
                   f"Sec-WebSocket-Protocol: {protocol}\r\nOrigin: {origin}\r\n"
                   "User-Agent: nlsql-ai-employee-e2e\r\n\r\n")
        self.sock.sendall(request.encode())
        while b"\r\n\r\n" not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise WebSocketClosed(None, "no HTTP answer")
            self.buf += chunk
        head, self.buf = self.buf.split(b"\r\n\r\n", 1)
        lines = head.decode("iso-8859-1").split("\r\n")
        status = int(lines[0].split()[1])
        headers = {}
        for line in lines[1:]:
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
        if status == 101:
            if headers.get("sec-websocket-accept") != ws_accept(key):
                raise ValueError("Sec-WebSocket-Accept does not match the key")
            if headers.get("upgrade", "").lower() != "websocket":
                raise ValueError("the answer does not upgrade to websocket")
        return status, headers

    def _frame(self):
        """(opcode, payload) of the first whole frame in the buffer, or None."""
        buf = self.buf
        if len(buf) < 2:
            return None
        opcode, masked, n, pos = buf[0] & 0x0F, buf[1] & 0x80, buf[1] & 0x7F, 2
        if n == 126:
            if len(buf) < 4:
                return None
            n, pos = struct.unpack(">H", buf[2:4])[0], 4
        elif n == 127:
            if len(buf) < 10:
                return None
            n, pos = struct.unpack(">Q", buf[2:10])[0], 10
        key = b""
        if masked:
            key, pos = buf[pos:pos + 4], pos + 4
        if len(buf) < pos + n:
            return None
        payload = buf[pos:pos + n]
        if masked:
            payload = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
        self.buf = buf[pos + n:]
        return opcode, payload

    def read_frame(self):
        """The next data frame's payload; control frames are handled here."""
        while True:
            frame = self._frame()
            if frame is None:
                chunk = self.sock.recv(65536)
                if not chunk:
                    raise WebSocketClosed(None, "connection closed without a close frame")
                self.buf += chunk
                continue
            opcode, payload = frame
            if opcode == 0x9:
                self.pings += 1
                self.sock.sendall(ws_frame(0xA, payload))
            elif opcode == 0x8:
                code = struct.unpack(">H", payload[:2])[0] if len(payload) >= 2 else None
                raise WebSocketClosed(code, payload[2:].decode("utf-8", "replace"))
            elif opcode in (0x0, 0x1, 0x2):
                return payload

    def recv_exact(self, n):
        while len(self.data) < n:
            self.data += self.read_frame()
        out, self.data = self.data[:n], self.data[n:]
        return out

    def send(self, payload):
        self.sock.sendall(ws_frame(0x2, payload))

    def close(self, code=1000):
        try:
            self.sock.sendall(ws_frame(0x8, struct.pack(">H", code)))
        except OSError:
            pass


def rfb_handshake(ws):
    """Up to the RFB ServerInit (no password, shared): {version, width, height, name}."""
    version = ws.recv_exact(12)
    if not re.fullmatch(rb"RFB \d{3}\.\d{3}\n", version):
        raise ValueError(f"not an RFB greeting: {version!r}")
    minor = int(version[8:11])
    ws.send(b"RFB 003.008\n" if minor >= 8 else version)
    if minor >= 7:
        count = ws.recv_exact(1)[0]
        if count == 0:
            reason = ws.recv_exact(struct.unpack(">I", ws.recv_exact(4))[0])
            raise ValueError(f"the server refused the connection: {reason!r}")
        types = ws.recv_exact(count)
        if 1 not in types:
            raise ValueError(f"no 'None' security type offered: {list(types)}")
        ws.send(b"\x01")
        if minor >= 8 and struct.unpack(">I", ws.recv_exact(4))[0] != 0:
            raise ValueError("security handshake failed")
    elif struct.unpack(">I", ws.recv_exact(4))[0] != 1:
        raise ValueError("the server asks for a password")
    ws.send(b"\x01")
    width, height = struct.unpack(">HH", ws.recv_exact(4))
    ws.recv_exact(16)  # pixel format
    name = ws.recv_exact(struct.unpack(">I", ws.recv_exact(4))[0])
    return {"version": version.decode().strip(), "width": width, "height": height,
            "name": name.decode("utf-8", "replace")}


def ws_idle(ws, seconds, clock=time.monotonic, poll=5.0):
    """Keeps the WebSocket open and idle for seconds, answering pings:
    (stayed_open, elapsed, reason)."""
    start = clock()
    ws.sock.settimeout(poll)
    while clock() - start < seconds:
        try:
            ws.read_frame()  # RFB data while idle (a bell, the clipboard) is fine
        except socket.timeout:
            continue
        except WebSocketClosed as exc:
            return False, clock() - start, str(exc)
        except OSError as exc:
            return False, clock() - start, type(exc).__name__
    return True, clock() - start, ""


def ws_connect(host, path, timeout=30):
    """(status, headers, WebSocket or None) for wss://host/path through CloudFront."""
    raw = socket.create_connection((host, 443), timeout=timeout)
    sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
    ws = WebSocket(sock)
    status, headers = ws.handshake(host, path, f"https://{host}")
    if status != 101:
        sock.close()
        return status, headers, None
    return status, headers, ws


# --- health -------------------------------------------------------------------

def classify_health(status, body):
    """UP, STARTING (the image's 503 gate), EDGE (a 5xx from CloudFront or the
    load balancer, or no answer) or OTHER <status>."""
    try:
        data = json.loads(body or b"")
    except ValueError:
        data = None
    if status == 200 and data == {"status": "UP"}:
        return "UP"
    if status == 503 and data == {"status": "STARTING"}:
        return "STARTING"
    if status is None or (status >= 500 and not isinstance(data, dict)):
        return "EDGE"
    return f"OTHER {status}"


def gate_503_checks(observations, old_targets):
    """How many of the load balancer's health checks of a new target (one not
    in old_targets) got the gate's 503: observations are describe_target_health
    descriptions, (target id, state, reason, description)."""
    return sum(1 for target, _, reason, description in observations
               if target not in old_targets and reason == "Target.ResponseCodeMismatch"
               and re.search(r"\b503\b", description or ""))


def judge_restart(sequence, gate_checks=0):
    """(ok, detail) for the /health answers seen while the task was replaced.
    Leading UPs are the old task before it left the target group. Then the
    sequence must end UP, stay UP once UP, and every answer the container
    gave before that must be the STARTING gate. The gate itself must be seen:
    a STARTING through CloudFront, or (gate_checks) a health check of the new
    target that got 503. With one target and a 30 s health check the load
    balancer often answers 5xx itself until the new target is healthy, so
    EDGE then UP alone proves nothing: an ungated task gives it too."""
    i = 0
    while i < len(sequence) and sequence[i] == "UP":
        i += 1
    rest = sequence[i:]
    if not rest:
        return False, "never left UP: the old task kept answering"
    if "UP" not in rest:
        return False, f"never came back UP (last {rest[-1]})"
    first_up = rest.index("UP")
    gap, after = rest[:first_up], rest[first_up:]
    others = sorted({c for c in gap if c not in ("STARTING", "EDGE")})
    counts = (f"{gap.count('EDGE')} edge 5xx, {gap.count('STARTING')} STARTING, then UP; "
              f"{gate_checks} health check(s) of the new target got 503")
    if others:
        return False, f"unexpected answers before UP: {', '.join(others)} ({counts})"
    if any(c != "UP" for c in after):
        return False, f"flapped after UP: {after}"
    if not gap.count("STARTING") and not gate_checks:
        return False, f"the gate was never seen ({counts})"
    return True, counts


def judge_entitled_before_up(entitled_ms, first_up_ms, skew_ms=5000):
    """(ok, detail): the new task logged its entitlement before /health first
    answered UP, allowing skew_ms between this host's clock and the task's.
    An ungated nginx answers UP before it."""
    if first_up_ms is None:
        return False, "never UP"
    if entitled_ms is None:
        return False, "the new task logged no entitlement line"
    lead = (first_up_ms - entitled_ms) / 1000
    return lead >= -skew_ms / 1000, f"first UP {lead:.0f} s after the entitlement line"


def startup_since(tasks, fallback_ms):
    """Where the running task's start-up lines begin: the earliest createdAt of
    the service's running tasks (ECS creates a task before any of its
    containers logs), else fallback_ms. deploy-test switches a stack in place,
    so its log group still holds the earlier tasks' start-up lines."""
    created = [t["createdAt"] for t in tasks if t.get("createdAt")]
    return int(min(created).timestamp() * 1000) if created else fallback_ms


# --- the checks -----------------------------------------------------------------

class Run:
    """Everything one run shares: the stack, the capture function, the token."""

    def __init__(self, args, session, env, app_id, secret, tenant):
        self.args, self.session, self.env = args, session, env
        self.app_id, self.secret, self.tenant = app_id, secret, tenant
        self.res = Result()
        self.extra_secrets = []
        mask_values(secret_values(env, [secret]))
        self.run_id = uuid.uuid4().hex[:10]
        # A new Teams user ID per run: a user ID belongs to the first channel
        # that used it, for good (UserReferences rows are never deleted)
        self.user = f"29:e2e-{self.run_id}"
        self.conv = f"a:e2e-{uuid.uuid4().hex}"
        self.dl_conv = f"e2e-dl-{uuid.uuid4().hex}"
        self.seen_ids = set()  # capture ids of the answers to questions, so proactive messages stand out

    # helpers
    def add(self, *a):
        self.res.add(*a)

    def say(self, name, text, attachments=None, want=None, expect="", conv=None, channel="msteams",
            timeout=REPLY_TIMEOUT, check_answer=True, one_answer=True):
        """Sends a message, checks the acknowledgement and that exactly one
        answer comes; returns the answers' texts."""
        conv = conv or self.conv
        act = activity("message", conv, self.service_url, self.app_id, self.tenant, self.user,
                       channel=channel, text=text, attachments=attachments)
        st, took, body = post_activity(self.endpoint, self.token, act)
        self.add(f"{name}: acknowledged at once", st in (200, 201, 202) and took < ACK_LIMIT,
                 f"HTTP {st} in {took:.1f}s {'' if st in (200, 201, 202) else body[:200]!r}")
        got = wait_reply(self.session, self.capture_log, self.since, conv, act["id"], timeout=timeout, want=want)
        answers = [r for r in got if is_answer(r)]
        self.seen_ids.update(r["returned_id"] for r in got)
        texts = [activity_text(r["final"]) for r in answers]
        if want is not None and check_answer:
            self.add(f"{name}: contains {expect}", any(want(t) for t in texts), summary(texts))
        if one_answer:
            self.add(f"{name}: exactly one answer", len(answers) == 1,
                     f"{len(answers)} answer(s), {len(got) - len(answers)} status message(s), "
                     f"{sum(r['edits'] for r in got)} in-place edit(s)")
        return texts, answers

    def task_lines(self, since_ms, wait=20):
        time.sleep(wait)  # CloudWatch Logs takes a few seconds to ingest
        return [m for _, m in log_events(self.session, self.log_group, since_ms)]

    def get_item(self, table, partition, row):
        resp = self.session.client("dynamodb").get_item(
            TableName=table, ConsistentRead=True,
            Key={"PartitionKey": {"S": partition}, "RowKey": {"S": row}})
        return resp.get("Item")

    def reference(self, user):
        """The bot's saved conversation reference of user: a UserReferences
        row in the tasks table (bot/tables_storage.js writes it to
        TASK_STORAGE_TABLE_NAME, where schedule_task.py looks it up)."""
        item = self.get_item(self.outputs["TasksTableName"], "UserReferences", user)
        return json.loads(item["ConversationReferenceJson"]["S"]) if item else {}

    def s3_object(self, key):
        try:
            return self.session.client("s3").get_object(Bucket=self.outputs["ConversationsBucketName"],
                                                        Key=key)["Body"].read()
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404", "NotFound"):
                return None
            raise

    def wait_for(self, probe, timeout, interval=5):
        deadline = time.monotonic() + timeout
        while True:
            value = probe()
            if value or time.monotonic() >= deadline:
                return value
            time.sleep(interval)

    def active_tasks(self):
        resp = self.session.client("dynamodb").query(
            TableName=self.outputs["TasksTableName"], ConsistentRead=True,
            KeyConditionExpression="PartitionKey = :p", FilterExpression="UserID = :u",
            ExpressionAttributeValues={":p": {"S": "ActiveTasks"}, ":u": {"S": self.user}})
        return resp.get("Items", [])

    # 0. the stack
    def load_stack(self):
        cfn = self.session.client("cloudformation")
        self.stack = cfn.describe_stacks(StackName=self.args.stack)["Stacks"][0]
        if {t["Key"]: t["Value"] for t in self.stack.get("Tags") or []}.get("nlsql:purpose") != "test":
            sys.exit(f"refusing to test {self.args.stack}: it is not tagged nlsql:purpose=test")
        self.outputs = {o["OutputKey"]: o["OutputValue"] for o in self.stack.get("Outputs", [])}
        self.params = {p["ParameterKey"]: p.get("ParameterValue", "") for p in self.stack.get("Parameters", [])}
        self.gemini = self.params.get("GeminiFeatures") == "Enabled"
        self.browser = self.params.get("BrowserAgent", "Enabled") == "Enabled"
        self.endpoint = self.outputs["MessagingEndpoint"]
        self.base = self.endpoint.rsplit("/api/messages", 1)[0]
        self.host = urllib.parse.urlparse(self.base).netloc
        self.log_group = self.outputs["LogGroupName"]
        self.created_ms = int(self.stack["CreationTime"].timestamp() * 1000)
        log(f"Stack {self.args.stack} (Gemini {'on' if self.gemini else 'off'}, browser agent "
            f"{'on' if self.browser else 'off'}), endpoint {self.endpoint}")
        self.add("mode: BrowserAgent and BrowserViewUrl agree", self.browser == ("BrowserViewUrl" in self.outputs),
                 f"BrowserAgent {self.params.get('BrowserAgent')!r}, BrowserViewUrl "
                 f"{'present' if 'BrowserViewUrl' in self.outputs else 'absent'}")
        if self.args.gemini_fallback and not self.gemini:
            sys.exit("--gemini-fallback needs a stack with GeminiFeatures=Enabled")

    # 1. health
    def check_health(self):
        log("health:")
        deadline = time.monotonic() + HEALTH_TIMEOUT
        seen = []
        while True:
            st, _, body = try_http("GET", self.base + "/health")
            kind = classify_health(st, body)
            if not seen or seen[-1] != kind:
                seen.append(kind)
            if kind == "UP" or time.monotonic() >= deadline:
                break
            time.sleep(15)
        self.add("/health 200 {\"status\":\"UP\"} (entitlement verified)", seen[-1] == "UP", " -> ".join(seen))

    def restart_probe(self):
        """Stops the service's task, so ECS starts a new one, and classifies
        /health every 2 s until it is UP again. A rolling deployment would keep
        the old task answering UP, so it would never show the gate."""
        log("restart probe:")
        ecs = self.session.client("ecs")
        elbv2 = self.session.client("elbv2")
        cluster, service = self.outputs["ClusterName"], self.outputs["ServiceName"]
        tg_arn = next(r["PhysicalResourceId"] for r in self.session.client("cloudformation")
                      .describe_stack_resources(StackName=self.args.stack)["StackResources"]
                      if r["LogicalResourceId"] == "TargetGroup")

        def targets():
            return [(d["Target"]["Id"], d["TargetHealth"].get("State", ""), d["TargetHealth"].get("Reason", ""),
                     d["TargetHealth"].get("Description", ""))
                    for d in elbv2.describe_target_health(TargetGroupArn=tg_arn)["TargetHealthDescriptions"]]

        old_targets = {t[0] for t in targets()}
        tasks = ecs.list_tasks(cluster=cluster, serviceName=service, desiredStatus="RUNNING")["taskArns"]
        if not tasks:
            self.add("restart probe: a running task to replace", False, "none running")
            return
        probe_since = int(time.time() * 1000) - 2000
        for arn in tasks:
            ecs.stop_task(cluster=cluster, task=arn, reason="NLSQL AI Employee e2e restart probe")
        sequence, observations, first_up_ms, last_targets = [], [], None, 0.0
        deadline = time.monotonic() + HEALTH_TIMEOUT
        while time.monotonic() < deadline:
            st, _, body = try_http("GET", self.base + "/health", timeout=10)
            kind = classify_health(st, body)
            sequence.append(kind)
            gap_open = any(c != "UP" for c in sequence)
            if kind == "UP" and gap_open and first_up_ms is None:
                first_up_ms = int(time.time() * 1000)
            # The load balancer's own health checks hit the new target
            # directly, every 30 s: their failure reason shows the gate's 503
            # even while CloudFront only sees the load balancer's 5xx
            if first_up_ms is None and time.monotonic() - last_targets >= 10:
                observations += targets()
                last_targets = time.monotonic()
            if len(sequence) > 3 and kind == "UP" and gap_open and sequence[-3:] == ["UP"] * 3:
                break
            time.sleep(2)
        ok, detail = judge_restart(sequence, gate_503_checks(observations, old_targets))
        self.add("restart probe: the gate answers 503 STARTING until the new task is entitled", ok, detail)
        prefix = template_parts(STRINGS["entitlement_verified"])[0]
        lines = [ts for ts, m in log_events(self.session, self.log_group, probe_since,
                                            '"entitlement verified for NLSQL AI Employee"') if prefix in m]
        ok, detail = judge_entitled_before_up(min(lines) if lines else None, first_up_ms)
        self.add("restart probe: the new task is entitled before /health answers UP", ok, detail)
        self.check_logs_since(probe_since, "the replacement task")

    # 2. edge
    def check_edge(self):
        log("edge:")
        cfn = self.session.client("cloudformation")
        lb_arn = next(r["PhysicalResourceId"] for r in
                      cfn.describe_stack_resources(StackName=self.args.stack)["StackResources"]
                      if r["LogicalResourceId"] == "LoadBalancer")
        lb_dns = self.session.client("elbv2").describe_load_balancers(
            LoadBalancerArns=[lb_arn])["LoadBalancers"][0]["DNSName"]
        st, _, _ = try_http("GET", f"http://{lb_dns}/health", timeout=10)
        self.add("load balancer refuses requests that bypass CloudFront", st in (None, 403),
                 "no connection" if st is None else f"got {st}")

        def alb_403(method, path, **kwargs):
            st, _, body = try_http(method, self.base + path, **kwargs)
            return st == 403 and body.strip() == b"Forbidden", f"{st} {body[:60]!r}"

        internal = [("POST", "/api/prompt", {"body": {"user_id": self.user, "prompt": "hi"}}),
                    ("GET", "/proactive", {}), ("GET", "/validate-vnc", {}), ("GET", "/", {}),
                    ("GET", "/internal/browser-view", {})]
        for method, path, kwargs in internal:
            ok, detail = alb_403(method, path, **kwargs)
            self.add(f"{method} {path}: the load balancer's 403", ok, detail)
        for path in ("/vnc/", "/websockify/1", "/api/browser-use/session"):
            refused, detail = alb_403("GET", path)
            if self.browser:
                self.add(f"GET {path} is routed to the task, with Gemini on or off", not refused, detail)
            else:
                self.add(f"browser agent off: GET {path} gets the load balancer's 403", refused, detail)

        self.edge_since = int(time.time() * 1000) - 2000
        for path in ("/vnc/%2e%2e/api/prompt", "/static/..%2f..%2fapi/prompt",
                     "/websockify/1/..%2f..%2fapi/prompt", "/static/%2e%2e/api/prompt",
                     "/api/messages/..%2fprompt", "/api/browser-use/..%2fprompt",
                     "/api/browser-use/%2e%2e/prompt"):
            st, _, body = try_http("POST", self.base + path, body={"user_id": self.user, "prompt": "hi"})
            quart = b'"error"' in (body or b"") and b"unauthorized" in (body or b"")
            self.add(f"encoded traversal {path} never reaches the server",
                     st in (400, 403, 404, 405) and not quart, f"{st} {(body or b'')[:60]!r}")

        st, _, body = try_http("GET", self.base + "/callback")
        self.add("GET /callback reaches the image (its 400 page without a state)",
                 st == 400 and STRINGS["sign_in_link_invalid"].encode() in body, f"HTTP {st}")
        self.fake_code = "e2e-fake-code-" + secrets.token_hex(12)
        self.forged = forged_state(self.user)
        self.extra_secrets += [self.fake_code, self.forged]
        query = urllib.parse.urlencode({"code": self.fake_code, "state": self.forged})
        st, _, body = try_http("GET", f"{self.base}/callback?{query}")
        self.add("a forged OAuth state gets the 400 page and no sign-in",
                 st == 400 and STRINGS["sign_in_link_invalid"].encode() in body
                 and b"Authentication Successful" not in body, f"HTTP {st}")
        self.add("the forged sign-in wrote nothing",
                 self.get_item(self.outputs["UsersTableName"], "UserData", TEST_OID) is None, "")
        st, _, body = try_http("GET", f"{self.base}/api/browser-use/session?aad_id={TEST_OID}")
        self.add("GET /api/browser-use/session?aad_id= never returns a path",
                 st in (403, 404, 405) and b"websockify" not in (body or b""), f"HTTP {st}")
        st, _, body = try_http("POST", self.endpoint, body={"type": "message", "text": "hi"})
        self.add("/api/messages refuses an activity without a token", st == 401, f"HTTP {st}")
        if self.args.login_url:
            for name, ok, detail in analyse_login_url(self.args.login_url, self.app_id, self.tenant,
                                                      self.outputs["OAuthRedirectUri"]):
                self.add(name, ok, detail)
            state = (urllib.parse.parse_qs(urllib.parse.urlparse(self.args.login_url).query).get("state")
                     or [""])[0]
            self.extra_secrets.append(state)
            tampered = tamper_token(state) if state else forged_state(self.user)
            query = urllib.parse.urlencode({"code": self.fake_code, "state": tampered})
            st, _, body = try_http("GET", f"{self.base}/callback?{query}")
            self.add("a real state with a changed signature gets the 400 page",
                     st == 400 and STRINGS["sign_in_link_invalid"].encode() in body, f"HTTP {st}")

    # 3. conversation
    def start_capture(self):
        log("capture function:")
        self.service_url, self.alt_url, self.capture_log = ensure_capture(self.session, self.args.stack)
        log(f"  serviceUrl {self.service_url}, second host {urllib.parse.urlparse(self.alt_url).netloc}")
        self.token = entra_token(self.tenant, self.app_id, self.secret)
        self.add("the test token is issued to the bot's own app",
                 token_is_for_app(jwt_claims(self.token), self.app_id, self.tenant), "")
        self.since = int(time.time() * 1000) - 5000

    def check_welcome_and_help(self):
        log("conversation:")
        welcome = activity("conversationUpdate", self.conv, self.service_url, self.app_id, self.tenant,
                           self.user, members_added=[{"id": self.user, "name": "E2E Tester"}])
        st, took, body = post_activity(self.endpoint, self.token, welcome)
        self.add("conversationUpdate acknowledged", st in (200, 201, 202) and took < ACK_LIMIT,
                 f"HTTP {st} in {took:.1f}s {'' if st in (200, 201, 202) else body[:200]!r}")
        got = wait_reply(self.session, self.capture_log, self.since, self.conv, welcome["id"], timeout=60)
        self.seen_ids.update(r["returned_id"] for r in got)
        texts = [activity_text(r["final"]) for r in got]
        text = "\n".join(texts)
        sentence = capability_sentence(False, browser=self.browser)
        self.add("welcome: one message, NLSQL AI Employee's first line",
                 len(got) == 1 and STRINGS["welcome"] in text, f"{len(got)} message(s)")
        self.add("welcome: names this stack's features, without Microsoft 365", sentence in text, sentence)
        self.add("welcome: no code mode, no sign-in off a Teams host",
                 not any(w in text for w in ("write code", "code mode", "`login`")), summary(texts))
        if not self.browser:
            self.add("welcome: no browsing with the browser agent off", "browse the web" not in text, "")

        texts, _ = self.say("/help", "/help", want=lambda t: sentence in t, expect="the capability sentence")
        text = "\n".join(texts)
        self.add("/help: no code mode, no sign-in, no /watch off a Teams host",
                 not any(w in text for w in ("code mode", "/projects", "`login`", "/watch")), "")
        self.add("/help: says why there is no sign-in", STRINGS["teams_only_sign_in"] in text, "")
        if not self.browser:
            self.add("/help: no browsing with the browser agent off", "browse the web" not in text, "")

    def check_answer_and_storage(self):
        self.say("answer", "What is 17 multiplied by 23? Answer with the number only.",
                 want=lambda t: "391" in t, expect="391")
        log("storage:")
        ref = self.reference(self.user)
        self.add("DynamoDB: the Teams user's conversation reference", ref.get("channelId") == "msteams",
                 f"channelId {ref.get('channelId')!r}")
        snapshot = self.wait_for(lambda: self.s3_object(f"{self.user}.json"), 60)
        self.add("S3: the conversation snapshot <user>.json within 60 s",
                 snapshot is not None and b"391" in snapshot, "present" if snapshot else "absent")

    def check_identity(self):
        log("identity:")
        texts, _ = self.say("Direct Line sender with the Teams user's ID",
                            "What number did you give me as the answer to my multiplication question earlier in "
                            "this conversation? If there was no earlier question, answer NONE.",
                            conv=self.dl_conv, channel="directline")
        self.add("Direct Line: no Teams history in the answer", texts and not any("391" in t for t in texts),
                 summary(texts))
        dl_user = f"directline:{self.user}"
        ref = self.reference(dl_user)
        self.add("Direct Line: served as directline:<id>, its own reference", ref.get("channelId") == "directline",
                 f"channelId {ref.get('channelId')!r}")
        ref = self.reference(self.user)
        self.add("Direct Line: the Teams user's reference is still Teams'",
                 ref.get("channelId") == "msteams" and (ref.get("conversation") or {}).get("id") == self.conv,
                 f"channelId {ref.get('channelId')!r}")
        snapshot = self.wait_for(lambda: self.s3_object(f"{dl_user}.json"), 60)
        self.add("Direct Line: its own snapshot, without the Teams conversation",
                 snapshot is not None and b"391" not in snapshot and b"17 multiplied" not in snapshot,
                 "present" if snapshot else "absent")
        self.dl_user = dl_user

    def check_login(self):
        texts, answers = self.say("login", "login", want=lambda t: STRINGS["teams_only_sign_in"] in t,
                                  expect="the Teams-only sentence")
        self.add("login: no sign-in link off a Teams host",
                 not any("oauth2/v2.0/authorize" in json.dumps(r["final"]) for r in answers), "")
        if not self.args.login_url:
            log("  info  the sign-in link (tenant, client_id, redirect_uri, scopes, state) needs a real Teams "
                "turn: make e2e-test LOGIN_URL='<the link of a Teams login card>'")

    def check_attachments(self):
        log("attachments:")
        file_url = self.service_url + "files/" + ATTACHMENT_NAME
        q_since = int(time.time() * 1000) - 2000
        self.say("attachment", "What is the secret number in the attached file?",
                 attachments=[file_attachment(file_url, ATTACHMENT_NAME),
                              html_part("What is the secret number in the attached file?")],
                 want=lambda t: "7319" in t, expect="7319")
        # Another host, with Bot Connector words in its path: no token may go there
        leak_url = self.alt_url + "trafficmanager/smba.trafficmanager.net/files/" + LEAK_NAME
        self.say("attachment on another host", "What is the secret number in the attached file?",
                 attachments=[{"contentType": "text/plain", "contentUrl": leak_url, "name": LEAK_NAME}],
                 want=lambda t: "5843" in t, expect="5843")
        records = [r for r in captured(self.session, self.capture_log, q_since) if r["method"] == "GET"]
        own = [r for r in records if r["path"].endswith("/" + ATTACHMENT_NAME)]
        leak = [r for r in records if r["path"].endswith("/" + LEAK_NAME)]
        alt_host = urllib.parse.urlparse(self.alt_url).netloc
        self.add("attachment: fetched without the bot's token (not a Bot Connector host)",
                 bool(own) and not any(r["auth_header_present"] for r in own), f"{len(own)} fetch(es)")
        self.add("attachment on another host: fetched there, without the bot's token",
                 bool(leak) and all(r["host"] == alt_host and r["auth_header_present"] is False for r in leak),
                 f"{len(leak)} fetch(es)")

        refused_since = int(time.time() * 1000) - 2000
        http_url = "http://" + urllib.parse.urlparse(self.service_url).netloc + "/files/e2e-http.txt"
        texts, _ = self.say("refused attachments", "Did my two attached files arrive? Answer YES or NO.",
                            attachments=[{"contentType": "text/plain", "contentUrl": http_url,
                                          "name": "e2e-http.txt"},
                                         {"contentType": "text/plain",
                                          "contentUrl": "https://169.254.169.254/latest/meta-data/",
                                          "name": "e2e-link-local.txt"}])
        lines = self.task_lines(refused_since)
        prefix = template_parts(STRINGS["attachment_refused"])[0]
        self.add("http:// attachment refused", any(prefix + REFUSED_HTTP in line for line in lines), "")
        self.add("link-local attachment refused",
                 any(prefix in line and REFUSED_PRIVATE in line and "169.254.169.254" in line for line in lines), "")
        self.add("the http:// attachment was never fetched",
                 not any(r["path"].endswith("/e2e-http.txt")
                         for r in captured(self.session, self.capture_log, refused_since)), "")
        self.add("refused attachments: an answer and no credentials in it",
                 bool(texts) and not any("AccessKeyId" in t for t in texts), summary(texts))

    def check_schedule(self):
        log("schedule:")
        if self.args.skip_schedule:
            log("  skipped (--skip-schedule): the Teams user's proactive delivery is not checked")
            return
        t0 = time.monotonic()
        sched_since = int(time.time() * 1000) - 2000
        self.say("/schedule", f"/schedule in 2 minutes, reply with the code word {SCHEDULE_WORD}")
        rows = self.wait_for(self.active_tasks, 60)
        self.add("/schedule: an ActiveTasks row for the user", bool(rows), f"{len(rows or [])} row(s)")
        msg = wait_text(self.session, self.capture_log, sched_since, self.conv,
                        lambda t: SCHEDULE_WORD in t, timeout=max(60, 300 - (time.monotonic() - t0)),
                        exclude_ids=self.seen_ids)
        self.add("/schedule: the result arrives in Teams within 5 minutes", msg is not None,
                 f"after {time.monotonic() - t0:.0f}s" if msg else "nothing")
        if msg:
            self.seen_ids.add(msg["returned_id"])
        gone = self.wait_for(lambda: not self.active_tasks(), 60)
        self.add("/schedule: the row is deleted after it ran", gone, "")
        dl = [m for m in conversation_messages(captured(self.session, self.capture_log, sched_since), self.dl_conv)
              if SCHEDULE_WORD in activity_text(m["final"])]
        self.add("identity: the Teams user's proactive message went to Teams only", not dl,
                 f"{len(dl)} in the Direct Line conversation")

    def check_refresh(self):
        log("refresh:")
        self.say("refresh", "refresh", want=lambda t: STRINGS["refreshed"] in t, expect=STRINGS["refreshed"])
        gone = self.wait_for(lambda: self.s3_object(f"{self.user}.json") is None, 60)
        self.add("refresh: the snapshot is deleted", gone, "")
        # The Direct Line user's snapshot goes too, so the run leaves no history behind
        self.say("refresh (Direct Line)", "refresh", conv=self.dl_conv, channel="directline",
                 want=lambda t: STRINGS["refreshed"] in t, expect=STRINGS["refreshed"])

    def check_browser_off(self):
        log("browser agent off:")
        since = int(time.time() * 1000) - 2000
        self.say("browse request", BROWSE_PROMPT)
        lines = self.task_lines(since)
        marker = template_parts(STRINGS["tool_marker"])[0]
        self.add("browser agent off: no use_browser_agent call",
                 not any(marker + "use_browser_agent" in line for line in lines), "")

    def check_gemini_fallback(self, since):
        """Gemini's fallback lines since the browser and image checks began:
        each once at least with --gemini-fallback (the key fails), never
        otherwise (Gemini answers, or Gemini is off)."""
        log("Gemini fallback:")
        lines = self.task_lines(since, wait=10)
        expect = self.args.gemini_fallback
        checks = [("gemini_image_failed", "read_image")]
        if self.browser:
            checks.insert(0, ("gemini_browser_failed", "the browser agent"))
        for key, what in checks:
            prefix = template_parts(STRINGS[key])[0]
            seen = sum(prefix in line for line in lines)
            self.add(f"{what}: Gemini's fallback line {'logged' if expect else 'absent'}",
                     (seen > 0) == expect, f"{seen} line(s)")

    def check_live_view(self):
        log("live browser:")
        since = int(time.time() * 1000) - 2000
        marker = template_parts(STRINGS["tool_marker"])[0]
        texts, answers = self.say("browse request", BROWSE_PROMPT,
                                  want=lambda t: STRINGS["live_browser_ready"] in t,
                                  expect=STRINGS["live_browser_ready"], check_answer=False,
                                  one_answer=False)
        cards = [r for r in conversation_messages(captured(self.session, self.capture_log, since), self.conv)
                 if STRINGS["live_browser_ready"] in activity_text(r["final"])]
        self.add("browse: the 'Your live browser is ready.' card with the Open live browser button",
                 any(has_live_browser_button(c["final"]) for c in cards), f"{len(cards)} card(s)")
        lines = self.task_lines(since, wait=10)
        self.add("browse: the browser agent ran", any(marker + "use_browser_agent" in line for line in lines), "")

        invoke = open_live_browser_invoke(self.conv, self.service_url, self.app_id, self.tenant, self.user)
        st, took, body = post_activity(self.endpoint, self.token, invoke)
        try:
            task = json.loads(body or b"{}").get("task") or {}
        except ValueError:
            task = {}
        value = task.get("value") or {}
        url = value.get("url", "") if isinstance(value, dict) else ""
        self.add("Open live browser: task/fetch answered with the dialog at once",
                 st == 200 and took < ACK_LIMIT and task.get("type") == "continue"
                 and value.get("title") == STRINGS["live_browser_title"]
                 and url.startswith(self.base + "/vnc/?t="),
                 f"HTTP {st} in {took:.1f}s, {task.get('type')!r}")
        token = (urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("t") or [""])[0]
        if not token:
            self.add("live view: a viewer token", False, "the dialog had no URL")
            return
        self.extra_secrets.append(token)

        st, _, body = try_http("POST", self.base + "/api/browser-use/session", body={"token": token})
        try:
            lookup = json.loads(body or b"{}")
        except ValueError:
            lookup = {}
        path = lookup.get("path", "")
        m = re.fullmatch(r"websockify/([A-Za-z0-9_-]{22})\?token=(.+)", path)
        self.add("session lookup: websockify/<sid>?token= for the live session",
                 st == 200 and lookup.get("active") is True and bool(m) and m.group(2) == token, f"HTTP {st}")
        if not m:
            return
        sid = m.group(1)
        st, _, _ = try_http("POST", self.base + "/api/browser-use/session", body={"token": tamper_token(token)})
        self.add("session lookup: 403 for a tampered token", st == 403, f"HTTP {st}")

        for label, ws_path in (("no token", f"/websockify/{sid}"),
                               ("a tampered token", f"/websockify/{sid}?token={tamper_token(token)}"),
                               ("another sid", f"/websockify/{secrets.token_urlsafe(16)}?token={token}")):
            try:
                st, _, ws = ws_connect(self.host, ws_path)
                if ws:
                    ws.close()
                    ws.sock.close()
            except (OSError, ValueError, WebSocketClosed) as exc:
                st = type(exc).__name__
            self.add(f"WSS with {label}: 403", st == 403, f"{st}")

        self.live_view_idle(sid, token, since)

        t0 = time.monotonic()
        closed_at = int(time.time() * 1000)
        st, _, body = try_http("POST", self.base + "/api/browser-use/close-session", body={"token": token},
                               timeout=60)
        self.add("close-session: success within 60 s", st == 200 and b"success" in (body or b""),
                 f"HTTP {st} in {time.monotonic() - t0:.1f}s")
        st, _, body = try_http("POST", self.base + "/api/browser-use/session", body={"token": token})
        try:
            active = json.loads(body or b"{}").get("active")
        except ValueError:
            active = None
        self.add("session lookup after close: not active", st == 200 and active is False, f"HTTP {st}")
        try:
            st, _, ws = ws_connect(self.host, f"/websockify/{sid}?token={token}")
            if ws:
                ws.close()
                ws.sock.close()
        except (OSError, ValueError, WebSocketClosed) as exc:
            st = type(exc).__name__
        self.add("WSS with the stale token: 403", st == 403, f"{st}")
        time.sleep(60)
        after = [r for r in conversation_messages(captured(self.session, self.capture_log, closed_at), self.conv,
                                                   self.seen_ids) if r["ts"] >= closed_at]
        self.add("close-session: no result message after it", not after, summary(
            [activity_text(r["final"]) for r in after]))
        replies = conversation_messages(captured(self.session, self.capture_log, since), self.conv)
        self.add("live view: no viewer link or token in the Teams chat",
                 not any("/vnc/?t=" in json.dumps(r["final"]) or token in json.dumps(r["final"])
                         for r in replies), "")

    def live_view_idle(self, sid, token, since):
        """WSS through CloudFront, the RFB handshake, then idle. While it idles
        the browser agent waits on its help request; a 'not yet' before the
        image's reply timeout makes it ask again, so the session outlives the
        idle time."""
        result = {}

        def idle():
            try:
                st, headers, ws = ws_connect(self.host, f"/websockify/{sid}?token={token}")
                result["status"] = st
                result["protocol"] = headers.get("sec-websocket-protocol", "")
                if ws is None:
                    return
                result["rfb"] = rfb_handshake(ws)
                result["idle"] = ws_idle(ws, self.args.ws_idle)
                result["pings"] = ws.pings
                ws.close()
                ws.sock.close()
            except Exception as exc:  # reported below
                result["error"] = f"{type(exc).__name__}: {exc}"

        worker = threading.Thread(target=idle, daemon=True)
        worker.start()
        asked = wait_text(self.session, self.capture_log, since, self.conv,
                          lambda t: STRINGS["help_request_teams"] in t, timeout=300, exclude_ids=self.seen_ids)
        self.add("help request: 'Select **Open live browser** below...' with the button, no link",
                 asked is not None and has_live_browser_button(asked["final"]), "")
        def asked_at(msg):
            # From when the agent asked (the capture's timestamp), so the
            # margin under BROWSER_USER_REPLY_TIMEOUT holds
            return time.monotonic() - max(0.0, time.time() - msg["ts"] / 1000) if msg else time.monotonic()

        last_ask = asked_at(asked)
        keepalives = 0
        while worker.is_alive():
            worker.join(15)
            if asked and worker.is_alive() and time.monotonic() - last_ask > KEEPALIVE_INTERVAL:
                self.seen_ids.add(asked["returned_id"])
                keep_since = int(time.time() * 1000) - 2000
                act = activity("message", self.conv, self.service_url, self.app_id, self.tenant, self.user,
                               text=KEEPALIVE_REPLY)
                st, took, _ = post_activity(self.endpoint, self.token, act)
                self.add(f"keep-alive reply {keepalives + 1}: acknowledged at once",
                         st in (200, 201, 202) and took < ACK_LIMIT, f"HTTP {st} in {took:.1f}s")
                keepalives += 1
                asked = wait_text(self.session, self.capture_log, keep_since, self.conv,
                                  lambda t: STRINGS["help_request_teams"] in t, timeout=300,
                                  exclude_ids=self.seen_ids)
                last_ask = asked_at(asked)
                for r in replies_to(captured(self.session, self.capture_log, keep_since), self.conv, act["id"]):
                    self.seen_ids.add(r["returned_id"])
        self.add("WSS through CloudFront: 101, protocol binary",
                 result.get("status") == 101 and result.get("protocol") == "binary",
                 result.get("error") or f"{result.get('status')}")
        rfb = result.get("rfb") or {}
        self.add("RFB: greeting and ServerInit over the WebSocket", bool(rfb),
                 f"{rfb.get('version')} {rfb.get('width')}x{rfb.get('height')}" if rfb else result.get("error", ""))
        stayed, elapsed, reason = result.get("idle") or (False, 0, result.get("error", "not reached"))
        self.add(f"WSS idle for {self.args.ws_idle} s through CloudFront and the load balancer", stayed,
                 f"{elapsed:.0f}s, {result.get('pings', 0)} ping(s), {keepalives} keep-alive reply(ies)"
                 f"{', ' + reason if reason else ''}")
        if self.args.ws_idle < WS_IDLE_DEFAULT:
            log(f"  note: idle {self.args.ws_idle} s is shorter than S4's 15 minutes")

    def check_images(self):
        log("read_image:")
        marker = template_parts(STRINGS["tool_marker"])[0]
        key = f"{self.user}.json"

        def snapshot_text():
            data = self.s3_object(key)
            return data.decode("utf-8", "replace") if data else ""

        for name, word in IMAGE_WORDS.items():
            since = int(time.time() * 1000) - 2000
            before = snapshot_text()
            url = self.service_url + "files/" + name
            texts, _ = self.say(f"read_image {name}", IMAGE_PROMPT,
                                attachments=[{"contentType": IMAGE_TYPES[name], "contentUrl": url, "name": name}],
                                want=lambda t, w=word: word_answered(w, t), expect=word, check_answer=False)
            # The snapshot is written behind the turn; it keeps the tool's output
            after = self.wait_for(lambda: (lambda s: s if s != before else "")(snapshot_text()), 60) or before
            for check in judge_image(name, word, texts, before, after):
                self.add(*check)
            lines = self.task_lines(since, wait=10)
            self.add(f"read_image {name}: the tool ran", any(marker + "read_image" in line for line in lines), "")

    # 11. logs
    def check_logs(self, test_since):
        log("logs:")
        extra = list(self.extra_secrets)
        arn = self.outputs.get("EncryptionKeySecretArn")
        if arn:
            extra.append(self.session.client("secretsmanager").get_secret_value(SecretId=arn)["SecretString"])
        values = secret_values(self.env, [self.secret, *extra])
        mask_values(values)
        self.hygiene_values = values
        time.sleep(20)
        events = log_events(self.session, self.log_group, self.created_ms)
        messages = [m for _, m in events]
        problems = hygiene_problems(messages, values)
        self.add("log hygiene since the stack was created", not problems,
                 f"{len(messages)} events, {len(values)} secret values; "
                 + (", ".join(f"{n}: {c}" for n, c in sorted(problems.items())) or "clean"))
        recent = [m for ts, m in events if ts >= test_since]
        for line in ERROR_LINES:
            hits = sum(line in m for m in recent)
            self.add(f"no '{line}' since the test began", hits == 0, f"{hits} line(s)")
        for line in BOOT_ERROR_LINES:
            hits = sum(line in m for m in messages)
            self.add(f"no '{line}' since the stack was created", hits == 0, f"{hits} line(s)")
        # The start-up lines of the running task only: earlier tasks of a
        # switched stack logged other models and features
        ecs = self.session.client("ecs")
        cluster, service = self.outputs["ClusterName"], self.outputs["ServiceName"]
        arns = ecs.list_tasks(cluster=cluster, serviceName=service, desiredStatus="RUNNING")["taskArns"]
        tasks = ecs.describe_tasks(cluster=cluster, tasks=arns)["tasks"] if arns else []
        since = startup_since(tasks, self.created_ms)
        boot = [m for ts, m in events if ts >= since]
        entitled = template_parts(STRINGS["entitlement_verified"])
        self.add("'entitlement verified for NLSQL AI Employee' logged by the running task",
                 any(entitled[0] in m and f"product {PRODUCT_CODE}" in m for m in boot), "")
        model = self.params.get("LanguageModel", "")
        probe = STRINGS["bedrock_probe_ok"].replace("<model>", model)
        self.add(f"'{probe}' logged by the running task", any(probe in m for m in boot), "")
        self.add("'Features: code mode is off' logged by the running task",
                 any(STRINGS["coder_off"] in m for m in boot), "")
        browser_off = any(STRINGS["browser_off"] in m for m in boot)
        self.add("'Features: the browser agent is off' "
                 f"{'logged' if not self.browser else 'absent'} in the running task's start-up",
                 browser_off != self.browser, "")
        gemini_unset = any(STRINGS["gemini_unset"] in m for m in boot)
        self.add("'Features: no GEMINI_API_KEY or GEMINI_MODEL' "
                 f"{'absent' if self.gemini else 'logged'} in the running task's start-up",
                 gemini_unset != self.gemini, "")
        replies = captured(self.session, self.capture_log, self.since)
        self.add("no AccessKeyId in any reply", not any("AccessKeyId" in json.dumps(r["activity"]) for r in replies),
                 f"{len(replies)} captured request(s)")

    def check_logs_since(self, since_ms, what):
        """Log hygiene and the boot errors again, for lines check_logs could
        not see (the restart probe's replacement task)."""
        time.sleep(20)
        messages = [m for _, m in log_events(self.session, self.log_group, since_ms)]
        problems = hygiene_problems(messages, getattr(self, "hygiene_values", ()))
        self.add(f"log hygiene of {what}", not problems,
                 f"{len(messages)} events; "
                 + (", ".join(f"{n}: {c}" for n, c in sorted(problems.items())) or "clean"))
        for line in BOOT_ERROR_LINES:
            hits = sum(line in m for m in messages)
            self.add(f"no '{line}' from {what}", hits == 0, f"{hits} line(s)")


def run_checks(args, session, env, app_id, secret, tenant):
    """Every check against the deployed stack; returns the Result."""
    run = Run(args, session, env, app_id, secret, tenant)
    test_since = int(time.time() * 1000) - 5000
    run.load_stack()
    run.check_health()
    run.check_edge()
    run.start_capture()
    run.check_welcome_and_help()
    run.check_answer_and_storage()
    run.check_identity()
    run.check_login()
    run.check_attachments()
    run.check_schedule()
    browser_since = int(time.time() * 1000) - 2000
    if run.browser:
        run.check_live_view()
    else:
        run.check_browser_off()
    run.check_images()
    run.check_gemini_fallback(browser_since)
    run.check_refresh()
    run.check_logs(test_since)
    if args.restart_probe:
        run.restart_probe()
    return run.res


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("env_file", nargs="?")
    ap.add_argument("--stack", default=os.environ.get("STACK_NAME") or "nlsql-ai-employee-test")
    ap.add_argument("--region", default=os.environ.get("AWS_REGION") or "us-east-1")
    ap.add_argument("--skip-schedule", action="store_true",
                    help="skip /schedule (about 3 minutes) and with it the proactive delivery check")
    ap.add_argument("--restart-probe", action="store_true",
                    help="at the end, stop the task and check /health's 503 sequence until the new one is UP")
    ap.add_argument("--ws-idle", type=int, default=WS_IDLE_DEFAULT, metavar="SECONDS",
                    help=f"how long the live view's WebSocket stays idle (default {WS_IDLE_DEFAULT}, "
                         "S4's 15 minutes; shorter for a quick run)")
    ap.add_argument("--login-url", default=os.environ.get("LOGIN_URL", ""),
                    help="the sign-in link of a real Teams login card, to check its tenant, client_id, "
                         "redirect_uri, scopes and state (default $LOGIN_URL, which keeps its state off "
                         "the command line)")
    ap.add_argument("--teardown", action="store_true", help="remove the capture function and role, then exit")
    ap.add_argument("--keep-capture", action="store_true", help="leave the capture function in place")
    ap.add_argument("--gemini-fallback", action="store_true",
                    help="Gemini on with a key that fails: expect the browser agent and read_image to log "
                         "Gemini's failure and answer from Bedrock")
    args = ap.parse_args(argv)
    if args.ws_idle < 1:
        ap.error("--ws-idle must be at least 1 second")
    if not args.teardown and not args.env_file:
        ap.error("env_file is required")
    return args


def main(argv=None):
    args = parse_args(argv)
    if boto3 is None:
        sys.exit("scripts/e2e-test.py needs boto3 (pip install boto3)")
    session = boto3.Session(region_name=args.region)
    if args.teardown:
        teardown(session, args.stack)
        return 0
    env = read_env(args.env_file)
    app_id = env.get("connections__serviceConnection__settings__clientId") or env.get("MicrosoftAppId", "")
    secret = env.get("connections__serviceConnection__settings__clientSecret") or env.get("MicrosoftAppPassword", "")
    tenant = env.get("connections__serviceConnection__settings__tenantId") or env.get("MicrosoftAppTenantId", "")
    if not (app_id and secret and tenant):
        sys.exit("the env file lacks the bot's clientId, clientSecret or tenantId")

    refused = False
    try:
        res = run_checks(args, session, env, app_id, secret, tenant)
    except NotTestTagged:
        refused = True  # nothing of ours to remove, and teardown would refuse it too
        raise
    finally:
        # Also when a check crashes: the capture function is a public URL.
        if not args.keep_capture and not refused:
            teardown(session, args.stack)

    log("")
    log(f"summary: {len(res.rows) - len(res.failed)} passed, {len(res.failed)} failed")
    return 1 if res.failed else 0


if __name__ == "__main__":
    sys.exit(main())
