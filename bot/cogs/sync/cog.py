import logging
from typing import Callable, Iterable

import aiohttp
from discord import Guild, Member, Role
from discord.ext import commands
from discord.ext.commands import Bot

from bot import constants
from bot.cogs.sync import syncers

log = logging.getLogger(__name__)


class Sync:
    """Captures relevant events and sends them to the site."""

    # The server to synchronize events on.
    # Note that setting this wrongly will result in things getting deleted
    # that possibly shouldn't be.
    SYNC_SERVER_ID = constants.Guild.id

    # An iterable of callables that are called when the bot is ready.
    ON_READY_SYNCERS: Iterable[Callable[[Bot, Guild], None]] = (
        syncers.sync_roles,
        syncers.sync_users
    )

    def __init__(self, bot):
        self.bot = bot

    async def on_ready(self):
        guild = self.bot.get_guild(self.SYNC_SERVER_ID)
        if guild is not None:
            for syncer in self.ON_READY_SYNCERS:
                syncer_name = syncer.__name__[5:]  # drop off `sync_`
                log.info("Starting `%s` syncer.", syncer_name)
                total_created, total_updated = await syncer(self.bot, guild)
                log.info(
                    "`%s` syncer finished, created `%d`, updated `%d`.",
                    syncer_name, total_created, total_updated
                )

    async def on_guild_role_create(self, role: Role):
        await self.bot.api_client.post(
            'bot/roles',
            json={
                'colour': role.colour.value,
                'id': role.id,
                'name': role.name,
                'permissions': role.permissions.value
            }
        )

    async def on_guild_role_delete(self, role: Role):
        log.warning(
            (
                "Attempted to delete role `%s` (`%d`), but role deletion "
                "is currently not implementeed."
            ),
            role.name, role.id
        )

    async def on_guild_role_update(self, before: Role, after: Role):
        if (
                before.name != after.name
                or before.colour != after.colour
                or before.permissions != after.permissions
        ):
            await self.bot.api_client.put(
                'bot/roles/' + str(after.id),
                json={
                    'colour': after.colour.value,
                    'id': after.id,
                    'name': after.name,
                    'permissions': after.permissions.value
                }
            )

    async def on_member_join(self, member: Member):
        packed = {
            'avatar_hash': member.avatar,
            'discriminator': int(member.discriminator),
            'id': member.id,
            'in_guild': True,
            'name': member.name,
            'roles': sorted(role.id for role in member.roles)
        }

        got_error = False

        try:
            # First try an update of the user to set the `in_guild` field and other
            # fields that may have changed since the last time we've seen them.
            await self.bot.api_client.put('bot/users/' + str(member.id), json=packed)

        except aiohttp.client_exceptions.ClientResponseError as e:
            # If we didn't get 404, something else broke - propagate it up.
            if e.status != 404:
                raise

            got_error = True  # yikes

        if got_error:
            # If we got `404`, the user is new. Create them.
            await self.bot.api_client.post('bot/users', json=packed)

    async def on_member_remove(self, member: Member):
        await self.bot.api_client.put(
            'bot/users/' + str(member.id),
            json={
                'avatar_hash': member.avatar,
                'discriminator': int(member.discriminator),
                'id': member.id,
                'in_guild': True,
                'name': member.name,
                'roles': sorted(role.id for role in member.roles)
            }
        )

    async def on_member_update(self, before: Member, after: Member):
        if (
                before.name != after.name
                or before.avatar != after.avatar
                or before.discriminator != after.discriminator
                or before.roles != after.roles
        ):
            try:
                await self.bot.api_client.put(
                    'bot/users/' + str(after.id),
                    json={
                        'avatar_hash': after.avatar,
                        'discriminator': int(after.discriminator),
                        'id': after.id,
                        'in_guild': True,
                        'name': after.name,
                        'roles': sorted(role.id for role in after.roles)
                    }
                )
            except aiohttp.client_exceptions.ClientResponseError as e:
                if e.status != 404:
                    raise

                log.warning(
                    "Unable to update user, got 404. "
                    "Assuming race condition from join event."
                )

    @commands.group(name='sync')
    @commands.has_permissions(administrator=True)
    async def sync_group(self, ctx):
        """Run synchronizations between the bot and site manually."""

    @sync_group.command(name='roles')
    @commands.has_permissions(administrator=True)
    async def sync_roles_command(self, ctx):
        """Manually synchronize the guild's roles with the roles on the site."""

        initial_response = await ctx.send("📊 Synchronizing roles.")
        total_created, total_updated = await syncers.sync_roles(self.bot, ctx.guild)
        await initial_response.edit(
            content=(
                f"👌 Role synchronization complete, created **{total_created}** "
                f"and updated **{total_created}** roles."
            )
        )

    @sync_group.command(name='users')
    @commands.has_permissions(administrator=True)
    async def sync_users_command(self, ctx):
        """Manually synchronize the guild's users with the users on the site."""

        initial_response = await ctx.send("📊 Synchronizing users.")
        total_created, total_updated = await syncers.sync_users(self.bot, ctx.guild)
        await initial_response.edit(
            content=(
                f"👌 User synchronization complete, created **{total_created}** "
                f"and updated **{total_created}** users."
            )
        )
