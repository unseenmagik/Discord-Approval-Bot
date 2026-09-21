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
from approval_bot.views import AdminActionButton, VerifyPanelView

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
