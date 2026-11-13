#!/usr/bin/env python3
"""ops-runner v2 — 寄生于 GitHub Actions 的统一 cron 虚拟机.

公开仓 = runner(免费分钟); 私有仓 = 任务模板(ops-tasks.json) + 数据/日志落点(data-core-01).
每 15 分钟被 schedule 唤醒一次:
  1. 拉 hub(私有 data-core-01) → tasks/manifest.json + state/last_run.json
  2. 遍历 manifest.repos[].tasks[] → 按 every_minutes 判到期
  3. 到期任务: ssh(deploy key) clone 任务仓 → 跑 cmd(缺 needs 环境变量则记 SKIP)
     → 可选 deploy_cmd(cmd 成功才跑) → artifacts 回写任务仓(或 commit_in 指定的 clone)
  4. legacy 兼容: 每天 14:00UTC 档首次唤醒时, 仍按老 rotation 在 data-core-01 跑 always+组任务
  5. 全部日志落 data-core-01/ci_logs/; state 更新后回写 — 私密信息不出现在公开 CI 日志

手动触发: workflow_dispatch inputs.only = "repo/name" 或 "legacy"
"""
import json, os, subprocess, sys, time, datetime

DATA_REPO = os.environ.get("DATA_REPO_GIT", "git@github.com:greatyingzi/data-core-01.git")
WORK = os.path.join(os.getcwd(), "work")
DATA_DIR = os.path.join(WORK, "data-core-01")
STATE_F = os.path.join(DATA_DIR, "state", "last_run.json")
SSH_DIR = os.path.expanduser("~/.ssh")
BUDGET_MIN = float(os.environ.get("MAX_RUN_MINUTES", "95"))
ONLY = (os.environ.get("ONLY_FILTER") or "").strip()
T0 = time.time()

LOGBUF = []
def sh(cmd, cwd=None, env=None, timeout=None):
    r = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True,
                       env=env, timeout=timeout)
    LOGBUF.append("$ [%s]\n%s\n%s" % (cmd[:160], (r.stdout or "")[-4000:], (r.stderr or "")[-2000:]))
    return r.returncode

def log(msg):
    print(msg, flush=True)

def elapsed_min():
    return (time.time() - T0) / 60.0

def ssh_env_for(repo):
    key = os.path.join(SSH_DIR, "dk_" + repo)
    if not os.path.exists(key):
        return None
    e = dict(os.environ)
    e["GIT_SSH_COMMAND"] = "ssh -i %s -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new" % key
    return e

def write_keys():
    raw = os.environ.get("DEPLOY_KEYS_JSON") or ""
    if not raw:
        return
    os.makedirs(SSH_DIR, exist_ok=True)
    try:
        keys = json.loads(raw)
    except json.JSONDecodeError:
        log("!! DEPLOY_KEYS_JSON 解析失败"); return
    for repo, priv in keys.items():
        p = os.path.join(SSH_DIR, "dk_" + repo)
        with open(p, "w") as f: f.write(priv)
        os.chmod(p, 0o600)
    sh("ssh-keyscan -t ed25519 github.com >> ~/.ssh/known_hosts 2>/dev/null || true")

def load_state():
    try: return json.load(open(STATE_F))
    except Exception: return {"tasks": {}, "legacy": ""}

def save_state(st):
    os.makedirs(os.path.dirname(STATE_F), exist_ok=True)
    json.dump(st, open(STATE_F, "w"), ensure_ascii=False, indent=1)

def clone_repo(repo, path, depth=1):
    env = ssh_env_for(repo)
    if env is None:
        log("!! %s 无 deploy key, 跳过" % repo); return 1
    if os.path.exists(os.path.join(path, ".git")):
        return sh("git -C %s fetch origin && git -C %s reset --hard origin/main" % (path, path), env=env)
    return sh("git clone --depth %d git@github.com:greatyingzi/%s.git %s" % (depth, repo, path), env=env)

def missing_needs(task):
    return [k for k in (task.get("needs") or []) if not (os.environ.get(k) or "").strip()]

def commit_env_for(task, tdir):
    """artifacts 回写目标: 默认任务仓; commit_in 指定 extra clone 时按 commit_repo 找 key."""
    ci = task.get("commit_in")
    if not ci:
        return tdir, ssh_env_for(task.get("_repo"))
    crepo = task.get("commit_repo")
    if not crepo:
        for c in task.get("extra_clones") or []:
            if c.get("path") == ci:
                crepo = c.get("git", "").split("/")[-1].replace(".git", "")
                break
    return os.path.join(tdir, ci), ssh_env_for(crepo or task.get("_repo"))

def run_repo_task(repo, task, st):
    task["_repo"] = repo
    name = task.get("name", "?")
    key = "%s/%s" % (repo, name)
    if ONLY and ONLY not in (key, repo): return
    every = float(task.get("every_minutes", 1440))
    last = float(st["tasks"].get(key, 0))
    if time.time() - last < every * 3600:
        return
    if elapsed_min() > BUDGET_MIN:
        log("… 预算用尽, %s 留到下次" % key); return
    log("▶ 任务 %s (每%.0f分, 上次 %s)" % (key, every,
        datetime.datetime.fromtimestamp(last).strftime("%m-%d %H:%M") if last else "从未"))
    miss = missing_needs(task)
    if miss:
        log("SKIP %s: 缺环境变量 %s" % (key, ",".join(miss)))
        LOGBUF.append("SKIP %s missing=%s" % (key, ",".join(miss)))
        st["tasks"][key] = time.time()
        return
    tdir = os.path.join(WORK, repo)
    if clone_repo(repo, tdir) != 0:
        st["tasks"][key] = time.time(); log("!! %s clone 失败" % key); return
    for c in task.get("extra_clones") or []:
        cpath = os.path.join(tdir, c.get("path", "data"))
        crepo = c.get("git", "").rstrip(".git").rstrip("/").split("/")[-1]
        if clone_repo(crepo, cpath) != 0:
            st["tasks"][key] = time.time(); log("!! %s extra clone 失败" % c.get("git")); return
    tmo = float(task.get("timeout_min", 90)) * 60
    env = dict(os.environ)
    for k, src in (task.get("env") or {}).items():
        env[k] = os.environ.get(src, "")
    rc = sh(task["cmd"], cwd=tdir, env=env, timeout=tmo)
    log("   cmd rc=%d (%.1f 分)" % (rc, elapsed_min()))
    ok = rc == 0
    if ok and task.get("deploy_cmd"):
        denv = dict(os.environ)
        for k, src in (task.get("deploy_env") or {}).items():
            denv[k] = os.environ.get(src, "")
        rc2 = sh(task["deploy_cmd"], cwd=tdir, env=denv,
                 timeout=float(task.get("deploy_timeout_min", 30)) * 60)
        log("   deploy rc=%d" % rc2); ok = ok and rc2 == 0
    if ok:
        cdir, cenv = commit_env_for(task, tdir)
        if cenv is None:
            log("   !! 回写仓无 key, 跳过 commit")
        for a in task.get("artifacts") or []:
            sh('git add "%s" 2>/dev/null || true' % a, cwd=cdir, env=cenv)
        if sh("git diff --cached --quiet", cwd=cdir, env=cenv) != 0:
            sh('git commit -m "cron %s %s"' % (key, datetime.datetime.utcnow().strftime("%F %T")),
               cwd=cdir, env=cenv)
            sh("git push origin main", cwd=cdir, env=cenv)
            log("   artifacts 回写完成")
        else:
            log("   无产物变更")
        for pc in task.get("post_cmd") or []:
            sh(pc, cwd=tdir, env=env)
    st["tasks"][key] = time.time()

def run_legacy(manifest, st):
    today = datetime.datetime.utcnow().strftime("%F")
    if st.get("legacy") == today: return
    if ONLY and ONLY != "legacy": return
    group = os.environ.get("TASK_GROUP") or manifest["rotation"][datetime.datetime.utcnow().weekday() % len(manifest["rotation"])]
    def norm(ts):
        return [t if isinstance(t, dict) else {"name": str(t)[:40], "cmd": str(t)} for t in ts]
    tasks = norm(manifest["always"]) + norm(manifest["groups"].get(group, []))
    log("▶ legacy 组 %s: %s" % (group, [t["name"] for t in tasks]))
    for t in tasks:
        sh(t["cmd"], cwd=DATA_DIR, timeout=60*60)
    for t in manifest.get("discovery_weekly", []):
        if datetime.datetime.utcnow().weekday() == 0:
            sh(t, cwd=DATA_DIR, timeout=60*60)
    cd = DATA_DIR
    for f in manifest["artifacts"] + ["ci_logs/"]:
        sh('git add "%s" 2>/dev/null || true' % f, cwd=cd)
    if sh("git diff --cached --quiet", cwd=cd) != 0:
        sh('git commit -m "legacy run %s group=%s"' % (today, group), cwd=cd)
        sh("git push origin main", cwd=cd)
    st["legacy"] = today

def main():
    ts = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    write_keys()
    os.makedirs(WORK, exist_ok=True)
    if clone_repo("data-core-01", DATA_DIR) != 0:
        sys.exit("hub clone failed")
    manifest = json.load(open(os.path.join(DATA_DIR, "tasks", "manifest.json")))
    st = load_state()
    try:
        run_legacy(manifest, st)
        for ent in manifest.get("repos", []):
            repo = ent.get("repo") or ent
            for task in ent.get("tasks", []):
                try:
                    run_repo_task(repo, task, st)
                except subprocess.TimeoutExpired:
                    log("!! %s 超时被杀" % repo); st["tasks"]["%s/%s" % (repo, task.get("name","?"))] = time.time()
        # 任务日志统一落 hub
        os.makedirs(os.path.join(DATA_DIR, "ci_logs"), exist_ok=True)
        open(os.path.join(DATA_DIR, "ci_logs", "run_%s.log" % ts), "w").write("\n\n".join(LOGBUF))
        save_state(st)
        sh('git add state/ ci_logs/ 2>/dev/null || true', cwd=DATA_DIR)
        if sh("git diff --cached --quiet", cwd=DATA_DIR) != 0:
            sh('git commit -m "cron state %s"' % ts, cwd=DATA_DIR)
            sh("git push origin main", cwd=DATA_DIR)
        log("✅ 本轮完成: %.1f 分, 日志 run_%s.log" % (elapsed_min(), ts))
    except Exception as e:
        import traceback
        try:
            os.makedirs(os.path.join(DATA_DIR, "ci_logs"), exist_ok=True)
            open(os.path.join(DATA_DIR, "ci_logs", "crash_%s.log" % ts), "w").write(traceback.format_exc())
        except Exception:
            pass
        raise

if __name__ == "__main__":
    main()
