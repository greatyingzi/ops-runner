#!/usr/bin/env python3
"""Generic scheduled task runner. Reads task manifest from the data repo,
executes commands, commits artifacts back. No domain logic lives here."""
import json, os, subprocess, sys, datetime

DATA_REPO = os.environ.get("DATA_REPO_GIT", "git@github.com:greatyingzi/data-core-01.git")
DATA_DIR = os.path.join(os.getcwd(), "data")

LOGBUF = []

def sh(cmd, cwd=None):
    r = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True)
    LOGBUF.append("$ %s\n%s\n%s" % (cmd, r.stdout or "", r.stderr or ""))
    return r.returncode

def main():
    if os.path.exists(DATA_DIR):
        sh("git -C %s fetch origin && git -C %s reset --hard origin/main" % (DATA_DIR, DATA_DIR))
    else:
        rc = sh("git clone --depth 1 %s %s" % (DATA_REPO, DATA_DIR))
        if rc != 0: sys.exit("clone failed")

    manifest = json.load(open(os.path.join(DATA_DIR, "tasks", "manifest.json")))

    group = os.environ.get("TASK_GROUP") or manifest["rotation"][datetime.datetime.utcnow().weekday() % len(manifest["rotation"])]
    def _norm_tasks(ts):
        out = []
        for t in ts:
            out.append(t if isinstance(t, dict) else {"name": str(t)[:40], "cmd": str(t)})
        return out
    tasks = _norm_tasks(manifest["always"]) + _norm_tasks(manifest["groups"].get(group, []))
    print("group:", group, "tasks:", [t["name"] for t in tasks])
    for t in tasks:
        rc = sh(t["cmd"], cwd=DATA_DIR)
        print("task", t["name"], "rc=", rc)

    if os.environ.get("WEEKLY") == "1":
        for t in manifest.get("discovery_weekly", []): sh(t, cwd=DATA_DIR)

    # task logs go into the private repo, never to CI logs
    import datetime as _dt
    os.makedirs(os.path.join(DATA_DIR, "ci_logs"), exist_ok=True)
    open(os.path.join(DATA_DIR, "ci_logs", "run_%s.log" % _dt.datetime.utcnow().strftime("%Y%m%d_%H%M")), "w").write("\n\n".join(LOGBUF))
    cd = DATA_DIR
    for f in manifest["artifacts"] + ["ci_logs/"]:
        sh('git add "%s" 2>/dev/null || true' % f, cwd=cd)
    if sh('git diff --cached --quiet', cwd=cd) != 0:
        sh('git commit -m "run %s group=%s"' % (datetime.datetime.utcnow().strftime("%F %T"), group), cwd=cd)
        sh("git push origin main", cwd=cd)
        print("data pushed")
    else:
        print("no data changes")

if __name__ == "__main__": main()
