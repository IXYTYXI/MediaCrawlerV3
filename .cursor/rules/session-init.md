# 会话初始化规则

## 规则说明
每次新会话开始时，AI 助手应自动执行以下操作：

1. **切换工作目录** 到 `/data/vonjan/program/MediaCrawler-stable`
2. **读取项目上下文** 文件 `.cursor/rules/project-context.md`
3. **不要要求用户重复说明** 已有的背景信息
4. **使用中文简体** 回复所有消息
5. **查找历史记录时** 搜索 `/root/.cursor/projects/` 下所有子目录的 `agent-transcripts`，而非仅当前工作区

## 会话结束时
- 将重要的工作进展更新到 `project-context.md` 的「待办事项」和「历史工作记录」中
- 确保下次会话可以无缝衔接
