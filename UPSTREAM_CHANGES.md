# 上游归属与公开增量

上游：[nitanandsinghupw/langgraph-support-agent](https://github.com/nitanandsinghupw/langgraph-support-agent)，固定提交 `b6fe9faebff8a1af549ae7b8cabcb0b6e6479cec`，MIT。LICENSE原始版权为Copyright (c) 2026 Nitanand Singh，完整保留。NOTICE保留来源说明。

上游已有LangGraph工具循环、词法政策检索、代码政策、人工中断、SQLite checkpoint选项、离线模型、24项测试及11个场景，不计为个人新增贡献。`support_agent`及其原始测试/数据属于上游。

CaseFlow在其上增量实现结构化提案、确认/审批与账本、幂等和恢复，随后扩展ModelPlanner、独立业务HTTP、工单、Trace、UI及评测。公开源快照来自开发提交28437c4；没有复制其Git历史或未运行评测。

公开打包适配不改变业务或评分语义：support_agent/service.py增加sys.prefix/share/caseflow数据查找后备；pyproject打包7份公开演示数据并锁定构建工具；两套历史评测仅将Git信息改为可选、限定源码指纹范围。

| 公开文件 | 归属/修改 |
|---|---|
| `.env.example` | 上游原件（换行规范化比较） |
| `.gitattributes` | 公开版文档、配置或脱敏证据投影 |
| `.github/workflows/ci.yml` | 公开版文档、配置或脱敏证据投影 |
| `.gitignore` | 上游原件（换行规范化比较） |
| `ARCHITECTURE.md` | 公开版文档、配置或脱敏证据投影 |
| `data/agent_eval_v1.json` | CaseFlow已公开/已观察的合成演示或历史评测数据 |
| `data/business_extra.json` | CaseFlow已公开/已观察的合成演示或历史评测数据 |
| `data/caseflow_tasks.json` | CaseFlow已公开/已观察的合成演示或历史评测数据 |
| `data/policies.md` | 上游原件（换行规范化比较） |
| `data/policies_zh.md` | CaseFlow已公开/已观察的合成演示或历史评测数据 |
| `data/scenarios.json` | 上游原件（换行规范化比较） |
| `data/store.json` | 上游原件（换行规范化比较） |
| `docs/EVALUATION.md` | 公开版文档、配置或脱敏证据投影 |
| `docs/evidence/historical-smoke.json` | 公开版文档、配置或脱敏证据投影 |
| `docs/PUBLICATION_REPORT.md` | 公开版文档、配置或脱敏证据投影 |
| `docs/UPSTREAM_README.md` | 上游README归属副本；只修正docs位置的相对链接 |
| `LICENSE` | 上游原件（换行规范化比较） |
| `NOTICE` | 公开版文档、配置或脱敏证据投影 |
| `pyproject.toml` | 公开版文档、配置或脱敏证据投影 |
| `README.md` | 公开版文档、配置或脱敏证据投影 |
| `requirements-lock.txt` | 公开版文档、配置或脱敏证据投影 |
| `RESULTS.md` | 公开版文档、配置或脱敏证据投影 |
| `scripts/check_public_snapshot.py` | 公开版文档、配置或脱敏证据投影 |
| `src/caseflow/__init__.py` | CaseFlow工程增量；与来源开发版一致 |
| `src/caseflow/__main__.py` | CaseFlow工程增量；与来源开发版一致 |
| `src/caseflow/agent_eval.py` | CaseFlow增量；公开副本仅适配可选Git元数据/包源码指纹 |
| `src/caseflow/api.py` | CaseFlow工程增量；与来源开发版一致 |
| `src/caseflow/auth.py` | CaseFlow工程增量；与来源开发版一致 |
| `src/caseflow/baseline.py` | CaseFlow工程增量；与来源开发版一致 |
| `src/caseflow/business_api.py` | CaseFlow工程增量；与来源开发版一致 |
| `src/caseflow/contracts.py` | CaseFlow工程增量；与来源开发版一致 |
| `src/caseflow/demo_planner.py` | CaseFlow工程增量；与来源开发版一致 |
| `src/caseflow/evaluate.py` | CaseFlow增量；公开副本仅适配可选Git元数据/包源码指纹 |
| `src/caseflow/execution.py` | CaseFlow工程增量；与来源开发版一致 |
| `src/caseflow/gateway.py` | CaseFlow工程增量；与来源开发版一致 |
| `src/caseflow/ledger.py` | CaseFlow工程增量；与来源开发版一致 |
| `src/caseflow/local_server.py` | CaseFlow工程增量；与来源开发版一致 |
| `src/caseflow/planner.py` | CaseFlow工程增量；与来源开发版一致 |
| `src/caseflow/policy_search.py` | CaseFlow工程增量；与来源开发版一致 |
| `src/caseflow/redaction.py` | CaseFlow工程增量；与来源开发版一致 |
| `src/caseflow/repository.py` | CaseFlow工程增量；与来源开发版一致 |
| `src/caseflow/runtime.py` | CaseFlow工程增量；与来源开发版一致 |
| `src/caseflow/semantic_guard.py` | CaseFlow工程增量；与来源开发版一致 |
| `src/caseflow/service.py` | CaseFlow工程增量；与来源开发版一致 |
| `src/caseflow/settings.py` | CaseFlow工程增量；与来源开发版一致 |
| `src/caseflow/source_info.py` | 公开包新增元数据兼容辅助，不参与业务或判分 |
| `src/caseflow/spending.py` | CaseFlow工程增量；与来源开发版一致 |
| `src/caseflow/support_business.py` | CaseFlow工程增量；与来源开发版一致 |
| `src/caseflow/telemetry.py` | CaseFlow工程增量；与来源开发版一致 |
| `src/caseflow/web/app.css` | CaseFlow工程增量；与来源开发版一致 |
| `src/caseflow/web/app.js` | CaseFlow工程增量；与来源开发版一致 |
| `src/caseflow/web/index.html` | CaseFlow工程增量；与来源开发版一致 |
| `src/support_agent/__init__.py` | 上游原模块，保留版权与实现 |
| `src/support_agent/__main__.py` | 上游原模块，保留版权与实现 |
| `src/support_agent/api.py` | 上游原模块，保留版权与实现 |
| `src/support_agent/evaluate.py` | 上游原模块，保留版权与实现 |
| `src/support_agent/graph.py` | 上游原模块，保留版权与实现 |
| `src/support_agent/models.py` | 上游原模块，保留版权与实现 |
| `src/support_agent/offline_model.py` | 上游原模块，保留版权与实现 |
| `src/support_agent/policies_kb.py` | 上游原模块，保留版权与实现 |
| `src/support_agent/policy.py` | 上游原模块，保留版权与实现 |
| `src/support_agent/service.py` | 上游原模块；公开副本增加安装包演示数据定位后备 |
| `src/support_agent/store.py` | 上游原模块，保留版权与实现 |
| `src/support_agent/tools.py` | 上游原模块，保留版权与实现 |
| `tests/caseflow/http_support.py` | CaseFlow回归测试；公开子集原文件保持 |
| `tests/caseflow/test_agent_eval.py` | CaseFlow回归测试；公开子集原文件保持 |
| `tests/caseflow/test_business_http.py` | CaseFlow回归测试；公开子集原文件保持 |
| `tests/caseflow/test_cross_turn_evidence.py` | CaseFlow回归测试；公开子集原文件保持 |
| `tests/caseflow/test_deepseek_protocol.py` | CaseFlow回归测试；公开子集原文件保持 |
| `tests/caseflow/test_evaluation.py` | CaseFlow回归测试；公开子集原文件保持 |
| `tests/caseflow/test_ledger.py` | CaseFlow回归测试；公开子集原文件保持 |
| `tests/caseflow/test_proposals.py` | CaseFlow回归测试；公开子集原文件保持 |
| `tests/caseflow/test_protocol_fairness.py` | CaseFlow回归测试；公开子集原文件保持 |
| `tests/caseflow/test_provider.py` | CaseFlow回归测试；公开子集原文件保持 |
| `tests/caseflow/test_semantic_refinement.py` | CaseFlow回归测试；公开子集原文件保持 |
| `tests/caseflow/test_upgrade_model.py` | CaseFlow回归测试；公开子集原文件保持 |
| `tests/caseflow/test_web_runtime.py` | CaseFlow回归测试；公开子集原文件保持 |
| `tests/conftest.py` | 上游原件（换行规范化比较） |
| `tests/test_api.py` | 上游原件（换行规范化比较） |
| `tests/test_conversations.py` | 上游原件（换行规范化比较） |
| `tests/test_policy_and_tools.py` | 上游原件（换行规范化比较） |

| `examples/scenarios.txt` | 上游原始合成对话示例，保留归属 |
| `repository-metadata.json` | 建议仓库元数据，不表示已创建远端 |
| `docs/evidence/release-verification.json` | 本轮公开包验收摘要，未包含原始日志或数据库 |
| `PUBLICATION_MANIFEST.json` | 生成的公开文件校验清单 |

私有完整开发仓库、原始证据及未运行评测不在公开快照中，未删除或改写。逐文件公开完整性校验另见PUBLICATION_MANIFEST.json。
