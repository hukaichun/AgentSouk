# The agent loop and its injection points

A reference model, not a funduq mechanism. It exists because "what can a
caller do to a running agent?" kept getting answered per-protocol, and the
answers were not comparable. This page answers it once, against the machine
underneath, and then reads each protocol's operations off it.

## The machine

```
loop:
  ●①
      completion = model(messages, tools)      ← 生成中，不可注入
  ●①
      if stop_reason == tool_use:
          for call in tool_calls:
  ●②
                  result = execute(call)
  ●③
                  messages.append(result)
              continue                         ← 被迫的
  ●④
      elif check(...):
              messages += additional prompt
              continue                         ← 自願的
      break
```

**① 輪隙** — `messages` ｜ `tools` ｜ 私有 state
**② 動作前** — 准不准這個 call ｜ 改它的參數
**③ 動作後** — 給結果 ｜ 改結果
**④ 續跑判斷** — 續不續（准入） ｜ 追加什麼（迴圈自撰的輸入）

**中止**不屬於任何一個 ●，它隨時可到，因為它不需要迴圈受理。

Three properties do the work:

- **迴圈絕大部分牆鐘時間花在不可注入的那一段。** 沒有任何模型 API 有「往
  進行中的 completion 插入」這個原語。
- **① 的第一次是輸入，之後每一次是注入。** 同一個位置，第一次叫 input，
  第二次起叫 interjection——而協定通常只為第一次設計。
- **兩個 `continue` 不同性質。** tool 之後那個是**被迫的**：模型要求了工具，
  它必須看到結果，沒有選擇。④ 那個是**自願的**：迴圈自己判斷還沒完成。
  只有後者是決定，所以「准不准再跑」只存在於 ④。

### ④ 是私有的，而這件事有後果

`check(...)` 是 agent 自己的政策：輸出驗證沒過、結構化輸出沒 parse、要求
反思一次、該叫的工具沒叫。pydantic-ai 的 `ModelRetry` 就是這個分支——它的
docstring 寫著「raise from tool functions, output validators, and capability
hooks … to send a retry prompt back to the model asking it to try again」。

後果是：**一個 run 裡有幾次 ①，由 ④ 決定，而 ④ 在盒子裡。**

所以 run ≠ turn。呼叫端看到一個 run，裡面可能有一次或二十次模型呼叫。任何
指向「這一輪」的協定操作，指的都是一個**呼叫端看不到邊界的東西**——這不是
規範寫得不夠細，是那個邊界在外面本來就不可觀察。

## 空隙

外面的東西不會剛好落在 ● 上，所以一定先在某處等。那段等待是空隙，它有方向：

- **入向空隙** — 訊息抵達 → 迴圈下次走到 ①
- **出向空隙** — 迴圈停在 ② → 外面的人回答

空隙一旦存在，四題就不能不答。沒答的實作是把答案藏在行為裡。

| | 問題 | funduq 的答案 |
| --- | --- | --- |
| 持有者 | 誰拿著 | thread queue，`thread_queue_limit` 預設 8 |
| 排序 | 跟其他等待者誰先 | 抵達順序 |
| 過期 | 消費得太晚還算數嗎 | 降級成普通的下一輪 |
| 放棄 | 迴圈先結束了呢 | 同上 |

`Interrupt.expires_at`（AG-UI）是出向空隙的過期欄位。入向空隙的對應物兩個
協定都沒有；funduq 用 [`addressedRunId`](mechanisms/requests.md) 頂著，並在
註解裡寫明會讓位給 A2A 日後的載體。

## AG-UI 的操作點

以 `ag-ui-protocol 0.1.19` 的型別為準——funduq 自己釘的那一版，每一列都是對著
安裝好的套件讀出來的。

| 操作 | 落點 |
| --- | --- |
| `RunAgentInput.messages` | ① messages |
| `RunAgentInput.tools` | ① tools（每次輸入重新宣告） |
| `RunAgentInput.state` | ① 私有 state |
| `RunAgentInput.context` / `forwarded_props` | ① 不透明夾帶 |
| `RunAgentInput.resume[]` → `ResumeEntry` | ② 准不准 + 改參數 |
| `messages` 裡的 `ToolMessage` | ③ 給結果 |
| `STEP_STARTED` / `STEP_FINISHED`（出向） | **④ 的唯一協定標記**——迴圈又繞了一圈 |
| — | **④ 准不准再跑：無** |
| — | **中止：無** |

出向：`Interrupt{tool_call_id, response_schema, expires_at}` 掛在
`RunFinishedEvent.outcome` 上——**問一次就結束一輪**。

## A2A 的操作點

以 `a2a-sdk 1.1.2` 為準，它送的是 `PROTOCOL_VERSION_CURRENT = 1.0` 的 protobuf。

**已出貨的與提案中的分開讀。** 1.1.2 的 descriptor 裡沒有任何 elicitation——
沒有 `Elicitation` 訊息、沒有 `ElicitationState`、沒有 `elicitationId` 欄位，
`A2AService` 也沒有對應的方法。下表標 *(提案)* 的列出自 v1.1 的草案；它們在
這裡是因為落點正是本頁要問的問題，不是因為今天送得到。

| 操作 | 落點 |
| --- | --- |
| `SendMessage`（無 task id） | ① messages，第一次 = 輸入 |
| `SendMessage`（帶 `taskId`） | ① messages，但**是哪一個 ① 未定義** |
| `SendMessage`（答 `INPUT_REQUIRED`） | ②／③（協定不區分是准駁還是給資料） |
| `SendMessage`（帶 `elicitationId`）*(提案)* | 同上，第二套 correlation，且 advisory |
| `CancelTask` | 中止 |
| `SubscribeToTask` / push config | 觀察路由 |
| — | **tools：無** |
| — | **私有 state：無**（opacity 不允許） |
| — | **④ 完全無**——連迴圈繞了幾圈都看不到 |

出向：`INPUT_REQUIRED`（已出貨），加上 `Elicitation`、`ElicitationState`、
`elicitationId`（提案）——**四套機制全部落在 ②**，而 A2A 對入向空隙仍是零套。

## a2a-python 的控制點

以 `a2a-sdk 1.1.2` 的 server 側為準。它是 A2A 的參考實作——看它把控制點放在
哪，就知道協定留白處實作者實際上怎麼填。

### 它不持有迴圈

```python
# ActiveTask._run_producer
while True:
    req = await self._request_queue.get()         # ← 入向空隙的持有者
    await self._request_lock.acquire()            # ← 上一個 execute 的事件排乾才放行
    await self._agent_executor.execute(req, self._event_queue_agent)
    await enqueue(_RequestCompleted(request_id))  # ← consumer 收到才 release
```

`AgentExecutor.execute(context, event_queue)` 是唯一的委派點，①②③④ 全部在它
裡面。而 `RequestContext` 是凍結的快照（`message`、`task_id`、`current_task`、
`related_tasks`、`configuration`），`event_queue` 只出不進：

> **`execute()` 有出向通道，沒有入向通道。**

| 控制點 | 落點 |
| --- | --- |
| `ActiveTask._request_queue` + `_request_lock` | **入向空隙**，四題全部寫死 |
| `RequestContextBuilder` | ① 送進去前組裝 context |
| `execute(context, event_queue)` | ①②③④ 的黑箱入口 |
| `event_queue` → `EventConsumer` | 出向觀察 |
| `cancel()` + `asyncio.CancelledError` | 中止（async 取消 + 顯式呼叫） |
| `TaskStore` / `TaskManager` | 持久化 |
| push notification sender | 出向路由 |
| — | **迴圈內部：零個** |

迴圈內零控制點跟 opacity 一致，不是缺陷。但它意味著 pydantic-ai 開出來的那些
（`prepare_tools`、`before_tool_execute`、`ModelRetry`、`UsageLimits`）在這個架構
下**不是「A2A 沒有」，是結構上到不了**。

### 它對四題的答案，跟 pydantic-ai 相反

| | a2a-python | pydantic-ai |
| --- | --- | --- |
| 持有者 | SDK（`ActiveTask`） | 框架（`AgentRun`） |
| 排序 | FIFO | `'asap'` 先於 `'when_idle'` |
| 消費點 | **上一個 `execute()` 返回之後** | `'asap'` 可搆進進行中的 run |
| 插話 | **結構上不可能**（`_request_lock`） | 可以，甚至能延長一個要終止的 run |

同一個問題，兩個 SDK 做了相反的選擇，而規範裡一個字都沒有。

### `WAITING` 在這個架構下沒有送達路徑

提案對上已出貨的架構：`ElicitationState.WAITING`（[#2149](https://github.com/a2aproject/A2A/pull/2149)）
還沒進 1.1.2，但下面那道牆已經在了。它的定義是「不擋路的提問，agent 繼續
工作」。在這個架構下那表示 `execute()` 沒有
返回。但答案走 `SendMessage` 進 `_request_queue`，卡在 `_request_lock`——而鎖
要等 `execute()` 返回才放（`active_task.py:519` acquire，`:170` 於
`_RequestCompleted` release）。

- agent 真的繼續工作 → 答案永遠送不到
- 答案送到了 → `execute()` 已返回 → 那個提問其實是**擋路的**

SDK 自己的 docstring 已經承認這個限制。講 `AUTH_REQUIRED` 的時候：

> **Out-of-bound**: The agent should not return from `execute()`. It should wait
> for the out-of-band auth provider to complete the authentication and then
> continue execution.

**當 A2A 需要一個答案送進正在跑的 `execute()`，它的指示是走協定外。**

## pydantic-ai 的操作點

以 `pydantic-ai 2.33.0` 為準。它是對照組：框架自己持有迴圈，所以它有能力
把每一個點都開出來。看它開了哪些，兩個協定缺的就讀得出是選擇還是疏漏。

| 操作 | 落點 |
| --- | --- |
| `user_prompt` / `message_history` | ① messages |
| `process_history` capability | ① 每輪重寫 messages |
| `toolsets=` / `prepare_tools` hook | ① tools，**每輪重算** |
| `deps` | ① 私有 state（型別化） |
| `before_model_request` / `wrap_model_request` | ① 送進模型前最後一刻改請求 |
| `usage_limits`（`UsageLimits`）／`retries` | **④ 續不續**（上限封頂） |
| `ModelRetry`（從 tool／output validator／hook 拋出） | **④ 追加什麼**——送一則 retry prompt 回模型 |
| `reinject_system_prompt` capability | ④／① 每次請求補回 system prompt |
| `after_model_request` / `on_output_validate_error` | ④ 觸發 `check(...)` 的位置 |
| `requires_approval` → `ToolApproved(override_args=…)` / `ToolDenied` | ② 准駁 + 改參數 |
| `before_tool_execute` / `wrap_tool_execute` | ② 攔截動作 |
| `before_tool_validate` / `after_tool_validate` | ② 改參數 |
| `after_tool_execute` | ③ 改結果 |
| `deferred_tool_results` | ③ 給結果 |
| `event_stream_handler` / `on_event` | 觀察 |
| async 取消 `run_stream` | 中止 |
| **`AgentRun.enqueue(*items, priority=…)`** | **入向空隙，帶宣告的意圖** |

**四個點全部開出來，加上中止與入向空隙。**

### `enqueue` 的兩個動詞

```
PendingMessagePriority = Literal['asap', 'when_idle']
```

- `'asap'` — 最早的機會送達：接到下一個 `ModelRequest` 前面；**若 agent 本來
  就要終止，改為把 run 導向再跑一輪**。
- `'when_idle'` — 只在 agent 本來就要終止時送達，排在 `'asap'` 之後，
  **不打斷進行中的工作**。

這跟 funduq 的兩個動詞是同一組區分，各自獨立長出來：

| | funduq | pydantic-ai |
| --- | --- | --- |
| 進到這一輪 | `addressedRunId` | `'asap'` |
| 接在後面，下一輪 | `parentRunId`（AG-UI 自己的欄位） | `'when_idle'` |

一處不同值得記著：`'asap'` 可以**延長**一個本來要結束的 run，funduq 的則是
來得太晚就降級成下一輪。兩者都拒絕用目標的存活狀態去猜意圖——意圖由呼叫端
宣告。

## 五個讀得出來的結論

**一、A2A 對出向空隙做了四套機制，對入向空隙做了零套。**
不是疏忽：出向符合 job 模型（一份工作可以卡在等輸入），入向不符合（沒有人對
一個正在跑的批次工作講話）。它在把 agent 當 job 建模，所以只長得出 job 那
一側。

**二、`CancelTask` 是 A2A 唯一的 mid-task 動詞，因為它是唯一沒有入向空隙的
操作。** 它不需要被讀進 message list，只是設個旗標。A2A 的 mid-task 面之所以
這麼窄，不是保守，是它沒有空隙這個概念。

**三、兩個協定的覆蓋幾乎不相交。** AG-UI 有 tools 與私有 state、沒有中止；
A2A 有中止、沒有 tools 與私有 state。兩邊都沒有 ④ 的控制。而同時持有迴圈的
pydantic-ai 四個點全開——**所以缺的那些是協定的選擇，不是做不到。**

**四、④ 私有，所以「這一輪」在協定層是不可定址的。** 一個 run 裡有幾次 ①
由 `check(...)` 決定，那在盒子裡。AG-UI 的 `STEP_STARTED`／`STEP_FINISHED`
是唯一承認這件事的協定標記——它在報「迴圈又繞了一圈」；A2A 連這個都沒有，
所以它的 task 是一個**呼叫端無法知道內部有幾個受理時機**的黑箱。這是
`SendMessage`（帶 `taskId`）語意未定義的根因：它指向的那個「當前輪」，邊界
本來就不可觀察。

**五、入向空隙有三個獨立實作，協定零個詞彙。** funduq 的 thread queue、
pydantic-ai 的 `AgentRun.enqueue`、a2a-python 的 `ActiveTask._request_queue`
——站在迴圈外面的東西都得持有它，所以三個都長出來了。而三者對「插不插得進
去」給了不同答案：前兩個有兩個動詞，第三個只有一種行為。這是規範缺口最強的
證據形式——**A2A 自己的參考伺服器不得不私下做了一個。**

## funduq 的位置

funduq 站在兩扇門後面、provider 前面，所以它是那個**必須持有空隙**的東西：
訊息在 caller 選的時刻抵達，provider 的迴圈在它自己選的時刻受理，中間那段由
funduq 拿著。上面那四題不是 funduq 多做的功能，是站在這個位置就必須回答的。

把這頁的模型套回 funduq 自己的詞彙：

| funduq 的東西 | 落點 |
| --- | --- |
| 遞給 provider 的 `RunAgentInput` | ①——**兩扇門都翻譯成這一個形狀**，協定差異在抵達 provider 之前就已經消失 |
| `forwardedProps`（`caller`／`kyok`／`actorChain`） | ① 不透明夾帶 |
| thread queue（`thread_queue_limit`，預設 8） | **入向空隙的持有者** |
| `addressedRunId`（插話擴充） | 入向空隙，帶宣告的意圖——funduq 只轉，判斷在 agent 自己的迴圈裡 |
| `parentRunId`（AG-UI 自己的欄位） | 接在後面，下一輪 |
| `input-required` ＋ `resume`／`ResumeEntry` | ②／③——結果落在待決的那個問題上 |
| `Interrupt.expires_at`、`paused_no_resume` | **出向空隙**的過期 |
| `cancel_run`／`CancelTask` | 中止，不屬於任何 ● |
| `run_events`（存下來的 AG-UI 事件流） | 出向觀察，**也是兩個出口唯一的翻譯來源** |
| — | **④：看不到，也不該看到** |

最後一列是這個座位的邊界。`check(...)` 在 provider 的盒子裡，所以一個 run
裡有幾次 ① 對 funduq 不可觀察——run ≠ turn，funduq 因此不去定址「這一輪」。
它不 pace 對話（[the thread gate is
retired](design-records.md#the-thread-gate-is-retired-funduq-does-not-pace-a-providers-conversation)），
不猜一個插話該不該進得去（目標的 agent 才是判斷者），對 ④ 沒有任何意見。

### 由此得出 core 的邊界

- **core 做的決定，只有這個座位逼它做的那些**：空隙四題、身分，以及一個 run
  的結局是什麼——而那是觀察，不是意圖。
- **其餘一切協定形狀的東西都是翻譯**，而且只有一個翻譯來源：存下來的事實
  （`runs` 的狀態與 `run_events`），入向與出向都是。
- **wire 留在下游。** 信封、方法名、SSE framing、HTTP status 都不是翻譯，是
  傳輸；`tests/test_core_is_network_free.py` 是守這條線的那個測試。

「雙出口」因此不是兩個等價的 API，是同一份事實的兩個投影，各自在自己的方向
有損：AG-UI 有 tools 與私有 state、沒有中止；A2A 有中止、沒有 tools 與私有
state。A2A 講不出來的那些有一個具名的去處
（[`agui_event`／`agui_events`](design-records.md#what-a2a-cannot-say-lands-in-one-named-place-exhaustively)），
而不是被「補齊」成一個兩邊都不是的東西。

### 為什麼空隙不能交給 SDK

a2a-python 也持有一個空隙，四題的答案卻是相反的：消費點在上一個 `execute()`
返回之後，所以插話結構上不可能。接上 `DefaultRequestHandler` ＋
`AgentExecutor` ＋ `ActiveTask` 那一疊，等於默默改採那一組答案，而且不會有
任何東西變紅。要用的是 `RequestHandler` 這個**介面**（在下游實作），不是它
的迴圈持有者。
