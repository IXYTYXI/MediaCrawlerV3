# Web 任务配置目录

本目录存放 **Web 页面创建的任务** 的配置（每个任务一个 `{task_id}.json`）。  
通过 Dashboard 的「任务」功能新建、启动、停止任务时，只会读写本目录和运行时的 main.py 参数，**不会修改下面这些数据**：

## 18 位作者相关数据（请勿删除）

- **批量进度**：`data/batch_progress_task_20260210.json`
- **各作者增量进度**：`data/xhs/progress/creator_*_progress.json`
- **已爬作品数据**：`data/xhs/json/creator_contents_*.json`

上述文件仅在被执行「A 部门 / 18 位作者」的**创作者模式**爬取时使用；在 Web 上开**新任务**（例如 B 部门搜索）与它们**完全隔离**，不会覆盖或清空这些数据。

新任务仅通过 `config/tasks/{task_id}.json` 传参给 main.py，产出数据会写到新的 JSON 文件（如 `search_contents_*.json`），与作者进度互不影响。

## 导出与导入

- **导出**：在 Dashboard 任务列表中点击某任务的「导出」，可下载该任务的 JSON 配置文件（不含运行时状态），便于备份或迁移。
- **导入**：点击「导入任务」选择之前导出的 JSON 文件，或通过 API `POST /api/dashboard/tasks/import`（JSON 请求体）、`POST /api/dashboard/tasks/import/file`（上传文件）导入。导入后会生成新的 `task_id`，不会覆盖已有任务，可直接在新任务上继续使用。
