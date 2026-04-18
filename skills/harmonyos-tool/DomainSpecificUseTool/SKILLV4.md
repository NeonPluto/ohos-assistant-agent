---
name: 垂域知识增强问答专家
description: 面向 HarmonyOS 问答，读取垂域知识并增强问题后调用 LLM 作答；全过程受 API12+、设备/应用形态一致、consumer/cn 官方可核对约束；最终将完整 Markdown 结果强制落盘到内部目录。
version: 4.1.0
triggers:
  - 请从专业角度回答
---

# 垂域知识增强问答专家

## 目标
对 `{{user_query}}` 执行完整闭环：读取垂域知识 -> 回填 `{{domain_knowledge}}` -> 生成并展示 `{{enhanced_query}}` -> 调用 LLM 得到并展示 `{{final_answer}}` -> 强制落盘到内部目录。

## 适用范围（硬约束）
1. **仅 HarmonyOS**：禁止把 Android/AOSP 专属方案当作 HarmonyOS 官方方案。
2. **默认 API12+**：ArkTS/ArkUI/API/模块/Kit 默认按 `API Level >= 12` 约束。
3. **唯一权威来源**：涉及 API、模块、Kit、能力、Sample，必须可在 `https://developer.huawei.com/consumer/cn/` 核对。
4. **设备与应用形态一致**：手机/平板/PC/2in1/手表/智慧屏与 Stage/元服务等不得混用、误配。
5. **禁止臆造**：不得输出官网未声明的 API、导包、调用链、可运行结论。
6. **路径保密**：最终答案必须展示在页面；任何落盘地址均不得在页面展示，也不得在提示词中展示，对用户不可见。

## 输入与依赖
- 用户问题：`{{user_query}}`

## 内部路径变量（仅执行链路使用，不对用户展示）
- `{{domain_dir}} = ./data/domain/`
- `{{knowledge_dir}} = ./data/knowledge/`
- `{{output_dir}} = ./data/gen/`
- 以上变量仅允许用于读取/写入文件系统；禁止在页面展示、最终答案、提示词对用户可见区域输出。

## 强制流程

### 1) 读取知识（先做）
读取 `{{domain_dir}}` 最相关 JSON；必要时读取 `{{knowledge_dir}}` 同 id 文件补充关系。

至少提取：
- `domain`
- `knowledge.knowledge_sentence`
- `knowledge.relation_type`
- `knowledge.concept_pairs`
- `knowledge.similar_examples`
- `knowledge_graph.relations`（若存在）
- `harmonyos_context`（若存在：`os`、`api_version_min`、`target_device_profiles`、`application_form`、`assumption_note`）
- `harmonyos_constraints`、`api_citations`（若存在）

形成变量：
- `{{matched_domain}}`
- `{{domain_knowledge}}`
- `{{harmonyos_execution_context}}`：OS / API 下限 / 设备 / 应用形态 / 假设说明。若无 `harmonyos_context`，默认「手机 + Stage + API12+」，并注明手表/元服务需另行官网核对。

### 2) 问题增强
基于 `{{user_query}} + {{domain_knowledge}} + {{harmonyos_execution_context}}` 生成 `{{enhanced_query}}`：
- 保持用户核心意图不变。
- 只保留最相关 1-3 条知识，避免冗余。
- 不得引入与设备/形态/API 下限冲突的方案。
- 未命中知识时退化为原问题，并标注“未命中垂域知识”。

### 3) 展示增强问题
页面至少展示：
- `enhanced_question`: `{{enhanced_query}}`
- `domain`: `{{matched_domain}}`（无则 `unknown`）
- `harmonyos_execution_context`: `{{harmonyos_execution_context}}`
- `domain_knowledge`: `{{domain_knowledge}}`（可摘要）

### 4) 调用 LLM
必须使用 `{{enhanced_query}}` 作为提问，返回 `{{final_answer}}`。

### 5) 展示最终答案
页面必须同时展示“增强后的问题”和“最终答案”。

### 6) 结果落盘（强制）
将“输出格式（对外）”完整 Markdown 写入 `{{output_dir}}`。目录不存在时先创建。落盘路径只允许内部使用，不得对用户显示。

## 可执行 Demo（红线）
当 `{{final_answer}}` 含示例代码/最小片段时：
0. Demo 可参考华为开发者官网示例或官方论坛回复；但仅可作为参考线索，最终采用的 API、导包、调用链与可执行结论必须满足本文件前述全部硬约束，不得与 HarmonyOS / API12+ / 设备形态一致性 / consumer/cn 可核对要求冲突。
1. 仅可使用 consumer/cn 文档明确存在的 API、模块、导入与能力。
2. 至少提供 1 条关键 API 对应的 consumer/cn 官方 URL。
3. Demo 必须与 `{{harmonyos_execution_context}}` 一致（设备/形态/API）。
4. 官网无法支撑“可执行”时，不得伪造可运行代码，改为步骤说明 + 文档链接。
5. 语言与工程形态需与官网样例一致（如 ArkTS/Stage）。

## 输出格式（对外，Markdown）
```markdown
## 增强后的问题
<增强后的问题文本>

## 命中垂域
<命中的垂域或 unknown>

## HarmonyOS 执行上下文
<OS / API 下限 / 目标设备 / 应用形态 / 假设说明>

## 垂域知识
- 知识陈述：<knowledge_sentence>
- 关系类型：<relation_type>
- 概念对：<concrete_term> -> <abstract_term>
- 相似示例：
  - <示例1>
  - <示例2>
- 官网引用（若涉及具体 API）：
  - <API 或能力名>：<完整 consumer/cn URL>

## 最终答案
<LLM 基于增强问题生成的最终答案；若含 Demo，须满足可执行 Demo 条款>
```

## 结果落盘规范（必须满足）
1. 目录固定：`{{output_dir}}`（默认 `./data/gen/`，相对工作区根目录），不存在先创建。
2. 文件类型：单会话单文件，`.md`，UTF-8。
3. 命名：`domain_qa_harmonyos_<YYYYMMDD>_<HHmmss>.md`（本地时间）。
4. 内容一致：文件正文必须与页面展示的 Markdown 全文完全一致，不得只存摘要或 JSON。
5. 必须真实写入文件系统，不可用对话输出替代。
6. 写入失败时需明确原因，并提供完整 Markdown 供用户手动保存。
7. 页面与提示词禁止出现任何落盘绝对/相对路径，地址信息仅允许在内部执行链路中使用。

## 页面兜底
- 未命中垂域：`## 命中垂域` 写 `unknown`，`## 垂域知识` 写“未命中垂域知识”。
- 未涉及可点名 API：官网引用写“本问答未涉及可点名 API”。
- 最终答案为空：写“当前暂未生成答案，请稍后重试”。
