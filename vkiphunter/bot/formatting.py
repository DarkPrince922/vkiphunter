"""Тексты сообщений бота (HTML parse mode)."""

from __future__ import annotations

import time
from html import escape

from ..cloud.base import FloatingIpInfo, PortInfo
from ..hunter import Hunter, HunterStatus
from ..state import (
    MODE_FLOATING_IP,
    MODE_TITLES,
    OUTCOME_ERROR,
    OUTCOME_QUOTA,
    OUTCOME_MATCH,
    OUTCOME_POOL_EMPTY,
    OUTCOME_REJECT,
    Attempt,
    AppState,
    FoundPort,
    HuntSettings,
)

OUTCOME_ICON = {
    OUTCOME_QUOTA: "🚧",
    OUTCOME_MATCH: "🎯",
    OUTCOME_REJECT: "❌",
    OUTCOME_POOL_EMPTY: "🕳",
    OUTCOME_ERROR: "⚠️",
}

OUTCOME_TITLE = {
    OUTCOME_QUOTA: "квота исчерпана",
    OUTCOME_MATCH: "попадание",
    OUTCOME_REJECT: "не тот пул",
    OUTCOME_POOL_EMPTY: "пул пуст (409)",
    OUTCOME_ERROR: "ошибка API",
}


def human_delta(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    seconds = int(max(0, seconds))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    parts = []
    if days:
        parts.append(f"{days} д")
    if hours:
        parts.append(f"{hours} ч")
    if minutes and not days:
        parts.append(f"{minutes} мин")
    if not parts:
        parts.append(f"{seconds} с")
    return " ".join(parts)


def clock(ts: float | None) -> str:
    if not ts:
        return "—"
    return time.strftime("%H:%M:%S", time.localtime(ts))


def status_text(state: AppState, hunter: Hunter, credentials=None) -> str:
    stats = state.stats
    settings = state.settings

    if credentials is not None and not credentials.configured:
        return (
            "☁️ <b>Облако не подключено</b>\n\n"
            "Бот спросит доступы сам — нажмите кнопку ниже или отправьте "
            "/connect. Быстрее всего прислать содержимое openrc-файла."
        )

    if hunter.running:
        head = "🟢 <b>Охота идёт</b>"
    elif hunter.status is HunterStatus.STOPPING:
        head = "🟡 <b>Останавливаюсь…</b>"
    else:
        head = "⚪️ <b>Охота остановлена</b>"

    lines = [head]
    if credentials is not None and credentials.simulate:
        lines.append("<i>режим симуляции — реальные порты не создаются</i>")
    workers = f" · воркеров: <b>{settings.workers}</b>" if settings.workers > 1 else ""
    lines.append(f"Способ: <b>{MODE_TITLES[settings.mode]}</b>{workers}")
    if hunter.account:
        lines.append(f"Аккаунт: <code>{escape(hunter.account)}</code>")
    lines.append("")

    run = stats.since(getattr(hunter, "run_baseline", None))
    scope = "За запуск" if run["attempts"] != stats.attempts else "Попыток"
    lines.append(
        f"{scope}: <b>{run['attempts']}</b> · "
        f"мимо: {run['rejected']} · 409: {run['pool_empty']} · "
        f"ошибок: {run['errors']}"
    )
    if run["attempts"] != stats.attempts:
        lines.append(
            f"<i>Всего за всё время: {stats.attempts} · мимо {stats.rejected} · "
            f"409 {stats.pool_empty}</i>"
        )
    if stats.quota:
        lines.append(
            f"🚧 Квота исчерпана: <b>{stats.quota}</b> раз — "
            "освободите выданные адреса или уменьшите число воркеров."
        )
    if stats.started_at:
        lines.append(
            f"В работе: <b>{human_delta(time.time() - stats.started_at)}</b>"
            f" (старт {clock(stats.started_at)})"
        )
    lines.append(f"Уникальных адресов: <b>{len(stats.seen_ips)}</b>")

    if hunter.running and hunter.next_attempt_at:
        left = int(hunter.next_attempt_at - time.time())
        lines.append(f"Следующая попытка через: <b>{max(0, left)} с</b>")

    last = state.attempts[-1] if state.attempts else None
    if last:
        lines.append("")
        lines.append("Последняя попытка: " + attempt_line(last))

    found = state.last_found()
    if found:
        lines.append("")
        lines.append(found_block(found))
        if len(state.found) > 1:
            lines.append(
                f"<i>Всего удержано целевых адресов: {len(state.found)} — "
                "воркеры нашли несколько почти одновременно. Все сохранены "
                "и защищены от уборки.</i>"
            )

    if hunter.last_error:
        lines.append("")
        lines.append(f"⚠️ {escape(hunter.last_error)}")

    lines.append("")
    lines.append(
        f"<i>сеть {escape(settings.network)} · {escape(settings.subnet_summary())} · "
        f"интервал {settings.interval} с · целей {len(settings.targets)}</i>"
    )
    return "\n".join(lines)


def attempt_line(attempt: Attempt) -> str:
    icon = OUTCOME_ICON.get(attempt.outcome, "•")
    title = OUTCOME_TITLE.get(attempt.outcome, attempt.outcome)
    if attempt.ip:
        text = f"{icon} {clock(attempt.ts)} <code>{escape(attempt.ip)}</code> — {title}"
    else:
        text = f"{icon} {clock(attempt.ts)} — {title}"
    show_detail = attempt.outcome in (
        OUTCOME_ERROR,
        OUTCOME_POOL_EMPTY,
        OUTCOME_QUOTA,
    ) or (
        "НЕ удалён" in attempt.detail
    )
    if attempt.worker is not None:
        text = text.replace(f"{icon} ", f"{icon} [в{attempt.worker}] ", 1)
    if attempt.subnet:
        text += f" · <code>{escape(attempt.subnet)}</code>"
    if attempt.detail and show_detail:
        text += f"\n    <i>{escape(attempt.detail[:200])}</i>"
    return text


def found_block(found: FoundPort) -> str:
    lines = [
        "🎯 <b>Целевой IP найден</b>",
        f"IP: <code>{escape(found.ip)}</code>",
        f"Диапазон: <code>{escape(found.matched_cidr)}</code>",
        f"{found.title_cap}: <code>{escape(found.port_id)}</code>",
        f"Метка: <code>{escape(found.name)}</code>",
        f"Найден: {clock(found.found_at)}",
    ]
    if found.server_id:
        lines.append(
            f"ВМ: <code>{escape(found.server_name or '')}</code> "
            f"(<code>{escape(found.server_id)}</code>)"
        )
    else:
        lines.append(
            f"<i>{found.title_cap} сохранён. Удалять его нельзя — "
            "вместе с ним уйдёт IP.</i>"
        )
    return "\n".join(lines)


def history_text(state: AppState, limit: int = 20) -> str:
    attempts = state.attempts[-limit:]
    if not attempts:
        return "История пуста — охота ещё не запускалась."
    lines = [f"<b>Последние {len(attempts)} попыток</b>", ""]
    lines += [attempt_line(a) for a in reversed(attempts)]
    return "\n".join(lines)


def top_ips_text(state: AppState, limit: int = 15) -> str:
    seen = state.stats.seen_ips
    if not seen:
        return "Адресов пока не выдавалось."
    ranked = sorted(seen.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    lines = [f"<b>Выданные адреса</b> (всего уникальных: {len(seen)})", ""]
    for ip, count in ranked:
        suffix = f" ×{count}" if count > 1 else ""
        lines.append(f"• <code>{escape(ip)}</code>{suffix}")
    return "\n".join(lines)


def settings_text(settings: HuntSettings) -> str:
    volume = f"{settings.volume_size} ГБ"
    if settings.volume_type:
        volume += f" ({escape(settings.volume_type)})"
    return "\n".join(
        [
            "⚙️ <b>Настройки</b>",
            "",
            f"Способ добычи адреса: <b>{MODE_TITLES[settings.mode]}</b>",
            f"Воркеров: <b>{settings.workers}</b>",
            f"Внешняя сеть: <code>{escape(settings.network)}</code>",
            f"Подсети: <code>{escape(settings.subnet_summary())}</code>",
            f"Префикс портов: <code>{escape(settings.prefix)}</code>",
            f"Интервал между попытками: <b>{settings.interval} с</b>",
            f"Тайм-аут создания порта: <b>{settings.port_timeout} с</b>",
            f"Тайм-аут создания ВМ: <b>{settings.server_timeout} с</b>",
            f"Лимит попыток: <b>{settings.max_attempts or 'без лимита'}</b>",
            f"Сводка каждые: <b>{settings.report_every} попыток</b>",
            f"Диск новой ВМ: <b>{volume}</b>",
            "",
            f"Целевых диапазонов: <b>{len(settings.targets)}</b> · "
            f"исключений: <b>{len(settings.excludes)}</b>",
        ]
    )


def targets_text(settings: HuntSettings) -> str:
    lines = ["🎯 <b>Целевые диапазоны</b>", ""]
    lines += [f"• <code>{escape(t)}</code>" for t in settings.targets] or ["<i>пусто</i>"]
    lines.append("")
    lines.append("🚫 <b>Исключения</b>")
    lines.append("")
    if settings.excludes:
        lines += [f"• <code>{escape(e)}</code>" for e in settings.excludes]
    else:
        lines.append("<i>нет</i>")
    lines.append("")
    lines.append(
        "<i>Проверка идёт по математике CIDR, а не по префиксу строки. "
        "Исключения приоритетнее целей.</i>"
    )
    return "\n".join(lines)


def floating_ips_text(fips: list[FloatingIpInfo], prefix: str) -> str:
    if not fips:
        return "Floating IP в проекте не найдено."
    own = [f for f in fips if f.description.startswith(prefix)]
    other = [f for f in fips if not f.description.startswith(prefix)]

    lines = ["🎈 <b>Floating IP проекта</b>", ""]
    lines.append(f"<b>Свои ({len(own)})</b>")
    if own:
        for fip in own[:30]:
            attached = " · привязан" if fip.attached else ""
            lines.append(
                f"• <code>{escape(fip.ip or '—')}</code> · "
                f"<code>{escape(fip.id[:8])}</code>{attached}"
            )
    else:
        lines.append("<i>нет</i>")

    lines.append("")
    lines.append(f"<b>Прочие ({len(other)})</b> — не трогаю")
    for fip in other[:15]:
        attached = " · привязан" if fip.attached else ""
        lines.append(f"• <code>{escape(fip.ip or '—')}</code>{attached}")
    if len(other) > 15:
        lines.append(f"<i>…и ещё {len(other) - 15}</i>")
    return "\n".join(lines)


def ports_text(ports: list[PortInfo], prefix: str, network: str) -> str:
    if not ports:
        return f"В сети <code>{escape(network)}</code> портов не найдено."
    own = [p for p in ports if p.name.startswith(prefix)]
    other = [p for p in ports if not p.name.startswith(prefix)]

    lines = [f"🔌 <b>Порты сети {escape(network)}</b>", ""]
    lines.append(f"<b>Свои probe-порты ({len(own)})</b>")
    if own:
        for port in own[:30]:
            attached = " · подключён к ВМ" if port.device_id else ""
            lines.append(
                f"• <code>{escape(port.ip or '—')}</code> · "
                f"<code>{escape(port.id[:8])}</code>{attached}"
            )
    else:
        lines.append("<i>нет</i>")

    lines.append("")
    lines.append(f"<b>Прочие порты ({len(other)})</b> — не трогаю")
    for port in other[:15]:
        lines.append(
            f"• <code>{escape(port.ip or '—')}</code> · {escape(port.name or 'без имени')}"
        )
    if len(other) > 15:
        lines.append(f"<i>…и ещё {len(other) - 15}</i>")
    return "\n".join(lines)


def cloud_info_text(info, network: str) -> str:
    return "\n".join(
        [
            "☁️ <b>Облако доступно</b>",
            "",
            f"Проект: <code>{escape(info.project_name or info.project_id)}</code>",
            f"Пользователь: <code>{escape(info.username)}</code>",
            f"Регион: <code>{escape(info.region)}</code>",
            f"Сеть <code>{escape(network)}</code>: <code>{escape(info.network_id)}</code>",
            f"Подсетей: {len(info.subnets)}",
        ]
    )


HELP = """<b>VK IP Hunter</b>

Перебирает публичные IPv4, создавая по одному внешнему порту в сети
<code>internet</code>, и останавливается на адресе из целевого диапазона.
Неподходящий порт удаляется, найденный — сохраняется.

<b>Команды</b>
/menu — главное меню
/hunt — запустить охоту
/stop — остановить
/status — текущее состояние
/history — последние попытки
/ips — какие адреса уже выдавались
/ports — порты или floating IP проекта
/cleanup — освободить свои пробные адреса
/vm — создать ВМ на найденном порту
/attach — привязать найденный floating IP к ВМ
/settings — параметры цикла
/targets — целевые диапазоны и исключения
/networks — какие внешние сети есть и что в них за пулы
/subnets — из каких подсетей просить адрес
/availability — сколько свободных адресов в каждом пуле
/connect — подключить VK Cloud (спрошу доступы здесь же)
/disconnect — забыть доступы к облаку
/cloud — проверить доступ к облаку
/log — последние строки лога
/debug — подробный лог запросов к облаку
/whoami — мой Telegram ID

<b>Важно</b>
Попадание в CIDR — только предварительный фильтр. Реальную пригодность
адреса подтверждает лишь запрос через нужную SIM/APN с выключенным Wi-Fi.
"""
