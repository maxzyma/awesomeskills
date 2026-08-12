# awesomeskills 产品定义（Draft v0.1）

> 状态：草稿 v0.1，承接两轮战略讨论 + 形态收敛（单 skill vs plugin vs MCP）后落地。
> 本文件是规划 SSoT；结论有变先改这里。对外 README 以英文为门面，本文件用中文详写规划。
> v0.1 相对 v0：分发层从 "MCP server" 收敛为 "单 skill + 静态 index"；新增责任边界一节；MCP 降为可选未来项。

## 0. 一句话定位

**agent-native、AI-ready 的公开技能发现层。护城河是"可信"，不是"收录全"。**
类比：**context7 之于文档 → awesomeskills 之于 skill**——一个 agent 能按需撞上来、查询、拉取的发现层；
但因为 skill 是可执行代码+prompt（非只读文档），核心增值从"最新"变成"可信"。

## 1. 痛点与市场空位（信源：两轮本地/业界调研）

- 技能仓在 2025-10 后爆发，人和 agent 都**看不到全貌、不知道信不信**。
- 业界发现层现状：
  - 人读 awesome-list（`anthropics/skills` 16.7万★、`ComposioHQ/awesome-claude-skills` 7.2万★但压 1213 未处理 issue、`VoltAgent` 3.0万★）——纯 markdown、无机器索引、无质量信号、**vanity star 严重**。
  - 爬取型 storefront（SkillsMP 自报 190万、agentskill.sh 等）——量大质杂，自己声明"不认证质量与安全"。
  - 包管理/可安装（ccpi 2.6k、sk 16、npx skills）——牵引力**三位数以下**，需求侧未验证。
  - 官方 `anthropics/claude-plugins-official`（3.3万★，CLI 原生）——只解决"人装"，不解决"agent 在会话里自主发现"。
- **明确空位**：① agent-native 机器可读发现（SEP-2640 仍是 draft，无事实标准）；② 去 vanity-star 的真实健康度信号；③ 标准化质量/安全评级（抽样 skill 36% 有 prompt injection）；④ 中文生态覆盖（全网仅 LobeHub 一处沾边）。

**押注**：占住 ①④ 两个空位——agent-native + 可信/中文——趁官方 registry 未落地的时间窗，靠**先发 + 信任心智**成为事实入口。

## 2. 五层语义校准（遵 product-prototyping 规则）

1. **买单者/战略语义**：站免费，但"买单"的是**信任**——用户/组织为"可信的 agent 技能供给、降低供应链风险"而依赖它。战略目标：成为 agent 生态的 skill 发现事实入口。
2. **领域模型语义**：`SourceRepo`（被收录的公开技能仓）、`Skill`、`Assessment`（health / security / language 三类信号）、`TrustSignal`、`Index`（机器可读产物）、`SubmissionRequest`（收录请求）。
3. **产品语义**：三层——**门面**（Human 查阅器 / 收录请求 / `llm.txt`）、**护城河**（AI-ready 处理）、**分发**（agent 侧 skill 薄客户端）。
4. **技术语义**：离线流水（爬取 + 评估 + grounding）→ 生成 `index.json` + `llm.txt`（静态托管）→ agent 侧 `awesomeskills` skill 读取 → 静态站消费同一 index。**无常驻服务端。**
5. **使用者语义**：**Human**（浏览、评估、提交收录）；**Agent**（装一个 finder skill，按需查静态 index、只拿到经评估的 skill）；**Maintainer**（跑离线流水、审收录请求）。

## 3. 核心原则：trust over coverage

- **不追全量**。不与 SkillsMP 的 190万 比数量。
- **每个入库 skill 必带三信号**：健康度（真实活跃度，非 star）、安全（评级，MVP 先诚实标 `unrated` 逐步补）、语言覆盖（是否含/覆盖中文场景）。
- **agent 只消费经评估的 skill**——把"让 agent 自主找+装+用"里最危险的一环（自动分发未审代码）用信任层兜住。

### grounding 四层（护城河实质 · 全做目标）

grounding = 我们对每个 skill 的**多层评估**（站内、非外链源仓）。当前只有一层的元数据（health/frontmatter），远不够。目标四层，每层标注「可全量自动」还是「需 LLM + 持续运营」：

1. **健康度 health（可全量自动）**：真实活跃度综合分（recency + commit 频率 + issue 堆积/响应 + 去 vanity 的 star/关注比），**有解释、有区分度**——不是当前满屏 100 的裸 recency 分。数据源 GitHub API。
2. **安全 security（可全量自动·规则）**：静态扫描 SKILL.md + scripts，检测危险命令、数据外泄、prompt-injection、供应链模式；`pass/warn/fail` + 命中项。当前全 `unrated`（未实现）。
3. **功能/场景分析（需 LLM）**：读 SKILL.md 正文提炼 用途 / 适用场景 / 输入输出 / 依赖 / 能力边界；替换当前照抄 description 的 summary。数据源 SKILL.md（已抓）。
4. **社区 grounding（需 LLM + 持续运营，差异化最大）**：真实口碑——issue 质量与活跃、star 增长去 vanity、HN/Reddit/**中文社区**评价与核实。复用 `github-trends` grounding 内核为 skill 适配。数据源 GitHub issue/搜索 + 社区。

原则：① grounding 是**我们的评估**（权威源 index.json，非外链源仓）；② 每 skill 各层**独立标注覆盖状态**（已评/未评），不假装全评过；③ 分「全量自动」（1、2）与「需 LLM + 运营」（3、4）——后者先小样本、再规模化，覆盖率如实 `log` 不静默。

## 4. 责任边界：评估者，非执行者（v0.1 新增）

**awesomeskills 交付的是"信任信号"，不是"执行环境"。**

- 下载来的第三方 skill，在**用户自己的 agent 环境**（用户的 Claude Code / Cursor 等）里执行，不在我们的机器上。
- 因此沙箱 / 隔离是**消费端 agent runtime** 的责任，**不在本产品范围**。我们不写、不承诺任何执行隔离逻辑。
- 我们说"这个 skill 健康度 80 / 安全 pass / 覆盖中文"，用户与 agent 据此决定装不装；真正运行时的隔离由其 agent 环境负责。
- 这条同时决定了：**本产品不需要常驻服务端**（评估是离线批处理，产物是静态文件）。

## 5. 分发形态：单 skill + 静态 index（v0.1 收敛）

形态三者不同维度：**skill = 能力单元（薄客户端）、MCP = 跨 agent 程序化接口、plugin = Claude Code 打包分发糖**。在"不做服务端"约束下：

- **选定：单 skill 薄客户端 + 静态 `index.json`/`llm.txt`。** 零常驻服务：`index.json` 静态托管（GitHub raw / Pages / 对象存储），finder skill 每次 `curl` 拉最新——数据集中更新无需 server。
- **MCP：可选未来项。** remote MCP 需自托管常驻 server（正是当前不做的）；local MCP 更重且丢集中价值。等愿意托管 server（做大规模搜索/埋点/鉴权）再上，届时 `index.json` 直接作数据源，不浪费。
- **plugin：Claude Code 首推安装入口（已实现）。** `.claude-plugin/marketplace.json` 已就绪，`/plugin marketplace add maxzyma/awesomeskills` + `/plugin install awesomeskills@awesomeskills` 一键安装、可更新，与官方 `anthropics/skills` 一致（官方只给 plugin 通道）。plugin 是"怎么装"，不改变"单 skill + 静态 index"这个接口本质。

### 安装 / 分发边界（原则，勿违反）

跨 agent 分发时，**只负责我们权威的部分，别人的落盘位置指向其官方文档、不复制、不维护对照表**：

- **First-party（我们权威）**：Claude Code plugin marketplace 是我们的，讲清楚、我们负责（一键 install + 手动 `~/.claude/skills/` 全局 / `.claude/skills/` 项目）。
- **Third-party（各 agent 落盘目录）**：**不维护各 agent 目录对照表**。理由：① 易腐（各 agent 目录常变，如 `~/.codex/skills` 是社区误传、官方实为 `~/.agents/skills`）；② 越界（`agentskills.io` 标准把落盘位置留给各 agent 实现，我们是发现/评估层不是安装器）；③ 制造第二事实源必然漂移，且写错反噬"可信"招牌。正确做法：**指向各 agent 官方文档**。
- **跨 agent 中立默认**：`.agents/skills/`（项目）/ `~/.agents/skills/`（全局）是收敛中的中立路径（Codex/Gemini/Amp/Goose/Cursor 都读；Claude Code 用 `.claude/skills`，其它多接受它作 fallback）。文档给这个"够用默认"即可。
- **我们只担保**：skill 是标准的（agentskills.io）、可信的（trust 信号）、任何 agent 都能消费；**落盘是各 agent 的事**。

**闭环（纯 skill + 静态 index，可跑通）**：
1. agent 需要一个没有的能力 → 触发 `awesomeskills`
2. skill `curl` 静态 `index.json` → 按 trust 筛 → 返回候选（带 health/security/zh）
3. agent 选定 → skill 指导/执行安装（目标 skill 多为 Agent Skills 标准：`git clone` / 复制到 `.claude/skills/`）
4. index 带 `source_url` + 每文件 `sha256` → 拉完**校验 digest 再落地**（对齐 SEP-2640 精神），在 skill 脚本内完成

## 6. 数据契约（静态 index，最小 schema）

`registry/index.json`（生成物，v0.2 起为 **skill 级**：每个 SKILL.md 一条；无 SKILL.md 的 awesome-list/hub 回退为 repo 级一条）：

```jsonc
{
  "schema_version": "0.2",
  "generated_at": "<ISO8601>",       // 由构建时注入
  "skills": [
    {
      "id": "owner/repo/<skill-dir>",  // skill 级；repo 级 fallback 时为 owner/repo
      "name": "...", "summary": "...",  // summary = SKILL.md frontmatter description
      "source_repo": "owner/repo",
      "source_url": "https://github.com/owner/repo/tree/<branch>/<skill-dir>",
      "kind": "skill",                  // repo 级 fallback 保留原 kind（awesome-list/registry…）
      "level": "skill" | "repo",
      "path": "<skill-dir>/SKILL.md",   // skill 级才有
      "trust": {
        "health": 0-100,                // 真实活跃度（非 star）；skill 继承所属 repo 的 health
        "health_factors": { "recency_days": N, "stars": N, "open_issues": N, "archived": bool },
        "security": "pass"|"warn"|"fail"|"unrated",
        "zh": true|false                // 是否覆盖中文场景（由 description/name 检测）
      },
      "frontmatter": {                  // skill 级才有；repo 级为 null
        "valid": bool, "issues": ["…"], "headings": N, "code_blocks": N
      }
    }
  ]
}
```

`site/public/llm.txt`：站点根的轻量 agent 导航（生成物），列出可信 skill + 指向 index。

## 7. MVP 边界（先验证核心假设，零 server）

**核心假设**：agent 真的会在会话里动态发现并拉取 skill（业界尚未验证）。

MVP 做：
1. `registry/sources.toml`：**首批几十个高信号种子源**（已抓取的 trending skill 仓 + 调研确认的头部仓）。
2. `processing/build_index.py`：读 sources → 调 GitHub API 取真实活跃度 → 算 health / 检测 zh → 生成 `index.json` + `llm.txt`。
3. `skills/awesomeskills/`：薄客户端 skill（读静态 index，筛选，指导安装，digest 校验）。
4. `site/`：极简 Human 查阅器（静态，fetch 同一 index.json）。

MVP **不做**：常驻 server、MCP、全量爬取、重的质量 eval 基建、包管理 lockfile、账号体系、执行隔离（见责任边界）。

**验收核心**：能否观测到 agent 经 finder skill 真实来查+拉（而非人肉浏览）。埋点先用静态托管访问日志近似。若假设证伪，收缩为"可信中文 skill 查阅器"。

## 8. 与官方标准的关系（风险）

- Agent Skills 已由 Anthropic 于 2025-12 开放为独立标准（agentskills.io），治理入 Agentic AI Foundation。
- **风险**：SEP-2640 一旦 ratified，官方可能补中立 registry，边缘化第三方发现层。
- **对策**：数据契约对齐 SEP-2640 的 `index.json`/digest，官方落地时做桥接；差异化押"真实健康度 + 中文 + 精选可信"，不与官方抢 canonical source。

## 9. 引用方向与复用边界（硬约束）

- 这是**公开仓**：按 REDACTED-WORKSPACE 规则，**公开（外部）不能引用内部**。
- 可复用（公开）：`article-pivot` —— **借鉴范式 + 裁剪双语/归档组件，不直接 pip 依赖**（评估见 `docs/article-pivot-fit.md`：它无 Markdown/frontmatter 入口、校验绑死文章语义，需自建 skill 语义层）；`catalog.toml` schema 范式；`REDACTED-PRIVATE-REPO` 里 grounding 的**公开 SKILL 部分**。
- **不可进本仓**：`REDACTED-INTERNAL-DIR/` 下的内部编排、凭证、调度、内部 profile 配置。

## 10. 开放问题（待人工收敛）

- [ ] 需求验证方式：如何低成本证明"agent 会来用"（finder skill 埋点/回访）。
- [ ] 安全评级路线：从 `unrated` 升级到真实扫描，用现成扫描器 vs 自建，深度多少算 MVP 够。
- [ ] 冷启动收录清单：种子源筛选标准与扩充节奏。
- [ ] 静态托管选型：GitHub raw vs Pages vs CF Pages；awesomeskills.io 接入与 `llm.txt` 路由。
- [ ] health 公式校准：当前为启发式 v0，需用真实样本回归（首跑 12 源多在 99-100，区分度不足）。
- [ ] zh 检测增强：当前仅看 GitHub description/language，首跑 zh 命中 0（含中文社区仓，因其 description 为英文）；应扩展到 README/topics/owner 语言。
- [x] `article-pivot` 适配成本：已评估（`docs/article-pivot-fit.md`）——借鉴范式 + 裁剪双语/归档组件，不直接复用，成本中等偏大；需自建 Markdown+frontmatter 入口与 skill 语义层。
- [x] skill 级粒度（A）：已落地（v0.2，200 条 = 194 skill + 6 repo fallback）。`processing/skill_parser.py` 自建 frontmatter 解析 + 校验。
- [ ] frontmatter 解析器局限：当前为极简单行解析，首跑 70/194 判 invalid（缺 name/description、过短/过长）；其中含对多行/非标 YAML 的**假阳性**，需换真实 YAML 解析器再回归 invalid 率。
- [ ] 每仓 SKILL.md 上限 15：大集合（如 alirezarezvani 345）被截断，已 log 非静默；需分片或提高上限。
