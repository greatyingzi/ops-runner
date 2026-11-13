# ops-runner — 寄生于 GitHub Actions 的统一 cron 虚拟机

公开仓 = runner（私有仓定时任务不消耗免费分钟额度）；私有 hub 仓 = 任务模板与数据落点。

## 架构

```
schedule/workflow_dispatch → run.yml → runner.py
  1. ssh(deploy key) clone 私有 hub 仓
  2. 读 tasks/manifest.json:
       repos[]: [{repo, tasks:[{name, every_minutes, needs, cmd, deploy_cmd, artifacts, env, ...}]}]
       legacy:  rotation 组任务(旧机制兼容)
  3. 按 state/last_run.json 判任务到期 → clone 任务仓 → 跑 cmd(缺 needs 则 SKIP)
  4. 可选 deploy_cmd → artifacts 回写任务仓
  5. repo-sync: 新私有仓自动注册为空模板(register.py --auto)
  6. 日志/状态全部回写 hub(私有), 公开 CI 日志只留脱敏状态行
```

## 新增一个私有仓的定时任务

1. `register repo` workflow_dispatch 填仓名（或等 repo-sync 自动注册 stub）
2. 在 hub 仓 `tasks/manifest.json` 对应 repo 下加 tasks 条目
3. 需要凭据时在 runner 仓 Secrets 加值（任务里用 needs 声明变量名）

## 安全约定

- 公开日志不出现私有仓名(前两字符+***)与任何 secret 值；命令输出只进 hub 私有日志
- deploy key 按仓独立(title=ops-runner-cron)，注销走 `register repo --remove`
- 任务命令不打印凭据；公开仓保持 owner-only 写
