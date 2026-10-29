#!/usr/bin/env python3
"""Generic scheduled task runner. Reads task manifest from the data repo,
executes commands, commits artifacts back. No domain logic lives here."""
import json, os, subprocess, sys, datetime

DATA_REPO = os.environ.get("DATA_REPO_GIT", "git@github.com:greatyingzi/seo-radar.git")
DATA_DIR = os.path.join(os.getcwd(), "data")

def sh(cmd, cwd=None):
    r = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True)
    print(r.stdout[-2000:] if r.stdout else "", r.stderr[-800:] if r.returncode else "", sep="\n")
    return r.returncode

def main():
    if os.path.exists(DATA_DIR):
        sh("git -C %s fetch origin && git -C %s reset --hard origin/main" % (DATA_DIR, DATA_DIR))
    else:
        rc = sh("git clone --depth 1 %s %s" % (DATA_REPO, DATA_DIR))
        if rc != 0: sys.exit("clone failed")

    manifest = json.load(open(os.path.join(DATA_DIR, "tasks", "manifest.json")))

    group = os.environ.get("TASK_GROUP") or manifest["rotation"][datetime.datetime.utcnow().weekday() % len(manifest["rotation"])]
    tasks = manifest["always"] + manifest["groups"].get(group, [])
    print("group:", group, "tasks:", [t["name"] for t in tasks])
    for t in tasks:
        rc = sh(t["cmd"], cwd=DATA_DIR)
        print("task", t["name"], "rc=", rc)

    if os.environ.get("WEEKLY") == "1":
        for t in manifest.get("discovery_weekly", []): sh(t, cwd=DATA_DIR)

    cd = DATA_DIR
    for f in manifest["artifacts"]:
        sh('git add "%s" 2>/dev/null || true' % f, cwd=cd)
    if sh('git diff --cached --quiet', cwd=cd) != 0:
        sh('git commit -m "run %s group=%s"' % (datetime.datetime.utcnow().strftime("%F %T"), group), cwd=cd)
        sh("git push origin main", cwd=cd)
        print("data pushed")
    else:
        print("no data changes")

if __name__ == "__main__": main()
