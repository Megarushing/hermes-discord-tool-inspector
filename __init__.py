"""Hermes entrypoint for per-call Discord inspection."""
from .inspector.runtime import Inspector
from .inspector.storage import Store

_INSTANCE = None


def register(ctx):
    global _INSTANCE
    from plugins.plugin_storage import plugin_data_dir
    channels = ctx.get_config('channels', [])
    if not channels:
        return  # Explicit opt-in required; ["*"] enables all originating Discord conversations.
    store = Store(plugin_data_dir('discord-tool-inspector'),
                  retention_days=ctx.get_config('retention_days', 30))
    _INSTANCE = Inspector(store, channels,
                          owner_aliases=ctx.get_config('owner_aliases', []),
                          admin_user_ids=ctx.get_config('admin_user_ids', []))
    ctx.register_hook('post_tool_call', _INSTANCE.after_tool)
    ctx.register_hook('post_api_request', _INSTANCE.after_api)
    ctx.register_platform_handler('discord', _INSTANCE.platform_factory())
    ctx.on_unload(_INSTANCE.close)
