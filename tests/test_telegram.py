import asyncio
import logging

import pytest

from fakes import FakeHttp, tick
from voice_agent.db import Database
from voice_agent.telegram import TelegramAlerts, TelegramNotifier, notification_text, send_message

CALL = {
    'id': '20261013T164551_cea46fc5',
    'session_id': 'cea46fc5',
    'caller': '0901234567',
    'started_at': '2026-10-13T16:45:51.829460+07:00',
    'ended_at': '2026-10-13T16:46:12.970192+07:00',
    'duration_seconds': 82,
    'caller_name': 'Anh Nam',
    'summary': 'Nhờ gọi lại về hợp đồng.',
    'urgency': 'trung bình',
    'category': 'bình thường',
    'transcript': [],
    'usage': {},
    'cost': {'total_usd': 0.011468, 'total_vnd': 303},
}


@pytest.fixture(autouse=True)
def no_retry_wait(monkeypatch):
    monkeypatch.setattr('voice_agent.telegram.RETRY_DELAYS', (0, 0))


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / 'voicemail.db')
    yield database
    database.close()


def make_notifier(db, http, **kwargs):
    return TelegramNotifier(http=http, token='TOKEN', chat_id='42', db=db, **kwargs)


def saved(db, **fields):
    call = {**CALL, **fields}
    db.save_call(call)
    return db.get_call(call['id'])


async def test_error_log_lines_reach_telegram_at_most_once_per_interval(db):
    http = FakeHttp()
    alerts = TelegramAlerts(make_notifier(db, http), interval=3600)
    logger = logging.getLogger('voice_agent.test_alerts')
    logger.addHandler(alerts)
    try:
        try:
            raise ConnectionError('quota exceeded')
        except ConnectionError:
            logger.exception('[abc] failed to start session')
        logger.error('second failure')  # inside the interval: held back, counted
        logger.warning('not an error')
        logging.getLogger('voice_agent.telegram').error('telegram is down')  # can't go through Telegram
        await tick()

        assert [payload['text'] for _, payload in http.posts] == [
            '⚠️ Voice Agent: [abc] failed to start session (ConnectionError: quota exceeded)',
        ]

        alerts._last -= 3600  # the interval has passed
        logger.error('third failure')
        await tick()
        assert http.posts[-1][1]['text'] == '⚠️ Voice Agent: third failure (+1 lỗi trước đó)'
    finally:
        logger.removeHandler(alerts)


def test_notification_text_gives_who_what_how_urgent_how_long_and_what_it_cost():
    assert notification_text(CALL) == '\n'.join([
        'Cuộc gọi nhỡ từ 0901234567 lúc 16:45 13/10/2026',
        'Người gọi: Anh Nam',
        'Nội dung: Nhờ gọi lại về hợp đồng.',
        'Độ khẩn: trung bình',
        'Thời lượng: 1:22 · Chi phí: $0.0115 (~303đ)',
    ])


def test_notification_text_copes_with_an_unknown_caller_and_no_exchange_rate():
    text = notification_text({**CALL, 'caller': None, 'cost': {'total_usd': 0.0035}})

    assert text.startswith('Cuộc gọi nhỡ từ số không rõ lúc')
    assert text.endswith('Chi phí: $0.0035')


async def test_send_message_posts_to_the_bot_api_with_an_optional_link_button():
    http = FakeHttp()

    assert await send_message(http, 'TOKEN', '42', 'xin chào') is True
    assert await send_message(http, 'TOKEN', '42', 'có nút', button=('Nghe ghi âm', 'https://vm.example/c/abc?t=1.x'))

    assert http.posts == [
        ('https://api.telegram.org/botTOKEN/sendMessage', {'chat_id': '42', 'text': 'xin chào'}),
        ('https://api.telegram.org/botTOKEN/sendMessage', {
            'chat_id': '42', 'text': 'có nút',
            'reply_markup': {'inline_keyboard': [[{'text': 'Nghe ghi âm', 'url': 'https://vm.example/c/abc?t=1.x'}]]},
        }),
    ]


async def test_send_message_truncates_to_telegrams_limit():
    http = FakeHttp()

    await send_message(http, 'TOKEN', '42', 'x' * 5000)

    assert len(http.posts[0][1]['text']) == 4096


async def test_a_refused_message_is_reported_as_failed_without_retrying(caplog):
    http = FakeHttp(status=403)
    assert await send_message(http, 'TOKEN', '42', 'xin chào') is False
    assert len(http.posts) == 1, 'a refusal will not change on a retry'
    assert 'sendMessage failed: 403' in caplog.text
    assert 'TOKEN' not in caplog.text


@pytest.mark.parametrize('http', [FakeHttp(status=502), FakeHttp(error=OSError('down'))])
async def test_a_telegram_outage_or_network_drop_is_retried_before_giving_up(http):
    assert await send_message(http, 'TOKEN', '42', 'xin chào') is False
    assert len(http.posts) == 3


async def test_a_network_error_is_logged_without_leaking_the_bot_token(caplog):
    http = FakeHttp(error=OSError('cannot reach https://api.telegram.org/botTOKEN/sendMessage'))

    assert await send_message(http, 'TOKEN', '42', 'xin chào') is False
    assert 'telegram' in caplog.text.lower()
    assert 'TOKEN' not in caplog.text


async def test_a_normal_call_is_announced_once_with_a_link_to_listen(db):
    http = FakeHttp()
    notifier = make_notifier(db, http, link_for=lambda call_id: f'https://vm.example/c/{call_id}?t=tok')

    await notifier.notify_call(saved(db))

    [(_, payload)] = http.posts
    assert payload['text'] == notification_text(CALL)
    assert payload['reply_markup']['inline_keyboard'][0][0] == {
        'text': 'Nghe ghi âm', 'url': 'https://vm.example/c/20261013T164551_cea46fc5?t=tok',
    }
    notifier.close()


async def test_without_a_public_address_the_message_has_no_button(db):
    http = FakeHttp()
    notifier = make_notifier(db, http)

    await notifier.notify_call(saved(db))

    assert 'reply_markup' not in http.posts[0][1]
    notifier.close()


async def test_spam_is_not_announced(db):
    http = FakeHttp()
    notifier = make_notifier(db, http)

    await notifier.notify_call(saved(db, category='spam', urgency='cao'))

    assert http.posts == []
    notifier.close()


async def test_an_urgent_call_is_flagged_and_repeated_until_it_is_read(db):
    http = FakeHttp()
    notifier = make_notifier(db, http, reminder_minutes=0.001, max_reminders=3)  # 60ms apart

    await notifier.notify_call(saved(db, urgency='cao'))
    assert http.posts[0][1]['text'].startswith('🔴 KHẨN CẤP\nCuộc gọi nhỡ từ 0901234567')

    await asyncio.sleep(0.1)  # one reminder goes out…
    db.mark_read(CALL['id'])  # …then the owner opens the call
    await asyncio.sleep(0.25)

    texts = [payload['text'] for _, payload in http.posts]
    assert len(texts) == 2
    assert texts[1].startswith('🔴 Nhắc lại lần 1/3: cuộc gọi khẩn chưa xem.\nCuộc gọi nhỡ từ 0901234567')
    notifier.close()


async def test_urgent_reminders_stop_after_the_maximum(db):
    http = FakeHttp()
    notifier = make_notifier(db, http, reminder_minutes=0.0005, max_reminders=2)  # 30ms apart

    await notifier.notify_call(saved(db, urgency='cao'))
    await asyncio.sleep(0.4)

    assert len(http.posts) == 1 + 2
    notifier.close()


async def test_closing_the_notifier_cancels_pending_reminders(db):
    http = FakeHttp()
    notifier = make_notifier(db, http, reminder_minutes=0.001)

    await notifier.notify_call(saved(db, urgency='cao'))
    notifier.close()
    await asyncio.sleep(0.2)

    assert len(http.posts) == 1
