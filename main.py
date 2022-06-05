from cProfile import label
from re import search
import discord
import asyncio
import functools
import itertools
import math
import random
import os
from keep_alive import keep_alive
import youtube_dl
from async_timeout import timeout
from discord.ext import commands
from dotenv import load_dotenv
from discord import app_commands
from discord.ui import Button, View
load_dotenv()



# Silence useless bug reports messages
youtube_dl.utils.bug_reports_message = lambda: ''
intents = discord.Intents().all()
#MY_GUILD = discord.Object(id=373491685331828756)
bot = commands.Bot(command_prefix=commands.when_mentioned_or(os.environ['COMMAND_PREFIX']),intents = intents, description='Much better than fredboat')

class VoiceError(Exception):
    pass


class YTDLError(Exception):
    pass


class YTDLSource(discord.PCMVolumeTransformer):
    YTDL_OPTIONS = {
        'format': 'bestaudio/best',
        'extractaudio': True,
        'audioformat': 'mp3',
        'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
        'restrictfilenames': True,
        'noplaylist': True,
        'nocheckcertificate': True,
        'ignoreerrors': False,
        'logtostderr': False,
        'quiet': True,
        'no_warnings': True,
        'default_search': 'auto',
        'source_address': '0.0.0.0',
    }

    FFMPEG_OPTIONS = {
        'before_options':
        '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
        'options': '-vn',
    }

    ytdl = youtube_dl.YoutubeDL(YTDL_OPTIONS)

    def __init__(self,
                 ctx: commands.Context,
                 source: discord.FFmpegPCMAudio,
                 *,
                 data: dict,
                 volume: float = 0.5):
        super().__init__(source, volume)

        self.requester = ctx.author
        self.channel = ctx.channel
        self.data = data

        self.uploader = data.get('uploader')
        self.uploader_url = data.get('uploader_url')
        date = data.get('upload_date')
        self.upload_date = date[6:8] + '.' + date[4:6] + '.' + date[0:4]
        self.title = data.get('title')
        self.thumbnail = data.get('thumbnail')
        self.description = data.get('description')
        self.duration = self.parse_duration(int(data.get('duration')))
        self.tags = data.get('tags')
        self.url = data.get('webpage_url')
        self.views = data.get('view_count')
        self.likes = data.get('like_count')
        self.dislikes = data.get('dislike_count')
        self.stream_url = data.get('url')

    def __str__(self):
        return '**{0.title}** by **{0.uploader}**'.format(self)

    @classmethod
    async def create_source(cls,
                            ctx: commands.Context,
                            search: str,
                            *,
                            loop: asyncio.BaseEventLoop = None):
        loop = loop or asyncio.get_event_loop()

        partial = functools.partial(cls.ytdl.extract_info,
                                    search,
                                    download=False,
                                    process=False)
        data = await loop.run_in_executor(None, partial)

        if data is None:
            raise YTDLError(
                'Couldn\'t find anything that matches `{}`'.format(search))

        if 'entries' not in data:
            process_info = data
        else:
            process_info = None
            for entry in data['entries']:
                if entry:
                    process_info = entry
                    break

            if process_info is None:
                raise YTDLError(
                    'Couldn\'t find anything that matches `{}`'.format(search))

        webpage_url = process_info['webpage_url']
        partial = functools.partial(cls.ytdl.extract_info,
                                    webpage_url,
                                    download=False)
        processed_info = await loop.run_in_executor(None, partial)

        if processed_info is None:
            raise YTDLError('Couldn\'t fetch `{}`'.format(webpage_url))

        if 'entries' not in processed_info:
            info = processed_info
        else:
            info = None
            while info is None:
                try:
                    info = processed_info['entries'].pop(0)
                except IndexError:
                    raise YTDLError(
                        'Couldn\'t retrieve any matches for `{}`'.format(
                            webpage_url))

        return cls(ctx,
                   discord.FFmpegPCMAudio(info['url'], **cls.FFMPEG_OPTIONS),
                   data=info)

    @staticmethod
    def parse_duration(duration: int):
        minutes, seconds = divmod(duration, 60)
        hours, minutes = divmod(minutes, 60)
        days, hours = divmod(hours, 24)

        duration = []
        if days > 0:
            duration.append('{} days'.format(days))
        if hours > 0:
            duration.append('{} hours'.format(hours))
        if minutes > 0:
            duration.append('{} minutes'.format(minutes))
        if seconds > 0:
            duration.append('{} seconds'.format(seconds))

        return ', '.join(duration)


class Song:
    __slots__ = ('source', 'requester')

    def __init__(self, source: YTDLSource):
        self.source = source
        self.requester = source.requester

    def create_embed(self):
        embed = (discord.Embed(
            title='Now playing',
            description='```css\n{0.source.title}\n```'.format(self),
            color=discord.Color.blurple()).add_field(
                name='Duration', value=self.source.duration).add_field(
                    name='Requested by',
                    value=self.requester.mention).add_field(
                        name='Uploader',
                        value='[{0.source.uploader}]({0.source.uploader_url})'.
                        format(self)).add_field(
                            name='URL',
                            value='[Click]({0.source.url})'.format(self)).
                 set_thumbnail(url=self.source.thumbnail))

        return embed


class SongQueue(asyncio.Queue):
    def __getitem__(self, item):
        if isinstance(item, slice):
            return list(
                itertools.islice(self._queue, item.start, item.stop,
                                 item.step))
        else:
            return self._queue[item]

    def __iter__(self):
        return self._queue.__iter__()

    def __len__(self):
        return self.qsize()

    def clear(self):
        self._queue.clear()

    def shuffle(self):
        random.shuffle(self._queue)

    def remove(self, index: int):
        del self._queue[index]


class VoiceState(discord.VoiceState):
    def __init__(self, bot: commands.Bot, ctx: commands.Context):
        self.bot = bot
        self._ctx = ctx

        self.current = None
        self.voice = None
        self.next = asyncio.Event()
        self.songs = SongQueue()

        self._loop = False
        self._volume = 0.5
        self.skip_votes = set()

        self.audio_player = bot.loop.create_task(self.audio_player_task())

    def __del__(self):
        self.audio_player.cancel()

    @property
    def loop(self):
        return self._loop

    @loop.setter
    def loop(self, value: bool):
        self._loop = value

    @property
    def volume(self):
        return self._volume

    @volume.setter
    def volume(self, value: float):
        self._volume = value

    @property
    def is_playing(self):
        return self.voice and self.current

    async def audio_player_task(self):
        while True:
            self.next.clear()

            if not self.loop:
                # Try to get the next song within a day.
                # If no song will be added to the queue in time,
                # the player will disconnect due to performance
                # reasons.
                try:
                    async with timeout(86400):  # 1 day
                        self.current = await self.songs.get()
                except asyncio.TimeoutError:
                    self.bot.loop.create_task(self.stop())
                    return

            self.current.source.volume = self._volume
            self.voice.play(self.current.source, after=self.play_next_song)
            await self.current.source.channel.send(
                embed=self.current.create_embed())

            await self.next.wait()

    def play_next_song(self, error=None):
        if error:
            raise VoiceError(str(error))

        self.next.set()

    def skip(self):
        self.skip_votes.clear()

        if self.is_playing:
            self.voice.stop()

    async def stop(self):
        self.songs.clear()

        if self.voice:
            await self.voice.disconnect()
            self.voice = None

class MyView(View):





    @discord.ui.button(label = 'Pause', style = discord.ButtonStyle.red)
    async def pause_button(self, interaction:discord.Interaction, button: discord.ui.Button):
        if interaction.guild.voice_client.is_playing():
            button.style = discord.ButtonStyle.green
            button.label = 'Resume'
            interaction.guild.voice_client.pause()
            await interaction.response.edit_message(content = f'{interaction.user.mention} Paused',view = self)
            asyncio.sleep(15)
            await interaction.response.edit_message(content = f'Music player controls',view = self)            
            return

        elif interaction.guild.voice_client.is_paused():
            button.style = discord.ButtonStyle.red
            button.label = 'Pause'
            interaction.guild.voice_client.resume()
            await interaction.response.edit_message(content = f'{interaction.user.mention} Resumed',view = self)
            asyncio.sleep(15)
            await interaction.response.edit_message(content = f'Music player controls',view = self)
            return
        else:
            interaction.response.edit_message(content = f'{interaction.user.mention} Resumed',view = self)
        
    # @discord.ui.button(label = 'Resume', style = discord.ButtonStyle.green)
    # async def resume_button(self, interaction:discord.Interaction, button: discord.ui.Button):
    #     interaction.guild.voice_client.resume()
    #     await interaction.response.edit_message(content = f'Resuming',view = self)


    @discord.ui.button(label = 'Skip', style = discord.ButtonStyle.blurple)
    async def skip_button(self, interaction:discord.Interaction, button: discord.ui.Button):
        interaction.guild.voice_client.stop()
        await interaction.response.edit_message(content = f'{interaction.user.mention} Skipped',view = self)



class Music(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot: commands.Bot = bot
        self.voice_states = {}

    def get_voice_state(self, ctx: commands.Context):
        state = self.voice_states.get(ctx.guild.id)
        if not state:
            state = VoiceState(self.bot, ctx)
            self.voice_states[ctx.guild.id] = state

        return state

    def cog_unload(self):
        for state in self.voice_states.values():
            self.bot.loop.create_task(state.stop())

    def cog_check(self, ctx: commands.Context):
        if not ctx.guild:
            raise commands.NoPrivateMessage(
                'You tryin to slide into my DM\'s?')

        return True

    async def cog_before_invoke(self, ctx: commands.Context):
        ctx.voice_state = self.get_voice_state(ctx)

    async def cog_command_error(self, ctx: commands.Context,
                                error: commands.CommandError):
        await ctx.send('An error occurred: {}'.format(str(error)))

    @commands.hybrid_command(name='come', invoke_without_subcommand=True)
    async def _join(self, ctx: commands.Context):
        """Joins your voice channel."""

        destination = ctx.author.voice.channel
        if ctx.voice_state.voice:
            await ctx.voice_state.voice.move_to(destination)
            await ctx.send('Joining {} '.format(ctx.author.mention))
            return

        ctx.voice_state.voice = await destination.connect()
        await ctx.send('Joining {} '.format(ctx.author.mention))

    @commands.hybrid_command(name='goto',aliases=['summon'])
    #@commands.has_permissions(manage_guild=True)
    async def _summon(self, ctx: commands.Context, channel: discord.VoiceChannel):
        """Sends the bot to a voice channel.

        If no channel was specified, it joins your channel.
        """

        destination = channel
        if ctx.voice_state.voice:
            await ctx.voice_state.voice.move_to(destination)
            await ctx.send('Joining {}'.format(destination))
            return

        ctx.voice_state.voice = await destination.connect()
        await ctx.send('Joining {}'.format(destination))

    @commands.hybrid_command(name='leave', aliases=['getout','l','fuckoff'])
    #@commands.has_permissions(manage_guild=True)
    async def _leave(self, ctx: commands.Context):
        """Clears the queue and leaves the voice channel."""

        if not ctx.voice_state.voice:
            return await ctx.send('Not connected to any voice channel.')

        await ctx.voice_state.stop()
        del self.voice_states[ctx.guild.id]
        await ctx.send('OK bye')

    @commands.hybrid_command(name='volume')
    @app_commands.describe(volume = 'Volume from 0 to 100')
    @commands.has_permissions(manage_guild=True)
    async def _volume(self, ctx: commands.Context, volume: int = None):
        """Sets the volume of the player."""
        if not volume:
            return await ctx.send(ctx.voice_state.volume*100)

        if not ctx.voice_state.is_playing:
            return await ctx.send('Nothing being played at the moment.')

        if 0 > volume > 100:
            return await ctx.send('Volume must be between 0 and 100')

        ctx.voice_state.volume = volume / 100
        await ctx.send('Volume of the player set to {}%'.format(volume))

    @commands.hybrid_command(name='now', aliases=['current', 'playing'])
    async def _now(self, ctx: commands.Context):
        """Displays the currently playing song."""
        
        await ctx.send(embed=ctx.voice_state.current.create_embed())

    @commands.hybrid_command(name='sync')
    #@commands.has_permissions(manage_guild=True)
    async def _sync(self, ctx: commands.Context):
        """**DEVELOPER COMMAND syncs updated commands**"""
        
        #bot.tree.copy_global_to(guild=discord.Object(id=373491685331828756))
        #await bot.tree.sync(guild=discord.Object(id=396633477186977812))
        #await bot.tree.sync(guild=discord.Object(id=373491685331828756))
        await bot.tree.sync()
        await ctx.send('syncing')

    @commands.hybrid_command(name='pause')
    #@commands.has_permissions(manage_guild=True)
    async def _pause(self, ctx: commands.Context):
        """Pauses the currently playing song."""

        if ctx.voice_state.is_playing and ctx.voice_state.voice.is_playing():
            ctx.voice_state.voice.pause()
            #await ctx.message.add_reaction('⏯')
            await ctx.send('Pausing')

    @commands.hybrid_command(name='resume')
    #@commands.has_permissions(manage_guild=True)
    async def _resume(self, ctx: commands.Context):
        """Resumes a currently paused song."""

        if ctx.voice_state.is_playing and ctx.voice_state.voice.is_paused():
            ctx.voice_state.voice.resume()
            #await ctx.message.add_reaction('⏯')
            await ctx.send('Resuming')

    @commands.hybrid_command(name='stop')
    #@commands.has_permissions(manage_guild=True)
    async def _stop(self, ctx: commands.Context):
        """Stops playing song and clears the queue."""

        ctx.voice_state.songs.clear()

        if ctx.voice_state.is_playing:
            ctx.voice_state.voice.stop()
            #await ctx.message.add_reaction('⏹')
            await ctx.send('Stopping')

    @commands.hybrid_command(name='skip')
    async def _skip(self, ctx: commands.Context):
        """Vote to skip a song. The requester can automatically skip.
        2 skip votes are needed for the song to be skipped.
        """

        if not ctx.voice_state.is_playing:
            return await ctx.send('Not playing any music right now...')

        voter = ctx.message.author
        if voter == ctx.voice_state.current.requester:
            #await ctx.message.add_reaction('⏭')
            await ctx.send('Skipping')
            ctx.voice_state.skip()

        elif voter.id not in ctx.voice_state.skip_votes:
            ctx.voice_state.skip_votes.add(voter.id)
            total_votes = len(ctx.voice_state.skip_votes)

            if total_votes >= 1:
                #await ctx.message.add_reaction('⏭')
                await ctx.send('Skipping')
                ctx.voice_state.skip()
            else:
                await ctx.send('Skip vote added, currently at **{}/1**'.format(
                    total_votes))

        else:
            await ctx.send('You have already voted to skip this song.')

    @commands.hybrid_command(name='queue',aliases=['list'])
    @app_commands.describe(page = 'Page of queue')
    async def _queue(self, ctx: commands.Context, *, page: int = 1):
        """Shows the player's queue.

        You can optionally specify the page to show. Each page contains 10 elements.
        """

        if len(ctx.voice_state.songs) == 0:
            return await ctx.send('Empty queue.')

        items_per_page = 10
        pages = math.ceil(len(ctx.voice_state.songs) / items_per_page)

        start = (page - 1) * items_per_page
        end = start + items_per_page

        queue = ''
        for i, song in enumerate(ctx.voice_state.songs[start:end],
                                 start=start):
            queue += '`{0}.` [**{1.source.title}**]({1.source.url})\n'.format(
                i + 1, song)

        embed = (discord.Embed(description='**{} tracks:**\n\n{}'.format(
            len(ctx.voice_state.songs), queue)).set_footer(
                text='Viewing page {}/{}'.format(page, pages)))
        await ctx.send(embed=embed)

    @commands.hybrid_command(name='shuffle')
    async def _shuffle(self, ctx: commands.Context):
        """Shuffles the queue."""

        if len(ctx.voice_state.songs) == 0:
            return await ctx.send('Empty queue.')

        ctx.voice_state.songs.shuffle()
        #await ctx.message.add_reaction('✅')
        await ctx.send('Shuffling')

    @commands.hybrid_command(name='remove')
    @app_commands.describe(index = 'Removes the song in queue at index')
    async def _remove(self, ctx: commands.Context, index: int):
        """Removes a song from the queue at a given index."""

        if len(ctx.voice_state.songs) == 0:
            return await ctx.send('Empty queue.')

        await ctx.send('Removing {0.source.title} at index:{1}'.format(ctx.voice_state.songs[index-1],index))
        ctx.voice_state.songs.remove(index - 1)
        #await ctx.message.add_reaction('✅')
        
        

    # @commands.hybrid_command(name='loop')
    # async def _loop(self, ctx: commands.Context):
    #     """Loops the currently playing song.

    #     Invoke this command again to unloop the song.
    #     """

    #     if not ctx.voice_state.is_playing:
    #         return await ctx.send('Nothing being played at the moment.')

    #     # Inverse boolean value to loop and unloop.
    #     ctx.voice_state.loop = not ctx.voice_state.loop
    #     await ctx.send('looping')

    @commands.hybrid_command(name='play',aliases=['p'])
    @app_commands.describe(search = 'Title of song or youtube URL')
    async def _play(self, ctx: commands.Context, *, search: str):
        """Plays a song, press enter to start typing in the argument for URL or song name.

        If there are songs in the queue, this will be queued until the
        other songs finished playing.

        This command automatically searches from various sites if no URL is provided.
        A list of these sites can be found here: https://rg3.github.io/youtube-dl/supportedsites.html
        """

        if not ctx.voice_state.voice:
            await ctx.invoke(self._join)

        async with ctx.typing():
            try:
                source = await YTDLSource.create_source(ctx,
                                                        search,
                                                        loop=self.bot.loop)
            except YTDLError as e:
                await ctx.send(
                    'An error occurred while processing this request: {}'.
                    format(str(e)))
            else:
                song = Song(source)

                await ctx.voice_state.songs.put(song)
                await ctx.send('{} Enqueued {}'.format(ctx.author.mention,str(source)))

    @commands.hybrid_command(name='player',aliases=['gui','buttons'])
    
    async def player(self,ctx: commands.Context):
        """Music player GUI buttons"""
        view=MyView(timeout = None)
        await ctx.send("Music player",view=view)


    @commands.hybrid_command(name='fix')
    #@commands.has_permissions(manage_guild=True)
    async def fix(self, ctx: commands.Context):
        """Tries to fix the bot if broken"""
        try:

            ctx.voice_state.voice.pause()
            ctx.voice_state.skip()
            ctx.voice_state.songs.clear()
            await ctx.voice_state.stop()
            del self.voice_states[ctx.guild.id]

        except:
            await ctx.send('fixing')


    
    # @commands.hybrid_command(name='log')
    # async def log(self, ctx: commands.Context):
    #     logchannel = discord.utils.get(ctx.guild.text_channels, name="logs")
    #     await logchannel.send('Joined')






    
    


@bot.tree.context_menu(name='Show Join Date')
async def show_join_date(interaction: discord.Interaction, member: discord.Member):
    # The format_dt function formats the date time into a human readable representation in the official client
    await interaction.response.send_message(f'{member} joined at {discord.utils.format_dt(member.joined_at)}')

# @bot.tree.command()
# async def hello(interaction: discord.Interaction) -> None:
#   await interaction.response.send_message("Hello from my command!")
# ### NOTE: the above is a global command, see the `main()` func below:








@bot.event
async def on_voice_state_update(member,before,after):
    print("{member},Joined")
    logchannel = discord.utils.get(member.guild.text_channels, name="logs")
    if not before.channel and after.channel:
        
        await logchannel.send(f"""{member.mention}Joined {after.channel}""")

@bot.event  
async def on_ready():
    print('Logged in as:\nBOT:{0.user.name}\nUSER:{0.user.id}'.format(bot))
    print(f"Discord API version: {discord.__version__}")
    print('Command Prefix:',os.environ['COMMAND_PREFIX'])
    await bot.change_presence(activity=discord.Game('with ass'))
    #print(await bot.tree.fetch_commands())


async def main():
    async with bot:
        
        await bot.add_cog(Music(bot))
        await bot.start(os.environ['TOKEN'])
        


asyncio.run(main())
