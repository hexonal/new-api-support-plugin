---
name: hermes-new-api-task-diagnostic
description: 当 New API / ima router 技术支持用户已经提供 task_id 或 request_id，并要求查询任务进度、任务状态、任务时间、耗时、失败原因或“你自己分析”时使用。此技能允许内部只读诊断，但对客回复不得暴露内部系统、工具、日志、数据库、project、logstore、region、IP、collection 或查询路径。
---

# New API 任务诊断

## 目标

用户已经提供 `task_...`、`request_...`、curl 或完整报错时，先使用可用的只读诊断能力自行分析，不要要求用户重复提供同一个标识。

## 触发场景

- 用户给出 `task_...` 并询问任务进度、任务状态、执行侧、任务时间、耗时或失败原因。
- 用户给出 `request_...` 并询问请求状态、路由、权限、额度或账单问题。
- 同一会话前文已给出任务/请求标识，后续说“你自己分析”“帮我查”“继续”。

## task_id 首选诊断路径

当用户给出 `task_...` 并询问状态、是否完成、时间或耗时时，必须先按下面顺序执行：

1. 首先使用 `sls_haiwai_work` 的日志查询工具查询 work 日志。
2. 查询参数固定为：
   - `project`: `ecs-liveme-api-work`
   - `logStore`: `ecs-work-us-east-1-prod`
   - `regionId`: `us-east-1`
   - `query`: 用户提供的完整 task ID
   - `from_time`: `now-7d`
   - `to_time`: `now`
   - `limit`: `30`
3. 如果第一步命中日志，直接从这些结果归纳任务状态、开始时间、完成时间和耗时。
4. 只有第一步完全未命中，才继续使用其他只读诊断路径。
5. 不要先查 MongoDB、PostgreSQL、项目列表、workspace 列表或猜测其他 project/logstore。

## 状态和时间判断

- 出现 `completed`、`successfully completed`、`成功完成`、`已完成`、结果回传成功等明确完成信号时，对客结论应为任务已完成。
- 出现明确失败、错误结束或回调失败信号时，对客结论应为任务未成功完成，并给出客户可理解的公开原因。
- 只有看到排队、执行中、轮询中且没有完成/失败信号时，才说仍在处理中。
- 用户询问“时间”“耗时”“多久”“什么时候开始/完成”时，输出开始时间、完成时间和耗时；如果只能确认其中一部分，只输出已确认的部分，不编造缺失字段。

## 内部诊断原则

- 可以使用已配置的只读诊断工具查询任务或请求。
- 不要先反问用户重复提供已有的 `task_id` / `request_id`。
- 若缺少必要上下文，只追问缺失的公开字段：请求时间、endpoint、模型、完整报错内容、脱敏 curl。
- 不要编造诊断结果；没有命中或工具不可用时，明确说当前还无法确认根因。
- joyme 等轮询类任务：直接搜 task ID 可能只看到大量 `polling attempt` / `task status: running` 日志，最终成功/失败状态被淹没。此时应改用关键词分流查询：先确认外部任务 ID，再用 `task_xxx and (completed or failed or "task status: completed" or "task status: failed")` 或外部 ID + 结果关键词定位最终状态。
- MongoDB 工具返回 Unauthorized 时：不再尝试其他 MongoDB 工具，继续按 SLS 日志链路排查；SLS 也未命中时直接告知查询工具暂时不可用。

## 对客输出硬约束

最终回复只允许包含：

- 当前结论或当前状态。
- 已确认的开始时间、完成时间、耗时。
- 用户可以理解的失败原因或未确认边界。
- 需要用户补充的公开字段。
- 下一步建议。

最终回复禁止包含：

- 内部系统名、工具名、MCP、SLS、日志平台、数据库名、project、logstore、collection、region、IP、server path。
- 工具调用过程、查询路径、原始内部日志、完整内部记录。
- “我查了 SLS / MCP / logstore / 数据库”等表述。
- 模型内部路由、work 名称、回调地址、内部任务链路、密钥、配置、文件路径。

## 推荐输出格式

命中状态时：

```text
当前状态：...
结论：...
下一步：...
```

命中状态和时间时：

```text
当前状态：已完成
开始时间：YYYY-MM-DD HH:mm:ss
完成时间：YYYY-MM-DD HH:mm:ss
耗时：约 X 分 Y 秒
```

未能确认时：

```text
当前还无法确认该任务的最终状态。
请补充请求时间、endpoint、模型和完整报错内容，我继续帮您定位。
```
