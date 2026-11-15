"""Discord transport; no model calls, core patches or replacement handlers."""
import asyncio
import logging
import itertools
import re

from .render import render, pages

log = logging.getLogger(__name__)
CUSTOM_ID = re.compile(r'^hti:([0-9a-f]{32}):(-?\d{1,9})$')


def route_for_call():
    """Require a real task-local gateway route; fail closed outside root Discord turns."""
    from gateway.session_context import bound_session_key, get_session_env
    from agent.delegation_context import is_delegated_child_context
    if not bound_session_key() or is_delegated_child_context():
        return None
    if get_session_env('HERMES_SESSION_PLATFORM') != 'discord':
        return None
    if get_session_env('HERMES_CRON_SESSION') == '1':
        return None
    channel = get_session_env('HERMES_SESSION_THREAD_ID') or get_session_env('HERMES_SESSION_CHAT_ID')
    user = get_session_env('HERMES_SESSION_USER_ID')
    session = get_session_env('HERMES_SESSION_ID')
    if not channel or not user or not session or not channel.isdigit() or not user.isdigit():
        return None
    return dict(channel_id=channel, user_id=user, session_id=session)


class Inspector:
    def __init__(self, store, channels, *, owner_aliases=(), admin_user_ids=()):
        self.store = store
        self.owner_aliases = frozenset(str(uid) for uid in owner_aliases)
        self.admin_user_ids = frozenset(str(uid) for uid in admin_user_ids)
        self.channels = frozenset(str(c) for c in channels)
        self.bot = None
        self.loop = None
        self.queue = None
        self.worker = None
        self._locks = {}
        self._interaction_sequence = itertools.count()

    def channel_allowed(self, channel_id):
        channel_id = str(channel_id)
        return channel_id.isdigit() and ('*' in self.channels or channel_id in self.channels)

    def platform_factory(self):
        # Hermes deduplicates factories by (plugin name, callable qualname), even
        # after unloading their callbacks. A generation-local name allows a fresh
        # registration without patching the adapter or bypassing unload cleanup.
        import uuid
        def wire_generation(bot, adapter):
            self.wire(bot, adapter)
        wire_generation.__qualname__ = 'Inspector.wire_' + uuid.uuid4().hex
        return wire_generation

    def wire(self, bot, adapter):
        """Called by Hermes on its Discord event loop after connection."""
        self._detach(self.bot, self.worker)
        previous = getattr(bot, '_hermes_tool_inspector', None)
        if previous is not None and previous is not self:
            previous._detach(previous.bot, previous.worker)
        bot._hermes_tool_inspector = self
        self.bot = bot
        self.loop = asyncio.get_running_loop()
        self.queue = asyncio.Queue()
        bot.add_listener(self.on_interaction, 'on_interaction')
        self.worker = self.loop.create_task(self._drain(self.queue))

    def _detach(self, bot, worker):
        if (bot is not None and getattr(bot, '_hermes_tool_inspector', None) is self
                and (bot is not self.bot or worker is self.worker)):
            bot.remove_listener(self.on_interaction, 'on_interaction')
            delattr(bot, '_hermes_tool_inspector')
        if worker is not None:
            worker.cancel()
        if self.bot is bot and self.worker is worker:
            self.loop = None

    def close(self):
        """Snapshot the attachment so delayed cleanup cannot detach its replacement."""
        bot, worker, loop = self.bot, self.worker, self.loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._detach, bot, worker)

    def after_tool(self, tool_name, args, result, **kwargs):
        route = route_for_call()
        if not route or not self.channel_allowed(route['channel_id']) or not self.loop or self.queue is None:
            return
        # Store enforces redaction before any data is persisted or queued.
        record = self.store.capture(tool_name, args, result, **route)
        self.loop.call_soon_threadsafe(self.queue.put_nowait, record)

    def after_api(self, assistant_message=None, api_request_id=None, **kwargs):
        from .reasoning import provider_summary
        route = route_for_call()
        if not route or not self.channel_allowed(route['channel_id']) or not self.loop or self.queue is None or not api_request_id:
            return
        text = provider_summary(assistant_message)
        if not text:
            return
        record = self.store.capture('reasoning_summary', {}, text, **route,
                                    kind='reasoning_summary', event_key=str(api_request_id))
        if record is not None:
            self.loop.call_soon_threadsafe(self.queue.put_nowait, record)

    async def _drain(self, queue):
        while True:
            try:
                record = await asyncio.wait_for(queue.get(), timeout=3600)
            except asyncio.TimeoutError:
                try:
                    await asyncio.to_thread(self.store.purge_expired)
                except Exception as exc:
                    log.error('Inspector retention cleanup failed (%s)', type(exc).__name__)
                continue
            try:
                await self.publish(record)
            except Exception as exc:
                # Never log raw payloads or SDK exceptions that can contain message data.
                log.error('Inspector publication failed (%s); local record retained', type(exc).__name__)
            finally:
                queue.task_done()
                record = None  # Do not retain the last payload throughout a long idle period.

    def view(self, record, page=-1):
        import discord
        view = discord.ui.LayoutView(timeout=None)
        rid = record['id']
        button = discord.ui.Button(label='⋁' if page < 0 else '⋀',
                                   custom_id=f'hti:{rid}:{0 if page < 0 else -1}',
                                   style=discord.ButtonStyle.secondary)
        view.add_item(discord.ui.Section(discord.ui.TextDisplay(render(record)), accessory=button))
        if page >= 0:
            chunks = pages(record)
            count = len(chunks)
            view.add_item(discord.ui.TextDisplay(chunks[page]))
            if count > 1:
                row = discord.ui.ActionRow()
                row.add_item(discord.ui.Button(label='Previous', custom_id=f'hti:{rid}:{max(0, page-1)}', disabled=page == 0))
                row.add_item(discord.ui.Button(label=f'Next · {page+1}/{count}', custom_id=f'hti:{rid}:{min(count-1, page+1)}', disabled=page == count-1))
                view.add_item(row)
        return view

    async def publish(self, record):
        import discord
        channel = self.bot.get_channel(int(record['channel_id']))
        if channel is None:
            channel = await self.bot.fetch_channel(int(record['channel_id']))
        message = await channel.send(view=self.view(record),
                                     allowed_mentions=discord.AllowedMentions.none(), silent=True)
        self.store.bind(record['id'], message.id)

    async def on_interaction(self, interaction):
        custom_id = (interaction.data or {}).get('custom_id', '')
        if not isinstance(custom_id, str) or not CUSTOM_ID.fullmatch(custom_id):
            return
        log.info('Inspector interaction received message=%s action=%s',
                    getattr(interaction.message, 'id', None), custom_id.rsplit(':', 1)[-1])
        sequence = next(self._interaction_sequence)
        try:
            await interaction.response.defer(thinking=False)
            await self._handle_interaction(interaction, sequence)
        except Exception as exc:
            log.error('Inspector interaction failed type=%s status=%s code=%s',
                      type(exc).__name__, getattr(exc, 'status', None), getattr(exc, 'code', None))
            raise
        else:
            log.info('Inspector interaction completed message=%s action=%s',
                        getattr(interaction.message, 'id', None), custom_id.rsplit(':', 1)[-1])

    async def _handle_interaction(self, interaction, sequence):
        import discord
        custom_id = (interaction.data or {}).get('custom_id', '')
        match = CUSTOM_ID.fullmatch(custom_id)
        if not match:
            return
        rid, index = match.groups()
        record = await asyncio.to_thread(self.store.get, rid)
        channel_id = str(interaction.channel_id)
        valid = (record and self.channel_allowed(channel_id) and record['channel_id'] == channel_id
                 and (str(interaction.user.id) in self.admin_user_ids
                      or str(interaction.user.id) == record['user_id']
                      or {str(interaction.user.id), record['user_id']} <= self.owner_aliases)
                 and str(getattr(interaction.message, 'id', '')) == record['message_id'])
        if not valid:
            await interaction.followup.send('This tool entry has expired, is unavailable, or belongs to another user.', ephemeral=True)
            return
        page = int(index)
        if page < -1 or page >= len(pages(record)):
            await interaction.followup.send('That page is unavailable.', ephemeral=True)
            return
        # Only the owner, linked accounts or configured admins can change the message. Discord supplies
        # the immutable original message/channel identity; custom IDs carry no data.
        state = self._locks.setdefault(rid, {'lock': asyncio.Lock(), 'latest': -1})
        state['latest'] = max(state['latest'], sequence)
        async with state['lock']:
            if sequence < state['latest']:
                return  # A newer authorised click already superseded this intent.
            await interaction.edit_original_response(content=None, embeds=[], attachments=[],
                                                     view=self.view(record, page),
                                                     allowed_mentions=discord.AllowedMentions.none())
