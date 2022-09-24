from telethon import TelegramClient, events

from telethon.tl.functions.channels import JoinChannelRequest, LeaveChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest, CheckChatInviteRequest

from telethon.errors.rpcerrorlist import InviteHashExpiredError, InviteHashEmptyError, MediaInvalidError, \
    MediaEmptyError

from telethon.tl.types import MessageMediaDocument, MessageMediaPhoto, MessageEntityTextUrl, \
    ChatInviteAlready, ChatInvite, ReplyInlineMarkup, KeyboardButtonRow, KeyboardButtonUrl

from uuid import uuid4
import os
import shutil
import re

import sqlite3
import json

import logging

import configparser
cfg = configparser.ConfigParser()
cfg.read('./config/config.cfg')

# TODO: VERY BIG TODO: Add subscriptions on sly channels like "admin-approved" or "subscription through bot"

# TODO: Unsubscribe actually unsubscribes client side from empty channels

# TODO: Set adequate logging [~]

# TODO: Little cosmetic when list is empty write different message

# TODO: We can disable event propagation

# TODO: (very hard) make ad filter [~]
#  some groups add links to THEIR channel, work this out [+]
#  add t.me/{etc} to ad flags [+]
#  check entities for text-url for links [+]
#  check buttons for links [+]
#  check text for links [+]
#  add 'www' to flags [+]

# TODO: Some kind of lock per user on sending albums (this will solve albums merging problem)

# TODO: Do not send media partly, just send link preview in any case except full media sent

# Mute channels on which we subscribe [+]
#  (we can't control other connected apps, Telegram Desktop (and others) is an app)

# Check if it is possible to track channels based on id of a channel, not on username (possible db overwrite) [+]

# Carefully look into possibility of tracking private channels [+]

# Consider re-upload if possible (limits on file size?), it will help with private channels [+]

# Check for "restricted", "scam", "fake" flags [+]

# Posts are duplicating! Look into it [+]

# Re-upload files through bot [+]

# Fix UserAlreadyParticipantError [+]

# Share subscriptions [+]
#  (you can send output from /list, which is enough)

logging.basicConfig(filename='newsletter.log', filemode='a',
                    format='%(asctime)s %(name)s %(levelname)s %(message)s',
                    datefmt='[%d-%m-%Y %H:%M:%S]',
                    level=logging.INFO)

logger = logging.getLogger('chat')

api_id = int(cfg['ACCOUNTS']['api_id'])
api_hash = cfg['ACCOUNTS']['api_hash']
bot_token = cfg['ACCOUNTS']['bot_token']
bot_client = TelegramClient('bot_client', api_id, api_hash)
bot_bot = TelegramClient('bot_bot', api_id, api_hash).start(bot_token=bot_token)


def db_add_new_channel_or_user(channel_id, channel_username, channel_title, user_id):
    with sqlite3.connect('channel_to_users.db') as db:
        db_cursor = db.cursor()

        sql_insert = 'INSERT INTO channels(id,username,title,users) VALUES(?,?,?,?)'
        sql_select = 'SELECT * FROM channels WHERE id = ?'
        sql_update = 'UPDATE channels SET users = ? WHERE id = ?'

        try:
            db_cursor.execute(sql_insert, (channel_id, channel_username, channel_title, json.dumps([user_id])))
        except sqlite3.IntegrityError:
            db_cursor.execute(sql_select, (channel_id,))
            data = db_cursor.fetchall()
            data = json.loads(data[0][3])
            if user_id not in data:
                data.append(user_id)
            db_cursor.execute(sql_update, (json.dumps(data), channel_id))


def db_get_channel_users(channel_id):
    with sqlite3.connect('channel_to_users.db') as db:
        db_cursor = db.cursor()
        sql_select = 'SELECT * FROM channels WHERE id = ?'
        db_cursor.execute(sql_select, (channel_id,))
        data = db_cursor.fetchall()
        data = json.loads(data[0][3])
        return data


def db_get_user_channels(user_id):
    with sqlite3.connect('channel_to_users.db') as db:
        db_cursor = db.cursor()
        sql = 'SELECT * FROM channels WHERE users LIKE ?'
        db_cursor.execute(sql, ('%{0}%'.format(user_id),))
        data = db_cursor.fetchall()
        return data


def db_delete_user_from_channel(channel_id, user_id):
    with sqlite3.connect('channel_to_users.db') as db:
        db_cursor = db.cursor()
        sql_select = 'SELECT * FROM channels WHERE id = ?'
        db_cursor.execute(sql_select, (channel_id,))

        data = db_cursor.fetchall()
        data = json.loads(data[0][3])
        data.remove(user_id)
        if data:
            sql_update = 'UPDATE channels SET users = ? WHERE id = ?'
            db_cursor.execute(sql_update, (json.dumps(data), channel_id))
        else:
            sql_delete = 'DELETE FROM channels WHERE id = ?'
            db_cursor.execute(sql_delete, (channel_id,))

        return 0


def db_set_new_user(user_id):
    with sqlite3.connect('channel_to_users.db') as db:
        db_cursor = db.cursor()

        try:
            sql_insert = 'INSERT INTO states(user,state) VALUES(?,?)'
            db_cursor.execute(sql_insert, (user_id, 'idle'))
        except sqlite3.IntegrityError:
            sql_update = 'UPDATE states SET state = ? WHERE user = ?'
            db_cursor.execute(sql_update, ('idle', user_id))

        return 0


def db_update_user_state(user_id, state):
    with sqlite3.connect('channel_to_users.db') as db:
        db_cursor = db.cursor()

        sql_update = 'UPDATE states SET state = ? WHERE user = ?'
        db_cursor.execute(sql_update, (state, user_id))

        return 0


def db_get_user_state(user_id):
    with sqlite3.connect('channel_to_users.db') as db:
        db_cursor = db.cursor()

        sql_select = 'SELECT * FROM states WHERE user = ?'
        db_cursor.execute(sql_select, (user_id,))

        data = db_cursor.fetchall()

        return data[0][1]


ad_flags = ['https://', 'http://', '@', 't.me/', 'www.', 'T.me/', 'WWW.']


def ad_check(message, channel_username):
    # Проверяем сообщение на ссылки
    for flag in ad_flags:
        # if message.message.find(flag) != -1:
        for found in re.finditer(flag, message.message):
            if channel_username is not None:
                # Вырезаем именно эту ссылку из сообщения

                # Отрезаем левую часть
                cut_left_part = message.message[found.start():]

                # Отрезаем правую часть
                next_line_symbol = cut_left_part.find('\n')
                space_symbol = cut_left_part.find(' ')

                # Если не нашли этих символов, то сообщение и есть ссылка
                if next_line_symbol == -1 and space_symbol == -1:
                    cut_right_part = cut_left_part
                # Если один из символов нашёлся, а второй нет - режем по первому
                elif next_line_symbol == -1:
                    cut_right_part = cut_left_part[:space_symbol]
                elif space_symbol == -1:
                    cut_right_part = cut_left_part[:next_line_symbol]
                # Если оба нашлись - режем по тому что ближе к началу ссылки
                elif next_line_symbol < space_symbol:
                    cut_right_part = cut_left_part[:next_line_symbol]
                elif space_symbol < next_line_symbol:
                    cut_right_part = cut_left_part[:space_symbol]
                # Если ничего не сработало это пометится как реклама
                else:
                    cut_right_part = ''

                # Если ссылка не на этот же канал (зачем они так делают)
                if cut_right_part.find(channel_username) == -1:
                    return True
            else:
                return True

    # Проверяем текст-ссылки если есть
    if message.entities:
        for entity in message.entities:
            if type(entity) == MessageEntityTextUrl:
                if channel_username is not None:
                    # Если ссылка не на этот же канал (зачем они так делают)
                    if entity.url.find(channel_username) == -1:
                        return True
                else:
                    return True

    # Проверяем кнопки если есть
    if message.reply_markup:
        if type(message.reply_markup) == ReplyInlineMarkup:
            for item in message.reply_markup.rows:
                if type(item) == KeyboardButtonRow:
                    for button in item.buttons:
                        if type(button) == KeyboardButtonUrl:
                            if channel_username is not None:
                                # Если ссылка не на этот же канал (зачем они так делают)
                                if button.url.find(channel_username) == -1:
                                    return True
                            else:
                                return True

    return False


@bot_client.on(events.Album())
async def handle_client_channels_albums(event):
    # Переменные для канала и сообщений в альбоме
    channel = event.chat
    messages = event.messages

    if channel.username is None:
        post_link = '[{0}](https://t.me/c/{1}/{2})'.format(channel.title, channel.id, messages[0].id)
    else:
        post_link = '[{0}](https://t.me/{1}/{2})'.format(channel.title, channel.username, messages[0].id)

    album_messages_content = ''

    for message in messages:
        album_messages_content += '{0}\n'.format(message.stringify())

    album_messages_content = album_messages_content[:-1]

    logger.info('{0} NEW album\n'
                '{1}'
                .format(post_link, album_messages_content))

    # Отсечка рекламных постов
    logger.info('{0} checking if album is an ad...'.format(post_link))
    if any(ad_check(message, channel.username) for message in messages):
        logger.info('{0} album is an ad'.format(post_link))
        users = db_get_channel_users(channel.id)
        logger.info('{0} sending to {1}'.format(post_link, json.dumps(users)))
        for user in users:
            await bot_bot.send_message(user, '{0}\n'
                                             'Возможно рекламный пост'
                                       .format(post_link),
                                       link_preview=False)
            logger.info('{0} sent to {1}'.format(post_link, user))
    else:
        logger.info('{0} album is not an ad'.format(post_link))
        # We assume that caption message always comes first
        caption = messages[0].message

        # Всякие конфигурационные переменные
        message_size_cap = 10485760  # 10 megabytes in bytes
        temp_dir_name = str(uuid4())
        os.mkdir('./{0}'.format(temp_dir_name))
        media = []

        for message in messages:
            # Если это документ или сжатая фотография
            if type(message.media) is MessageMediaPhoto:
                logger.info('{0} found compressed photo'.format(post_link))
                media_path = await message.download_media('./{0}/{1}'.format(temp_dir_name, str(uuid4())))
                logger.info('{0} downloaded compressed photo to {1}'.format(post_link, media_path))
                # media.append(os.path.basename(media_path))
                media.append(media_path)
            elif type(message.media) is MessageMediaDocument:
                logger.info('{0} found document'.format(post_link))
                message_size_cap -= message.media.document.size
                if message_size_cap < 0:
                    logger.info('{0} too big document, cancel download'.format(post_link))
                    break
                else:
                    media_path = await message.download_media('./{0}/{1}'.format(temp_dir_name, str(uuid4())))
                    logger.info('{0} downloaded document to {1}'.format(post_link, media_path))
                    # media.append(os.path.basename(media_path))
                    media.append(media_path)

        try:
            # Если лимит законился или ничего не скачали не отправляем ничего кроме ссылки на пост
            if message_size_cap < 0 or not media:
                logger.info('{0} nothing was downloaded, because media size exceeds 10 megabytes'.format(post_link))
                users = db_get_channel_users(channel.id)
                logger.info('{0} sending to {1}'.format(post_link, json.dumps(users)))
                for user in users:
                    await bot_bot.send_message(user, '{0}\n'
                                                     '{1}'
                                               .format(post_link, messages[0].message))
                    logger.info('{0} sent to {1}'.format(post_link, user))
            # Если всё удалось загрузить
            else:
                logger.info('{0} successfully downloaded all media'.format(post_link))
                users = db_get_channel_users(channel.id)
                logger.info('{0} sending to {1}'.format(post_link, json.dumps(users)))
                for user in users:
                    await bot_bot.send_file(user, media, caption='{0}\n'
                                                                 '{1}'
                                            .format(post_link, caption),
                                            link_preview=False)
                    logger.info('{0} sent to {1}'.format(post_link, user))
        # Если не получилось отослать картинки (на это мы повлиять не можем)
        except (MediaInvalidError, MediaEmptyError):
            logger.info('{0} some or all media was downloaded, but was not sent, '
                        'because some Telegram servers error'.format(post_link))
            users = db_get_channel_users(channel.id)
            logger.info('{0} sending to {1}'.format(post_link, json.dumps(users)))
            for user in users:
                await bot_bot.send_message(user, '{0}\n'
                                                 '{1}'
                                           .format(post_link, messages[0].message))
                logger.info('{0} sent to {1}'.format(post_link, user))
        try:
            # Удаляем временную папку
            logger.info('{0} removing temporary directory'.format(post_link))
            shutil.rmtree('./{0}'.format(temp_dir_name))
            logger.info('{0} removed temporary directory'.format(post_link))
        except FileNotFoundError:
            pass


@bot_client.on(events.NewMessage())
async def handle_client_channels(event):
    # Put this somewhere properly (no, this would get more views on posts, which may somehow violate telegram rules)
    # await bot_client.send_read_acknowledge(channel, message)

    # https://t.me/{channel_username}/{message_id}
    # https://t.me/c/{channel_id}/{message_id}

    channel = event.chat
    message = event.message

    # Отсекаем альбомы
    if message.grouped_id is None:

        if channel.username is None:
            post_link = '[{0}](https://t.me/c/{1}/{2})'.format(channel.title, channel.id, message.id)
        else:
            post_link = '[{0}](https://t.me/{1}/{2})'.format(channel.title, channel.username, message.id)

        logger.info('{0} NEW post\n'
                    '{1}'
                    .format(post_link, message.stringify()))

        # Отсекаем рекламу
        logger.info('{0} checking if post is an ad...'.format(post_link))
        if ad_check(message, channel.username):
            logger.info('{0} post is an ad'.format(post_link))
            users = db_get_channel_users(channel.id)
            logger.info('{0} sending to {1}'.format(post_link, json.dumps(users)))
            for user in users:
                await bot_bot.send_message(user, '{0}\n'
                                                 'Возможно рекламный пост'
                                           .format(post_link),
                                           link_preview=False)
                logger.info('{0} sent to {1}'.format(post_link, user))
        else:
            logger.info('{0} post is not an ad'.format(post_link))
            # Если есть какие-то файлы
            logger.info('{0} checking if post has media...'.format(post_link))
            if message.media is not None:
                logger.info('{0} post has media '.format(post_link))
                media_path = ''
                temp_dir_name = str(uuid4())
                message_size_cap = 10485760  # 10 megabytes in bytes
                # Если это документ или сжатая фотография
                if type(message.media) is MessageMediaPhoto:
                    logger.info('{0} found compressed photo'.format(post_link))
                    media_path = await message.download_media('./{0}/{1}'.format(temp_dir_name, str(uuid4())))
                    logger.info('{0} downloaded compressed photo to {1}'.format(post_link, media_path))
                    # media.append(os.path.basename(media_path))
                elif type(message.media) is MessageMediaDocument:
                    logger.info('{0} found document'.format(post_link))
                    message_size_cap -= message.media.document.size
                    if message_size_cap >= 0:
                        media_path = await message.download_media('./{0}/{1}'.format(temp_dir_name, str(uuid4())))
                        logger.info('{0} downloaded document to {1}'.format(post_link, media_path))
                        # media.append(os.path.basename(media_path))
                    else:
                        logger.info('{0} too big document, cancel download'.format(post_link))

                # Предполагаем что там только одно прикрепление и это фото или документ
                # Если превысили лимит или не получилось скачать, а файлы это фото или документ
                try:
                    if (message_size_cap < 0) and \
                            (type(message.media) == MessageMediaPhoto or type(message.media) == MessageMediaDocument):
                        logger.info('{0} nothing was downloaded, because media size exceeds 10 megabytes'
                                    .format(post_link))
                        users = db_get_channel_users(channel.id)
                        logger.info('{0} sending to {1}'.format(post_link, json.dumps(users)))
                        for user in users:
                            await bot_bot.send_message(user, '{0}\n'
                                                             '{1}'
                                                       .format(post_link, message.message))
                            logger.info('{0} sent to {1}'.format(post_link, user))
                    # Если скачалось (а мы качаем только фото или документы)
                    elif media_path:
                        logger.info('{0} successfully downloaded all media'.format(post_link))
                        users = db_get_channel_users(channel.id)
                        logger.info('{0} sending to {1}'.format(post_link, json.dumps(users)))
                        for user in users:
                            await bot_bot.send_file(user, media_path, caption='{0}\n'
                                                                              '{1}'
                                                    .format(post_link, message.message), link_preview=False)
                            logger.info('{0} sent to {1}'.format(post_link, user))
                    # Если не скачалось и файлы не фото или документы (все остальные случаи)
                    else:
                        logger.info('{0} post has no media to re-upload'.format(post_link))
                        users = db_get_channel_users(channel.id)
                        logger.info('{0} sending to {1}'.format(post_link, json.dumps(users)))
                        for user in users:
                            await bot_bot.send_message(user, '{0}\n'
                                                             '{1}'
                                                       .format(post_link,
                                                               message.message), link_preview=False)
                            logger.info('{0} sent to {1}'.format(post_link, user))
                # Если не получилось отослать картинки (на это мы повлиять не можем)
                except (MediaInvalidError, MediaEmptyError):
                    logger.info('{0} some or all media was downloaded, but was not sent, '
                                'because some Telegram servers error'.format(post_link))
                    users = db_get_channel_users(channel.id)
                    logger.info('{0} sending to {1}'.format(post_link, json.dumps(users)))
                    for user in users:
                        await bot_bot.send_message(user, '{0}\n'
                                                         '{1}'
                                                   .format(post_link, message.message))
                        logger.info('{0} sent to {1}'.format(post_link, user))
                try:
                    # Удаляем временную папку
                    logger.info('{0} removing temporary directory'.format(post_link))
                    shutil.rmtree('./{0}'.format(temp_dir_name))
                    logger.info('{0} removed temporary directory'.format(post_link))
                except FileNotFoundError:
                    pass

            # Если файлов нет
            else:
                logger.info('{0} post has no media'.format(post_link))
                users = db_get_channel_users(channel.id)
                logger.info('{0} sending to {1}'.format(post_link, json.dumps(users)))
                for user in users:
                    await bot_bot.send_message(user, '{0}\n'
                                                     '{1}'
                                               .format(post_link,
                                                       message.message), link_preview=False)
                    logger.info('{0} sent to {1}'.format(post_link, user))


async def check_link(message):
    link = message.message

    if link.startswith('https://t.me/joinchat/'):
        try:
            invite = await bot_client(CheckChatInviteRequest(link.split('/')[4]))
            if type(invite) == ChatInviteAlready:
                return 'chat_ok', invite.chat
            elif type(invite) == ChatInvite:
                if invite.channel:
                    updates = await bot_client(ImportChatInviteRequest(link.split('/')[4]))
                    chat = updates.chats[0]
                    if chat.restricted or chat.scam or chat.fake:
                        await bot_client(LeaveChannelRequest(chat))
                        return 'chat_bad', None
                    else:
                        return 'chat_ok', chat
                else:
                    return 'chat_is_group', None
        except (InviteHashExpiredError, InviteHashEmptyError):
            return 'chat_expired', None

    elif link.startswith('https://t.me/'):
        try:
            target = await bot_client.get_entity(link)
            if target.restricted or target.scam or target.fake:
                return 'chat_bad', None
            if not target.broadcast:
                return 'chat_is_group', None
            else:
                await bot_client(JoinChannelRequest(target))  # noqa
                return 'chat_ok', target
        except ValueError:
            return 'chat_expired', None

    # TODO: This is totally not working but it would be a very convenient thing
    # If this is forwarded post
    # elif message.fwd_from:
    #     if type(message.fwd_from.from_id) == PeerChannel:
    #         target = await bot_client.get_entity(message.fwd_from.from_id)
    #         if target.restricted or target.scam or target.fake:
    #             return 'chat_bad', None
    #         # I don't think that broadcast parameter can be applied to this
    #         # because if PeerChannel then it's 100% channel not a group chat
    #         if not target.broadcast:
    #             return 'chat_is_group', None
    #         else:
    #             await bot_client(JoinChannelRequest(target))
    #             return 'chat_ok', target
    #     else:
    #         return 'chat_expired', None

    else:
        return 'chat_expired', None


@bot_bot.on(events.NewMessage())
async def handle_bot_input_message(event):
    user = event.chat
    message = event.message

    if message.message == '/start':
        db_set_new_user(user.id)
        await message.reply('Привет, я бот который поможет тебе создать что-то похожее на ленту новостей.\n\n'
                            'Я буду пересылать сюда посты из каналов на которые ты подпишешься здесь, таким образом '
                            'ты можешь проверять только диалог со мной и смотреть свежие посты из всех групп сразу, '
                            'это ли не чудо?\n\n'
                            'Кстати ты можешь не подписываться на канал сам, а подписаться только здесь, но я не '
                            'советую так делать, потому что каналам важен каждый подписчик! (а ещё если я буду '
                            'недоступен - постов тоже не будет)\n\n'
                            'Команды для управления твоими подписками:\n'
                            '/list - список подписок\n'
                            '/subscribe - подписаться на канал\n'
                            '/unsubscribe - отменить подписку на канал\n\n'
                            '**ВАЖНО**\n'
                            'V1.4:\n'
                            '- Теперь я могу пересылать тебе посты из **приватных каналов!**\n'
                            '- Теперь я по-другому пересылаю тебе посты - не просто отправляю ссылку на пост, а '
                            '**перезаливаю пост полностью!** Это накладывает **ограничения**: '
                            'если в посте файлы больше чем можно переслать, то я отправлю что смогу, сообщу о том что '
                            'не смог переслать всё и оставлю ссылку на пост\n'
                            '- Что осталось по-старому, так это то, что я могу часто быть **недоступен** или '
                            '**медленно реагировать** из-за нагрузки, '
                            'интернета в том месте где я нахожусь или просто от тяжёлой жизни\n\n'
                            '**P.S.** Ты и так знаешь кто меня сделал, так что подумай нужна ли тут пересылка из '
                            'ВКонтакте и напиши ему в лс, конечно же в Телеграме', link_preview=False)

    state = db_get_user_state(user.id)
    
    if message.message == '/list':
        message_text = ''
        for idx, channel in enumerate(db_get_user_channels(user.id)):
            if channel[1] is None:
                message_text += '**{0}.** {1} (приватный)\n'.format(idx + 1, channel[2])
            else:
                message_text += '**{0}.** [{2}](https://t.me/{1})\n'.format(idx + 1, channel[1], channel[2])
        message_text = message_text[:-1]
        message_text = 'Понял, вот список каналов на которые ты подписан:\n\n' + message_text
        await message.reply(message_text, link_preview=False)

    elif message.message == '/subscribe':
        await message.reply('Хорошо, отправь мне ссылку на канал, она выглядит примерно так:\n'
                            'https://t.me/{имя_канала} (для публичных каналов)\n'
                            'или\n'
                            'https://t.me/joinchat/{разные_буквы_и_цифры} (для приватных каналов)\n\n'
                            '(из-за обновления API телеграма некоторые ссылки могут не работать)',
                            link_preview=False)
        db_update_user_state(user.id, 'subscribe')

    elif message.message == '/unsubscribe':
        message_text = ''
        for idx, channel in enumerate(db_get_user_channels(user.id)):
            if channel[1] is None:
                message_text += '**{0}.** {1} (приватный)\n'.format(idx + 1, channel[2])
            else:
                message_text += '**{0}.** [{2}](https://t.me/{1})\n'.format(idx + 1, channel[1], channel[2])
        message_text = message_text[:-1]
        message_text = 'Окей, список каналов на которые ты подписан:\n\n' + message_text
        message_text += '\n\nОтправь номер канала от которого хочешь отписаться, если передумал - отправь любой ' \
                        '**номер которого здесь нет** или **текст**'
        await message.reply(message_text, link_preview=False)
        db_update_user_state(user.id, 'unsubscribe')

    elif state == 'subscribe':
        # Public channels/groups: https://t.me/{channels_username}
        # Private channels/groups: https://t.me/joinchat/{some_code}

        result = await check_link(message)

        if result[0] == 'chat_bad':
            logger.warning('{0} was trying to subscribe to bad channel! DO SOMETHING!'.format(user.id))
            await message.reply('Я не могу подписывать тебя на каналы, которые нарушали правила', link_preview=False)
            db_update_user_state(user.id, 'idle')
        elif result[0] == 'chat_is_group':
            await message.reply('Я не могу подписывать тебя на групповые чаты', link_preview=False)
            db_update_user_state(user.id, 'idle')
        elif result[0] == 'chat_expired':
            await message.reply('Ссылка неправильная или её срок действия истёк', link_preview=False)
            db_update_user_state(user.id, 'idle')
        elif result[0] == 'chat_ok':
            db_add_new_channel_or_user(result[1].id, result[1].username, result[1].title, user.id)
            db_update_user_state(user.id, 'idle')
            await message.reply('Готово!\nСкоро тут начнут появляться посты из этого канала:\n{0}'
                                .format(result[1].title), link_preview=False)

    elif state == 'unsubscribe':
        try:
            idx = int(message.message) - 1
            if idx < 0:
                db_update_user_state(user.id, 'idle')
                await message.reply('Отмена. Все подписки остались на месте', link_preview=False)
            else:
                channel = db_get_user_channels(user.id)[idx]
                if channel[1] is None:
                    reply = 'Успешная отписка от {0} (приватный)\n'.format(channel[2])
                else:
                    reply = 'Успешная отписка от [{1}](https://t.me/{0})\n'.format(channel[1], channel[2])
                db_delete_user_from_channel(channel[0], user.id)
                db_update_user_state(user.id, 'idle')
                await message.reply(reply)
        except (ValueError, IndexError):
            db_update_user_state(user.id, 'idle')
            await message.reply('Отмена. Все подписки остались на месте', link_preview=False)

with bot_client:
    bot_client.loop.run_forever()
