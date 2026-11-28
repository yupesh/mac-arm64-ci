"""Check one stage SSO and Hermes turn with a packaged M1 on an ARM runner.

With stage connectivity and native Python 3.9 or newer, run:
    python3 arm_stage_turn.py /path/to/m1-agent /path/to/stage/m1-build.json

STAGE_AUTH supplies the stage QA account as JSON with username and password
fields. Standard output is one JSON receipt, suitable for redirection.
"""

import argparse
import hashlib
import http.cookiejar
import json
import os
import platform
import plistlib
import re
import select
import signal
import ssl
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib import error, parse, request


STAGE_COMMIT = "1ad2a0658e2d1510b243503b1fcfc5d37434c06a"


class CheckFailed(Exception):
    """Only fixed diagnostic strings from this scenario are printed."""


def require(condition, detail):
    if not condition:
        raise CheckFailed(detail)


def output(*args):
    return subprocess.check_output(args, stderr=subprocess.DEVNULL, timeout=60)


def verify_host():
    require(sys.platform == "darwin" and platform.machine() == "arm64",
            "A native arm64 Python process on Apple Silicon is required")


def verify_volume(work):
    # diskutil expects a mount point or device. The project can be below
    # /private/tmp on the Data volume or on a caller-selected APFS volume.
    device = output("/bin/df", "-P", str(work)).splitlines()[-1].split()[0].decode()
    volume = plistlib.loads(output("/usr/sbin/diskutil", "info", "-plist", device))
    require(volume.get("FilesystemType", "").lower() == "apfs", "Project volume must be APFS")


@contextmanager
def running_agent(agent, env, root):
    child = subprocess.Popen(
        ["/usr/bin/arch", "-" + platform.machine(), str(agent), "--no-browser", "--no-dev-tunnel", "--port", "0"],
        cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True,
    )
    try:
        ready, _, _ = select.select([child.stdout], [], [], 90)
        require(ready, "M1 startup timed out")
        launch = parse.urlsplit(child.stdout.readline(8192).decode().strip())
        require(launch.scheme == "http" and launch.hostname == "127.0.0.1" and launch.port
                and launch.username is None, "M1 did not return a loopback launch URL")
        yield launch, child.pid
    finally:
        child.stdout.close()
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGTERM)
            try:
                child.wait(12)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(10)


class LocalClient:
    def __init__(self, launch):
        self.base = f"http://127.0.0.1:{launch.port}/api/v1/"
        self.opener = request.build_opener(request.ProxyHandler({}),
                                           request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.csrf = None
        token, = parse.parse_qs(launch.query)["launch_token"]
        response = self.call("session/exchange", {"protocol": "m1-web-v1", "launch_token": token})
        self.csrf = response["csrf_token"]

    def call(self, path, body=None):
        headers = {"Content-Type": "application/json"}
        if self.csrf:
            headers["X-M1-CSRF"] = self.csrf
        payload = None if body is None else json.dumps(body).encode()
        query = request.Request(self.base + path, data=payload, headers=headers)
        try:
            with self.opener.open(query, timeout=80) as response:
                return json.load(response)
        except error.HTTPError as exc:
            raise CheckFailed(f"M1 {path} returned HTTP {exc.code}") from None

    def action(self, name, **values):
        return self.call("action", {"action": name, "payload": values})

    def wait(self, select_value, seconds, detail):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            value = select_value(self.call("state"))
            if value:
                return value
            time.sleep(1)
        raise CheckFailed(detail)


def check_sso_origin(uri):
    origin = parse.urlsplit(uri)
    require(origin.scheme == "https" and origin.hostname in {"auth.deh.deeppavlov.ai",
            "sso.deeppavlov.ai"} and origin.port in (None, 443)
            and origin.username is None and origin.password is None, "Unexpected stage SSO origin")
    return origin.hostname


class StageRedirect(request.HTTPRedirectHandler):
    def __init__(self, process_group):
        super().__init__()
        self.process_group = process_group

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        follows = super().redirect_request(req, fp, code, msg, headers, newurl)
        target = parse.urlsplit(newurl)
        if (target.scheme == "http" and target.hostname == "127.0.0.1" and target.port
                and target.username is None and target.path == "/callback" and follows.get_method() == "GET"):
            listeners = output("/usr/sbin/lsof", "-nP", "-a", "-iTCP:" + str(target.port),
                               "-sTCP:LISTEN", "-Fp")
            owners = [os.getpgid(int(line[1:])) for line in listeners.splitlines() if line.startswith(b"p")]
            require(owners and all(pid == self.process_group for pid in owners),
                    "SSO callback listener is outside the M1 process group")
        else:
            check_sso_origin(newurl)
        return follows


class LoginForm(HTMLParser):
    def __init__(self):
        super().__init__()
        self.forms = []
        self.active = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.active = (attrs.get("action"), {})
            self.forms.append(self.active)
        elif tag == "input" and self.active and attrs.get("name"):
            self.active[1][attrs["name"]] = attrs.get("value") or ""

    def handle_endtag(self, tag):
        if tag == "form":
            self.active = None


def authenticate(uri, credentials, process_group):
    check_sso_origin(uri)
    opener = request.build_opener(request.ProxyHandler({}), StageRedirect(process_group),
                                  request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
                                  request.HTTPSHandler(context=ssl.create_default_context(cafile="/etc/ssl/cert.pem")))
    submitted = False
    response = opener.open(uri, timeout=30)
    for _ in range(7):
        with response:
            form = LoginForm()
            form.feed(response.read().decode("utf-8"))
            url = response.geturl()
        if not form.forms:
            break
        # Keycloak may render an alternate-identity form next to the login.
        action, fields = next((f for f in form.forms if "password" in f[1]), form.forms[0])
        target = parse.urljoin(url, action or url)
        host = check_sso_origin(target)
        if "username" in fields or "password" in fields:
            require(not submitted and host == "sso.deeppavlov.ai"
                    and {"username", "password"} <= fields.keys(), "Stage login refused or form changed")
            fields.update(credentials)
            submitted = True
        response = opener.open(target, parse.urlencode(fields).encode(), timeout=30)
    else:
        response.close()
        raise CheckFailed("Stage SSO exceeded the expected number of forms")
    require(submitted, "Stage QA login form was not submitted")


def sign_in(client, credentials, process_group):
    client.action("login", switch_account=True)
    uri = client.wait(lambda state: state["auth"].get("authorization_uri"), 35, "SSO did not start")
    authenticate(uri, credentials, process_group)
    client.wait(lambda state: state["auth"].get("status") == "active" and state["auth"].get("account_id"),
                240, "SSO did not complete")


def run_turn(client, work, evidence):
    seed = work / "source.txt"
    target = work / "result.txt"
    original = ("DEH-ARM-" + uuid.uuid4().hex + "\n").encode()
    seed.write_bytes(original)
    client.call("conversation/new", {"workspace_display_path": str(work)})
    client.action("select_preset", preset_id="fast")
    harness = client.call("state")["harness"]
    profile = harness["selected_profile_id"]
    require(any(p["harness_profile_id"] == profile and p.get("harness_id") == "hermes"
                for p in harness["profiles"]), "Selected preset does not resolve to Hermes")
    command_id = uuid.uuid4().hex
    client.action("submit_message", text="Скопируй файл source.txt в новый result.txt побайтно, "
                  "сохранив перевод строки в конце.", command_id=command_id)

    def terminal(state):
        run = state.get("run") or {}
        if (run.get("request") or {}).get("idempotency_key") != command_id:
            return None
        require(run["run_request"]["harness_profile_id"] == profile, "Turn used another harness")
        run_id = run.get("run_id", "")
        require(re.fullmatch(r"run-[a-f0-9]{32}", run_id), "Unexpected stage run ID")
        evidence["run_id"] = run_id
        return run if run.get("result") else None

    try:
        run = client.wait(terminal, 300, "Hermes turn timed out")
    except CheckFailed:
        if "run_id" in evidence:
            client.action("cancel_run", run_id=evidence["run_id"])
        raise
    require(run["status"] == run["result"].get("status") == "succeeded", "Hermes turn did not succeed")
    for _ in range(15):
        if target.is_file():
            break
        time.sleep(1)
    require(target.is_file() and not target.is_symlink(), "Expected a delivered regular result.txt")
    actual = target.read_bytes()
    source_after = seed.read_bytes()
    evidence.update(harness="hermes", bytes=len(actual),
                    source_sha256=hashlib.sha256(original).hexdigest(),
                    source_after_sha256=hashlib.sha256(source_after).hexdigest(),
                    result_sha256=hashlib.sha256(actual).hexdigest())
    require(original == source_after == actual, "Source or output bytes differ")


def check(args, evidence):
    raw_auth = os.environ.pop("STAGE_AUTH", "")
    verify_host()
    require(raw_auth, "STAGE_AUTH is required")
    credentials = json.loads(raw_auth)
    require(isinstance(credentials, dict) and set(credentials) == {"username", "password"}
            and all(isinstance(v, str) and v for v in credentials.values()), "Invalid STAGE_AUTH format")
    agent = args.agent.resolve()
    metadata = args.metadata.resolve()
    require(re.fullmatch(r"[0-9a-f]{40}", args.commit), "Expected a full stage commit SHA")
    meta = json.loads(metadata.read_text())
    require(meta.get("environment") == "stage" and meta.get("commit_sha") == args.commit,
            "Build metadata does not match the expected stage commit")
    require(b"arm64" in output("/usr/bin/lipo", "-archs", str(agent)).split(),
            "Packaged M1 has no arm64 slice")
    with tempfile.TemporaryDirectory(prefix="deh-stage-", dir=args.scratch_parent) as tmp:
        root = Path(tmp).resolve()
        work = root / "project"
        home = root / "home"
        work.mkdir()
        home.mkdir()
        verify_volume(work)
        env = {"PATH": os.defpath, "HOME": str(home), "TMPDIR": str(root), "M1_SERVER": "main",
               "SSL_CERT_FILE": "/etc/ssl/cert.pem", "REQUESTS_CA_BUNDLE": "/etc/ssl/cert.pem"}
        match = subprocess.run(["/usr/bin/arch", "-arm64", str(agent), "--m1-match-build-metadata",
                                str(metadata)], cwd=root, env=env, capture_output=True, timeout=60)
        require(match.returncode == 0, "Embedded package metadata differs from supplied metadata")
        evidence.update(arch="arm64", macos=platform.mac_ver()[0], filesystem="apfs", commit=args.commit)
        with running_agent(agent, env, root) as (launch, process_group):
            client = LocalClient(launch)
            sign_in(client, credentials, process_group)
            evidence["sso"] = "authenticated"
            try:
                run_turn(client, work, evidence)
            finally:
                client.action("logout")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("agent", type=Path)
    parser.add_argument("metadata", type=Path)
    parser.add_argument("--commit", default=STAGE_COMMIT, help="full SHA of the verified stage deployment")
    parser.add_argument("--scratch-parent", type=Path, help="existing parent on the APFS volume to test")
    evidence = {"status": "failed"}
    try:
        check(parser.parse_args(), evidence)
        evidence["status"] = "passed"
    except CheckFailed as exc:
        evidence["reason"] = str(exc)
    except (Exception, KeyboardInterrupt) as exc:
        evidence["reason"] = type(exc).__name__
    evidence["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(json.dumps(evidence))
    return 0 if evidence["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
