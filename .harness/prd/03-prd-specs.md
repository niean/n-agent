<!-- SUMMARY: N-Agent 的迭代需求池，未经人工确认、禁止AI阅读或修改 -->
# 产品需求 - 迭代演进

## 约束
本文仅供自然人使用，未经人工确认、禁止AI阅读或修改。

## 需求列表
无论何时，你都必须遵循DDD分层规范。

[20260725]
- FR
    - 任务：顶导，新增菜单`安全`，把任务安全相关的策略和配置、放到这个页面


---

[待办]
- HE
- NFR
    - 集成：N-Agent套装，集成N-KB；发布镜像，完成一键冷启动
    - 知识：UDS，详细原理、Go样例
    - 治理：IAM，安全护栏
    - 前端：使用Element UI，重构前端代码，要求①保持功能一致、②最大限度的使用Element UI组件库(减少自己写的代码)。Element UI的项目规范，参考 /Users/niean/code/git.zuoyebang.cc/odin/odin-fe
    - 任务：LLM请求未命中缓存读，分析原因
- FR
    - 产品：对标Hermes，MoA
    - 管理：接入配置，秘钥Store(类似平台)
    - 产品：对标Manus，Project、Tenant(低优)

---

[待验证]
- 上下文
    - 消息时序，各角色是否OK
    - 工具描述，字节是否稳定
    - 缓存命中
- 工具
    - MCP支持stdio类型

