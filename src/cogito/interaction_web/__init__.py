"""interaction-web — FastAPI 服务器：静态前端托管 + Query/Command API。

架构 (ARCH-OVERVIEW / ACCESS-DELIVERY §2.2 §2.3)：
    interaction-web → agent-api (Query API + Command API) → service/repo → SQLite

-handler 绝不直接执行 SQL，所有增删改查都经由此包暴露的服务。
"""
