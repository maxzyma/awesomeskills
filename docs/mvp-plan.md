# awsomeskills MVP 执行计划（v0.1）

> 目标：一个**零常驻服务端**、能真实跑通的最小闭环，用来验证核心假设——
> "agent 会在会话里经 finder skill 主动发现并拉取可信 skill"。

## 里程碑

### M0 — 骨架可运行（本轮自主完成）
- [x] 私有仓 + monorepo 目录骨架
- [x] 产品定义 v0.1（分发=单skill+静态index，责任边界）
- [x] `registry/schema.md` + `registry/sources.toml`（首批种子源）
- [x] `processing/build_index.py`：sources → GitHub API → health/zh → `index.json` + `llm.txt`
- [x] 首版真实 `registry/index.json` + `site/public/llm.txt`（种子源实跑）
- [x] `skills/awsomeskills-finder/`：薄客户端 skill
- [x] `site/index.html`：极简查阅器（fetch index.json）

### M1 — 需求验证（需人工决策后启动）
- [ ] 静态托管 index.json（GitHub raw / Pages）+ finder skill 指向真实 URL
- [ ] finder skill 装进本地几个 agent，观测真实 query/pull
- [ ] 埋点：托管访问日志近似"agent 来用"信号

### M2 — 可信度加深（验证通过后）
- [ ] security 从 `unrated` → 真实静态扫描（prompt-injection / 数据外泄启发式）
- [ ] health 公式用真实样本回归校准
- [ ] 扩充种子源，收录请求入口上线

### M3+ — 形态升级（愿意托管 server 时）
- [ ] remote MCP（`skills.search`/`skills.get`），index.json 作数据源
- [ ] 打包 plugin 走 CC marketplace

## 本轮交付边界（我能自己负责的）
- 全部 M0 项 + 子模块 commit & push 到私有 remote。
- 父仓：登记 CHECKPOINT/CLAUDE/AGENTS，但**不 commit 父仓**（共享工作树有别的会话在途改动，提交边界留给 Human）。

## 待 Human 决策（我不擅自定）
- 静态托管选型与 awsomeskills.io 接入（M1）。
- security 评级路线（M2）。
- 是否/何时上 MCP（M3）。
- health 公式是否按当前启发式 v0 定稿。
