#!/usr/bin/env python3
"""register.py — 把私有仓接入 ops-runner cron 虚拟机（GitHub API 全自动，无本地依赖）.

用法:
  python3 register.py --repo NEWREPO              # 注册: deploy key + manifest 空模板 + keys 文件
  python3 register.py --repo NEWREPO --remove     # 注销: 删 key + manifest 条目 + keys 文件条目
  python3 register.py --repo NEWREPO --auto       # 供 runner repo-sync 调用(额外守卫)
鉴权: env OPS_ADMIN_TOKEN (classic PAT, repo scope)
keys 事实源: hub 私有仓 state/deploy_keys.json (DEPLOY_KEYS_JSON 只是 runner 启动 bootstrap)
"""
import argparse, base64, datetime, json, os, subprocess, sys, tempfile, urllib.request, urllib.error

OWNER = "greatyingzi"
RUNNER_REPO = "ops-runner"
HUB = "data-core-01"
TITLE = "ops-runner-cron"


def api(url, method="GET", body=None, tok=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={
        "Authorization": "token " + tok, "Accept": "application/vnd.github+json"}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return r.getcode(), (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw[:200].decode("utf-8", "replace")


def gh(token):
    return lambda url, method="GET", body=None: api(url, method, body, tok=token)


def repo_meta(name, G):
    c, d = G("https://api.github.com/repos/%s/%s" % (OWNER, name))
    if c != 200:
        sys.exit("repo %s 不可达: %s" % (name, c))
    return d


def gen_keypair():
    d = tempfile.mkdtemp(prefix="regkey")
    p = os.path.join(d, "k")
    subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-C",
                    "ops-runner-cron-auto", "-f", p, "-q"], check=True)
    return open(p).read(), open(p + ".pub").read().strip()


def seal(tok, value):
    import nacl.public
    c, pk = G_ ("https://api.github.com/repos/%s/%s/actions/secrets/public-key" % (OWNER, RUNNER_REPO), tok=tok) if False else api(
        "https://api.github.com/repos/%s/%s/actions/secrets/public-key" % (OWNER, RUNNER_REPO), tok=tok)
    if c != 200:
        sys.exit("public-key: %s" % c)
    pkobj = nacl.public.PublicKey(base64.b64decode(pk["key"]))
    return {"encrypted_value": base64.b64encode(
        nacl.public.SealedBox(pkobj).encrypt(value.encode())).decode(), "key_id": pk["key_id"]}


def hub_file(tok, path):
    c, d = api("https://api.github.com/repos/%s/%s/contents/%s" % (OWNER, HUB, path), tok=tok)
    if c == 200:
        return d, json.loads(base64.b64decode(d["content"]))
    return None, None


def put_hub_file(tok, path, obj, msg, old):
    body = {"message": msg, "content": base64.b64encode(
        json.dumps(obj, ensure_ascii=False, indent=1).encode()).decode()}
    if old:
        body["sha"] = old["sha"]
    c, r = api("https://api.github.com/repos/%s/%s/contents/%s" % (OWNER, HUB, path), "PUT", body, tok=tok)
    if c not in (200, 201):
        sys.exit("hub PUT %s: %s %s" % (path, c, r))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--remove", action="store_true")
    ap.add_argument("--auto", action="store_true")
    a = ap.parse_args()
    tok = (os.environ.get("OPS_ADMIN_TOKEN") or "").strip()
    if not tok:
        sys.exit("缺 OPS_ADMIN_TOKEN")
    G = lambda url, method="GET", body=None: api(url, method, body, tok=tok)

    meta = repo_meta(a.repo, G)
    if a.auto:
        if meta.get("private") is False:
            print("repo-sync skip %s: 非私有" % a.repo); return
        if meta.get("fork") or meta.get("archived"):
            print("repo-sync skip %s: fork/archived" % a.repo); return
        if meta.get("default_branch") != "main":
            print("repo-sync skip %s: 默认分支 %s ≠ main" % (a.repo, meta.get("default_branch"))); return
        pushed = datetime.datetime.fromisoformat(meta["pushed_at"].replace("Z", "+00:00"))
        age = (datetime.datetime.now(datetime.timezone.utc) - pushed).days
        if age > 180:
            print("repo-sync skip %s: %d 天未推送" % (a.repo, age)); return
    rid = meta["id"]

    _, keys = hub_file(tok, "state/deploy_keys.json")
    keys = keys or {}
    _, manifest = hub_file(tok, "tasks/manifest.json")
    manifest = manifest or {"rotation": ["A", "B", "C"], "always": [], "groups": {}, "artifacts": [], "repos": []}
    repos = manifest.setdefault("repos", [])
    entry = next((e for e in repos if (e.get("repo") if isinstance(e, dict) else e) == a.repo), None)

    if a.remove:
        c, ks = G("https://api.github.com/repositories/%d/keys?per_page=100" % rid)
        for k in (ks if isinstance(ks, list) else []):
            if k.get("title") == TITLE:
                G("https://api.github.com/repositories/%d/keys/%d" % (rid, k["id"]), "DELETE")
                print("removed deploy key %s#%d" % (a.repo, k["id"]))
        if entry:
            repos.remove(entry)
        keys.pop(a.repo, None)
        put_hub_file(tok, "tasks/manifest.json", manifest, "repo-sync: remove %s" % a.repo, None)
        c2, d2 = hub_file(tok, "tasks/manifest.json")
        put_hub_file(tok, "state/deploy_keys.json", keys, "repo-sync: remove %s key" % a.repo,
                     hub_file(tok, "state/deploy_keys.json")[0])
        print("removed manifest entry + key file entry:", a.repo)
        return

    if entry is not None and a.repo in keys:
        print("%s 已注册, 无操作" % a.repo); return
    priv, pub = gen_keypair()
    c, d = G("https://api.github.com/repositories/%d/keys" % rid, "POST",
             {"title": TITLE, "key": pub, "read_only": False})
    if c not in (201, 200):
        sys.exit("deploy key POST %s: %s %s" % (a.repo, c, d))
    print("deploy key OK %s id=%s" % (a.repo, d.get("id")))
    if entry is None:
        repos.append({"repo": a.repo, "tasks": []})
    else:
        entry["tasks"] = entry.get("tasks") or []
    keys[a.repo] = priv
    put_hub_file(tok, "tasks/manifest.json", manifest, "repo-sync: register %s(stub)" % a.repo, None)
    c2, d2 = hub_file(tok, "tasks/manifest.json")
    put_hub_file(tok, "state/deploy_keys.json", keys, "repo-sync: register %s key" % a.repo,
                 hub_file(tok, "state/deploy_keys.json")[0])
    print("manifest stub + keys file OK:", a.repo)


if __name__ == "__main__":
    main()
