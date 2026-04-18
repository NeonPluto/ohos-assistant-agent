# 工程反思

## 目前完全依赖[demo](./agents/s_full_langgraph_multisession_fs.py)进行流程和工程控制,当前项目流程与结构不够清晰
### 修改点
1. 根据 TOOL(限制工具范围,避免LLM泛化理解而导致的错误操作、权限窜改等问题) 设置TOOL白名单, 指定每个TOOL的权限约束和权限检查(即调用过程中,或者session创建后需要生成工具权限的白名单或者黑名单).
2. 限制SKILL调用逻辑,需参考业内的实现,默认的对话过程中不携带任何SKILL或者TOOL, 仅通过上文管理才能加载指定的SKILL.
3. 关于SKILL中调用TOOL的逻辑,必须要显式指定,如果不在SKILL中声明的TOOL,不允许被调用,避免不同的SKILL之间的业务打架.
4. 关于权限的问题,理论上每个页面可见的TOOL和SKILL应该是跟着权限走, 以当前版本参考(2026-04-18的版本),目前存在两个SKILL
- 垂域概念关系提炼与知识图谱生成专家
    - 理论上[垂域概念关系提炼与知识图谱生成专家](./skills/harmonyos-tool/DomainSpecificConceptMining/SKILLV9.md)这个 SKILL是给内部运营人员使用,能将内部的知识以一种合规的方式披露在外网使用
- 垂域知识增强问答专家
    - 理论上[垂域知识增强问答专家](./skills/harmonyos-tool/DomainSpecificUseTool/SKILL10.md) 这个SKILL是给外部开发者使用,能够基于我们已识别的问题来进行回答,避免用户泛化的输入而导致的答非所问