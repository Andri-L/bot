import asyncio
import logging
from textwrap import dedent
from typing import Optional, Tuple

from aiohttp import ClientResponseError
from discord import (
    Colour, Embed, Forbidden, HTTPException,
    Message, RawReactionActionEvent, TextChannel
)
from discord.ext.commands import Bot, CommandError, command
from discord.utils import get

from bot.constants import Channels, Colours, Guild, Keys, Roles, URLs
from bot.decorators import with_role


LVL1_STAR = "\u2b50"
LVL2_STAR = "\U0001f31f"
LVL3_STAR = "\U0001f4ab"
LVL4_STAR = "\u2728"

YES_EMOJI = "\u2705"
NO_EMOJI = "\u274e"
OK_HAND = "\U0001f44c"

THRESHOLDS = {
    LVL1_STAR: 2,
    LVL2_STAR: 5,
    LVL3_STAR: 10,
    LVL4_STAR: 20
}

ALLOWED_TO_STAR = (Roles.admin, Roles.moderator, Roles.owner, Roles.helpers)

log = logging.getLogger(__name__)


class NoStarboardException(CommandError):
    pass


class Starboard:
    def __init__(self, bot: Bot):
        self.bot = bot
        self.star_msg_map = {}
        self.headers = {"X-API-Key": Keys.site_api}
        self.bot.loop.create_task(self.async_init())

    async def async_init(self):
        """
        Asynchronous init method
        """

        # Get all starred messages to populate star to msg map
        response = await self.bot.http_session.get(
            url=URLs.site_starboard_api,
            headers=self.headers
        )
        json = await response.json()
        messages = json["messages"]

        for message in messages:
            key = int(message["message_id"])
            value = int(message["bot_message_id"])
            self.star_msg_map[key] = value

        log.debug(f"Populated star_msg_map: {self.star_msg_map}")

    @command(name="deletestarboard")
    @with_role(Roles.owner)
    async def delete_all_star_entries(self, ctx):
        """
        Sends a request to the starboard endpoint to delete all database entries.
        This does *not* delete the starboard messaged in discord as that would either
        be hit hard by ratelimit, or having to manually set the id of the new channel
        in constants.  Please manually delete / recreate the starboard to wipe it.

        This also clears the cache.
        """

        msg = await ctx.send("This will delete all entries from the starboard, are you sure?")
        await msg.add_reaction(YES_EMOJI)
        await msg.add_reaction(NO_EMOJI)

        def check(r, u):
            if r.emoji != YES_EMOJI and r.emoji != NO_EMOJI:
                return False

            if not get(u.roles, id=Roles.owner):
                return False

            if u.id != ctx.author.id:
                return False
            return True

        try:
            reaction, _ = await self.bot.wait_for(
                "reaction_add",
                check=check,
                timeout=60
            )
        except asyncio.TimeoutError:
            log.debug("No reaction to delete all starboard entries were given.")
            return

        if reaction.emoji == NO_EMOJI:
            log.info("No was selected to deleting all entries from starboard")
            return

        if reaction.emoji == YES_EMOJI:
            # Can never be too sure here.
            response = await self.bot.http_session.delete(
                url=f"{URLs.site_starboard_api}/delete",
                headers=self.headers,
            )
            if response.status != 200:
                log.warning("Deleting all starboard entries failed")
                try:
                    resp = await response.json()
                    resp = resp["error_message"]
                except ClientResponseError:
                    resp = await response.text()

                error_msg = f"error_message: {resp[:500]}"  # in case it sends html

                log.warning(error_msg)

                embed = Embed(
                    title="Something went wrong!",
                    description=error_msg,
                    color=Colours.soft_red
                )
                await ctx.send(embed=embed)
            else:
                log.info("All entries from starboard was deleted from site db.")
                await msg.add_reaction(OK_HAND)  # ok hand
                self.star_msg_map = []

    @command()
    @with_role(Roles.owner, Roles.admin, Roles.moderator)
    async def delete_starboard_entry(self, ctx, msg_id: int):
        """
        Delete an entry from the starboard.
        This involved deleting the starboard entry, remove the stars from the original message,
        and delete the database entry.

        :param ctx: Context
        :param msg_id: The original message id,
               or the id of the starboard message (Note this takes a bit longer)
        """

        original_id = None

        try:
            del self.star_msg_map[msg_id]
            original_id = msg_id
            log.debug("Deleted entry from cache by original id")

        except KeyError:
            # find the correct entry based on the
            for k, v in self.star_msg_map.items():
                if v == msg_id:
                    original_id = k
                    del self.star_msg_map[k]
                    log.debug("Deleted entry from cache by starboard id")
                    break
            else:
                log.debug("Didn't find entry in cache.")

        get_response = await self.bot.http_session.get(
            url=f"{URLs.site_starboard_api}?message_id={original_id or msg_id}",
            headers=self.headers
        )
        json_response = await get_response.json()
        if json_response["success"]:
            star_msg_id = int(json_response["message"]["bot_message_id"])
            channel_id = int(json_response["message"]["channel_id"])
        else:
            log.warning("Was not able to get an original message id after many attempts, "
                        "could not request delete from api")
            return await ctx.send("I couldn't find an id for the original message, "
                                  "if the reactions are still on it please re-use the "
                                  "command with the original messages id")

        response = await self.bot.http_session.delete(
            url=f"{URLs.site_starboard_api}?message_id={original_id}",
            headers=self.headers,
        )
        if response.status != 200:
            log.warning(f"Deleting the message with id {original_id} failed")
            text_resp = await response.text()
            log.warning(text_resp)
        else:
            log.info("Deleting the message with id {original_id} was successful.")

        starboard = self.bot.get_channel(Channels.starboard)
        star_msg = await starboard.get_message(star_msg_id)
        await star_msg.delete()
        log.info(f"Deleted starboard entry for message {original_id}")

        original_channel = self.bot.get_channel(channel_id)
        original_msg = await original_channel.get_message(original_id)
        await original_msg.clear_reactions()
        log.info(f"Cleared all reactions of message {original_id}")

        await ctx.message.add_reaction(OK_HAND)
        await asyncio.sleep(3)
        await ctx.message.delete()

    async def on_raw_reaction_remove(self, payload: RawReactionActionEvent):
        if payload.guild_id != Guild.id:
            return log.debug("Reaction was not added in the correct guild.")

        starboard = self.bot.get_channel(Channels.starboard)
        try:
            original, star_msg = await self.get_messages(payload, starboard)
        except (KeyError, NoStarboardException) as e:
            return log.exception(e)

        reaction = get(original.reactions, emoji=LVL1_STAR)

        if reaction is None:
            await self.delete_star(original, star_msg)
            return log.debug("There was not any reacted stars on the original message.")

        count = reaction.count

        if count < THRESHOLDS[LVL1_STAR]:
            if star_msg:
                await self.delete_star(original, star_msg)
                log.debug("Reaction count did not meet threshold anymore. Deleting entry")
            return

        await self.change_starcount(starboard, original, star_msg, count)
        log.debug("Updating existing starboard entry")

    async def on_raw_reaction_add(self, payload: RawReactionActionEvent):
        if payload.guild_id != Guild.id:
            return log.debug("Reaction was not added in the correct guild.")

        if payload.emoji.name != LVL1_STAR:
            return log.debug("Invalid emoji was reacted")

        if payload.channel_id == Channels.starboard:
            return log.debug("Can't star a message on the starboard.")

        starboard = self.bot.get_channel(Channels.starboard)

        try:
            original, star_msg = await self.get_messages(payload, starboard)
        except (KeyError, NoStarboardException) as e:
            return log.exception(e)

        reaction = get(original.reactions, emoji=LVL1_STAR)

        if reaction is None:
            await self.delete_star(original, star_msg)
            return log.debug("There was not any reacted stars on the original message.")

        count = reaction.count

        if count < THRESHOLDS[LVL1_STAR]:
            return log.debug("Reaction count did not meet threshold.")

        if star_msg is None:
            await self.post_new_entry(original, starboard)
            log.debug("star_msg was None, creating the starboard entry.")
        else:
            await self.change_starcount(starboard, original, star_msg, count)
            log.debug("star_msg was given, updating existing starboard entry")

    async def get_messages(
            self,
            payload: RawReactionActionEvent,
            starboard: TextChannel
    ) -> Tuple[Message, Optional[Message]]:
        """
        Method to fetch the message instance the payload represents,
        secondly checks the cache and API for an associated starboard entry

        There may not be a starboard entry, index 1 of tuple may be None.
        :param payload: The payload provided by on_raw_reaction_x
        :param starboard: A Discord.TextChannel where starboard entries are posted
        :return: Tuple[discord.Message, Optional[discord.Message]]
                 Returns the message instance that was starred and the associated
                 starboard message instance, if there is one else None.
        """

        if starboard is None:
            log.warning("Starboard TextChannel was not found!")
            raise NoStarboardException()

        # Better safe than sorry.
        try:
            original_channel = self.bot.get_channel(payload.channel_id)
        except KeyError as e:
            log.warning("Payload did not have a channel_id key")
            raise e

        try:
            original = await original_channel.get_message(payload.message_id)
        except KeyError as e:
            log.warning("Payload did not have a message_id key")
            raise e

        try:
            # See if it's stored in cache
            star_msg_id = self.star_msg_map[payload.message_id]
            star_msg = await starboard.get_message(star_msg_id)
            return original, star_msg
        except KeyError:
            log.debug(
                "star_msg_map did not have a starred message, checking API...")

        # Message was not in cache, but could be stored online
        url = f"{URLs.site_starboard_api}?message_id={payload.message_id}"
        response = await self.bot.http_session.get(
            url=url,
            headers=self.headers
        )
        json_data = await response.json()

        try:
            entry = json_data["message"]
        except KeyError:
            log.debug(
                "Response json from message_id endpoint didn't return a message key.")
            return original, None

        star_msg = None

        if entry is not None:
            try:
                star_msg_id = int(entry["bot_message_id"])
                star_msg = await starboard.get_message(star_msg_id)

            except (TypeError, IndexError):
                log.debug("No starboard message was found from cache or API")

        # If starboard message is not found star_msg is returned as None, and handled higher up.
        return original, star_msg

    async def post_new_entry(self, original: Message, starboard: TextChannel) -> None:
        """
        Posts a new entry to the starboard channel, constructs an embed with
        the channel, star count, message, author, avatar, and a jump to url.

        Posts the starboard entry to the starboard endpoint for storage.
        :param original: The original message that was starred
        :param starboard: The TextChannel the entry is posted to
        :return: None
        """

        embed = Embed()
        embed.description = dedent(
            f"""
                {original.content}

                [Jump to message]({original.jump_url})
                """
        )

        author = original.author
        embed.timestamp = original.created_at
        embed.set_author(name=author.display_name, icon_url=author.avatar_url)
        embed.colour = Colour.gold()

        try:
            star_msg = await starboard.send(
                f"{THRESHOLDS[LVL1_STAR]} {LVL1_STAR} {original.channel.mention}",
                embed=embed
            )
            log.debug(
                "Posted starboard entry embed to the starboard successfully.")
        except Forbidden as e:
            log.warning(
                f"Bot does not have permission to post in starboard channel ({starboard.id})")
            log.exception(e)
            return
        except HTTPException as e:
            log.warning("Something went wrong posting message to starboard")
            log.exception(e)
            return

        if await self.post_to_api(original, star_msg):
            self.star_msg_map[original.id] = star_msg.id

    async def change_starcount(
            self,
            starboard: TextChannel,
            original: Message,
            star_msg: Message,
            count: int
    ):
        """
        Message for changing the star counter on the starboard entry

        This method also makes sure the message ids are in cache, and that
        the api has the entry stored.

        :param original: The original message that was starred
        :param star_msg: The starboard entry message
        :param count: The count of stars on original
        :param starboard: The starboard TextChannel
        :return: None
        """

        star_embed = star_msg.embeds[0]

        if count < THRESHOLDS[LVL2_STAR]:
            star = LVL1_STAR
            color = Colour.gold()

        elif count < THRESHOLDS[LVL3_STAR]:
            star = LVL2_STAR
            color = Colour.gold()

        elif count < THRESHOLDS[LVL4_STAR]:
            star = LVL3_STAR
            color = Colour.orange()

        else:
            star = LVL4_STAR
            color = Colour.red()

        star_embed.color = color

        try:
            await star_msg.edit(
                content=f"{count} {star} {original.channel.mention}",
                embed=star_embed
            )
            log.debug("Edited starboard entry successfully.")
        except Forbidden as e:
            log.warning(
                f"Bot does not have permission to edit in starboard channel ({starboard.id})")
            log.exception(e)
            return
        except HTTPException as e:
            log.warning("Something went wrong editing message in starboard")
            log.exception(e)
            return

        # Make sure cache and API are up to date.
        if original.id not in self.star_msg_map:
            self.star_msg_map[original.id] = star_msg.id

        response = await self.bot.http_session.get(
            url=f"{URLs.site_starboard_api}?message_id={original.id}",
            headers=self.headers
        )
        json = await response.json()
        if not json["success"]:
            await self.post_to_api(original, star_msg)

    async def post_to_api(self, original: Message, star_msg: Message):
        """
        Utility method for posting a starboard entry to the api

        :param original: The original message that was starred
        :param star_msg: The star board entry message
        :return: True if the post was successful else False
        """

        response = await self.bot.http_session.post(
            url=URLs.site_starboard_api,
            headers=self.headers,
            json={
                "message_id": str(original.id),
                "bot_message_id": str(star_msg.id),
                "guild_id": str(Guild.id),
                "channel_id": str(original.channel.id),
                "author_id": str(original.author.id),
                "jump_to_url": original.jump_url
            }
        )

        if response.status != 200 and response.status != 400:
            # Delete it from the starboard before anyone notices our flaws in life.
            # 200 it was stored, 400 it exists already
            json_resp = await response.json()
            await star_msg.delete()
            log.warning(
                "Failed to post starred message with "
                f"status code {response.status} "
                f"response: {json_resp.get('response')}"
            )
            return False
        if response.status == 200:
            log.debug("Successfully posted json to endpoint, storing in cache...")
        elif response.status == 400:
            log.debug("Entry is already stored, ignoring.")
        return True

    async def delete_star(self, original: Message, star: Message):
        """
        Delete an entry on the starboard

        :param original: Message the starboard references
        :param star: Starboard message
        """

        deleting_failed = False

        try:
            await star.delete()
        except Forbidden:
            log.warning(
                "Failed to delete starboard entry, missing permissions!")
            deleting_failed = True
        except HTTPException:
            log.warning("Failed to delete starboard entry, HTTPexception.")
            deleting_failed = True

        url = f"{URLs.site_starboard_api}?message_id={original.id}"
        resp = await self.bot.http_session.delete(
            url=url,
            headers=self.headers
        )

        if resp.status != 200:
            log.warning(
                f"Failed to delete {original.id} from starboard db, status {resp.status}")
            if deleting_failed:
                log.warning("Failed to delete the starboard message, see previous warnings. "
                            "but database entry was not deleted.")
        else:
            log.info("Successfully deleted starboard entry from db")
            log.debug("Deleting entry from cache")
            try:
                del self.star_msg_map[original.id]
            except KeyError:
                log.debug(
                    f"Failed to delete from cache, KeyError - id: {original.id}")


def setup(bot):
    bot.add_cog(Starboard(bot))
    log.info("Cog loaded: Starboard")