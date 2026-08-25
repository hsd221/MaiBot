# ⚡ Action 组件（兼容层）

## 先确认 Action 的定位

RiyaBot 的聊天执行协议已经统一为 LLM Tool Call，但 `BaseAction` 组件本身仍然保留。它是现有插件的兼容组件，继续负责发送表情、语音、文件或执行其他聊天效果。

当前流程分成两层：

1. `ChatToolRegistry` 把原生 `BaseTool`、内置 `reply` 和兼容 `BaseAction` 汇总到同一个 Tool 目录；
2. 模型以 Tool Call 的形式选择 Action 后，`ActionManager` 再创建并执行原来的 `BaseAction` 实例。

因此，Action 已经迁移到统一的 Tool Call 执行边界，但没有迁移成 `BaseTool`。开发新插件时，请先看 [Tool 组件](./tool-components.md)；只有需要 Action 的激活类型、随机激活、消息类型过滤或既有 Action 行为时，才使用本页的 `BaseAction`。

另外请留意：`reply` 是聊天系统内置 Tool，不是 Action；模型不发起任何 Tool Call 就表示本轮静默结束。Action 也不是直接响应用户命令，而是让璃夜根据聊天情境选择额外行为。

### Action 的特点

- 🧠 **智能激活**：璃夜根据多种条件判断是否使用
- 🎲 **可随机性**：可以用随机数激活，让行为更难以预测，更接近真人交流
- 🤖 **拟人化**：让璃夜的回应更自然、更有个性
- 🔄 **情境感知**：基于聊天上下文做出合适的反应

---

## 🎯 Action 组件的基本结构

首先，所有的 Action 都应该继承 `BaseAction` 类。其次，每个 Action 组件都应该实现以下基本信息：

```python
class ExampleAction(BaseAction):
    action_name = "example_action" # 动作的唯一标识符
    action_description = "这是一个示例动作" # 动作描述
    activation_type = ActionActivationType.ALWAYS # 这里以 ALWAYS 为例
    associated_types = ["text", "emoji", ...] # 关联类型
    parallel_action = False # 是否允许与其他Action并行执行
    action_parameters = {"param1": "参数1的说明", "param2": "参数2的说明", ...}
    # Action使用场景描述 - 帮助LLM判断何时"选择"使用
    action_require = ["使用场景描述1", "使用场景描述2", ...]

    async def execute(self) -> Tuple[bool, str]:
        """
        执行Action的主要逻辑

        Returns:
            Tuple[bool, str]: (是否成功, 执行结果描述)
        """
        # ---- 执行动作的逻辑 ----
        return True, "执行成功"
```

### associated_types：该 Action 会发送的消息类型

例如文本、表情等，这部分由 Adapter 传递给处理器。以独立维护的
[OneBot/NapCat 适配器插件](https://github.com/hsd221/riyabot-plugin-onebot-adapter) 为例，可选项目如下：

| 类型 | 说明 | 格式 |
| --- | --- | --- |
| text | 文本消息 | str |
| emoji | 表情消息 | str: 表情包的无头base64|
| image | 图片消息 | str: 图片的无头base64 |
| reply | 回复消息 | str: 回复的消息ID |
| voice | 语音消息 | str: wav格式语音的无头base64 |
| command | 命令消息 | 参见Adapter文档 |
| voiceurl | 语音URL消息 | str: wav格式语音的URL |
| music | 音乐消息 | str: 这首歌在网易云音乐的音乐id |
| videourl | 视频URL消息 | str: 视频的URL |
| file | 文件消息 | str: 文件的路径 |

> 注意：不同的处理器支持的消息类型可能不同，开发时请以所用 Adapter 的支持为准。

### action_parameters：该 Action 的参数说明

这是一个字典，键为参数名，值为参数说明。这个字段帮助 LLM 理解如何使用 Action。每个自定义参数都会作为必填字符串字段加入兼容 Action 的 Tool schema；模型返回的自定义参数会传递到 Action 的 **`action_data`** 属性中。运行时仍会校验参数是否齐全，不能把模型返回的数据当作可信输入。

### 兼容 Action 的公共 Tool Call 参数

`ChatToolRegistry` 将 Action 转换为 Tool schema 时，还会自动加入两个必填参数：

| 参数 | 作用 |
|---|---|
| `target_message_id` | 要处理的真实消息 ID，必须来自当前聊天记录中的 `m+数字` 标识，且不能指向机器人自己的消息。 |
| `reason` | 选择该 Action 的直接原因；它只用于执行记录，不要在其中撰写发给用户的最终回复。 |

这两个参数由聊天层处理，不属于 `action_parameters`，也不会作为自定义字段写入 `action_data`。插件只需要读取 `action_data` 中自己声明的参数；Action 的执行结果会以 Tool Result 形式返回聊天流程。

---

## 🎯 兼容 Action 的决策机制

Action 采用**两层决策机制**来控制候选范围和最终调用：

> 设计目的：在加载了许多插件时降低 LLM 的决策压力，避免让璃夜在过多的选项中纠结。

**第一层：激活控制（Activation Control）**

激活决定璃夜是否"知道"这个 Action 的存在，即这个 Action 是否进入 Planner 的候选工具池。不被激活的 Action 不能被本轮选择。

**第二层：使用决策（Usage Decision）**

在 Action 被激活后，模型仍会根据工具描述、`action_require` 和当前聊天上下文决定是否调用它。进入候选池不代表每轮都会执行。

### 决策参数详解 🔧

#### 第一层：ActivationType 激活类型说明

| 激活类型 | 说明 | 使用场景 |
| ----------- | ---------------------------------------- | ---------------------- |
| [`NEVER`](#never-激活)     | 从不激活，Action对璃夜不可见               | 临时禁用某个Action      |
| [`ALWAYS`](#always-激活)    | 永远激活，Action总是在璃夜的候选池中        | 始终可用的插件动作，如状态同步 |
| `RANDOM`    | 基于随机概率决定是否激活                   | 增加行为随机性的功能     |
| `KEYWORD`   | 当检测到特定关键词时激活                   | 明确触发条件的功能       |

#### `NEVER` 激活

`ActionActivationType.NEVER` 会使得 Action 永远不会被激活。

```python
class DisabledAction(BaseAction):
    activation_type = ActionActivationType.NEVER  # 永远不激活

    async def execute(self) -> Tuple[bool, str]:
        # 这个Action永远不会被执行
        return False, "这个Action被禁用"
```

#### `ALWAYS` 激活

`ActionActivationType.ALWAYS` 会使 Action 始终进入候选池，但不保证模型每轮都会调用它。

这种激活方式适合需要始终出现在候选池中的插件动作，例如状态同步或持续可用的外部能力。

```python
class AlwaysActivatedAction(BaseAction):
    action_name = "sync_status"
    action_description = "同步当前聊天相关状态"
    activation_type = ActionActivationType.ALWAYS  # 永远激活

    async def execute(self) -> Tuple[bool, str]:
        # 执行插件自己的扩展功能
        return True, "状态同步完成"
```

#### `RANDOM` 激活

`ActionActivationType.RANDOM` 会让这个 Action 根据随机概率决定是否加入候选池。

概率由代码中的 `random_activation_probability` 控制。内部实现中，我们用 `random.random()` 生成一个 0 到 1 之间的随机数，再与这个概率比较。因此使用此方法需要实现 `random_activation_probability` 属性。

```python
class SurpriseAction(BaseAction):
    activation_type = ActionActivationType.RANDOM  # 基于随机概率激活
    # 随机激活概率
    random_activation_probability = 0.1  # 10%概率激活

    async def execute(self) -> Tuple[bool, str]:
        # 执行惊喜动作
        return True, "发送了惊喜内容"
```

#### `KEYWORD` 激活

`ActionActivationType.KEYWORD` 会让这个 Action 在检测到特定关键词时激活。

关键词由代码中的 `activation_keywords` 定义，`keyword_case_sensitive` 则控制匹配时是否区分大小写。内部实现中，我们使用 `in` 操作符检查消息内容是否包含这些关键词。因此使用此方法需要实现 `activation_keywords` 和 `keyword_case_sensitive` 属性。

```python
class GreetingAction(BaseAction):
    activation_type = ActionActivationType.KEYWORD  # 关键词激活
    activation_keywords = ["你好", "hello", "hi", "嗨"] # 关键词配置
    keyword_case_sensitive = False  # 不区分大小写

    async def execute(self) -> Tuple[bool, str]:
        # 执行问候逻辑
        return True, "发送了问候"
```

一个使用 `ActionActivationType.KEYWORD` 的实际例子请参考 `src/plugins/built_in/tts_plugin/plugin.py`。

#### 第二层：使用决策

**在 Action 被激活后，模型仍会根据使用条件决定什么时候调用这个 Action。**

这一层由以下因素综合决定：

- `action_require`：使用场景描述，帮助 LLM 判断何时选择
- `action_parameters`：所需参数，影响 Action 的可执行性
- 当前聊天上下文和璃夜的决策逻辑

---

### 决策流程示例

```python
class EmojiAction(BaseAction):
    # 第一层：激活控制
    activation_type = ActionActivationType.RANDOM  # 随机激活
    random_activation_probability = 0.1  # 10%概率激活

    # 第二层：使用决策
    action_require = [
        "表达情绪时可以选择使用",
        "增加聊天趣味性",
        "不要连续发送多个表情"
    ]
```

**决策流程**：

1. **第一层激活判断**：使用随机数进行决策，当 `random.random() < self.random_activation_probability` 时，璃夜才"知道"可以使用这个 Action；
2. **第二层使用决策**：即使 Action 被激活，模型还会根据 `action_require` 中的条件判断是否真正调用。例如，如果刚刚已经发过表情，根据"不要连续发送多个表情"的要求，璃夜可能不会选择这个 Action。

---

## Action 内置属性说明

```python
class BaseAction:
    def __init__(self):
        # 消息相关属性
        self.log_prefix: str          # 日志前缀
        self.group_id: str            # 群组ID
        self.group_name: str          # 群组名称
        self.user_id: str             # 用户ID
        self.user_nickname: str       # 用户昵称
        self.platform: str            # 平台类型 (qq, telegram等)
        self.chat_id: str             # 聊天ID
        self.chat_stream: ChatStream  # 聊天流对象
        self.is_group: bool           # 是否群聊

        # 消息体
        self.action_message: DatabaseMessages  # 触发本轮的消息对象

        # Action相关属性
        self.action_data: dict        # Action执行时的数据（LLM返回的参数）
        self.thinking_id: str         # 思考ID
```

`action_message` 是 `DatabaseMessages` 对象（定义见 `src.common.data_models.database_data_model.DatabaseMessages`），常用属性包括：

```python
message_id: str                      # 消息id
time: float                          # 时间戳
chat_id: str                         # 聊天ID
reply_to: str | None                 # 回复的消息id
interest_value: float | None         # 兴趣值
is_mentioned: bool | None            # 是否被提及
processed_plain_text: str | None     # 处理后的文本
additional_config: str | None        # Adapter 传来的附加配置
is_emoji: bool                       # 是否为表情
is_picid: bool                       # 是否为图片ID
is_command: bool                     # 是否为命令
user_id: str                         # 发送者用户ID
user_nickname: str                   # 发送者昵称
```

完整字段请查阅 `DatabaseMessages` 类定义。

---

## Action 内置方法说明

```python
class BaseAction:
    def get_config(self, key: str, default=None):
        """获取插件配置值，使用嵌套键访问"""

    async def wait_for_new_message(self, timeout: int = 1200) -> Tuple[bool, str]:
        """等待新消息或超时"""

    async def send_text(self, content: str, set_reply: bool = False, reply_message: Optional["DatabaseMessages"] = None, typing: bool = False, storage_message: bool = True) -> bool:
        """发送文本消息"""

    async def send_emoji(self, emoji_base64: str, set_reply: bool = False, reply_message: Optional["DatabaseMessages"] = None, storage_message: bool = True) -> bool:
        """发送表情包"""

    async def send_image(self, image_base64: str, set_reply: bool = False, reply_message: Optional["DatabaseMessages"] = None, storage_message: bool = True) -> bool:
        """发送图片"""

    async def send_custom(self, message_type: str, content: str | Dict, typing: bool = False, set_reply: bool = False, reply_message: Optional["DatabaseMessages"] = None, storage_message: bool = True) -> bool:
        """发送自定义类型消息，如 video、file、audio 等"""

    async def send_command(self, command_name: str, args: Optional[dict] = None, display_message: str = "", storage_message: bool = True) -> bool:
        """发送命令消息"""

    async def store_action_info(self, action_build_into_prompt: bool = False, action_prompt_display: str = "", action_done: bool = True) -> None:
        """存储动作信息到数据库"""
```

具体参数与用法参见 `BaseAction` 基类的定义。

## 相关文档

- [Tool 组件](./tool-components.md) - 新插件的原生结构化 Tool Call 组件。
- [插件快速开始](./quick-start.md) - 从最小插件开始，并了解何时使用兼容 Action。
- [Command 组件](./command-components.md) - 需要由用户明确输入命令时使用。
