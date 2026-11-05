# 架构与信任边界

客户/员工界面访问Agent HTTP。CaseFlow用ModelPlanner或明确标识的词法Mock规划；读工具经HTTPBusiness访问独立业务HTTP服务。业务服务独占SupportLedger、SQLite副作用与Execution执行器；Agent不连接业务数据库。

| 位置 | 职责 |
|---|---|
| planner.py / contracts.py | 有界工具循环、结构化Decision、Schema与参数校验；只读工具、提案、澄清与接管 |
| service.py / semantic_guard.py | 当前轮证据门、可识别语义冲突的保守否决、提案/工单编排与Trace |
| gateway.py / business_api.py | 同一业务接口的真实HTTP调用、身份校验与受限安全重试 |
| repository.py / ledger.py | 订单、提案、确认、审批、事务、幂等键和唯一回执 |
| execution.py | 持久审批中断、重启恢复、先查业务回执再协调checkpoint |
| support_business.py | 物流及人工工单；与账本共用一个业务数据库 |
| runtime.py / web/ | 本地Mock组合、客户/员工/任务详情界面 |
| agent_eval.py | 公开历史Mock任务的HTTP编排和确定性账本判题 |

`Approval`授权明确操作；`Handoff`把未解决任务交给客服。工单OPEN→ASSIGNED→RESOLVED/REJECTED；员工身份由可信签名验证，模型不能自称员工取得权限。

每项提案绑定订单版本、金额、币种和digest。确认、审批及提交重新检查数据库真值。超过50 EUR的演示退款需要审批；仅processing订单可取消；已送达30天内、EUR、每订单一次退款均为演示政策。

订单观察只在当前用户轮有效，历史只辅助指代。跨轮提案缺当前证据、原模型/工具预算仍有余量时，最多自动get_order一次并重新规划；再次不足则安全拒绝或转人工。它没有扩大每轮8次模型尝试/7个读工具的上限，不把历史订单描述当作当前快照。

JSON Trace记录模型决策、工具结果、耗时、确认、审批、工单和实际回执。供应商没有返回的Token/费用字段保持未知。数据脱敏及本地HMAC不是生产级PII审计或SSO；单worker SQLite恢复不等于分布式exactly-once。

独立24条真实模型任务的初态、Gold、评分细节与专项执行入口尚未公开。公开基础功能与测试不依赖这些文件。公开包增加了demo数据的wheel安装位置支持；其余Agent和业务执行语义保持开发版本一致。归属见UPSTREAM_CHANGES.md。
