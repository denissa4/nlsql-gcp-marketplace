#!/usr/bin/env python3
"""Offline tests of scripts/push-guard.sh, the image guard of `make push`,
and of `make check-pushed`.

Run: python3 -m unittest discover -s scripts -p 'test_*.py'

The guard runs with a fake `docker` first on PATH. It answers `image inspect`
from a canned description of the image and `run` with canned probe output,
and records its arguments, so no test needs Docker, an image or a network.
Each case breaks one rule of a good image and expects that rule's refusal.
check-pushed runs against a fake `docker buildx imagetools inspect --raw`
that prints canned manifests. All keys, codes and references are fake.
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
SCRIPT = REPO / "scripts" / "push-guard.sh"

CODE = "f5jcvx1rl6wb66fhzy5qycpnr"
KEY = "1" * 64
PLACEHOLDER = "c" * 64
PEM = "/app/marketplace/aws_marketplace_pubkey_v1.pem"
VERSION = "9.9.9"
IMAGE = f"ai-employee-aws:{VERSION}"
IMAGE_ID = "sha256:" + "d" * 64
# The environment of the application's runtime stage (names only matter).
RUNTIME_ENV = ["PATH=/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "PYTHONUNBUFFERED=1",
               "PYTHON_DOTENV_DISABLED=1", "TIKTOKEN_CACHE_DIR=/opt/tiktoken-cache", "ANONYMIZED_TELEMETRY=false",
               "BROWSER_EXECUTABLE_PATH=/usr/local/bin/ai-employee-chromium", "HOME=/home/nlsql"]
OVERLAY2 = '[["Backing Filesystem","extfs"],["Supports d_type","true"]]'
CONTAINERD = '[["driver-type","io.containerd.snapshotter.v1"]]'

FAKE_DOCKER = r'''#!/usr/bin/env python3
import json, os, sys
state = json.load(open(os.environ["FAKE_DOCKER_STATE"]))
with open(os.environ["FAKE_DOCKER_LOG"], "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\n")
args = sys.argv[1:]
if args[:2] == ["image", "inspect"]:
    if not state["exists"]:
        sys.exit(1)
    if "-f" not in args:
        print(state["raw"])
        sys.exit(0)
    fmt = args[args.index("-f") + 1]
    if ".Config.Env" in fmt:
        sys.stdout.write("".join(json.dumps(e) + "\n" for e in state["env"]))
        sys.exit(0)
    for marker, key in ((".Id", "id"), ("com.nlsql.stage", "stage"), ("pubkey-sha256", "label_key"),
                        (".Os", "platform"), (".Config.User", "user"), ("ExposedPorts", "ports"),
                        (".Config.Volumes", "volumes"), ("image.version", "version_label"),
                        (".Config.Entrypoint", "entrypoint"), (".Config.Cmd", "cmd")):
        if marker in fmt:
            print(state[key])
            sys.exit(0)
    sys.exit("unexpected format " + fmt)
if args[:1] == ["info"]:
    if state["drivers"] is None:
        sys.exit(1)
    print(state["drivers"])
    sys.exit(0)
if args[:1] == ["run"]:
    sys.stdout.write(state["run"])
    sys.exit(state["run_rc"])
sys.exit("unexpected docker call: " + " ".join(args))
'''


def good_image():
    return {"exists": True, "id": IMAGE_ID, "raw": json.dumps([{"Id": IMAGE_ID}]), "stage": "enforce", "label_key": KEY, "platform": "linux/amd64",
            "user": "10001", "ports": '{"8080/tcp":{}}', "volumes": "null", "version_label": VERSION, "run_rc": 0,
            "entrypoint": '["/usr/bin/tini","--"]', "cmd": '["/app/startup.sh"]', "env": RUNTIME_ENV,
            "drivers": OVERLAY2,
            "pem": KEY, "placeholder": PLACEHOLDER, "python": f"{CODE} | {CODE} | {CODE}", "node": f'["{CODE}"] ',
            "extra": [f"file {PEM}"]}


def probe_output(image):
    lines = [f"pem {image['pem']}"]
    if image["placeholder"] is not None:
        lines.append(f"placeholder {image['placeholder']}")
    if image["python"] is not None:
        lines.append(f"python {image['python']}")
    lines.append(f"node {image['node']}")
    return "\n".join(lines + image["extra"] + ["done"]) + "\n"


class PushGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.tmp)], check=False)
        fake = self.tmp / "bin" / "docker"
        fake.parent.mkdir()
        fake.write_text(FAKE_DOCKER, encoding="utf-8")
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        self.log = self.tmp / "docker.log"

    def guard(self, image, name=IMAGE, pinned=KEY, version=VERSION):
        state = dict(image, run=probe_output(image))
        state_file = self.tmp / "state.json"
        state_file.write_text(json.dumps(state), encoding="utf-8")
        self.id_file = self.tmp / "guarded-id"
        self.id_file.write_text("stale\n", encoding="utf-8")
        env = {"PATH": f"{self.tmp / 'bin'}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
               "HOME": str(self.tmp), "FAKE_DOCKER_STATE": str(state_file), "FAKE_DOCKER_LOG": str(self.log),
               "PUSH_GUARD_ID_FILE": str(self.id_file)}
        return subprocess.run(["bash", str(SCRIPT), name, CODE, pinned, version], env=env,
                              capture_output=True, text=True, timeout=60)

    def assertRefused(self, result, *needles):
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("refused", result.stderr)
        self.assertFalse(self.id_file.exists(), "a refused image must leave no ID to push")
        for needle in needles:
            self.assertIn(needle, result.stderr)

    def test_good_image_passes_and_runs_offline_as_root(self):
        result = self.guard(good_image())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"product {CODE}, public key {KEY}", result.stdout)
        runs = [json.loads(line) for line in self.log.read_text().splitlines() if line.startswith('["run"')]
        self.assertEqual(len(runs), 1)
        run = " ".join(runs[0])
        for flag in ("--rm", "--network none", "--pull never", "--platform linux/amd64", "--user 0:0",
                     "--entrypoint sh"):
            self.assertIn(flag, run)

    def test_checks_and_records_the_image_id_not_the_name(self):
        result = self.guard(good_image())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.id_file.read_text(encoding="utf-8").strip(), IMAGE_ID)
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        by_name = [c for c in calls if IMAGE in c]
        self.assertEqual(len(by_name), 2, by_name)  # the two lookups that resolve the name
        self.assertTrue(all(c[:2] == ["image", "inspect"] for c in by_name))
        run = next(c for c in calls if c[0] == "run")
        self.assertIn(IMAGE_ID, run)

    def test_index_is_refused(self):
        for media_type in ("application/vnd.oci.image.index.v1+json",
                           "application/vnd.docker.distribution.manifest.list.v2+json"):
            raw = json.dumps([{"Id": IMAGE_ID, "Descriptor": {"mediaType": media_type}}], indent=4)
            self.assertRefused(self.guard(dict(good_image(), raw=raw)), "is an index")
        raw = json.dumps([{"Descriptor": {"mediaType": "application/vnd.oci.image.manifest.v1+json"}}])
        self.assertEqual(self.guard(dict(good_image(), raw=raw)).returncode, 0)

    def test_containerd_store_without_descriptor_fails_closed(self):
        for drivers in (CONTAINERD, None):
            self.assertRefused(self.guard(dict(good_image(), drivers=drivers)), "does not report the image's Descriptor")
        raw = json.dumps([{"Id": IMAGE_ID, "Descriptor": {"mediaType": "application/vnd.oci.image.manifest.v1+json"}}])
        self.assertEqual(self.guard(dict(good_image(), drivers=CONTAINERD, raw=raw)).returncode, 0)

    def test_entrypoint_and_command(self):
        for entrypoint in ("null", "[]", '["/bin/sh","-c"]', '["/usr/bin/tini"]'):
            self.assertRefused(self.guard(dict(good_image(), entrypoint=entrypoint)),
                               f"the entrypoint is {entrypoint}, not")
        for cmd in ('["python","-m","pytest","tests/e2e"]', "null", '["/app/startup.sh","x"]', '["sh"]'):
            self.assertRefused(self.guard(dict(good_image(), cmd=cmd)), f"the command is {cmd}, not")

    def test_secret_names_in_the_environment(self):
        for name in ("VNC_SECRET_KEY", "ENCRYPTION_KEY", "INTERNAL_AUTH_SECRET", "GEMINI_API_KEY", "AWS_ACCESS_KEY_ID",
                     "AWS_REGION", "DB_PASSWORD", "GITHUB_TOKEN", "AWS_SHARED_CREDENTIALS_FILE", "APP_CREDENTIAL",
                     "PRIVATE", "vnc_secret_key", "PASSWD"):
            image = dict(good_image(), env=RUNTIME_ENV + [f"{name}=x"])
            result = self.guard(image)
            self.assertRefused(result, f"sets {name} in its environment")
            self.assertNotIn(f"{name}=x", result.stderr)
        # TIKTOKEN_CACHE_DIR, KEYBOARD_LAYOUT and TOKENIZERS_* hold a secret word only inside a longer one.
        image = dict(good_image(), env=RUNTIME_ENV + ["KEYBOARD_LAYOUT=us", "MONKEY=1", "TOKENIZERS_PARALLELISM=false"])
        result = self.guard(image)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_placeholder_secret_in_the_environment(self):
        for value in ("change-me-in-production", "prefix-CHANGE-ME-IN-PRODUCTION"):
            image = dict(good_image(), env=RUNTIME_ENV + [f"VNC_SIGNER={value}"])
            result = self.guard(image)
            self.assertRefused(result, "VNC_SIGNER hold 'change-me-in-production'")
            self.assertNotIn(value, result.stderr.replace("'change-me-in-production'", ""))

    def test_test_and_e2e_names(self):
        for name in ("ai-employee-aws:1.0.0-e2e", "ai-employee-aws:1.0.0-test", "ai-employee-aws-e2e:dev",
                     "ai-employee-aws-test:1", "ai-employee-aws-local:1"):
            reason = "ends in -local" if "-local" in name else "ends in -e2e or -test"
            self.assertRefused(self.guard(good_image(), name=name), name, reason)

    def test_test_only_files(self):
        for path in ("/opt/test-site", "/opt/local-test", "/app/tests", "/app/docker/local", "/app/docs",
                     "/app/pytest.ini", "/srv/entitlement_stub.py", "/app/fake_ecs_metadata.py", "/x/live_view_hook.py"):
            image = dict(good_image(), extra=[f"file {PEM}", f"test {path}"])
            self.assertRefused(self.guard(image), "test-only files", path)

    def test_probe_scans_the_whole_image(self):
        text = SCRIPT.read_text(encoding="utf-8")
        for needle in ('find / $skip -iname ".env*"', 'find /app $skip -type f', '-iname "*.pem" -o -iname "*.key"',
                       "-path /app/bot/node_modules", "grep -rli change-me-in-production",
                       'find / \\( -path /proc -o -path /sys -o -path /dev \\) -prune -o -type f -print0',
                       "/opt/test-site /opt/local-test /app/tests /app/docker/local /app/docs /app/pytest.ini",
                       "-name entitlement_stub.py -o -name fake_ecs_metadata.py -o -name live_view_hook.py"):
            self.assertIn(needle, text)
        self.assertNotIn("-name node_modules", text)
        # --exclude-dir skips every directory of that name, such as /app/x/dev.
        self.assertNotIn("--exclude-dir", text)
        # -xdev would stop at a volume or bind mount, so a planted file there
        # would go unseen.
        self.assertNotIn("-xdev", text)

    def test_volumes(self):
        for volumes in ('{"/home/nlsql/data":{}}', '{"/app":{},"/tmp":{}}'):
            self.assertRefused(self.guard(dict(good_image(), volumes=volumes)), "declares VOLUME " + volumes)
        for volumes in ("null", "{}"):
            self.assertEqual(self.guard(dict(good_image(), volumes=volumes)).returncode, 0)

    def test_version_label(self):
        for label in ("", "9.9.8", "9.9.9-local"):
            self.assertRefused(self.guard(dict(good_image(), version_label=label)),
                               f"image.version label is '{label}', not VERSION {VERSION}")
        result = self.guard(dict(good_image(), version_label="x"), version="")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_no_image(self):
        result = self.guard(dict(good_image(), exists=False))
        self.assertEqual(result.returncode, 1)
        self.assertIn("no local image", result.stderr)

    def test_local_tag(self):
        self.assertRefused(self.guard(good_image(), name="ai-employee-aws:9.9.9-local"), "-local")

    def test_stage_label(self):
        self.assertRefused(self.guard(dict(good_image(), stage="local")), "com.nlsql.stage label is 'local'")

    def test_key_label_differs_from_baked_pem(self):
        self.assertRefused(self.guard(dict(good_image(), label_key="2" * 64)),
                           "is not the sha256 of " + PEM)

    def test_placeholder_key(self):
        image = dict(good_image(), label_key=PLACEHOLDER, pem=PLACEHOLDER)
        self.assertRefused(self.guard(image, pinned=""), "PLACEHOLDER public key",
                           "obtain RegisterUsage public key version 1")

    def test_missing_placeholder_constant(self):
        self.assertRefused(self.guard(dict(good_image(), placeholder=None)), "defines no PUBKEY_SHA256_PLACEHOLDER")

    def test_unset_or_other_pinned_key(self):
        self.assertRefused(self.guard(good_image(), pinned=""), "PUBKEY_SHA256 in the Makefile is ''")
        self.assertRefused(self.guard(good_image(), pinned="3" * 64), "not PUBKEY_SHA256 " + "3" * 64)

    def test_platform(self):
        self.assertRefused(self.guard(dict(good_image(), platform="linux/arm64")), "not linux/amd64")

    def test_user(self):
        for user in ("", "0", "root", "10001:0"):
            self.assertRefused(self.guard(dict(good_image(), user=user)), f"runs as user '{user}'")
        self.assertEqual(self.guard(dict(good_image(), user="10001:10001")).returncode, 0)

    def test_ports(self):
        for ports in ('{"8080/tcp":{},"2222/tcp":{}}', '{"80/tcp":{}}', "null"):
            self.assertRefused(self.guard(dict(good_image(), ports=ports)), "not exactly 8080/tcp")

    def test_python_trust_anchors(self):
        other = "aaaaaaaaaaaaaaaaaaaaaaaaa"
        for codes in (f"{other} | {CODE} | {CODE}", f"{CODE} | {CODE} {other} | {CODE}", f"{CODE} | {CODE} | {other}",
                      f"{CODE} | | {CODE}", f"{CODE} | {CODE} | ", f"{CODE} {CODE}"):
            self.assertRefused(self.guard(dict(good_image(), python=codes)), "aws_marketplace_verify.py bake and trust")
        self.assertRefused(self.guard(dict(good_image(), python=None)), "bake and trust ''")

    def test_bot_trust_anchor(self):
        for codes in (f'["{CODE}","aaaaaaaaaaaaaaaaaaaaaaaaa"]', f'["{CODE}",""]', f'["{CODE}"]  ', CODE, "", "[]",
                      "Error: Cannot find module"):
            self.assertRefused(self.guard(dict(good_image(), node=codes)), "bot/entitlement.js trusts")

    def test_import_error(self):
        image = dict(good_image(), python=None, extra=[f"file {PEM}", "error python: ModuleNotFoundError: x"])
        self.assertRefused(self.guard(image), "importing the marketplace modules failed")

    def test_sshd(self):
        image = dict(good_image(), extra=[f"file {PEM}", "sshd /usr/sbin/sshd"])
        self.assertRefused(self.guard(image), "holds an sshd: /usr/sbin/sshd")

    def test_placeholder_secret_word(self):
        image = dict(good_image(), extra=[f"file {PEM}", "placeholder-secret /app/helpers/x.py"])
        self.assertRefused(self.guard(image), "'change-me-in-production' is in /app/helpers/x.py")

    def test_env_and_stray_pem(self):
        for stray in ("/app/.env", "/app/bot/.env.production", "/app/bot/cert.pem", "/home/nlsql/.env",
                      "/root/.env", "/app/helpers/node_modules/.env", "/app/helpers/x.PEM",
                      "/app/helpers/signing.key", "/etc/ssl/private/server.crt"):
            image = dict(good_image(), extra=[f"file {PEM}", f"file {stray}"])
            self.assertRefused(self.guard(image), "other than " + PEM, stray)

    def test_probe_must_finish(self):
        image = dict(good_image(), run_rc=125)
        self.assertRefused(self.guard(image), "could not be run offline")

    def test_every_failure_is_reported(self):
        image = dict(good_image(), stage="local", user="0", ports='{"2222/tcp":{}}',
                     extra=[f"file {PEM}", "sshd /usr/sbin/sshd"])
        result = self.guard(image)
        self.assertRefused(result, "stage label", "runs as user", "8080/tcp", "sshd", "(4 problem(s))")

    def test_script_makes_no_aws_call_and_runs_the_image_offline(self):
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("aws ", text.replace("aws_marketplace", ""))
        self.assertIn("--network none", text)


FAKE_IMAGETOOLS = r'''#!/bin/sh
[ "$1 $2 $3 $4" = "buildx imagetools inspect --raw" ] || { echo "unexpected docker call: $*" >&2; exit 2; }
cat "$FAKE_RAW"
'''


@unittest.skipUnless(shutil.which("make"), "make is not installed")
class CheckPushedTest(unittest.TestCase):
    """`make check-pushed` against a fake `docker buildx imagetools inspect --raw`."""

    def check(self, raw):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(subprocess.run, ["rm", "-rf", str(tmp)], check=False)
        (tmp / "bin").mkdir()
        fake = tmp / "bin" / "docker"
        fake.write_text(FAKE_IMAGETOOLS, encoding="utf-8")
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        (tmp / "raw.json").write_text(json.dumps(raw, indent=2), encoding="utf-8")
        env = {"PATH": f"{tmp / 'bin'}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
               "HOME": str(tmp), "FAKE_RAW": str(tmp / "raw.json")}
        return subprocess.run(["make", "--no-print-directory", "-C", str(REPO), "check-pushed",
                               "PUSHED_REF=registry.invalid/test:1"], env=env,
                              capture_output=True, text=True, timeout=60)

    def test_single_manifests_pass(self):
        for media_type in ("application/vnd.docker.distribution.manifest.v2+json",
                           "application/vnd.oci.image.manifest.v1+json"):
            result = self.check({"schemaVersion": 2, "mediaType": media_type, "config": {}, "layers": []})
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("single image manifest", result.stdout)

    def test_index_is_refused(self):
        for media_type in ("application/vnd.oci.image.index.v1+json",
                           "application/vnd.docker.distribution.manifest.list.v2+json"):
            result = self.check({"schemaVersion": 2, "mediaType": media_type, "manifests": []})
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("is an index", result.stdout)

    def test_other_manifest_is_refused(self):
        result = self.check({"schemaVersion": 1, "name": "x"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not a Docker v2 or OCI image manifest", result.stdout)


if __name__ == "__main__":
    unittest.main()
