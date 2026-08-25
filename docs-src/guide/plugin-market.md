# 插件市场

RiyaBot WebUI 的插件市场从后端配置的 Registry 读取已审核插件和版本。市场安装只接受插件 ID 与版本号，仓库、Tag 和 commit 均由 Registry 决定，浏览器不能临时覆盖 Registry 地址。

## 使用前准备

- 运行 RiyaBot 的主机需要安装 Git。
- 主机需要能够访问 `[webui].plugin_registry_url` 指向的 HTTPS Registry，以及插件对应的 HTTPS Git 仓库。Registry URL 不能包含凭据、查询参数或片段。
- 默认 Registry 地址由 RiyaBot 配置提供。需要使用其他 Registry 时，在 WebUI 配置页修改 `plugin_registry_url`，或编辑 `bot_config.toml` 的 `[webui]` 段后重启服务。

默认地址指向已经发布的 RiyaBot 官方 Registry。网络或端点临时不可用时，市场会显示不可用告警，本地插件管理仍按下文说明继续工作。

更换默认 Registry 只影响市场目录和后续首次安装。已经通过市场安装的插件仍绑定原 Registry；其版本列表、更新和回退继续从原 Registry 读取，不会被自动迁移到新的默认 Registry。需要切换来源时，应先卸载插件，再从新的 Registry 重新安装。

```toml
[webui]
plugin_registry_url = "https://raw.githubusercontent.com/hsd221/RiyaBot-Plugins-Registry/main/registry.json"
```

Registry 使用 `$meta` 加稳定插件 ID 的顶层对象。每个版本的 `manifest` 必须是完整的 Manifest v2；`ref` 和 `commit` 固定到审核版本：

```json
{
  "$meta": {
    "schema_version": 1,
    "name": "RiyaBot Official Plugin Market",
    "repository": "https://github.com/hsd221/RiyaBot-Plugins-Registry"
  },
  "github.alice.weather": {
    "repository": "https://github.com/alice/riyabot-weather",
    "status": "approved",
    "review_level": "community",
    "updated_at": "2026-08-08T00:00:00Z",
    "versions": [
      {
        "version": "1.2.3",
        "ref": "v1.2.3",
        "commit": "<40-or-64-lowercase-hex-characters>",
        "status": "approved",
        "released_at": "2026-08-08T00:00:00Z",
        "manifest": { "...": "Manifest v2" }
      }
    ]
  }
}
```

为兼容内部投影，RiyaBot 也接受把这些插件对象放在 `plugins` 字段下。`artifact_url` 与 `sha256` 目前只作为预留字段校验，ZIP 制品下载尚未实现；带制品的版本会显示为不可安装，必须使用没有制品字段的 Git Tag 版本。

## Registry 不可用时

Registry 只负责市场目录和审核版本操作。Registry 请求失败时，WebUI 会显示独立告警和重试按钮，但仍会读取本地插件目录和安装来源记录。此时可以继续：

- 查看已安装插件及其详情；
- 查看和修改插件配置；
- 卸载本地插件。

市场浏览、首次安装和版本切换需要有效的 Registry 快照，因此在 Registry 恢复前不可用。RiyaBot 不会把 Registry 故障当作整个插件管理页面加载失败，也不会静默切换到其他市场源。

## 安装插件

1. 打开 WebUI 的“插件市场”。
2. 查看插件的审核级别、兼容范围和能力声明。
3. 点击“获取”，在版本对话框中选择目标版本。
4. 确认安装，等待下载、校验和原子切换完成。

默认会选中与当前 RiyaBot 兼容的最新稳定审核版本。预发布版本不会被默认选中，但仍可在其状态为“已审核”且兼容时手动选择。

版本列表可能出现以下状态：

| 状态 | 含义 | 能否选择 |
| --- | --- | --- |
| 已审核 | Registry 当前允许安装 | 可以 |
| 已撤回 | 版本已停止提供新安装 | 不可以 |
| 已封禁 | Registry 明确阻止该版本 | 不可以 |
| 不兼容 | 不支持当前 RiyaBot 版本 | 不可以 |

“官方审核”或“社区审核”表示该版本通过了相应流程，不代表代码已被沙箱隔离，也不是绝对安全保证。

已通过当前 Registry 安装的插件即使后来被撤回或封禁，仍会保留在插件列表中并显示优先级最高的风险徽章：

- 插件或当前安装版本为 `blocked` 时显示“插件已封禁”；
- 插件或当前安装版本为 `yanked` 时显示“当前版本已撤回”；
- 被撤回或封禁的版本不能用于新安装或版本切换；
- Git、本地或其他 Registry 安装的同 ID 插件不会继承当前 Registry 的风险状态。

## 更新与回退

通过市场安装的插件会绑定安装时使用的 Registry。打开插件的“版本”对话框后，可以选择任一仍可安装的审核版本：

- 选择更高版本完成更新；
- 选择更低版本完成回退；
- 当前已安装版本不能重复提交。

更新请求不会接受新的仓库地址或任意 Git ref。后端会从绑定的 Registry 解析目标版本，校验审核 Tag 对应的固定 commit、Manifest 和兼容范围，再替换当前插件。下载或校验失败不会修改现有插件；切换或来源记录写入失败时会尝试恢复旧目录。

目录切换时只迁移明确的运行状态，不会把旧插件目录整体合并到审核后的新版本：

- 保留 `config.toml`、`plugin_config.toml`、插件声明的单层 TOML 配置及其备份；
- 保留顶层 `config_backup/`、`data/` 和 `logs/`；
- 不保留旧 Python 代码、依赖、模板或其他任意文件；
- 运行状态中存在符号链接、硬链接、异常文件类型，或新版本包含同名路径时，更新会在替换前拒绝。

这些运行状态通过同一文件系统内的原子移动保留，来源记录写入失败时也会随旧版本一起恢复。更新完成后仍应按界面提示重启 RiyaBot，让新代码和迁移后的配置重新加载。

## 查看安装来源

插件详情会展示当前安装方式，以及可用的来源信息：

- `市场安装`：显示绑定 Registry、Tag/ref 和 commit；
- `Git 安装`：显示仓库、分支或 Tag 和真实 commit；
- `上传安装` / `本地插件`：不具备市场来源绑定。

非市场安装不会使用市场更新接口，也不自动继承 Registry 的审核、撤回和版本历史。若要把同 ID 的 Git 或本地插件改为市场管理，请先卸载，再从市场选择审核版本安装。

RiyaBot 每次展示市场来源或打开版本列表前，都会复核本地 Manifest 的 ID、版本和仓库是否仍与安装记录一致。任一字段发生漂移时，插件仍作为本地插件保留查看、配置和卸载能力，但市场来源身份、审核状态、风险状态和版本入口会被隐藏。恢复市场管理需要还原匹配的 Manifest，或卸载后从 Registry 重新安装。

## 卸载

在插件详情中点击“卸载”。RiyaBot 会先把插件移动到隐藏目录，删除安装来源记录后再清理文件；来源记录删除失败时会恢复插件目录。

## 安全边界

当前插件在 RiyaBot 主进程中运行，能够使用该进程拥有的文件、网络、环境变量和系统权限。Manifest 的 `capabilities` 只用于展示和审核，不能限制运行时权限。

只安装你愿意信任的第三方代码。市场校验可以固定来源并发现一部分风险，但不能替代代码审查、最小权限部署和备份。

## 从旧版升级

升级到包含插件市场来源绑定的版本时：

- `bot_config.toml` 会按现有配置升级流程更新到 `7.9.0`，并在替换前保留备份；
- 应用数据库会自动创建 `plugin_installation` 表，用于记录安装方式、Registry、仓库、ref、commit 和当前版本；
- 已经存在但没有来源记录的插件按本地插件处理，不会被推断为市场安装；
- 普通 Git 安装和后续 Git 更新会记录真实仓库、ref 与 HEAD commit，但仍不获得市场审核状态；
- 需要 Registry 版本管理的旧插件，应先确认配置和数据已备份，再卸载并从市场重新安装。

原先随主仓库维护的两个外部插件已经拆到独立仓库，不再随 RiyaBot 源码或 Docker 镜像分发，也没有迁入内置插件目录：

- [OneBot/NapCat 适配器](https://github.com/hsd221/riyabot-plugin-onebot-adapter)，稳定 ID 为 `github.hsd221.onebot-adapter`，首个审核 Tag 为 `v1.0.0`；
- [QQ 收藏表情同步](https://github.com/hsd221/riyabot-plugin-qq-emoji-sync)，稳定 ID 为 `github.hsd221.qq-emoji-sync`，首个审核 Tag 为 `v1.1.0`。

两个版本都已收录进默认官方 Registry。已有部署中的 `plugins/onebot_adapter` 与 `plugins/qq_emoji_sync` 目录可以继续作为本地外部插件使用；新部署可以从市场安装审核版本，也可以从独立仓库进行非市场 Git 安装。

## 常见问题

### 没有可选择的版本

当前版本可能已撤回或封禁，也可能没有版本兼容正在运行的 RiyaBot。查看每个版本的状态和兼容范围，或升级 RiyaBot 后重试。

### 提示安装来源不一致

市场更新只允许使用安装时绑定的 Registry 和仓库。不要通过修改本地 Manifest 伪装来源；需要切换来源时，先卸载再重新安装。

### Registry 加载失败

确认 `plugin_registry_url` 使用不含查询参数或片段的 HTTPS 地址，主机能够访问该地址，并检查 Registry 是否符合 RiyaBot 的结构、大小和版本约束。RiyaBot 不会在该端点缺失时回退到其他市场源，前端也不会接受临时 Registry URL。告警存在期间仍可在“已安装”页查看、配置或卸载本地插件。
