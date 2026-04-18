---
name: 垂域知识增强问答专家
description: 面向用户问答场景，自动读取垂域概念关系与知识图谱数据，回填 domain_knowledge，生成并展示增强问题，再将增强问题发送给 LLM 作答并展示最终答案；最终须将符合「输出格式（对外）」的 Markdown 全文落盘至 ./data/gen/。
version: 3.0.0
triggers:
  - 请从专业角度回答
---

# 垂域知识增强问答专家

## 目标
针对用户输入问题，完成以下闭环：
1. 读取由「垂域概念关系提炼与知识图谱生成专家」产生的垂域知识。
2. 将可用知识回填到 `{{domain_knowledge}}`。
3. 基于 `{{domain_knowledge}}` 增强用户问题，生成 `{{enhanced_query}}`。
4. 在页面显示增强后的问题。
5. 将 `{{enhanced_query}}` 发送给 LLM 获取答案 `{{final_answer}}`。
6. 在页面显示最终答案。
7. 将最终结构化结果持久化到 `./data/gen/` 目录。

## 输入与依赖
- 用户原始问题：`{{user_query}}`
- 垂域结构化数据目录：`./data/domain/`
- 垂域知识图谱目录：`./data/knowledge/`
- 结果输出目录：`./data/gen/`

## 自动触发规则（开头匹配）
- 当用户输入以固定前缀 `请从专业角度回答` 开头（prefix match）时，自动调用本 SKILL。
- 未命中该固定前缀时，不自动触发本 SKILL。

## 强制执行流程

### 第一步：读取垂域知识（必须先执行）
优先读取 `./data/domain/` 下相关 JSON 文件；必要时补充读取 `./data/knowledge/` 进行关系校验。

调用要求：
```text
请使用 [tool:read_file] 读取 ./data/domain/ 下与用户问题语义最相关的知识文件。
如需关系补充，请再读取 ./data/knowledge/ 对应同 id 文件。
```

读取后，提取并回填：
- `domain`
- `knowledge.knowledge_sentence`
- `knowledge.relation_type`
- `knowledge.concept_pairs`
- `knowledge.similar_examples`
- `knowledge_graph.relations`（若存在）

将以上内容整合为：
- `{{domain_knowledge}}`：可供提示词使用的简洁知识块
- `{{matched_domain}}`：当前命中的垂域名

### 第二步：问题增强
基于 `{{user_query}}` 和 `{{domain_knowledge}}` 生成 `{{enhanced_query}}`。

增强原则：
- 不改变用户问题核心意图与动作。
- 对关键术语补充上位词/同义词/标准表达（如“即...”“属于...”）。
- 多条知识命中时，仅保留最相关 1-3 条，避免冗余。
- 若未命中有效知识，则 `{{enhanced_query}}` 退化为原问题，并标注“未命中垂域知识”。

### 第三步：页面展示增强问题
必须将增强结果写入页面可展示区，字段至少包括：
- `enhanced_question`: `{{enhanced_query}}`
- `domain`: `{{matched_domain}}`（未命中可为 `"unknown"`）
- `domain_knowledge`: `{{domain_knowledge}}`（可摘要）

### 第四步：调用 LLM 作答
将 `{{enhanced_query}}` 作为最终提问发送给 LLM，不可回退为原问题。
LLM 返回结果保存为 `{{final_answer}}`。

### 第五步：页面展示最终答案
页面最终必须同时展示：
- 增强后的问题
- 最终答案

### 第六步：结果持久化到 `./data/gen/`
本步为**强制步骤**，须按下文「结果落盘规范（必须满足）」执行；仅完成页面展示而未落盘视为未完成本 SKILL。

调用要求（示例）：
```text
请使用 [tool:write_file]（或等价写入能力）将「输出格式（对外）」的完整 Markdown 写入 ./data/gen/ 下文件。
若 ./data/gen/ 不存在，须先创建目录再写入。
```

## 输出格式（对外）
请使用 **Markdown** 完整输出，不要使用 JSON 结构，不要输出 JSON 代码块。

请严格按以下 Markdown 结构输出（不要附加多余说明）：
```markdown
## 增强后的问题
<增强后的问题文本>

## 命中垂域
<命中的垂域或 unknown>

## 垂域知识
- 知识陈述：<knowledge_sentence>
- 关系类型：<relation_type>
- 概念对：<concrete_term> -> <abstract_term>
- 相似示例：
  - <示例1>
  - <示例2>

## 最终答案
<LLM基于增强问题生成的最终答案>
```

## 结果落盘规范（必须满足）

参考「垂域概念关系提炼与知识图谱生成专家」对落盘路径与内容一致性的要求，本 SKILL 的问答结果须**同时满足**：

1. **落盘目录**：固定为 `./data/gen/`（相对工作区根目录）；目录不存在时须先创建再写入。
2. **文件形态**：单文件单会话，扩展名 `.md`，编码 **UTF-8**。
3. **命名规则**：`domain_qa_<YYYYMMDD>_<HHmmss>.md`，时间戳为生成时的本地时间（示例：`domain_qa_20260416_153045.md`）。
4. **内容一致性**：文件正文必须与「输出格式（对外）」结构一致，且与页面展示的 Markdown 全文**相同**（含各 `##` 区块及兜底文案），不得仅保存摘要或 JSON。
5. **写入动作**：必须实际写入文件系统（如 `[tool:write_file]`）；**禁止**仅以对话输出代替落盘。
6. **失败兜底**：若因环境限制无法写入，须在页面明确说明原因，并仍尽量给出完整 Markdown 供用户手动保存；能写入时不得省略落盘。

## 约束
- 必须先读取垂域知识，再增强问题，再调用 LLM。
- `domain_knowledge` 必须来自 `./data/domain/` 或 `./data/knowledge/` 的真实内容，不能臆造。
- 最终回答必须基于增强问题，而不是原始问题。
- 页面至少展示两个区块：“增强后的问题”“最终答案”。
- 页面展示必须使用 Markdown 完整内容渲染，不使用 JSON 字段展示。
- 生成结果必须落盘到 `./data/gen/`（Markdown 文件），并满足「结果落盘规范（必须满足）」全部条目。

## 页面展示约定（Markdown）
- 页面直接渲染 Markdown 全文，不做 JSON 字段拆分后展示。
- 最小渲染要求：至少包含 `## 增强后的问题` 与 `## 最终答案` 两个二级标题区块。
- 兜底规则：
  - 未命中垂域时，在 `## 命中垂域` 展示 `unknown`。
  - 缺少垂域知识时，在 `## 垂域知识` 写明“未命中垂域知识”。
  - `## 最终答案` 为空时，展示“当前暂未生成答案，请稍后重试”。
