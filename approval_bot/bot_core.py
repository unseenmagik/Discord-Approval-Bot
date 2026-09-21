from __future__ import annotations

import asyncio
import logging
import sys

import discord
from discord.ext import commands

from approval_bot.config import BotSettings, ConfigError, load_settings
from approval_bot.db import ApprovalDatabase
from approval_bot.referrers import ReferrerFileError, ReferrerList
from approval_bot.verification import VerificationService
from approval_bot.views import AdminActionButton, VerifyPanelView, find_panel, missing_panel_permissions, post_panel

log = logging.getLogger(__name__)


class ApprovalBot(commands.Bot):
    def __init__(self, settings: BotSettings):
        intents = discord.Intents.default()
        # Needed to look members up by username and read their roles.
        # Enable "Server Members Intent" in the Developer Portal as well.
        intents.members = True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents, help_command=None)
        self.settings = settings
        self.db = ApprovalDatabase(settings.database_file)
        self.referrers = ReferrerList(settings.referrers_file)
        self.verification = VerificationService(self)
        self._panel_checked = False

    async def setup_hook(self) -> None:
        await self.db.connect()
        self.referrers.load()
        log.info("Loaded %d referrer names from %s", len(self.referrers.names), self.referrers.path)

        self.add_view(VerifyPanelView())
        self.add_dynamic_items(AdminActionButton)
        await self.load_extension("approval_bot.cogs.admin")

        guild = discord.Object(id=self.settings.guild_id)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)

    async def close(self) -> None:
        await self.db.close()
        await super().close()

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s)", self.user, self.user.id if self.user else "unknown")
        guild = self.get_guild(self.settings.guild_id)
        if guild is None:
            log.error("The bot is not in guild %s. Check guild_id and invite the bot.", self.settings.guild_id)
            return
        if guild.get_role(self.settings.approved_role_id) is None:
            log.error("approved_role_id %s does not exist in %s", self.settings.approved_role_id, guild.name)
        me = guild.me
        if not me.guild_permissions.manage_roles:
            log.warning("The bot is missing the Manage Roles permission, so approvals will fail")

        # on_ready can fire again after reconnects; only check for the panel once.
        if self.settings.auto_post_panel and not self._panel_checked:
            self._panel_checked = True
            await self.ensure_panel()

    async def ensure_panel(self) -> None:
        channel_id = self.settings.welcome_channel_id
        channel = self.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            log.error("welcome_channel_id %s is not a text channel in this server, so the panel wasn't posted", channel_id)
            return
        missing = missing_panel_permissions(channel)
        if missing:
            log.error("Can't post the verification panel in #%s. The bot is missing: %s", channel.name, ", ".join(missing))
            return
        try:
            existing = await find_panel(channel, self.user.id)
            if existing:
                log.info("Verification panel already present in #%s (%s)", channel.name, existing.jump_url)
                return
            message, pinned = await post_panel(channel, self, reason="Verification panel posted on startup")
        except discord.HTTPException:
            log.exception("Failed to post the verification panel in #%s", channel.name)
            return
        log.info("Posted %sthe verification panel in #%s (%s)", "and pinned " if pinned else "", channel.name, message.jump_url)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s")
    try:
        settings = load_settings()
    except ConfigError as exc:
        log.error("Config error: %s", exc)
        sys.exit(1)

    async def run() -> None:
        async with ApprovalBot(settings) as bot:
            try:
                await bot.start(settings.token)
            except ReferrerFileError as exc:
                log.error("Referrers file error: %s", exc)
                sys.exit(1)

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
