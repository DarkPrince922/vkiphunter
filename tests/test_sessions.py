"""Persistent authentication survives restarts without storing bearer tokens."""
import json
import time

from vkiphunter.web.sessions import MAX_SESSIONS, SessionStore

PASSWORD = 'test-long-session-password'
TTL = 7 * 86400


def store(tmp_path, password=PASSWORD):
    return SessionStore(tmp_path / 'sessions.json', password, TTL)


async def test_sessions_survive_restart_without_plaintext_tokens(tmp_path):
    first = store(tmp_path)
    token, session = await first.create()
    contents = first.path.read_text()
    assert token not in contents and PASSWORD not in contents
    assert first.path.stat().st_mode & 0o777 == 0o600
    second = store(tmp_path)
    assert second.load() is False
    restored = second.get(token)
    assert restored and restored.csrf == session.csrf and restored.expires == session.expires
    assert second.get('not-a-valid-token') is None


async def test_activity_renews_expiry_and_persists_without_writing_every_request(tmp_path):
    first = store(tmp_path)
    token, session = await first.create()
    contents = first.path.read_text()
    assert await first.renew(token) is False
    assert first.path.read_text() == contents
    session.expires -= 7200
    old_expiry = session.expires
    assert await first.renew(token) is True
    assert session.expires > old_expiry + 7000
    second = store(tmp_path)
    second.load()
    assert second.get(token).expires == session.expires


async def test_logout_and_password_change_revoke_sessions_permanently(tmp_path):
    first = store(tmp_path)
    token, _ = await first.create()
    await first.revoke(token)
    second = store(tmp_path)
    second.load()
    assert second.get(token) is None
    token, _ = await first.create()
    changed = store(tmp_path, 'a-different-long-password')
    assert changed.load() is True
    assert changed.get(token) is None
    await changed.save()
    reverted = store(tmp_path)
    assert reverted.load() is True
    assert reverted.get(token) is None


async def test_expired_sessions_cannot_be_loaded_or_renewed(tmp_path):
    first = store(tmp_path)
    token, session = await first.create()
    session.expires = time.time() - 1
    assert first.get(token) is None
    assert await first.renew(token) is False
    await first.save()
    assert json.loads(first.path.read_text())['sessions'] == []
    second = store(tmp_path)
    second.load()
    assert second.get(token) is None


async def test_sign_in_rotates_previous_token_but_keeps_other_browsers(tmp_path):
    first = store(tmp_path)
    other, _ = await first.create()
    old, _ = await first.create()
    fresh, _ = await first.create(old)
    assert first.get(old) is None
    assert first.get(fresh) and first.get(other)
    second = store(tmp_path)
    second.load()
    assert second.get(old) is None and second.get(fresh) and second.get(other)


async def test_session_storage_is_bounded(tmp_path):
    first = store(tmp_path)
    oldest, _ = await first.create()
    newest = oldest
    for _ in range(MAX_SESSIONS):
        newest, _ = await first.create()
    assert len(first.records) == MAX_SESSIONS
    assert first.get(oldest) is None and first.get(newest)


async def test_corrupt_storage_fails_closed_and_is_replaced_with_private_file(tmp_path):
    first = store(tmp_path)
    first.path.write_text('{broken')
    assert first.load() is True
    assert not first.records
    await first.save()
    assert first.path.stat().st_mode & 0o777 == 0o600
    assert json.loads(first.path.read_text())['sessions'] == []
