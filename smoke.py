"""Start the agent binary and check loopback health and UI."""
import json, subprocess, sys, threading, queue
from urllib.parse import urlsplit
from urllib.request import urlopen

exe = sys.argv[1]
prefix = sys.argv[2:]
p = subprocess.Popen([*prefix, exe, "--profile", "integration", "--no-browser", "--port", "0"],
                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
q = queue.Queue()
threading.Thread(target=lambda: q.put(p.stdout.readline()), daemon=True).start()
rc = 1
try:
    try:
        line = q.get(timeout=90)
    except queue.Empty:
        line = ""
    u = urlsplit(line.strip())
    if u.hostname != "127.0.0.1" or not u.port:
        print("no launch url; first line:", repr(line[:80]))
    else:
        origin = f"http://127.0.0.1:{u.port}"
        health = json.load(urlopen(origin + "/health", timeout=30))
        html = urlopen(origin + "/", timeout=30).read(4096).decode()
        print("health:", health)
        vm = subprocess.run(["sudo", "-n", "vmmap", "-summary", str(p.pid)], capture_output=True, text=True).stdout
        print([l.strip() for l in vm.splitlines() if "Code Type" in l])
        print("ui served:", "<html" in html.lower())
        rc = 0 if health.get("status") == "ready" and "<html" in html.lower() else 1
finally:
    p.terminate()
    try:
        p.wait(timeout=10)
    except subprocess.TimeoutExpired:
        p.kill()
    err = p.stderr.read()[-3000:]
    print("exit code:", p.returncode)
    if err:
        print("stderr tail:\n" + err)
sys.exit(rc)
