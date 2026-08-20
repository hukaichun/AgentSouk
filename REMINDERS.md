# 待你过目的事(2026-08-19,session 收尾提醒)

按急迫度排,不是按大小。看完可删,都有别处的记录兜底。

## 1. docs/paper/ 在公开站点上是可访问的

MkDocs 会把 `docs/` 下**所有**文件复制进站点,不在导航里只是没有链接,
URL 直接打就能看 —— `docs/paper/bibliography-notes.md`(arXiv 准备材料)
现在就处于这个状态。如果论文材料还不想公开,要么挪出 `docs/`,要么在
`mkdocs.yml` 加 `exclude` 配置。**这是唯一可能正在漏东西的项,先看这个。**

## 2. agent-provider-guide.md 有过时段落,已经挂在站点上

末节还写着「KYOK experimental、`api_llm_bridge.py`、no test coverage」——
那是重设计之前的状态(bridge 文件已不存在,KYOK 有完整套件覆盖)。
「AG-UI 没有 cancel 路径」「gRPC 帧」等段落也值得按现状核一遍。
站点上线让旧文档从「没人看」变成「对外陈述」,这篇需要一次更新扫描。

## 3. PyPI 名字还没拍板(其他前置已清)

- `souk` 被占(SO:UK Data Centre,活跃项目)。建议:发行名 `agentsouk`
  (已查可用)+ `import souk` 不变;动手前先查 SO:UK 包安装的**模块名**
  确认 import 层不撞。
- 迁移打包已解决(#79);剩元数据机械活(license/readme/authors、
  llm-sdk 对 provider-sdk 的版本约束)等名字定了一起做。
- gateway 的 `Private :: Do Not Upload` 闸门已按你要求摘除
  (AgentSoukServer#25)——在名字落定前,souk-server 若误传 PyPI,
  其 `souk` 依赖会解析到陌生人的包。时点仍由你定,只是不再机械封死。

## 4. 下游通知已发(2026-08-20,AgentSoukServer#33)

跟版指南已贴:强制双向握手(收件人绑定、开关已删)、breaking 的
detach API(connection 必填、改同步、新增 detach_all_for)、排队
语义、未知 event 放行、thread id 规则。#31 已留言指向 #33,可关。
gateway 侧同类欠账还有:wire 帧模型替换四份手抄映射、
`souk-client-sdk` 退役(#55 的下游半)、其 #17/#18/#20 现在都有
core 端答案可对齐。

## 5. 停车场(等你有空的设计讨论,别处已有记录)

- **Key 轮换**:「身份即 key」没有轮换故事;草案方向是旧 key 签移交声明。
- **Caller 身份**:信任模型最大的单边;thread-id-即-capability 的
  授权故事和审计归属都等它。
- **#73**(KYOK 中继审计):你选的债,约束已记在 issue 里。
- **souk 加签 actor chain**:方向已写进 `trust-and-identity.md`
  (「recorded direction, not built」),落点是 #73 和联邦。
- **横向扩展开工**:第一件事是把 `probe_multiprocess.py` 从死掉的
  pull 模型重指到现行派发(文档自己标注的 first task)。

## 6. 小事

- souk 套件里那个 `1 warning` 是 aiosqlite 连接未关的测试基建噪音,
  改动前就存在,与任何本周变更无关;哪天顺手修 conftest 的清理即可。
- `docs/` 现在是对外陈述的全集:以后 PR 里改机制时,顺手问一句
  「哪一页需要跟着改」,能避免 agent-provider-guide 这种时滞再积累。
