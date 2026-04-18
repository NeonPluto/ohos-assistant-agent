---
name: Domain-Specific Concept Mining
description: 从用户提供的检索失败Badcase中，分析具体实例与抽象概念之间的语义关系，并输出结构化的垂域知识陈述，用于增强大模型对领域内概念层级关系的理解。
version: 1.0.1
allowed-tools: 
model: haiku
---

# SKILL：垂域概念关系提炼与持久化

## 触发方式
当用户输入包含以下关键词组合时自动激活：
- “Badcase 原始问题” + “缺失的关键答案/知识标题”
- “提炼概念关系” + 具体问题描述

## 执行流程
1. **解析输入**  
   提取 `原始问题` 和 `缺失答案标题`。
2. **关系推断**  
   识别具体实例（如 App 名称、操作对象）与抽象概念（如“三方应用”、“系统功能”）之间的语义关系类型：上下位关系 / 同义关系 / 场景包含关系。
3. **生成知识陈述**  
   输出一句完整的领域知识陈述句，明确映射关系。
4. **补充同类示例**  
   列举至少 2 个同类具体实例，增强泛化能力。
5. **持久化保存**  
   将生成的 JSON 写入本地文件 `./data/domain/{description}.json`。若目录不存在则自动创建。

## 输出格式约束
必须严格遵循以下 JSON Schema：

{
  "description": "所属垂域（英文/拼音命名，用于文件名）",
  "knowledge_sentence": "完整陈述句",
  "relation_type": "上下位关系 / 同义关系 / 场景包含关系",
  "concept_pairs": {
    "concrete_term": "具体实例",
    "abstract_term": "抽象概念"
  },
  "similar_examples": ["实例A", "实例B", "实例C"]
}

## 持久化规则
- 文件路径：`./data/domain/{description}.json`
- 若文件已存在，则追加新知识（需设计为数组结构）或覆盖（根据用户偏好设定，默认为覆盖提醒）。

## 用户交互示例
**用户输入：**
> Badcase 原始问题：为什么爱奇艺下载的视频在相册里看不到  
> 缺失的关键答案标题：三方应用下载的视频为何不在系统相册显示

**助手输出（保存至 `./data/domain/三方应用视频存储.json`）：**
{
  "description": "三方应用视频存储",
  "knowledge_sentence": "“爱奇艺”、“腾讯视频”、“优酷”等视频平台是“三方应用”的下位词，它们下载的视频通常存储在应用私有目录，不会自动同步至系统相册。讨论“爱奇艺下载视频看不到”本质上是讨论“三方应用私有存储与系统相册隔离”的问题。",
  "relation_type": "上下位关系",
  "concept_pairs": {
    "concrete_term": "爱奇艺",
    "abstract_term": "三方应用"
  },
  "similar_examples": ["腾讯视频", "优酷", "芒果TV", "Bilibili"]
}