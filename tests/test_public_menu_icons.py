import unittest
from unittest.mock import AsyncMock, patch

from telebot.asyncio_helper import ApiTelegramException

from public_ui import PublicUI
from ui_banners import banner_for


class PublicMenuIconTests(unittest.IsolatedAsyncioTestCase):
    def ui(self):
        bot = type('Bot', (), {})()
        bot.send_message = AsyncMock()
        bot.send_photo = AsyncMock()
        return PublicUI(bot, None, {}, {}, None, None, None, None, None)

    async def test_menu_keeps_actions_and_uses_native_icons_without_html(self):
        ui = self.ui()
        with patch.dict('os.environ', {'WEBAPP_URL': 'https://example.test/stats'}):
            rows = ui.keyboard().to_dict()['inline_keyboard']
        buttons = [b for row in rows for b in row]
        self.assertTrue(all(b.get('icon_custom_emoji_id', '').isdigit() for b in buttons))
        self.assertEqual(buttons[0]['web_app']['url'], 'https://example.test/stats')
        self.assertEqual(buttons[0]['icon_custom_emoji_id'], '5931472654660800739')
        self.assertEqual({b['callback_data'] for b in buttons if 'callback_data' in b},
                         {'ny:connect', 'ny:status', 'ny:notifications', 'ny:help', 'ny:data'})
        self.assertTrue(all('<' not in b['text'] for b in buttons))
        self.assertEqual(len(ui.keyboard(False).keyboard), 2)

    async def test_premium_rejection_retries_plain_keyboard_and_disables_icons(self):
        ui = self.ui()
        error = ApiTelegramException('sendMessage', None, {'error_code': 400, 'description': 'Bad Request: PREMIUM_ACCOUNT_REQUIRED'})
        ui.bot.send_message.side_effect = [error, 'sent', 'next']
        keyboard = ui.keyboard()
        original = keyboard.to_dict()
        self.assertEqual(await ui.send(100, 'Menu', reply_markup=keyboard), 'sent')
        retry = ui.bot.send_message.await_args.kwargs['reply_markup'].to_dict()
        self.assertTrue(all('icon_custom_emoji_id' not in b for row in retry['inline_keyboard'] for b in row))
        self.assertEqual(keyboard.to_dict(), original)
        self.assertEqual([b['callback_data'] for row in retry['inline_keyboard'] for b in row],
                         [b['callback_data'] for row in original['inline_keyboard'] for b in row])
        self.assertFalse(ui.button_icons_enabled)
        await ui.send(100, 'Next', reply_markup=keyboard)
        self.assertTrue(all('icon_custom_emoji_id' not in b for row in ui.bot.send_message.await_args.kwargs['reply_markup'].to_dict()['inline_keyboard'] for b in row))
        self.assertTrue(all('icon_custom_emoji_id' not in b for row in ui.keyboard().to_dict()['inline_keyboard'] for b in row))

    async def test_unrelated_errors_and_rate_limits_are_not_retried(self):
        for code, description in [(400, 'Bad Request: BUTTON_DATA_INVALID'), (429, 'Too Many Requests: retry after 10'), (403, 'Forbidden: bot was blocked')]:
            ui = self.ui()
            ui.bot.send_message.side_effect = ApiTelegramException('sendMessage', None, {'error_code': code, 'description': description})
            with self.assertRaises(ApiTelegramException):
                await ui.send(100, 'Menu', reply_markup=ui.keyboard())
            self.assertEqual(ui.bot.send_message.await_count, 1)
            self.assertTrue(ui.button_icons_enabled)

    async def test_dynamic_buttons_keep_confirmation_and_pagination_callbacks(self):
        ui = self.ui()
        for action in ('toggle:paused', 'toggle:edits', 'toggle:deletions', 'toggle:peers', 'toggle:own', 'confirm:nonce', 'unrule:100:200', 'rules:2'):
            value = ui.button('Action', callback_data='ny:' + action).to_dict()
            self.assertTrue(value['icon_custom_emoji_id'].isdigit())
            self.assertEqual(value['callback_data'], 'ny:' + action)

    async def test_section_photo_has_complete_caption_and_keyboard(self):
        ui = self.ui()
        photo_bytes = []
        async def receive(actor, image, **kwargs):
            photo_bytes.append(image.read())
            self.assertEqual(actor, 100)
            self.assertEqual(kwargs['caption'], 'Menu text')
            self.assertIn('reply_markup', kwargs)
        ui.bot.send_photo.side_effect = receive
        await ui.send(100, 'Menu text', banner='menu', reply_markup=ui.keyboard())
        self.assertEqual(photo_bytes, [banner_for('menu').read_bytes()])
        ui.bot.send_message.assert_not_awaited()

    async def test_long_privacy_text_is_not_truncated_into_photo_caption(self):
        ui = self.ui()
        text = 'Privacy details. ' * 200
        keyboard = ui.keyboard()
        await ui.send(100, text, banner='data', reply_markup=keyboard)
        self.assertNotIn('caption', ui.bot.send_photo.await_args.kwargs)
        self.assertEqual(ui.bot.send_message.await_args.args, (100, text))
        self.assertIs(ui.bot.send_message.await_args.kwargs['reply_markup'], keyboard)

    async def test_photo_retry_rewinds_stream_after_rejected_icons(self):
        ui = self.ui()
        attempts = []
        async def receive(actor, image, **kwargs):
            attempts.append(image.read())
            if len(attempts) == 1:
                raise ApiTelegramException('sendPhoto', None, {'error_code': 400, 'description': 'Bad Request: PREMIUM_ACCOUNT_REQUIRED'})
            self.assertTrue(all(not b.get('icon_custom_emoji_id') for row in kwargs['reply_markup'].to_dict()['inline_keyboard'] for b in row))
        ui.bot.send_photo.side_effect = receive
        await ui.send(100, 'Menu', banner='menu', reply_markup=ui.keyboard())
        self.assertEqual(attempts, [banner_for('menu').read_bytes()] * 2)

    async def test_all_section_assets_exist_and_unknown_paths_are_rejected(self):
        for section in ('menu', 'invite', 'connect', 'status', 'notifications', 'data', 'help', 'erase', 'rules', 'private_profiles', 'profiles', 'watch', 'events', 'settings'):
            signature = b'\xff\xd8\xff' if section == 'connect' else b'\x89PNG\r\n\x1a\n'
            self.assertTrue(banner_for(section).read_bytes().startswith(signature))
        self.assertIsNone(banner_for('../.env'))


if __name__ == '__main__':
    unittest.main()
