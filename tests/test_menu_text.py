import io
import unittest
from html.parser import HTMLParser
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telebot import types
from telebot.asyncio_helper import ApiTelegramException
from public_ui import PublicUI
from ui_text import menu_html, telegram_length, without_custom_emoji


class ValidTelegramHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []

    def handle_starttag(self, tag, attrs):
        assert tag in ('b', 'code', 'tg-emoji'), tag
        assert not (tag == 'b' and tag in self.stack), 'nested bold'
        if tag == 'tg-emoji':
            assert dict(attrs)['emoji-id'].isdigit()
        self.stack.append(tag)

    def handle_endtag(self, tag):
        assert self.stack.pop() == tag


class MenuTextTests(unittest.IsolatedAsyncioTestCase):
    def ui(self):
        return PublicUI(SimpleNamespace(send_photo=AsyncMock(), send_message=AsyncMock()),
                        None, {}, {}, None, None, None, None, None)

    def test_copy_has_hierarchy_and_escapes_dynamic_content(self):
        text = menu_html('Уведомления о сообщениях\n\nОтправка включена.\nСобытия: правки\n\n'
                         'Выберите «Мои данные».\n<script>& "user"</script>')
        self.assertIn('<b>Уведомления о сообщениях</b>', text)
        self.assertIn('<b>Отправка включена.</b>', text)
        self.assertIn('<b>События:</b> правки', text)
        self.assertIn('<b>«Мои данные»</b>', text)
        self.assertIn('&lt;script&gt;&amp;', text)
        validator = ValidTelegramHTML();validator.feed(text);validator.close()
        self.assertEqual(validator.stack, [])
        plain = without_custom_emoji(text)
        self.assertNotIn('tg-emoji', plain)
        self.assertIn('🔔 <b>', plain)

    def test_visible_budget_counts_entities_and_surrogate_pairs(self):
        self.assertEqual(telegram_length('<b>&lt;&amp;&#128276;</b>', 'HTML'), 4)
        self.assertEqual(telegram_length('🔔'), 2)
        self.assertEqual(telegram_length('<b>', None), 3)
        text = menu_html('Подключить аккаунт\n\n1. Откройте настройки.\n2. Выберите Nyan.\n3. Вернитесь сюда.')
        self.assertIn('<b>1.</b> Откройте', text)

    async def test_html_source_length_does_not_split_short_caption(self):
        ui = self.ui()
        text = '<b>' + '&amp;' * 500 + '</b>'
        await ui.send(100, text, banner='menu', parse_mode='HTML')
        ui.bot.send_photo.assert_awaited_once()
        self.assertEqual(ui.bot.send_photo.await_args.kwargs['caption'], text)
        ui.bot.send_message.assert_not_awaited()

    async def test_text_only_emoji_rejection_keeps_bold_and_unicode_on_later_calls(self):
        ui = self.ui()
        error = ApiTelegramException('synthetic', None,
            {'error_code':400, 'description':'Bad Request: CUSTOM_EMOJI_NOT_ALLOWED'})
        ui.bot.send_message.side_effect = [error, 'sent', 'later']
        text = menu_html('Помощь\n\nОткройте «Мои данные».')
        await ui.send(100, text, parse_mode='HTML')
        retry = ui.bot.send_message.await_args.args[1]
        self.assertIn('❓ <b>Помощь</b>', retry)
        self.assertNotIn('tg-emoji', retry)
        await ui.send(100, text, parse_mode='HTML')
        self.assertEqual(ui.bot.send_message.await_args.args[1], retry)
        self.assertFalse(ui.text_icons_enabled)

    async def test_media_retry_preserves_source_caption_and_rewinds_upload(self):
        ui = self.ui(); stream = io.BytesIO(b'photo')
        text = menu_html('Мои данные\n\nИстория на месте.')
        media = types.InputMediaPhoto(stream, caption=text, parse_mode='HTML')
        attempts = []
        async def edit(value, **kwargs):
            attempts.append((value.media.read(), value.caption))
            if len(attempts) == 1:
                raise ApiTelegramException('synthetic', None,
                    {'error_code':400, 'description':'Bad Request: PREMIUM_ACCOUNT_REQUIRED'})
        await ui._send_with_icons(edit, media, chat_id=100, message_id=1)
        self.assertEqual([data for data, caption in attempts], [b'photo', b'photo'])
        self.assertEqual(media.caption, text)
        self.assertNotIn('tg-emoji', attempts[1][1])
        self.assertIn('<b>Мои данные</b>', attempts[1][1])

    async def test_text_emoji_does_not_retry_rate_limit_or_parse_error(self):
        for code, description in ((429, 'Too Many Requests'), (400, "can't parse entities")):
            ui = self.ui()
            ui.bot.send_message.side_effect = ApiTelegramException('synthetic', None,
                {'error_code':code, 'description':description})
            with self.assertRaises(ApiTelegramException):
                await ui.send(100, menu_html('Помощь\n\nТекст'), parse_mode='HTML')
            self.assertEqual(ui.bot.send_message.await_count, 1)
