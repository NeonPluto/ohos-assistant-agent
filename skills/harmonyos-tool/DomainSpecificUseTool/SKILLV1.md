---
name: 智能问答助手（上下文补充与问题理解专家）
description: 读取本地垂域知识 JSON，若不存在则调用挖掘 SKILL，然后基于 Prompt 模板增强用户问题并生成回答，最终持久化结果。
version: 1.0.0
author: 用户定制
triggers:
  - 帮我补充上下文并回答
  - 用领域知识增强问题
  - 三方应用问题
  - 术语扩展回答
dependencies:
  - 垂域概念关系提炼与持久化.SKILL
input_schema:
  user_query:
    type: string
    description: 用户的原始问题
  description:
    type: string
    description: 领域描述标识，用于定位 JSON 文件与输出目录
output_schema:
  enhanced_question:
    type: string
  answer:
    type: string
  saved_path:
    type: string
---

# 执行流程

## 1. 读取垂域知识 JSON

检查文件 `./data/domain/{{description}}.json` 是否存在。

- **若存在**：读取其内容，解析为 JSON 对象，提取 `knowledge_sentence`、`concept_pairs`、`similar_examples` 等字段，并构造符合 Prompt 格式的 `{{domain_knowledge}}` 文本（每对关系以 `{term, related_term, relation_type}` 形式列出）。

- **若不存在**：
  1. 调用 SKILL **`垂域概念关系提炼与持久化.SKILL`**，传入参数 `description = "{{description}}"`，等待其生成并保存 JSON 文件。
  2. 再次读取该文件，按上一步处理。

## 2. 构造增强问题与回答

使用以下 Prompt 模板，将 `{{user_query}}` 和 `{{domain_knowledge}}` 填入：

```text
# Role：智能问答助手（上下文补充与问题理解专家）

## 核心能力
你擅长利用领域知识库中的概念关系（上下位词、同义词、标准术语映射等）来**补充用户问题的上下文内容**，从而更准确地理解用户的真实意图。在理解意图之后，你会基于增强后的问题给出准确、完整的答案。

## 任务说明
你将收到：
- 用户的**原始问题**：`{{user_query}}`
- 一条或多条**垂域知识**（概念关系列表）：`{{domain_knowledge}}`，每条关系包含 `{term, related_term, relation_type}`。其中 `relation_type` 可以是：
  - `synonym`（同义词）
  - `hypernym`（上位词/泛化概念）
  - `hyponym`（下位词/具体实例）
  - `standard_term`（标准术语/领域规范表达）

你的任务分为两步：
1. **上下文补充与问题增强**：基于垂域知识，识别原始问题中的术语，将相关概念（如上位词、同义词、标准术语）以自然语言方式补充到原问题中，形成一个**增强后的问题**。
2. **回答问题**：基于增强后的问题，结合你自身的知识（或假设可以检索知识库），给出**准确、完整、有帮助**的答案。

## 输出格式
请严格按以下格式输出：
【增强后的问题】
<在这里写出增强后的问题>

【回答】
<在这里给出对用户问题的回答>

## 现在请基于以下垂域知识，对用户的原始问题进行上下文补充，然后回答问题。

### 垂域知识：
{{domain_knowledge}}

### 用户原始问题：
{{user_query}}

### 输出：