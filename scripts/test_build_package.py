#!/usr/bin/env python3
"""Offline tests of teams/build-package.sh and teams/manifest.template.json.

Run: python3 -m unittest discover -s scripts -p 'test_*.py'

The script runs with a PATH of its own: links to the few tools it uses, and
a fake `aws` that records its arguments and prints canned describe-stacks
JSON, so no test can reach AWS. Each case runs with jq and with only python3,
and with zip and without it, when this machine has them. All IDs are fake.
"""
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TEAMS = REPO / "teams"
SCRIPT = TEAMS / "build-package.sh"

APP_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeee1"
DOMAIN = "d111111abcdef8.cloudfront.net"
COMMANDS = ["/help", "login", "refresh", "/schedule", "/tasks", "/memory", "/briefing", "/watch"]
TOOLS = ["bash", "dirname", "mkdir", "mktemp", "rm", "cp", "sed"]


def stack_json(app_id=APP_ID, domain=DOMAIN, browser_view_url=None):
    outputs = [{"OutputKey": "CloudFrontDomain", "OutputValue": domain},
               {"OutputKey": "MessagingEndpoint", "OutputValue": f"https://{domain}/api/messages"}]
    if browser_view_url:
        outputs.append({"OutputKey": "BrowserViewUrl", "OutputValue": browser_view_url})
    return {"Stacks": [{"Parameters": [{"ParameterKey": "GeminiFeatures", "ParameterValue": "Disabled"},
                                       {"ParameterKey": "MicrosoftAppId", "ParameterValue": app_id}],
                        "Outputs": outputs}]}


def renderers():
    """(with jq, with zip) combinations this machine can run."""
    combos = [(False, False)]
    if shutil.which("zip"):
        combos.append((False, True))
    if shutil.which("jq"):
        combos.append((True, bool(shutil.which("zip"))))
    return combos


class TemplateTest(unittest.TestCase):
    def test_template(self):
        raw = (TEAMS / "manifest.template.json").read_text(encoding="utf-8")
        self.assertTrue(all(c == "\n" or " " <= c <= "~" for c in raw), "non-printable-ASCII text")
        m = json.loads(raw)
        self.assertEqual(m["manifestVersion"], "1.19")
        self.assertEqual(m["id"], "<MicrosoftAppId>")
        self.assertLessEqual(len(m["name"]["short"]), 30)
        self.assertLessEqual(len(m["name"]["full"]), 100)
        self.assertLessEqual(len(m["description"]["short"]), 80)
        self.assertLessEqual(len(m["description"]["full"]), 4000)
        bot, = m["bots"]
        self.assertEqual(bot["botId"], "<MicrosoftAppId>")
        self.assertEqual(bot["scopes"], ["personal"])
        self.assertIs(bot["supportsFiles"], True)
        commands, = bot["commandLists"]
        self.assertEqual(commands["scopes"], ["personal"])
        self.assertEqual([c["title"] for c in commands["commands"]], COMMANDS)
        for c in commands["commands"]:
            self.assertLessEqual(len(c["title"]), 32)
            self.assertLessEqual(len(c["description"]), 128)
        self.assertEqual(m["validDomains"], ["<CloudFrontDomain>"])
        self.assertEqual([t["entityId"] for t in m["staticTabs"]], ["conversations", "browser-use"])
        tab = m["staticTabs"][1]
        self.assertEqual(tab["name"], "Browser-Use")
        self.assertEqual(tab["contentUrl"], "https://<CloudFrontDomain>/vnc/")
        self.assertNotIn("webApplicationInfo", m)
        for icon in ("color.png", "outline.png"):
            self.assertTrue((TEAMS / icon).is_file())

    def test_icon_sizes(self):
        # PNG IHDR: width and height at bytes 16-24, then bit depth and colour type 6 (RGBA)
        for icon, size in (("color.png", 192), ("outline.png", 32)):
            head = (TEAMS / icon).read_bytes()[:26]
            self.assertEqual(head[:8], b"\x89PNG\r\n\x1a\n", icon)
            self.assertEqual(int.from_bytes(head[16:20], "big"), size, icon)
            self.assertEqual(int.from_bytes(head[20:24], "big"), size, icon)
            self.assertEqual(head[25], 6, f"{icon} is not RGBA")


class BuildPackageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.out = self.dir / "out"
        self.home = self.dir / "home"
        self.home.mkdir()
        self.aws_log = self.dir / "aws-calls"
        self.aws_reply = self.dir / "aws-reply.json"

    def bin_dir(self, jq, zip_, aws):
        """A PATH holding only the tools the script may use."""
        name = f"bin-{int(jq)}{int(zip_)}{int(aws)}"
        path = self.dir / name
        if path.exists():
            return path
        path.mkdir()
        tools = TOOLS + ["python3"] + (["jq"] if jq else []) + (["zip"] if zip_ else [])
        for tool in tools:
            found = shutil.which(tool)
            self.assertIsNotNone(found, f"{tool} is not installed")
            (path / tool).symlink_to(found)
        if aws:
            fake = path / "aws"
            fake.write_text(
                "#!/bin/sh\n"
                f"printf '%s\\n' \"$*\" >> '{self.aws_log}'\n"
                f"[ -f '{self.aws_reply}' ] || exit 254\n"
                f"exec /bin/cat '{self.aws_reply}'\n")
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        return path

    def run_script(self, *args, jq=False, zip_=False, aws=True, env=None, reply=None):
        if reply is not None:
            self.aws_reply.write_text(json.dumps(reply))
        environ = {
            "PATH": str(self.bin_dir(jq, zip_, aws)),
            "HOME": str(self.home),
            "AWS_CONFIG_FILE": "/dev/null",
            "AWS_SHARED_CREDENTIALS_FILE": "/dev/null",
            "AWS_EC2_METADATA_DISABLED": "true",
            "LC_ALL": "C",
        }
        environ.update(env or {})
        return subprocess.run([shutil.which("bash"), str(SCRIPT), "--out-dir", str(self.out), *args],
                              env=environ, capture_output=True, text=True, timeout=60)

    def built(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = json.loads((self.out / "manifest.json").read_text(encoding="utf-8"))
        with zipfile.ZipFile(self.out / "nlsql-ai-employee-teams.zip") as z:
            self.assertEqual(sorted(z.namelist()), ["color.png", "manifest.json", "outline.png"])
            self.assertEqual(json.loads(z.read("manifest.json")), manifest)
            for icon in ("color.png", "outline.png"):
                self.assertEqual(z.read(icon), (TEAMS / icon).read_bytes())
        text = json.dumps(manifest)
        self.assertNotIn("<MicrosoftAppId>", text)
        self.assertNotIn("<CloudFrontDomain>", text)
        return manifest

    def aws_calls(self):
        return self.aws_log.read_text().splitlines() if self.aws_log.exists() else []

    def test_offline_the_browser_view_tab_is_added_by_default(self):
        # BrowserAgent Enabled, the default, gives the stack its live browser view
        for jq, zip_ in renderers():
            with self.subTest(jq=jq, zip=zip_):
                m = self.built(self.run_script("--app-id", APP_ID, "--domain", DOMAIN, jq=jq, zip_=zip_))
                self.assertEqual(m["id"], APP_ID)
                self.assertEqual(m["bots"][0]["botId"], APP_ID)
                self.assertEqual(m["validDomains"], [DOMAIN])
                self.assertEqual(m["version"], "1.0.0")
                tabs = {t["entityId"]: t for t in m["staticTabs"]}
                self.assertEqual(tabs["browser-use"]["contentUrl"], f"https://{DOMAIN}/vnc/")
        self.assertEqual(self.aws_calls(), [], "the offline form called aws")

    def test_offline_without_browser_view(self):
        for jq, zip_ in renderers():
            with self.subTest(jq=jq, zip=zip_):
                m = self.built(self.run_script("--app-id", APP_ID, "--domain", DOMAIN, "--no-browser-view",
                                               jq=jq, zip_=zip_))
                self.assertEqual(m["validDomains"], [DOMAIN])
                self.assertNotIn("staticTabs", m)
        self.assertEqual(self.aws_calls(), [], "the offline form called aws")

    def test_offline_with_browser_view_and_version(self):
        for jq, zip_ in renderers():
            with self.subTest(jq=jq, zip=zip_):
                m = self.built(self.run_script("--app-id", APP_ID, "--domain", DOMAIN, "--browser-view",
                                               "--version", "1.0.7", jq=jq, zip_=zip_))
                self.assertEqual(m["version"], "1.0.7")
                tabs = {t["entityId"]: t for t in m["staticTabs"]}
                self.assertEqual(tabs["browser-use"]["contentUrl"], f"https://{DOMAIN}/vnc/")
                self.assertEqual(tabs["browser-use"]["name"], "Browser-Use")
        self.assertEqual(self.aws_calls(), [])

    def test_app_id_from_env(self):
        for name in ("connections__serviceConnection__settings__clientId", "MicrosoftAppId"):
            with self.subTest(name=name):
                m = self.built(self.run_script("--domain", DOMAIN, env={name: APP_ID}))
                self.assertEqual(m["id"], APP_ID)
        self.assertEqual(self.aws_calls(), [])

    def test_stack_with_the_browser_view_output(self):
        for jq, zip_ in renderers():
            with self.subTest(jq=jq, zip=zip_):
                m = self.built(self.run_script("--stack", "ai-employee", "--region", "eu-west-1", jq=jq, zip_=zip_,
                                               reply=stack_json(browser_view_url=f"https://{DOMAIN}/vnc/")))
                self.assertEqual(m["id"], APP_ID)
                self.assertEqual(m["validDomains"], [DOMAIN])
                self.assertIn("browser-use", [t["entityId"] for t in m["staticTabs"]])
        self.assertEqual(set(self.aws_calls()),
                         {"cloudformation describe-stacks --stack-name ai-employee --region eu-west-1 --output json"})

    def test_stack_without_the_browser_view_output(self):
        # BrowserAgent Disabled (or GeminiFeatures Disabled before template 1.0.2)
        for jq, zip_ in renderers():
            with self.subTest(jq=jq, zip=zip_):
                m = self.built(self.run_script("--stack", "ai-employee", jq=jq, zip_=zip_, reply=stack_json()))
                self.assertNotIn("staticTabs", m)
        self.assertEqual(set(self.aws_calls()),
                         {"cloudformation describe-stacks --stack-name ai-employee --output json"})

    def test_command_line_wins_over_stack(self):
        other = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeee2"
        m = self.built(self.run_script("--stack", "s", "--app-id", other, "--no-browser-view",
                                       reply=stack_json(browser_view_url=f"https://{DOMAIN}/vnc/")))
        self.assertEqual(m["id"], other)
        self.assertNotIn("staticTabs", m)

    def test_refusals(self):
        cases = [
            (("--app-id", "not-a-guid", "--domain", DOMAIN), "GUID"),
            (("--app-id", APP_ID, "--domain", f"https://{DOMAIN}"), "CloudFrontDomain"),
            (("--app-id", APP_ID, "--domain", "bot.example.com"), "CloudFrontDomain"),
            (("--app-id", APP_ID, "--domain", DOMAIN, "--version", "1.0"), "x.y.z"),
            (("--app-id", APP_ID), "no CloudFront domain"),
            (("--domain", DOMAIN), "no bot app ID"),
            (("--app-id", APP_ID, "--domain", DOMAIN, "--region", "us-east-1"), "--region needs --stack"),
            (("--bogus",), "unknown argument"),
        ]
        for args, message in cases:
            with self.subTest(args=args):
                result = self.run_script(*args)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)
                self.assertFalse((self.out / "nlsql-ai-employee-teams.zip").exists())
        self.assertEqual(self.aws_calls(), [])

    def test_stack_refusals(self):
        result = self.run_script("--stack", "s", reply=stack_json(browser_view_url="https://elsewhere/vnc/"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("BrowserViewUrl", result.stderr)
        self.aws_reply.unlink()
        result = self.run_script("--stack", "missing")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot read stack missing", result.stderr)
        result = self.run_script("--stack", "s", aws=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("needs the AWS CLI", result.stderr)
        self.assertFalse((self.out / "nlsql-ai-employee-teams.zip").exists())


if __name__ == "__main__":
    unittest.main()
