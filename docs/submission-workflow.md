# Skill 收录处理流程

## 原则：提交是指针，不是背书

收录请求只提供一个公开 GitHub 仓库指针（`owner/repo`）以及帮助维护者理解它的背景。
提交者不能填写、覆盖或协商 health、仓库 grade、security、frontmatter 完整度、中文覆盖或
community grounding；这些信任信号全部由同一条 pipeline 从公开证据重新计算。

因此，“进入 `sources.toml`”只表示允许 pipeline 评估该公开来源，不表示 awesomeskills 已经为它
背书。站点排序和展示仍以生成后的信任信号为准，来源作者、star 数或 issue 中的自述都不能绕过
计算规则。

## 当前流程：自动预检 + 对话审批闸门

`.github/workflows/submission.yml` 在带 `submission` label 的 issue 创建、编辑、重新打开或加标时
自动运行。Action 按以下顺序处理：

1. **规范化指针**：只接受公开 `github.com/owner/repo`；拒绝私有仓、内部地址、下载包和可变的
   第三方镜像，并检查 `sources.toml` 是否已收录。
2. **确认可评估内容**：查看默认分支是否存在标准命名的 `SKILL.md`。没有时记录真实形态：
   awesome-list / registry 可作为 repo-level 来源；只有 `.skill` 等归档包时，必须明确当前抓取器
   是否支持解包，不能把“包内存在”误报成“Git 树中存在”。
3. **自动形态结论**：公开、未重复、Git 树完整且形态与所选 kind 一致时标记 `pass`；树截断、
   skill 类仓缺少标准 `SKILL.md`、只有 `.skill` 包时标记 `needs_review`；私有或重复项标记 `fail`。
4. **添加指针**：`pass` 时在机器人专属分支仅把规范化后的 `id`、枚举 `kind` 和固定事实性 `note`
   加入 `registry/sources.toml`。Issue 自由文本不会进入命令或仓库。不要写 health、
   grade、security 或“可信”结论。
5. **确定性重算**：运行 `build_index.py` 生成 `base-index.json`，确认全部 source 均存在且
   health、security、frontmatter、digest 和 repo grade 完整。任何抓取失败都必须终止构建。
6. **可选增量 enrichment**：用 `detect_enrichment_changes.py` 生成待处理清单；Agent 结果必须通过
   `validate_enrichment.py` 并与当前 digest 匹配，再由 `merge_index.py` 生成公开 index。enrichment
   缺失或失败不改变可信分，也不阻止确定性 index 发布。
7. **草稿 PR 与对话闸门**：Action 把指针与确定性生成物提交到草稿 PR，并更新同一条机器人
   Issue 评论。私有控制器把仓库范围、可信分、security 汇总、pipeline diff 和精确 head SHA 写成
   review receipt；maintainer 可在受信任对话中批准、拒绝或暂缓，不需要打开 GitHub 页面。
8. **批准后的受控执行**：批准必须携带 receipt 中的完整 token。执行器重新确认 PR 仍为 open、head
   SHA 未变化、`deterministic-validation` 对该 SHA 为 success 且没有 security fail，随后才把私有
   对话决策转换为 GitHub Review 和 squash merge。SHA 或 diff 变化会使旧批准自动失效。
9. **合并后自动收尾**：等待 main 的公开产物验证成功，再快进本地公开仓并只提交父仓的 submodule
   引用。Issue 由 PR 的 `Closes #N` 自动关闭。Action 本身仍永不自动合并。

人工闸门的最低检查可以运行：

```bash
GITHUB_TOKEN=... python3 processing/validate_submission.py owner/repo
```

脚本只做公开性、重复项和标准文件形态检查，不生成信任结论，也不修改仓库。

## 自动化安全边界

Action 当前会：

1. 读取并规范化 issue 中的 repo 指针；
2. 执行与本地相同的只读校验，把结果评论回 issue；
3. 校验通过后创建一个草稿 PR，人工内容只修改 `sources.toml`；
4. 在 GitHub runner 中运行无 LLM 的完整确定性 pipeline，把机器生成物和可信信号提交到该 PR；
5. 要求 maintainer 通过私有对话 receipt 审核；失败或证据不足时保留 issue，不自动收录。

Action 不接受 issue 中的信任字段，不把提交者声明映射为评分，也不自动合并。它仅使用短期
`github.token`，不需要 Gemini、Codex 或第三方模型密钥。Issue 和目标仓内容都按不可信数据处理：
自由文本不进入 shell，仓库脚本不执行，GitHub API 或完整构建失败时 fail closed。

仓库设置必须允许 GitHub Actions 创建 Pull Request；否则预检仍会安全失败并在 workflow 日志中
留下原因。机器人 PR 由只读的 `submission-pr.yml` 在 `pull_request_target` 上复算；它只接受同仓
`automation/submission-*` 分支，检出精确 head SHA，不持久化凭据，也不使用 secrets。这个只读
check 使用独立名称，避免与 Issue pipeline 写入的 required status `deterministic-validation` 冲突。
分支保护和 SHA 绑定的 maintainer receipt 仍是合并条件；私有对话原文不公开，只在 GitHub Review
记录 decision receipt 哈希。

## 当前格式边界核验（2026-08-12）

- `simonw/claude-skills`：默认分支只有 `.gitignore` 和 `README.md`；README 明确旧文件已迁到
  `anthropics/skills` 的历史目录。不是抓漏。
- `abubakarsiddik31/claude-skills-collection`：默认分支只有 `README.md`。不是抓漏。
- `jherrodthomas/automotive-skills-suite`：Git 树没有 `SKILL.md`，但 `skills/*.skill` 是 ZIP 归档，
  抽样包内含标准 `SKILL.md`。当前 builder 只扫描 Git 树中的文件，不解包 `.skill`，所以会产生
  repo-level fallback；这是尚未支持的分发格式，不是 GitHub tree API 漏抓。
