# article-pivot 复用评估（面向 SKILL.md 处理层）

> 评估方式：只读调研 article-pivot 源码（canonical AST / validation / translation / adapter 契约）。
> 结论用于 awesomeskills 的 AI-ready 处理层选型。

## 结论：借鉴范式，不直接复用。适配成本：中等偏大。

article-pivot 是**面向"文章"的确定性内容处理库**，数据流固定为
`RawSnapshot → CanonicalDocument → TranslationOverlay → CanonicalPackage → Archive/Publication`。
它能贡献"内容处理骨架 + 高质量中英双语正文渲染 + dry-run 归档范式"这半边；
**帮不上 skill 的核心**——frontmatter 规范校验、scripts 建模、skill 专属元数据。

## 能借的（真金，可裁剪复用）

1. **双语渲染**：`TranslationOverlay`（block_id 分段翻译）+ `render_bilingual_markdown`（中文优先双语，连公式/表格/引用都处理）。若 awesomeskills 双语目标含"正文逐段中英对照"，这块最有复用价值、质量高。
2. **canonical AST 范式**：`Block`/`InlineNode`（稳定 id、to_dict/from_dict、walk）——作 skill 正文中间表示合理。
3. **工程范式**：adapter `Protocol` 契约 + dry-run-first 归档（plan/write/verify、拒绝覆盖、生成 `.metadata.json` 机器可读元数据）。这套"确定性、可 dry-run、产机器可读 metadata"值得照搬。

## 帮不上的（必须自建）

- **无 Markdown / frontmatter 入口**：库里所有 `parse()` 都是 HTML→Block（BeautifulSoup），没有 Markdown 解析器、没有 YAML 解析器（grep 到的 frontmatter 全在**输出端**）。要自写 Markdown→Block + 把 frontmatter 建成一等公民。
- **校验绑死文章语义**：`validate_document` 校验的是正文结构（heading 层级/code/math/block id），且**强制 `source.canonical_url` + `revision.source_hash`**；归档/发布还强制 `published_at` 和 editorial 的 category/key_points/glossary。skill 天然没有这些，直接用会判失败或要造假值。skill 要的"规范校验"（name 命名规则、description 达标、scripts/ 结构）它一条都不覆盖。
- **scripts/ 无模型**：`assets` 实际只建模 image；`code` block 是"正文内联片段"，语义 ≠ 仓里的可执行脚本文件。
- **产出 schema 不对口**：归档 metadata 是 `article-archive.v1`（title_zh/source_url/author/published_at…），与 skill 元数据几乎无重叠，需重定义。
- **无 adapter 注册表**：CLI 把 adapter 硬编码，新增要改源码，非配置化插拔。

## 建议路径

在 article-pivot 的 **AST + adapter Protocol 范式之上**，补齐 Markdown+frontmatter 入口和 skill 语义层（frontmatter 校验、scripts 建模、skill metadata schema），并**裁剪复用**其双语渲染/归档组件。**不做 pip 直接依赖**（避免继承文章强制字段与生命周期耦合）。

## 成本拆分

| 部件 | 成本 | 说明 |
|---|---|---|
| Markdown→Block 解析器 | 中 | 库里完全没有 |
| frontmatter 一等公民建模 + 校验规则 | 中（全新） | skill 核心，库不覆盖 |
| skill source adapter | 小-中 | 去 URL 化、本地仓语义 |
| skill metadata schema | 小-中 | article-archive.v1 不能直接用 |
| 翻译 / 双语渲染 | 小 | 基本可拿来用 |

**一句话**：省下的是"确定性内容 pipeline 骨架 + 双语渲染"，要新写的是"frontmatter/scripts 语义那一半"。
