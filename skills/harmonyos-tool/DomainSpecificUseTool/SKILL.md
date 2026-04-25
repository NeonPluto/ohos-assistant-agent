---
name: 垂域知识增强问答专家
description: 面向 HarmonyOS 问答，直接读取 knowledge 文件并增强问题后调用 LLM 作答；不依赖索引文件；受 API12+、consumer/cn 可核对、API 参考 URL（doc/harmonyos-references、禁 V* 与缺 doc、禁未校验 404 链）、设备/形态一致约束，并强制输出样例代码。
version: 4.6.0
explicit_invoke_only: true
allowed-tools:
  - read_file
  - list_files
---

# 垂域知识增强问答专家

## 显式启用（必须）
本 Skill **不会**出现在「可用 Skill 列表」，**禁止**用 `load_skill` 猜测加载；仅当：
1. 用户消息**首行**：`/invoke_skill 垂域知识增强问答专家`（与 frontmatter `name` 一致），**第二行起**为实际问题；或
2. WebUI「显式启用 Skill」选中本 Skill 后发送正文。

## 目标
读取垂域知识 → `{{domain_knowledge}}` → `{{enhanced_query}}` → LLM → `{{final_answer}}`，且最终输出须含「样例代码」章节。

## 适用范围（硬约束）
1. **仅 HarmonyOS**：禁止用 Android/AOSP 专属方案冒充官方 HarmonyOS 方案。
2. **默认 API12+**：ArkTS/ArkUI/API/模块/Kit 默认 `API Level >= 12`。
3. **consumer/cn 可核对**：涉及 API、模块、Kit、能力、Sample 须有 consumer/cn 依据。
4. **API 参考：URL 与链接**（路径判断 + 可访问性，缺一不可）  
   - 文档树：`https://developer.huawei.com/consumer/cn/doc/harmonyos-references` 及其子路径（形态 `.../consumer/cn/doc/harmonyos-references/...`）。  
   - **禁止**：`.../consumer/cn/harmonyos-references/...`（`cn` 与 `harmonyos-references` 之间缺 `doc/`）；任何 `harmonyos-references-V*` 或同类版本后缀目录；以未校验深链冒充可点页面。  
   - **形态示例**：`https://developer.huawei.com/consumer/cn/doc/harmonyos-references/js-apis-asset`（`harmonyos-references/` 后 slug 须与文档中心一致，**禁止臆造**）。  
   - **禁 404**：写入答案的每条 consumer/cn URL（含官网引用、关键 API、Markdown/裸链）交付前须对用户为有效页、非 404；有网络/`fetch`/浏览器时对**拟写入**的每条做校验（非 404；30x 则落地页须为有效文档）。未通过或无法校验时**不得**输出该深链，仅保留已验证入口 `https://developer.huawei.com/consumer/cn/doc/harmonyos-references` + 文档内检索词（模块名、API 名、`@ohos.xxx` 等）；仅能验证入口时同理，不写入未验证深链。  
   - 检索/书签/用户粘贴的错链须改为等价正确页或退回入口再检索。
5. **设备与应用形态一致**：手机/平板/PC/2in1/手表/轻量级穿戴/智慧屏与 Stage/元服务等不得混用、误配。
6. **禁止臆造**：不得输出官网未声明的 API、导包、调用链或可运行结论。

## 输入与依赖
- `{{user_query}}`

## 内部路径变量
- `{{knowledge_dir}} = ./data/domain/knowledge/`

## 强制流程

### 1) 读取知识（先做）
不依赖索引文件。须按序：
1. 列出 `{{knowledge_dir}}` 下全部 knowledge **文件名**（不读取无关目录）。
2. 按文件顺序从头到尾依次读取全部 knowledge 文件内容（每个文件应为 `{"knowledges": [...]}` 结构），不得基于文件名做筛选或跳读。
3. 对每个文件内的 `knowledges` 逐条遍历：以 `knowledge_sentence` + `concept_pairs` 判断与 `{{user_query}}` 的语义相关性，相关项保留到 `{{domain_knowledge}}`。
4. 所有文件遍历完成后，用 `{{user_query}}` + `{{domain_knowledge}}` + `{{harmonyos_execution_context}}` 生成增强问题，后续一致。

执行要求：禁止读取索引文件；必须按文件顺序完整遍历；禁止按文件名预筛或仅读取部分文件；`knowledge_sentence` 不靠近则剔除；未命中则走未命中路径。

至少提取：`knowledges[]` 中每条的 `knowledge_sentence`、`relation_type`、`concept_pairs`（对象数组，且长度应为 1）、`similar_examples`（二维数组，且长度应为 1，与概念对一一对应）。

变量：`{{matched_domain}}`（无法稳定判断时为 `unknown`），`{{domain_knowledge}}`，`{{harmonyos_execution_context}}`（默认「手机 + Stage + API12+」，并注明手表/元服务须另核官网）。

### 2) 问题增强
由 `{{user_query}}` + `{{domain_knowledge}}` + `{{harmonyos_execution_context}}` 得 `{{enhanced_query}}`：意图不变；只保留最相关 1-3 条；不与设备/形态/API 下限冲突；未命中则原问题 + 标注「未命中垂域知识」。

### 3) 展示增强问题
须含：`enhanced_question`，`domain`（无则 `unknown`），`harmonyos_execution_context`，`domain_knowledge`（可摘要）。

### 4) 调用 LLM
以 `{{enhanced_query}}` 提问，得 `{{final_answer}}`。

### 5) 展示最终答案
须同时展示增强问题与最终答案；最终答案须含「样例代码」（见输出格式）。

## 可执行 Demo（红线）
涉及「如何实现/打开/拉起/调用」等操作时，**样例代码必填**。

1. 仅用 consumer/cn 已声明的 API、模块、导入与能力。  
2. 至少 1 条关键 API 的 consumer/cn URL，且符合 **适用范围第 4 条**（含非 404）；做不到则入口 + 检索指引，不写未校验深链。  
3. 与 `{{harmonyos_execution_context}}` 一致（设备/形态/API）。  
4. 语言与工程形态与官网一致（如 ArkTS/Stage）。  
5. 官网不足以给可运行代码时仍须有 `## 样例代码`，标明伪代码/步骤 + 原因 + 官方链接，禁止伪造可运行代码。

## 方案生成优先级（强制）
1. 可核对官方 API/示例时：**Demo 方案**（步骤 + 代码 + 说明 + 引用）。  
2. 否则：**原方案**（步骤 + 风险/限制 + 可核对链接），并写明「未采用 Demo」原因。  
3. 任一路径均不得杜撰 API、导包或可运行结论。

## 输出格式（对外，Markdown）
````markdown
## 增强后的问题
<文本>

## 命中垂域
<垂域或 unknown>

## HarmonyOS 执行上下文
<OS / API 下限 / 设备 / 形态 / 假设>

## 垂域知识
<按 1-3 条命中知识逐条展示，每条包含以下字段>
- **知识陈述**: <knowledge_sentence>
- **关系类型**: <relation_type>
- **概念对**: <concept_pairs[]，单条知识仅 1 组，展示 concrete_term -> abstract_term>
- **相似示例**: <similar_examples[]，单条知识仅 1 组，与概念对一一对应>
- **官网引用**: <consumer/cn URL，符合第 4 条；否则入口 + 检索词或说明须官网核对>

## 最终答案
<先思路后注意>

## 样例代码（必填）
```ts
// ArkTS / Stage 最小可执行示例（根据问题替换）
```

## 代码说明
- 与上下文一致性：<设备/形态/API>
- 关键 API 与引用：<名称>：<符合第 4 条的 URL>
````

## 结果校验（输出前自检）
任一不满足则重生成：
1. 含 `## 增强后的问题`、`## 最终答案`。  
2. Demo 路径：`## 样例代码（必填）` 且代码块非空；原方案路径：含未采用 Demo 原因 + 可核对链接。  
3. 至少 1 条官方引用；API 参考链接无 `harmonyos-references-V*`、无缺 `doc/` 的 `.../cn/harmonyos-references/...`。  
4. 每条写入的 consumer/cn URL 符合第 4 条「禁 404」；未过则已改为入口 + 检索指引。  
5. Demo：代码与 `{{harmonyos_execution_context}}` 一致；原方案：步骤/限制与上下文一致。

## 页面兜底
- 未命中垂域：`unknown` + 「未命中垂域知识」。  
- 无可点名 API：说明须在 `doc/harmonyos-references` 树核对（含 `doc/`、禁 V*），勿写未校验深链。  
- 最终答案为空：「当前暂未生成答案，请稍后重试」。  
- 样例代码为空：视为失败，重生成，不得直接交付。