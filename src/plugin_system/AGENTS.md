# src/plugin_system - Plugin SDK and Runtime

## Architecture
The plugin system discovers plugin classes, validates manifests and dependencies, registers components, dispatches events, and exposes stable APIs to plugins. Public plugin code should import from `src.plugin_system` or `src.plugin_system.apis`; avoid reaching into `core/` unless changing the runtime itself.

## Directory Map
- `base/plugin_base.py`: manifest loading, configuration files, dependency checks, and shared plugin lifecycle.
- `base/base_plugin.py`: component registration for concrete plugins.
- `base/base_action.py`, `base/base_command.py`, `base/base_tool.py`, and `base/base_events_handler.py`: supported component contracts.
- `base/component_types.py`: component metadata, event types, chat modes, and dependency records.
- `base/config_types.py`: WebUI-facing plugin configuration schema and layout types.
- `core/plugin_manager.py`: discovery, module loading, compatibility checks, and plugin instances.
- `core/component_registry.py`: namespaced component and plugin registries.
- `core/events_manager.py`, `core/global_announcement_manager.py`, and `core/tool_use.py`: event dispatch, enable/disable announcements, and tool execution.
- `apis/`: the supported chat, send, message, model, database, person, emoji, configuration, tool, and plugin facades.
- `utils/manifest_utils.py`: manifest and host-version validation.

## Plugin Authoring Contract
Every plugin directory needs `_manifest.json` and a module containing a `BasePlugin` subclass decorated with `@register_plugin`. Implement `get_plugin_components()` and return `(ComponentInfo, component class)` pairs. Use:

- `BaseTool` for explicit structured LLM tool calls.
- `BaseAction` for compatible autonomous actions selected through the chat registry.
- `BaseCommand` for regex-matched direct commands.
- `BaseEventHandler` for lifecycle and message events.

Declare configuration through `ConfigField`-based `config_schema`; runtime values belong in the file named by `config_file_name`, not the manifest. Use the API facades for sending, querying messages, accessing models, and managing components. For model calls specifically reuse `apis/llm_api.py`; do not add provider HTTP clients inside a plugin. See `docs-src/plugins/` for the authoring contract. Maintained external examples live in the standalone `hsd221/riyabot-plugin-onebot-adapter` and `hsd221/riyabot-plugin-qq-emoji-sync` repositories rather than this source tree.

## Compatibility and Safety
- Plugin and component names must not contain `.`; the registry adds namespaces such as `tool.search` itself.
- Use `activation_type` for Actions. `focus_activation_type` and `normal_activation_type` remain compatibility fields, not patterns for new plugins.
- Python dependencies are checked, not installed automatically. Add repository dependencies deliberately through `pyproject.toml` when required by bundled code.
- Plugins execute in the RiyaBot process without a sandbox. Validate external input, bound network/file work, and never expose secrets in logs or tool results.
- Preserve manifest host-version checks and re-check enablement before executing components.

## Verification

```bash
uv run python -m unittest tests.test_plugin_manager tests.test_component_registry
uv run python -m unittest tests.test_plugin_base_classes tests.test_plugin_apis tests.test_plugin_events
```
