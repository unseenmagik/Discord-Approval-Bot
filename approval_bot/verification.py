from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import discord

from approval_bot.db import Result

if TYPE_CHECKING:
    from approval_bot.bot_core import ApprovalBot

log = logging.getLogger(__name__)

_MENTION_RE = re.compile(r"^<@!?(\d{15,21})>$")
_ID_RE = re.compile(r"^\d{15,21}$")

ALREADY_VERIFIED_MESSAGE = "You're already verified, so there's nothing more to do here."
MISSING_ROLE_MESSAGE = "You can't verify yet. Please finish registering first, then try again."
LOCKED_MESSAGE = (
    "You've used all your verification attempts. An admin has been notified and will contact you "
    "to finish verification."
)


@dataclass(frozen=True)
class Outcome:
    ok: bool
    message: str


@dataclass
class ReferralCheck:
    method: str  # "discord" or "name"
    value: str
    referrer: discord.Member | None = None
    matched_name: str | None = None
    decline_reason: str | None = None

    @property
    def passed(self) -> bool:
        return self.decline_reason is None


class RoleAssignmentError(Exception):
    pass


def _clip(text: str, limit: int = 100) -> str:
    text = discord.utils.escape_markdown(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


class VerificationService:
    def __init__(self, bot: ApprovalBot):
        self.bot = bot
        self.settings = bot.settings
        self._user_locks: dict[int, asyncio.Lock] = {}

    # --- role checks --------------------------------------------------------

    def is_admin(self, user: discord.abc.User) -> bool:
        if user.id in self.settings.admin_user_ids:
            return True
        return any(role.id in self.settings.admin_role_ids for role in getattr(user, "roles", ()))

    def is_verified(self, member: discord.Member) -> bool:
        return any(role.id == self.settings.approved_role_id for role in member.roles)

    def can_refer(self, member: discord.Member) -> bool:
        return any(role.id in self.settings.referrer_role_ids for role in member.roles)

    async def check_eligibility(self, member: discord.Member) -> str | None:
        """Returns a message explaining why the member can't verify, or None if they can."""
        if self.is_verified(member):
            return ALREADY_VERIFIED_MESSAGE
        required = self.settings.required_role_ids
        if required and not any(role.id in required for role in member.roles):
            return MISSING_ROLE_MESSAGE
        if (await self.bot.db.get_attempts(member.id)).locked:
            return LOCKED_MESSAGE
        return None

    # --- referral checks ----------------------------------------------------

    async def resolve_member(self, guild: discord.Guild, raw: str) -> discord.Member | None:
        """Finds a member by exact username, @mention or user ID."""
        value = raw.strip()
        mention = _MENTION_RE.match(value)
        if mention or _ID_RE.match(value):
            user_id = int(mention.group(1) if mention else value)
            member = guild.get_member(user_id)
            if member is None:
                try:
                    member = await guild.fetch_member(user_id)
                except discord.HTTPException:
                    return None
            return member

        target = value.lstrip("@").strip()
        if target.endswith("#0"):
            target = target[:-2]
        target = target.casefold()
        for member in guild.members:
            if member.name.casefold() == target:
                return member
            if member.discriminator != "0" and f"{member.name}#{member.discriminator}".casefold() == target:
                return member
        return None

    async def _check_discord_referrer(self, member: discord.Member, raw: str) -> ReferralCheck:
        check = ReferralCheck("discord", raw)
        referrer = await self.resolve_member(member.guild, raw)
        if referrer is None:
            check.decline_reason = "I couldn't find a server member with that username or user ID."
        elif referrer.id == member.id:
            check.decline_reason = "You can't enter yourself as your referrer."
        elif referrer.bot:
            check.decline_reason = "A bot can't be your referrer."
        elif not self.can_refer(referrer):
            check.decline_reason = "That member isn't verified, so they can't refer new members."
        else:
            check.referrer = referrer
        return check

    def _check_name(self, raw: str) -> ReferralCheck:
        check = ReferralCheck("name", raw)
        check.matched_name = self.bot.referrers.match(raw)
        if check.matched_name is None:
            check.decline_reason = "That name isn't on the referral list."
        return check

    # --- main flow ----------------------------------------------------------

    async def verify(self, member: discord.Member, *, referrer_input: str, name_input: str) -> Outcome:
        lock = self._user_locks.setdefault(member.id, asyncio.Lock())
        async with lock:
            if reason := await self.check_eligibility(member):
                return Outcome(False, reason)

            if referrer_input:
                check = await self._check_discord_referrer(member, referrer_input)
            else:
                check = self._check_name(name_input)

            if check.passed:
                return await self._approve(member, check)
            return await self._decline(member, check)

    async def _approve(self, member: discord.Member, check: ReferralCheck) -> Outcome:
        try:
            await self._grant_role(member, "Referral verified")
        except RoleAssignmentError as exc:
            log.error("Could not assign approved role to %s: %s", member, exc)
            await self._log(member, Result.ERROR, check, reason=str(exc))
            await self._post_admin(
                embed=self._check_embed(
                    member, check, title="⚠️ Approval failed: role not assigned", color=discord.Color.orange()
                ).add_field(name="Error", value=_clip(str(exc), 1000), inline=False),
                user_ids=self.settings.lockout_mention_user_ids,
                role_ids=self.settings.lockout_mention_role_ids,
            )
            return Outcome(
                False,
                "Your referral checked out, but I couldn't give you your role. An admin has been notified "
                "and will sort it out shortly.",
            )

        await self.bot.db.reset_attempts(member.id)
        await self._log(member, Result.APPROVED, check)
        await self._post_admin(
            embed=self._check_embed(member, check, title="✅ Member approved", color=discord.Color.green()),
            user_ids=self.settings.approval_mention_user_ids,
            role_ids=self.settings.approval_mention_role_ids,
        )
        return Outcome(True, "✅ **You're approved!** Your role has been added and you now have access.")

    async def _decline(self, member: discord.Member, check: ReferralCheck) -> Outcome:
        max_attempts = self.settings.max_attempts
        state = await self.bot.db.record_failure(member.id, max_attempts)
        await self._log(member, Result.DECLINED, check, reason=check.decline_reason)

        if state.locked:
            await self._log(member, Result.LOCKED, check, reason=f"Reached {max_attempts} failed attempts")
            await self._post_lockout_alert(member)
            return Outcome(False, f"❌ {check.decline_reason}\n\nThat was your last attempt. {LOCKED_MESSAGE}")

        left = max_attempts - state.failed_count
        return Outcome(
            False,
            f"❌ {check.decline_reason}\n\nYou have **{left}** attempt{'s' if left != 1 else ''} left. "
            "Check the spelling and try again.",
        )

    # --- admin actions ------------------------------------------------------

    async def manual_approve(self, member: discord.Member, actor: discord.abc.User, *, announce: bool) -> Outcome:
        if self.is_verified(member):
            return Outcome(False, f"{member.mention} is already verified.")
        try:
            await self._grant_role(member, f"Manually approved by {actor} ({actor.id})")
        except RoleAssignmentError as exc:
            return Outcome(False, f"Couldn't assign the role: {exc}")

        await self.bot.db.reset_attempts(member.id)
        await self.bot.db.log_event(
            user_id=member.id, username=member.name, result=Result.MANUAL_APPROVED, actor_id=actor.id
        )
        if announce:
            embed = discord.Embed(
                title="✅ Member approved manually",
                description=f"{member.mention} (`{member.name}`, {member.id})",
                color=discord.Color.green(),
            ).add_field(name="Approved by", value=actor.mention)
            await self._post_admin(embed=embed)
        await self._dm(member, "✅ You've been approved and now have access to the server.")
        return Outcome(True, f"{member.mention} has been approved.")

    async def reset_attempts(self, member: discord.Member, actor: discord.abc.User) -> Outcome:
        await self.bot.db.reset_attempts(member.id)
        await self.bot.db.log_event(user_id=member.id, username=member.name, result=Result.RESET, actor_id=actor.id)
        await self._dm(member, "An admin has reset your verification attempts. You can press **Verify** again.")
        return Outcome(True, f"Attempts reset for {member.mention}. They can verify again.")

    # --- helpers ------------------------------------------------------------

    async def _grant_role(self, member: discord.Member, reason: str) -> None:
        role = member.guild.get_role(self.settings.approved_role_id)
        if role is None:
            raise RoleAssignmentError(f"Approved role {self.settings.approved_role_id} does not exist")
        try:
            await member.add_roles(role, reason=reason)
        except discord.Forbidden as exc:
            raise RoleAssignmentError(
                "Missing permission. The bot needs Manage Roles, and its role must be above "
                f"{role.name} in the role list"
            ) from exc
        except discord.HTTPException as exc:
            raise RoleAssignmentError(f"Discord API error: {exc}") from exc

    async def _log(self, member: discord.Member, result: str, check: ReferralCheck, *, reason: str | None = None) -> None:
        await self.bot.db.log_event(
            user_id=member.id,
            username=member.name,
            result=result,
            method=check.method,
            input_value=check.value,
            referrer_id=check.referrer.id if check.referrer else None,
            reason=reason,
        )

    def _check_embed(
        self, member: discord.Member, check: ReferralCheck, *, title: str, color: discord.Color
    ) -> discord.Embed:
        embed = discord.Embed(
            title=title,
            description=f"{member.mention} (`{member.name}`, {member.id})",
            color=color,
            timestamp=discord.utils.utcnow(),
        )
        if check.referrer:
            embed.add_field(
                name="Referred by", value=f"{check.referrer.mention} (`{check.referrer.name}`)", inline=False
            )
        elif check.matched_name:
            embed.add_field(name="Name on referral list", value=_clip(check.matched_name), inline=False)
        return embed

    async def _post_lockout_alert(self, member: discord.Member) -> None:
        from approval_bot.views import AdminActionButton

        attempts = await self.bot.db.list_events(
            results=(Result.DECLINED,), user_id=member.id, limit=self.settings.max_attempts
        )
        lines = [
            f"<t:{event.created_at}:t> · {'Discord user' if event.method == 'discord' else 'Name'} "
            f"`{_clip(event.input_value or '', 60)}`: {event.reason}"
            for event in reversed(attempts)
        ]
        embed = discord.Embed(
            title="🚫 Manual verification needed",
            description=(
                f"{member.mention} (`{member.name}`, {member.id}) failed verification "
                f"{self.settings.max_attempts} times and is now locked."
            ),
            color=discord.Color.red(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Attempts", value="\n".join(lines)[:1024] or "None recorded", inline=False)
        embed.set_footer(text="Approve them yourself, or reset their attempts so they can try again.")

        view = discord.ui.View(timeout=None)
        view.add_item(AdminActionButton("approve", member.id))
        view.add_item(AdminActionButton("reset", member.id))
        await self._post_admin(
            embed=embed,
            view=view,
            user_ids=self.settings.lockout_mention_user_ids,
            role_ids=self.settings.lockout_mention_role_ids,
        )

    async def _post_admin(
        self,
        *,
        embed: discord.Embed,
        view: discord.ui.View | None = None,
        user_ids: tuple[int, ...] = (),
        role_ids: tuple[int, ...] = (),
    ) -> None:
        channel_id = self.settings.admin_channel_id
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except discord.HTTPException:
                log.error("Admin channel %s not found or not accessible", channel_id)
                return
        if not isinstance(channel, discord.abc.Messageable):
            log.error("Admin channel %s is not a text channel", channel_id)
            return

        content = " ".join([f"<@{uid}>" for uid in user_ids] + [f"<@&{rid}>" for rid in role_ids]) or None
        allowed = discord.AllowedMentions(
            everyone=False,
            users=[discord.Object(uid) for uid in user_ids],
            roles=[discord.Object(rid) for rid in role_ids],
        )
        try:
            await channel.send(content=content, embed=embed, view=view or discord.utils.MISSING, allowed_mentions=allowed)
        except discord.HTTPException:
            log.exception("Failed to post to admin channel %s", channel_id)

    @staticmethod
    async def _dm(member: discord.Member, message: str) -> None:
        try:
            await member.send(message)
        except discord.HTTPException:
            log.info("Could not DM %s (DMs are probably closed)", member)


def now_ts() -> int:
    return int(time.time())
