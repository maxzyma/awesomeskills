# Skill 收录处理流程

## 原则：提交是指针，不是背书

收录请求只提供一个公开 GitHub 仓库指针（`owner/repo`）以及帮助维护者理解它的背景。
提交者不能填写、覆盖或协商 health、仓库 grade、security、frontmatter 完整度、中文覆盖或
community grounding；这些信任信号全部由同一条 pipeline 从公开证据重新计算。

因此，“进入 `sources.toml`”只表示允许 pipeline 评估该公开来源，不表示 awesomeskills 已经为它
背书。站点排序和展示仍以生成后的信任信号为准，来源作者、star 数或 issue 中的自述都不能绕过
计算规则。

## MVP：人工闸门

维护者按以下顺序处理带 `submission` label 的 issue：

1. **规范化指针**：只接受公开 `github.com/owner/repo`；拒绝私有仓、内部地址、下载包和可变的
   第三方镜像，并检查 `sources.toml` 是否已收录。
2. **确认可评估内容**：查看默认分支是否存在标准命名的 `SKILL.md`。没有时记录真实形态：
   awesome-list / registry 可作为 repo-level 来源；只有 `.skill` 等归档包时，必须明确当前抓取器
   是否支持解包，不能把“包内存在”误报成“Git 树中存在”。
3. **人工范围闸门**：确认仓库与 agent skill 发现相关、没有明显的冒充或所有权异常，issue 信息足以
   解释为何值得花 pipeline 成本。这里不人工打可信分。
4. **添加指针**：仅把 `id`、`kind` 和事实性 `note` 加入 `registry/sources.toml`。不要写 health、
   grade、security 或“可信”结论。
5. **确定性重算**：运行 `build_index.py` 生成 `base-index.json`，确认全部 source 均存在且
   health、security、frontmatter、digest 和 repo grade 完整。任何抓取失败都必须终止构建。
6. **可选增量 enrichment**：用 `detect_enrichment_changes.py` 生成待处理清单；Agent 结果必须通过
   `validate_enrichment.py` 并与当前 digest 匹配，再由 `merge_index.py` 生成公开 index。enrichment
   缺失或失败不改变可信分，也不阻止确定性 index 发布。
7. **给出可审计结论**：PR/issue 记录“收录 / 暂缓 / 拒绝”的原因和 pipeline 证据。低分或 warning
   不应被人工改成 pass；若风险超出展示边界，可以不收录并说明政策依据。

人工闸门的最低检查可以运行：

```bash
GITHUB_TOKEN=... python3 processing/validate_submission.py owner/repo
```

脚本只做公开性、重复项和标准文件形态检查，不生成信任结论，也不修改仓库。

## 后续：GitHub Action 半自动开 PR

后续 Action 可以在 `submission` issue 创建或修改时：

1. 读取并规范化 issue 中的 repo 指针；
2. 执行与本地相同的只读校验，把结果评论回 issue；
3. 校验通过后创建一个草稿 PR，且只修改 `sources.toml`；
4. 在受保护环境中运行完整 pipeline，把生成物与机器计算的信任信号提交到该 PR；
5. 要求 maintainer 审核后合并，失败或证据不足时保留 issue，不自动收录。

Action 不应接受 issue 中的信任字段，不应把提交者声明映射为评分，也不应自动合并。凭据只放在
GitHub Actions secrets/environment 中；来自 fork 或不可信 issue 文本的内容不能进入 shell 命令。

## 当前格式边界核验（2026-08-12）

- `simonw/claude-skills`：默认分支只有 `.gitignore` 和 `README.md`；README 明确旧文件已迁到
  `anthropics/skills` 的历史目录。不是抓漏。
- `abubakarsiddik31/claude-skills-collection`：默认分支只有 `README.md`。不是抓漏。
- `jherrodthomas/automotive-skills-suite`：Git 树没有 `SKILL.md`，但 `skills/*.skill` 是 ZIP 归档，
  抽样包内含标准 `SKILL.md`。当前 builder 只扫描 Git 树中的文件，不解包 `.skill`，所以会产生
  repo-level fallback；这是尚未支持的分发格式，不是 GitHub tree API 漏抓。
