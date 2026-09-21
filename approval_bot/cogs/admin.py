from __future__ import annotations

import csv
import io
import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

import discord
from discord import app_commands
from discord.ext import commands

from approval_bot.db import RESULT_FILTERS, Event, Result
from approval_bot.referrers import ReferrerFileError, ReferrerList
from approval_bot.views import VerifyPanelView

if TYPE_CHECKING:
    from approval_bot.bot_core import ApprovalBot

log = logging.getLogger(__name__)

MAX_IMPORT_BYTES = 1_000_000

_RESULT_LABELS = {
    Result.APPROVED: "✅ Approved",
    Result.MANUAL_APPROVED: "✅ Approved (manual)",
    Result.DECLINED: "❌ Declined",
    Result.LOCKED: "🚫 Locked",
    Result.RESET: "🔄 Reset",
    Result.ERROR: "⚠️ Error",
}

STATUS_CHOICES = [
    app_commands.Choice(name="All", value="all"),
    app_commands.Choice(name="Approved", value="approved"),
    app_commands.Choice(name="Declined", value="declined"),
    app_commands.Choice(name="Locked (needs admin)", value="locked"),
]


def admin_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        return interaction.client.verification.is_admin(interaction.user)  # type: ignore[attr-defined]

    return app_commands.check(predicate)


def _escape(text: str | None, limit: int = 60) -> str:
    text = discord.utils.escape_markdown(text or "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def format_event(event: Event, *, include_user: bool = True) -> str:
    parts = [f"<t:{event.created_at}:f>", _RESULT_LABELS.get(event.result, event.result)]
    if include_user:
        parts.append(f"<@{event.user_id}> (`{_escape(event.username, 40)}`)")
    if event.method == "discord":
        parts.append(f"referrer <@{event.referrer_id}>" if event.referrer_id else f"user `{_escape(event.input_value)}`")
    elif event.method == "name":
        parts.append(f"name `{_escape(event.input_value)}`")
    if event.actor_id:
        parts.append(f"by <@{event.actor_id}>")
    if event.reason and event.result in (Result.DECLINED, Result.ERROR):
        parts.append(f"*{_escape(event.reason, 80)}*")
    return " · ".join(parts)


def _join_lines(lines: list[str], limit: int = 4000) -> str:
    out: list[str] = []
    size = 0
    for line in lines:
        if size + len(line) + 1 > limit:
            out.append(f"…and {len(lines) - len(out)} more")
            break
        out.append(line)
        size += len(line) + 1
    return "\n".join(out)


class AdminCog(commands.Cog):
    approvals = app_commands.Group(name="approvals", description="Review and manage verifications", guild_only=True)
    referrers = app_commands.Group(name="referrers", description="Manage the referral names list", guild_only=True)

    def __init__(self, bot: ApprovalBot):
        self.bot = bot

    @property
    def names(self) -> ReferrerList:
        return self.bot.referrers

    # --- panel --------------------------------------------------------------

    @app_commands.command(name="verify-panel", description="Post and pin the verification panel")
    @app_commands.describe(channel="Channel to post in (defaults to the configured welcome channel)")
    @app_commands.guild_only()
    @admin_only()
    async def verify_panel(self, interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None) -> None:
        await interaction.response.defer(ephemeral=True)
        settings = self.bot.settings
        target = channel or self.bot.get_channel(settings.welcome_channel_id)
        if not isinstance(target, discord.TextChannel):
            await interaction.followup.send("The welcome channel isn't set up correctly. Check `welcome_channel_id`.")
            return

        embed = discord.Embed(title=settings.panel_title, description=settings.panel_description, color=settings.embed_color)
        message = await target.send(embed=embed, view=VerifyPanelView())
        try:
            await message.pin(reason=f"Verification panel posted by {interaction.user}")
            note = "and pinned it"
        except discord.HTTPException:
            note = "but couldn't pin it (the bot needs **Manage Messages** / **Pin Messages** there)"
        await interaction.followup.send(f"Posted the panel in {target.mention} {note}.")

    # --- approvals ----------------------------------------------------------

    @approvals.command(name="list", description="Show recent verification results with timestamps")
    @app_commands.describe(status="Which results to show", user="Only show this user", limit="How many (1-50)")
    @app_commands.choices(status=STATUS_CHOICES)
    @admin_only()
    async def approvals_list(
        self,
        interaction: discord.Interaction,
        status: Optional[app_commands.Choice[str]] = None,
        user: Optional[discord.User] = None,
        limit: app_commands.Range[int, 1, 50] = 15,
    ) -> None:
        key = status.value if status else "all"
        events = await self.bot.db.list_events(
            results=RESULT_FILTERS.get(key), user_id=user.id if user else None, limit=limit
        )
        counts = await self.bot.db.count_by_result()
        approved = counts.get(Result.APPROVED, 0) + counts.get(Result.MANUAL_APPROVED, 0)

        embed = discord.Embed(
            title=f"Verification log: {status.name if status else 'All'}" + (f" for {user.name}" if user else ""),
            description=_join_lines([format_event(e) for e in events]) or "Nothing recorded yet.",
            color=self.bot.settings.embed_color,
        )
        embed.set_footer(
            text=f"All time: {approved} approved · {counts.get(Result.DECLINED, 0)} declined · "
            f"{counts.get(Result.LOCKED, 0)} locked"
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @approvals.command(name="user", description="Show a member's verification status and history")
    @admin_only()
    async def approvals_user(self, interaction: discord.Interaction, user: discord.User) -> None:
        state = await self.bot.db.get_attempts(user.id)
        events = await self.bot.db.list_events(user_id=user.id, limit=15)
        member = interaction.guild.get_member(user.id) if interaction.guild else None

        if member is None:
            status = "Not in the server"
        elif self.bot.verification.is_verified(member):
            status = "✅ Verified"
        elif state.locked:
            status = "🚫 Locked, needs an admin"
        else:
            status = f"Not verified ({state.failed_count}/{self.bot.settings.max_attempts} failed attempts)"

        embed = discord.Embed(title=f"Verification: {user.name}", color=self.bot.settings.embed_color)
        embed.add_field(name="User", value=f"{user.mention} ({user.id})", inline=False)
        embed.add_field(name="Status", value=status, inline=False)
        embed.add_field(
            name="History",
            value=_join_lines([format_event(e, include_user=False) for e in events], 1000) or "No attempts yet.",
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @approvals.command(name="approve", description="Approve a member manually and give them the approved role")
    @admin_only()
    async def approvals_approve(self, interaction: discord.Interaction, member: discord.Member) -> None:
        await interaction.response.defer(ephemeral=True)
        outcome = await self.bot.verification.manual_approve(member, interaction.user, announce=True)
        await interaction.followup.send(outcome.message)

    @approvals.command(name="reset", description="Reset a member's failed attempts so they can verify again")
    @admin_only()
    async def approvals_reset(self, interaction: discord.Interaction, member: discord.Member) -> None:
        outcome = await self.bot.verification.reset_attempts(member, interaction.user)
        await interaction.response.send_message(outcome.message, ephemeral=True)

    @approvals.command(name="export", description="Download the full verification log as CSV")
    @admin_only()
    async def approvals_export(self, interaction: discord.Interaction) -> None:
        events = await self.bot.db.list_events()
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(
            ["timestamp_utc", "user_id", "username", "result", "method", "input", "referrer_id", "reason", "actor_id"]
        )
        for e in events:
            writer.writerow([
                datetime.fromtimestamp(e.created_at, timezone.utc).isoformat(),
                e.user_id, e.username, e.result, e.method or "", e.input_value or "",
                e.referrer_id or "", e.reason or "", e.actor_id or "",
            ])
        file = discord.File(io.BytesIO(buffer.getvalue().encode("utf-8")), filename="verification-log.csv")
        await interaction.response.send_message(f"{len(events)} entries.", file=file, ephemeral=True)

    # --- referrer names -----------------------------------------------------

    @referrers.command(name="list", description="Show the names accepted as referrers")
    @admin_only()
    async def referrers_list(self, interaction: discord.Interaction) -> None:
        names = sorted(self.names.names, key=str.casefold)
        body = "\n".join(f"• {discord.utils.escape_markdown(n)}" for n in names)
        if not names:
            await interaction.response.send_message("The referral list is empty.", ephemeral=True)
        elif len(body) <= 4000:
            embed = discord.Embed(title=f"Referral names ({len(names)})", description=body, color=self.bot.settings.embed_color)
            await interaction.response.send_message(embed=embed, ephemeral=True)
        else:
            file = discord.File(io.BytesIO("\n".join(names).encode("utf-8")), filename="referrers.txt")
            await interaction.response.send_message(f"{len(names)} names (attached).", file=file, ephemeral=True)

    @referrers.command(name="add", description="Add a name to the referral list")
    @app_commands.describe(name="The name to accept, e.g. Jane Smith")
    @admin_only()
    async def referrers_add(self, interaction: discord.Interaction, name: app_commands.Range[str, 1, 100]) -> None:
        added = await self.names.add(name)
        message = f"Added **{discord.utils.escape_markdown(name.strip())}**." if added else "That name is already on the list."
        log.info("%s referrer name %r by %s", "Added" if added else "Skipped duplicate", name, interaction.user)
        await interaction.response.send_message(message, ephemeral=True)

    @referrers.command(name="remove", description="Remove a name from the referral list")
    @admin_only()
    async def referrers_remove(self, interaction: discord.Interaction, name: str) -> None:
        removed = await self.names.remove(name)
        if removed:
            log.info("Removed referrer name %r by %s", removed, interaction.user)
        message = f"Removed **{discord.utils.escape_markdown(removed)}**." if removed else "That name isn't on the list."
        await interaction.response.send_message(message, ephemeral=True)

    @referrers_remove.autocomplete("name")
    async def _referrer_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        needle = current.casefold()
        matches = [n for n in self.names.names if needle in n.casefold()]
        return [app_commands.Choice(name=n[:100], value=n[:100]) for n in matches[:25]]

    @referrers.command(name="export", description="Download the referral list as JSON")
    @admin_only()
    async def referrers_export(self, interaction: discord.Interaction) -> None:
        file = discord.File(io.BytesIO(self.names.to_json().encode("utf-8")), filename="referrers.json")
        await interaction.response.send_message(file=file, ephemeral=True)

    @referrers.command(name="import", description="Upload a JSON file of names to replace or merge with the list")
    @app_commands.describe(
        file='JSON: ["Name", ...] or {"names": ["Name", ...]}',
        mode="Replace the whole list, or add to it",
    )
    @app_commands.choices(mode=[
        app_commands.Choice(name="Replace", value="replace"),
        app_commands.Choice(name="Merge", value="merge"),
    ])
    @admin_only()
    async def referrers_import(
        self,
        interaction: discord.Interaction,
        file: discord.Attachment,
        mode: Optional[app_commands.Choice[str]] = None,
    ) -> None:
        if file.size > MAX_IMPORT_BYTES:
            await interaction.response.send_message("That file is too large (1 MB max).", ephemeral=True)
            return
        try:
            names = ReferrerList.parse(json.loads(await file.read()))
        except (json.JSONDecodeError, UnicodeDecodeError, ReferrerFileError) as exc:
            await interaction.response.send_message(f"Couldn't read that file: {exc}", ephemeral=True)
            return

        before = len(self.names.names)
        if mode and mode.value == "merge":
            names = self.names.names + names
        total = await self.names.replace(names)
        log.info("Imported referrers (%s) by %s: %d -> %d", mode.value if mode else "replace", interaction.user, before, total)
        await interaction.response.send_message(
            f"Referral list updated: {before} → **{total}** names.", ephemeral=True
        )

    # --- errors -------------------------------------------------------------

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        if isinstance(error, app_commands.CheckFailure):
            message = "You don't have permission to use this command."
        else:
            log.exception("Command failed", exc_info=error)
            message = "Something went wrong running that command. Check the bot logs."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


async def setup(bot: ApprovalBot) -> None:
    await bot.add_cog(AdminCog(bot))
