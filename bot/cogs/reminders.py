import asyncio
import logging
import random
import textwrap
from datetime import datetime
from operator import itemgetter

from dateutil.relativedelta import relativedelta
from discord import Colour, Embed
from discord.ext.commands import Bot, Context, group

from bot.constants import (
    Channels, Icons, NEGATIVE_REPLIES, POSITIVE_REPLIES, Roles
)
from bot.converters import ExpirationDate
from bot.pagination import LinePaginator
from bot.utils.scheduling import Scheduler
from bot.utils.time import humanize_delta, wait_until

log = logging.getLogger(__name__)

WHITELISTED_CHANNELS = (Channels.bot,)
MAXIMUM_REMINDERS = 5


class Reminders(Scheduler):

    def __init__(self, bot: Bot):
        self.bot = bot
        super().__init__()

    async def on_ready(self):
        # Get all the current reminders for re-scheduling
        response = await self.bot.api_client.get(
            'bot/reminders',
            params={'active': 'true'}
        )

        now = datetime.utcnow()
        loop = asyncio.get_event_loop()

        for reminder in response:
            remind_at = datetime.fromisoformat(reminder['expiration'][:-1])

            # If the reminder is already overdue ...
            if remind_at < now:
                late = relativedelta(now, remind_at)
                await self.send_reminder(reminder, late)

            else:
                self.schedule_task(loop, reminder["id"], reminder)

    @staticmethod
    async def _send_confirmation(ctx: Context, on_success: str):
        """
        Send an embed confirming the change was made successfully.
        """

        embed = Embed()
        embed.colour = Colour.green()
        embed.title = random.choice(POSITIVE_REPLIES)
        embed.description = on_success
        await ctx.send(embed=embed)

    async def _scheduled_task(self, reminder: dict):
        """
        A coroutine which sends the reminder once the time is reached.

        :param reminder: the data of the reminder.
        :return:
        """

        reminder_id = reminder["id"]
        reminder_datetime = datetime.fromisoformat(reminder['expiration'][:-1])

        # Send the reminder message once the desired duration has passed
        await wait_until(reminder_datetime)
        await self.send_reminder(reminder)

        log.debug(f"Deleting reminder {reminder_id} (the user has been reminded).")
        await self._delete_reminder(reminder_id)

        # Now we can begone with it from our schedule list.
        self.cancel_task(reminder_id)

    async def _delete_reminder(self, reminder_id: str):
        """
        Delete a reminder from the database, given its ID.

        :param reminder_id: The ID of the reminder.
        """

        await self.bot.api_client.delete('bot/reminders/' + str(reminder_id))

        # Now we can remove it from the schedule list
        self.cancel_task(reminder_id)

    async def _reschedule_reminder(self, reminder):
        """
        Reschedule a reminder object.

        :param reminder: The reminder to be rescheduled.
        """

        loop = asyncio.get_event_loop()

        self.cancel_task(reminder["id"])
        self.schedule_task(loop, reminder["id"], reminder)

    async def send_reminder(self, reminder, late: relativedelta = None):
        """
        Send the reminder.

        :param reminder: The data about the reminder.
        :param late: How late the reminder is (if at all)
        """

        channel = self.bot.get_channel(reminder["channel_id"])
        user = self.bot.get_user(reminder["author"])

        embed = Embed()
        embed.colour = Colour.blurple()
        embed.set_author(
            icon_url=Icons.remind_blurple,
            name="It has arrived!"
        )

        embed.description = f"Here's your reminder: `{reminder['content']}`"

        if late:
            embed.colour = Colour.red()
            embed.set_author(
                icon_url=Icons.remind_red,
                name=f"Sorry it arrived {humanize_delta(late, max_units=2)} late!"
            )

        await channel.send(
            content=user.mention,
            embed=embed
        )
        await self._delete_reminder(reminder["id"])

    @group(name="remind", aliases=("reminder", "reminders"), invoke_without_command=True)
    async def remind_group(self, ctx: Context, expiration: ExpirationDate, *, content: str):
        """
        Commands for managing your reminders.
        """

        await ctx.invoke(self.new_reminder, expiration=expiration, content=content)

    @remind_group.command(name="new", aliases=("add", "create"))
    async def new_reminder(self, ctx: Context, expiration: ExpirationDate, *, content: str):
        """
        Set yourself a simple reminder.
        """

        embed = Embed()

        # Make sure the reminder should actually be made.
        if ctx.author.top_role.id not in STAFF_ROLES:

            # If they don't have permission to set a reminder in this channel
            if ctx.channel.id not in WHITELISTED_CHANNELS:
                embed.colour = Colour.red()
                embed.title = random.choice(NEGATIVE_REPLIES)
                embed.description = "Sorry, you can't do that here!"

                return await ctx.send(embed=embed)

            # Get their current active reminders
            active_reminders = await self.bot.api_client.get(
                'bot/reminders',
                params={
                    'user__id': str(ctx.author.id)
                }
            )

            # Let's limit this, so we don't get 10 000
            # reminders from kip or something like that :P
            if len(active_reminders) > MAXIMUM_REMINDERS:
                embed.colour = Colour.red()
                embed.title = random.choice(NEGATIVE_REPLIES)
                embed.description = "You have too many active reminders!"

                return await ctx.send(embed=embed)

        # Now we can attempt to actually set the reminder.
        reminder = await self.bot.api_client.post(
            'bot/reminders',
            json={
                'author': ctx.author.id,
                'channel_id': ctx.message.channel.id,
                'content': content,
                'expiration': expiration.isoformat()
            }
        )

        # Confirm to the user that it worked.
        await self._send_confirmation(
            ctx, on_success="Your reminder has been created successfully!"
        )

        loop = asyncio.get_event_loop()
        self.schedule_task(loop, reminder["id"], reminder)

    @remind_group.command(name="list")
    async def list_reminders(self, ctx: Context):
        """
        View a paginated embed of all reminders for your user.
        """

        # Get all the user's reminders from the database.
        data = await self.bot.api_client.get(
            'bot/reminders',
            params={'user__id': str(ctx.author.id)}
        )

        now = datetime.utcnow()

        # Make a list of tuples so it can be sorted by time.
        reminders = sorted(
            (
                (rem['content'], rem['expiration'], rem['id'])
                for rem in data
            ),
            key=itemgetter(1)
        )

        lines = []

        for content, remind_at, id_ in reminders:
            # Parse and humanize the time, make it pretty :D
            remind_datetime = datetime.fromisoformat(remind_at[:-1])
            time = humanize_delta(relativedelta(remind_datetime, now))

            text = textwrap.dedent(f"""
            **Reminder #{id_}:** *expires in {time}* (ID: {id_})
            {content}
            """).strip()

            lines.append(text)

        embed = Embed()
        embed.colour = Colour.blurple()
        embed.title = f"Reminders for {ctx.author}"

        # Remind the user that they have no reminders :^)
        if not lines:
            embed.description = "No active reminders could be found."
            return await ctx.send(embed=embed)

        # Construct the embed and paginate it.
        embed.colour = Colour.blurple()

        await LinePaginator.paginate(
            lines,
            ctx, embed,
            max_lines=3,
            empty=True
        )

    @remind_group.group(name="edit", aliases=("change", "modify"), invoke_without_command=True)
    async def edit_reminder_group(self, ctx: Context):
        """
        Commands for modifying your current reminders.
        """

        await ctx.invoke(self.bot.get_command("help"), "reminders", "edit")

    @edit_reminder_group.command(name="duration", aliases=("time",))
    async def edit_reminder_duration(self, ctx: Context, id_: int, expiration: ExpirationDate):
        """
        Edit one of your reminders' expiration.
        """

        # Send the request to update the reminder in the database
        reminder = await self.bot.api_client.patch(
            'bot/reminders/' + str(id_),
            json={'expiration': expiration.isoformat()}
        )

        # Send a confirmation message to the channel
        await self._send_confirmation(
            ctx, on_success="That reminder has been edited successfully!"
        )

        await self._reschedule_reminder(reminder)

    @edit_reminder_group.command(name="content", aliases=("reason",))
    async def edit_reminder_content(self, ctx: Context, id_: int, *, content: str):
        """
        Edit one of your reminders' content.
        """

        # Send the request to update the reminder in the database
        reminder = await self.bot.api_client.patch(
            'bot/reminders/' + str(id_),
            json={'content': content}
        )

        # Send a confirmation message to the channel
        await self._send_confirmation(
            ctx, on_success="That reminder has been edited successfully!"
        )
        await self._reschedule_reminder(reminder)

    @remind_group.command("delete", aliases=("remove",))
    async def delete_reminder(self, ctx: Context, id_: int):
        """
        Delete one of your active reminders.
        """

        await self._delete_reminder(id_)
        await self._send_confirmation(
            ctx, on_success="That reminder has been deleted successfully!"
        )


def setup(bot: Bot):
    bot.add_cog(Reminders(bot))
    log.info("Cog loaded: Reminders")
