from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, cast

import discord

from approval_bot.verification import now_ts

if TYPE_CHECKING:
    from approval_bot.bot_core import ApprovalBot

log = logging.getLogger(__name__)

VERIFY_BUTTON_ID = "approval:verify"

_ADMIN_ACTIONS: dict[str, tuple[str, discord.ButtonStyle, str]] = {
    "approve": ("Approve", discord.ButtonStyle.success, "Approved"),
    "reset": ("Reset attempts", discord.ButtonStyle.secondary, "Attempts reset"),
}


def _bot(interaction: discord.Interaction) -> ApprovalBot:
    return cast("ApprovalBot", interaction.client)


async def _reply(interaction: discord.Interaction, message: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


class VerifyPanelView(discord.ui.View):
    """The pinned panel's Verify button. Persistent: keeps working after restarts."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="Verify", emoji="✅", style=discord.ButtonStyle.success, custom_id=VERIFY_BUTTON_ID)
    async def verify(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        bot = _bot(interaction)
        member = interaction.user
        if not isinstance(member, discord.Member) or interaction.guild_id != bot.settings.guild_id:
            await _reply(interaction, "This button only works inside the server.")
            return
        if reason := await bot.verification.check_eligibility(member):
            await _reply(interaction, reason)
            return
        await interaction.response.send_modal(ReferralModal())


class ReferralModal(discord.ui.Modal, title="Who referred you?"):
    referrer = discord.ui.TextInput(
        label="Referrer's Discord username or user ID",
        placeholder="e.g. janedoe or 123456789012345678",
        required=False,
        max_length=100,
    )
    name = discord.ui.TextInput(
        label="Or, your referrer's name",
        placeholder="Use this only if you don't know their Discord username",
        required=False,
        max_length=100,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        referrer_input = self.referrer.value.strip()
        name_input = self.name.value.strip()
        if bool(referrer_input) == bool(name_input):
            await _reply(
                interaction,
                "Please fill in **one** of the two boxes: your referrer's Discord username/ID, **or** their name. "
                "Press **Verify** to try again. This didn't count as an attempt.",
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        member = cast(discord.Member, interaction.user)
        outcome = await _bot(interaction).verification.verify(
            member, referrer_input=referrer_input, name_input=name_input
        )
        await interaction.followup.send(outcome.message, ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.exception("Verification modal failed", exc_info=error)
        await _reply(interaction, "Something went wrong while checking your referral. Please try again shortly.")


class AdminActionButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"approval:admin:(?P<action>approve|reset):(?P<user_id>\d+)",
):
    """Approve / Reset buttons on lockout alerts. The target user ID is stored in the custom_id."""

    def __init__(self, action: str, user_id: int) -> None:
        label, style, _ = _ADMIN_ACTIONS[action]
        super().__init__(discord.ui.Button(label=label, style=style, custom_id=f"approval:admin:{action}:{user_id}"))
        self.action = action
        self.user_id = user_id

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /
    ) -> AdminActionButton:
        return cls(match["action"], int(match["user_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        service = _bot(interaction).verification
        if not service.is_admin(interaction.user):
            await _reply(interaction, "Only admins can use these buttons.")
            return
        guild = interaction.guild
        if guild is None:
            return

        member = guild.get_member(self.user_id)
        if member is None:
            try:
                member = await guild.fetch_member(self.user_id)
            except discord.HTTPException:
                await _reply(interaction, f"<@{self.user_id}> is no longer in the server.")
                return

        if self.action == "approve":
            outcome = await service.manual_approve(member, interaction.user, announce=False)
        else:
            outcome = await service.reset_attempts(member, interaction.user)

        if not outcome.ok:
            await _reply(interaction, outcome.message)
            return

        message = interaction.message
        embed = message.embeds[0] if message and message.embeds else discord.Embed()
        embed.color = discord.Color.green() if self.action == "approve" else discord.Color.greyple()
        embed.add_field(
            name="Resolved",
            value=f"{_ADMIN_ACTIONS[self.action][2]} by {interaction.user.mention} <t:{now_ts()}:R>",
            inline=False,
        )
        await interaction.response.edit_message(embed=embed, view=None)
