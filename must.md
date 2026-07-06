# 环境
使用conda创建并激活使用cogito 要求python3.12,以后都使用这个环境
# framework
read "D:\Code\PythonCode\cogito-v1\.md\cogito-agent-initial-framework-spec.md" 框架不需要完全实现，按照文档上，只需要实现初始部分，后续逐步补齐每一phase
# Bus
read "D:\Code\PythonCode\cogito-v1\.md\message-system-plan.md" and 在此基础上实现，禁止功能越界
# config
所有配置文件放到config文件夹下，apikey在开发阶段保持明文存放！
# workspace
目前将workspace放到"D:\Code\PythonCode\cogito-v1\.workspace"下
# database
数据库的设计在"E:\WJH\Code\PythonCode\cogito-v1\.md\personal_agent_sqlite_database_design.md"上实现，其他部件需要增删改查数据库，必须调用数据库的服务
# 使用真实模型进行测试和开发
你可以使用我的真实模型进行测试和开发，前提是省着点用，同时，尽量不要使用规则化，正则化的匹配，要足够智能。
# channel
channel有很多，以后会加入qq,微信，telegram,cli,web，这些要能复用kernel。web也是channel，不是测试用的！！！！