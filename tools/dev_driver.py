"""Drives a running manager through its page's API, as the page does, against the fake Supervisor.

    python tools/dev_driver.py <manager URL> <stub URL>

Run by tools/dev_smoke.sh from a container whose address is one of the manager's development peers.  Standard
library only; exits 1 on the first failed check."""

import json
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, "/src/hri_manager")

MGR, STUB = sys.argv[1].rstrip("/"), sys.argv[2].rstrip("/")
# what the Supervisor's ingress sets: the ids of the stub's users (tests/fakes/stub.py USERS)
ALICE_ID, BOB_ID = "a11ce00000000000000000000000a11c", "b0b00000000000000000000000000b0b"
USER = {"X-Remote-User-Id": ALICE_ID, "X-Remote-User-Name": "alice"}
FAILED = []


def call(method, url, body=None, headers=None, user=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={**(USER if user is None else user), **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as err:
        return err.code, err.read().decode()


def api(method, path, body=None):
    headers = {"X-Requested-With": "fetch", "Content-Type": "application/json"} if method != "GET" else {}
    status, text = call(method, f"{MGR}/{path}", body if method != "GET" else None, headers)
    return status, json.loads(text) if text.startswith("{") else text


def check(label, ok, detail=""):
    print(f"{'ok  ' if ok else 'FAIL'}  {label}{'' if ok else '  ' + str(detail)}")
    if not ok:
        FAILED.append(label)


def job(label, status_body, want="succeeded"):
    status, body = status_body
    if status != 202:
        check(label, False, (status, body))
        return None
    job_id = body["job"]["id"]
    started = time.time()
    while True:
        _, data = api("GET", f"api/jobs/{job_id}")
        if data["job"]["state"] != "running":
            break
        time.sleep(0.5)
    j = data["job"]
    check(f"{label}: {j['state']} in {time.time() - started:.1f} s", j["state"] == want, j.get("error") or j["lines"][-3:])
    return j


def stub(path, body=None):
    status, text = call("POST" if body is not None else "GET", f"{STUB}/{path}", body, {"Content-Type": "application/json"})
    return json.loads(text)


def main() -> int:
    status, html = call("GET", f"{MGR}/")
    check("the page loads", status == 200 and "static/mgr.js?v=" in html, status)
    status, text = call("GET", f"{MGR}/", user={"X-Remote-User-Id": BOB_ID, "X-Remote-User-Name": "bob"})
    check("a Home Assistant user who is not an administrator gets 403", status == 403 and "administrators" in text, status)
    status, s = api("GET", "api/status")
    check("status: dev mode, role, map", status == 200 and s["dev"] and s["role_ok"] and s["map_ok"], s)
    status, r = api("GET", "api/releases")
    check("releases from 0.25.0 on", status == 200 and [x["version"] for x in r["releases"]] == ["0.26.0b1", "0.25.1", "0.25.0"], r)

    job("create garage (release 0.25.0)", api("POST", "api/instances", {"name": "garage", "channel": "release", "version": "0.25.0"}))
    job("create lab (git main)", api("POST", "api/instances", {"name": "lab", "channel": "git", "ref": "main"}))
    job("create attic (release 0.25.0)", api("POST", "api/instances", {"name": "attic", "channel": "release", "version": "0.25.0"}))
    job("create tmp1 (release 0.25.0)", api("POST", "api/instances", {"name": "tmp1", "channel": "release", "version": "0.25.0"}))
    status, body = api("POST", "api/instances", {"name": "garage", "channel": "release", "version": "0.25.0"})
    j = job("create garage again is refused", (status, body), want="failed")

    status, data = api("GET", "api/instances")
    inst = {i["name"]: i for i in data["instances"]}
    check("four instances listed", sorted(inst) == ["attic", "garage", "lab", "tmp1"], sorted(inst))
    check("garage started with its panel", inst["garage"]["state"] == "started" and inst["garage"]["ingress_panel"], inst["garage"])
    check("lab is a git build", inst["lab"]["channel"] == "git" and inst["lab"]["installed_version"].startswith("0.0.0-"), inst["lab"])
    check("attic offers 0.25.1", inst["attic"]["newer_release"] == "0.25.1", inst["attic"])
    check("the published HRI app is listed, not managed", any(o["kind"] == "published" for o in data["others"]), data["others"])
    check("no instance password in the listing", "child-secret-password" not in json.dumps(data))

    job("update garage to 0.25.1", api("POST", "api/instances/garage/update", {"version": "0.25.1"}))
    job("stop garage", api("POST", "api/instances/garage/stop", {}))
    job("start garage", api("POST", "api/instances/garage/start", {}))
    job("restart garage", api("POST", "api/instances/garage/restart", {}))
    j = job("rebuild lab with no new commit", api("POST", "api/instances/lab/update", {}))
    check("  nothing rebuilt", j and j["result"].get("unchanged") is True, j and j["result"])
    stub("_stub/control", {"advance": "main"})
    job("rebuild lab after a new commit on main", api("POST", "api/instances/lab/update", {}))
    job("delete tmp1 with its data", api("DELETE", "api/instances/tmp1", {"remove_data": True, "confirm": "tmp1"}))

    status, body = api("DELETE", "api/instances/attic", {"remove_data": True, "confirm": "nope"})
    check("delete with data needs the typed name", status == 400, (status, body))
    status, _ = call("POST", f"{MGR}/api/instances/garage/stop", {}, {"Content-Type": "application/json"})
    check("a POST without X-Requested-With is refused", status == 403, status)
    status, _ = call("POST", f"{MGR}/api/instances/garage/stop", {}, {"X-Requested-With": "fetch", "Content-Type": "text/plain"})
    check("a POST that is not JSON is refused", status == 415, status)
    status, body = api("POST", "api/instances/foreign/stop", {})
    check("a local app without the marker is refused", status == 400, (status, body))

    state = stub("_stub/state")
    check("the stub saw no call outside its endpoints", state["unexpected"] == [], state["unexpected"])
    from hrimgr import supervisor

    outside = [(m, p) for m, p, _ in state["calls"] if not any(r.method == m and r.pattern.fullmatch(p) for r in supervisor.RULES)]
    check(f"all {len(state['calls'])} Supervisor calls are in the allow-list", outside == [], outside)
    changed_foreign = [(m, p) for m, p, _ in state["calls"] if m == "POST" and ("foreign" in p or "core_" in p or "hass_remote_integration" in p)]
    check("nothing but instances was changed", changed_foreign == [], changed_foreign)
    check("installed now: attic, garage, lab", sorted(s for s in state["installed"] if s.startswith("local_hri_") and s != "local_hri_foreign")
          == ["local_hri_attic", "local_hri_garage", "local_hri_lab"], sorted(state["installed"]))
    print(f"\n{len(FAILED)} failed" if FAILED else "\nall checks passed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
