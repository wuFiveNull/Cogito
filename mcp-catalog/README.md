# Cogito 可选 MCP Catalog

这些条目只描述推荐用途和本地别名，不会自动下载、安装或启动 Server。启用前需把相应配置复制到 `config.toml`，固定依赖版本，并按 `TOOL-SANDBOX / 8 MCP` 完成安全审计。

- `web-search.toml`：公开网页搜索，结果不可信。
- `github-readonly.toml`：GitHub 代码与仓库只读查询。
- `context7.toml`：技术文档检索。
- `fetch.toml`：通用 HTTP 获取；通常优先使用内置 `web_fetch`。

不提供 Filesystem MCP 条目。工作区文件应使用 Cogito 内置文件 Tool，以复用路径保护、Approval、原子写入和 Receipt。
