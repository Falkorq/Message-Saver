"""Structured presentation data for the web UI; uses existing statistical counters."""
import json
import time
from collections import Counter
from datetime import datetime

from .analytics import MOSCOW, day_of, histogram_summary, period_bounds, observation_windows, day_is_known
from .service import MEDIA_NAMES


def period_totals(row, metrics, period, now=None):
    """Reuse retained totals only when their entire stored time range is in scope.

    Never distribute undated legacy counters across a narrower period. Graphs
    and response timings still use detailed records, even for a complete scope.
    """
    start, end = period_bounds(period, now)
    first, last = row['first_at'], row['last_at']
    complete = period == 'all' or (first is not None and last is not None
                                   and first > 0 and last >= first and start <= first and last < end)
    own = row['owner_messages'] if complete else metrics['o_n']
    peer = row['peer_messages'] if complete else metrics['p_n']
    return {'own': own, 'peer': peer, 'total': own + peer,
            'basis': 'retained' if complete else 'detailed', 'complete_scope': complete}


def web_chats(repository, owner, account, period, now=None, include_empty=False):
    rows = repository.filtered_chats(owner, account, 'all', now)
    per_chat = {}
    if period != 'all':
        for daily in repository.daily_rows(owner, account, period=period, now=now):
            per_chat.setdefault((daily['account_id'], daily['chat_id']), Counter()).update(json.loads(daily['metrics']))
    result = []
    for row in rows:
        all_metrics = row['metrics']
        row['undetailed'] = max(0, row['owner_messages'] - all_metrics['o_n']) + max(0, row['peer_messages'] - all_metrics['p_n'])
        metrics = all_metrics if period == 'all' else per_chat.get((row['account_id'], row['chat_id']), Counter())
        totals = period_totals(row, metrics, period, now)
        row.update(metrics=metrics, total_messages=totals['total'], owner_messages=totals['own'],
                   peer_messages=totals['peer'], totals_basis=totals['basis'])
        if include_empty or period == 'all' or totals['total']:
            result.append(row)
    return result


def series(repository, owner, account, period, now, chat=None, metrics_override=None):
    start_bound, end_bound = period_bounds(period, now)
    accounts = repository.check_account(owner, account)
    chart_day = day_of(max(start_bound or end_bound-90*86400, end_bound-90*86400))
    rows = repository.db.execute("SELECT d.day,SUM(COALESCE(json_extract(d.metrics,'$.o_n'),0)) own,"
        "SUM(COALESCE(json_extract(d.metrics,'$.p_n'),0)) peer FROM business_stats_daily d JOIN business_chat_stats s "
        'ON s.account_id=d.account_id AND s.chat_id=d.chat_id WHERE s.owner_id=? AND d.account_id IN ('+
        ','.join('?' for _ in accounts)+') AND d.day>=? AND d.day<?'+(' AND d.chat_id=?' if chat is not None else '')+' GROUP BY d.day',
        (owner,*accounts,chart_day,day_of(end_bound),*((chat,) if chat is not None else ()))).fetchall() if accounts else []
    days = {row['day']:[row['own'],row['peer']] for row in rows}
    counters = Counter(metrics_override or {})
    if metrics_override is None:
        for row in repository.filtered_chats(owner, account, period, now):
            if chat is None or row['chat_id']==chat:
                counters.update({k:v for k,v in row['metrics'].items() if not k.endswith('_max')})
    start, end = period_bounds(period, now)
    progress = dict(repository.db.execute('SELECT name,value FROM business_stats_progress'))
    accounts = repository.check_account(owner, account)
    windows=observation_windows(repository.db,accounts,progress,now)
    # The overview graph shows at most 90 calendar days; counters retain the full period.
    chart_start = max(start or end - 90 * 86400, end - 90 * 86400)
    if period == 'all' and days:
        first_day = int(datetime.fromisoformat(min(days)).replace(tzinfo=MOSCOW).timestamp())
        chart_start = max(chart_start, first_day)
    result = []
    for stamp in range(chart_start, end, 86400):
        day = day_of(stamp)
        values = days.get(day)
        known = values is not None or day_is_known(windows,stamp,now)
        result.append({'day': day, 'own': values[0] if values else 0 if known else None,
                       'peer': values[1] if values else 0 if known else None})
    return result, counters, progress


def media_data(metrics, legacy=None):
    kinds = set(legacy or {}) | {k.split('_media_', 1)[1] for k in metrics if '_media_' in k}
    return [{'kind': k, 'name': MEDIA_NAMES.get(k, k), 'own': metrics[f'o_media_{k}'],
             'peer': metrics[f'p_media_{k}'],
             'total': (legacy or {}).get(k, metrics[f'o_media_{k}'] + metrics[f'p_media_{k}'])}
            for k in sorted(kinds) if (legacy or {}).get(k, 0) or metrics[f'o_media_{k}'] or metrics[f'p_media_{k}']]


def mine_data(metrics):
    return {'messages': metrics['o_n'], 'voice': metrics['o_media_voice'],
            'voice_seconds': metrics['o_voice_seconds'], 'voice_missing': metrics['o_voice_missing'],
            'deleted': metrics['o_deleted'], 'edits': metrics['o_edits'], 'starts': metrics['o_starts']}


def enrich_overview(repository, owner, account, period, rows, now=None):
    now = int(time.time()) if now is None else now
    combined=Counter()
    for row in rows: combined.update({k:v for k,v in row['metrics'].items() if not k.endswith('_max')})
    chart, metrics, progress = series(repository, owner, account, period, now,metrics_override=combined)
    hours = [0] * 24
    for row in rows:
        # Charts consistently use dated detailed history, never retained totals.
        values = [row['metrics'][f'h_{i}'] for i in range(24)]
        hours = [a + b for a, b in zip(hours, values)]
    return {'days': chart, 'hours': hours, 'mine': mine_data(metrics),
            'media': media_data(metrics), 'coverage': {'since': min([progress.get(f'continuous_{a}', now) for a in repository.check_account(owner, account)] or [now]),
            'recovering': not bool(progress.get('backfill_complete')), 'detailed': metrics['o_n'] + metrics['p_n'],
            'total': sum(r['total_messages'] for r in rows),
            'undetailed': sum(r['undetailed'] for r in rows),
            'partial_period': any(r['totals_basis'] == 'detailed' and r['undetailed'] for r in rows)}}


def enrich_detail(repository, owner, account, chat, period, data, now=None, side='both', page=0):
    now = int(time.time()) if now is None else now
    row, legacy_days, _ = data['legacy']
    m = data['metrics']
    chart, _, _ = series(repository, owner, account, period, now, chat, metrics_override=m)
    active = sorted(d for d, count in data['days'].items() if count)
    streak = longest = 0
    previous = None
    for day in active:
        date = datetime.fromisoformat(day)
        streak = streak + 1 if previous and (date-previous).days == 1 else 1
        longest = max(longest, streak)
        previous = date
    peak_day = max(data['days'], key=data['days'].get) if active else None
    totals = period_totals(row, m, period, now)
    own, peer = totals['own'], totals['peer']
    hours = [m[f'h_{i}'] for i in range(24)]
    cached = repository.db.execute('SELECT metrics FROM business_stats_chat_metrics WHERE account_id=? AND chat_id=?',
                                   (account, chat)).fetchone()
    all_m = Counter(json.loads(cached[0]) if cached else {})
    undetailed_own = max(0, row['owner_messages'] - all_m['o_n'])
    undetailed_peer = max(0, row['peer_messages'] - all_m['p_n'])
    words = data['words']
    word_total = data['word_count']
    words_basis = 'detailed'
    if totals['complete_scope'] and side == 'both':
        words = repository.db.execute('SELECT word,count FROM business_stats_words WHERE account_id=? AND chat_id=? '
                                      'ORDER BY count DESC,word LIMIT 50 OFFSET ?', (account,chat,page*50)).fetchall()
        word_total = repository.db.execute('SELECT COUNT(*) FROM business_stats_words WHERE account_id=? AND chat_id=?',
                                           (account,chat)).fetchone()[0]
        words_basis = 'retained'
    weekdays = [0] * 7
    for day, count in data['days'].items():
        weekdays[datetime.fromisoformat(day).weekday()] += count
    return {'series': chart, 'hours': hours, 'weekdays': weekdays,
            'active_days': legacy_days if totals['complete_scope'] else len(active),
            'totals': totals,
            'lengths': {s: {'mean': round((row[f'{"owner" if s == "o" else "peer"}_chars'] if totals['complete_scope'] else m[f'{s}_chars']) / n, 1) if n else 0,
                            'max': data['lengths'][s]} for s, n in (('o', own), ('p', peer))},
            'replies': {s: {'normal': histogram_summary(m, f'reply_{s}_'),
                           'long': histogram_summary(m, f'long_{s}_'), 'starts': m[f'{s}_starts']}
                        for s in ('o', 'p')},
            'events': {s: {'deleted': m[f'{s}_deleted'], 'edited': m[f'{s}_edited'], 'edits': m[f'{s}_edits'],
                          'delay': histogram_summary(m, f'delete_{s}_')} for s in ('o', 'p')},
            'durations': {s: {k: {'seconds': m[f'{s}_{k}_seconds'], 'missing': m[f'{s}_{k}_missing']}
                             for k in ('voice', 'video_note')} for s in ('o', 'p')},
            'media': media_data(m, json.loads(row['media_json']) if totals['complete_scope'] else None),
            'words': [dict(w) for w in words], 'words_basis': words_basis,
            'word_total': word_total, 'word_page_size': 50,
            'records': {'day': peak_day, 'day_messages': data['days'][peak_day] if peak_day else 0,
                        'streak': longest},
            'last': dict(data['last']) if data['last'] and not data['unknown'] else None,
            'coverage': {'since': data['available'], 'detailed': m['o_n'] + m['p_n'],
                         'total': own + peer, 'unknown': data['unknown'],
                         'undetailed': undetailed_own + undetailed_peer,
                         'undetailed_own': undetailed_own, 'undetailed_peer': undetailed_peer,
                         'basis': totals['basis'], 'partial_period': not totals['complete_scope'] and bool(undetailed_own + undetailed_peer),
                         'first_at': row['first_at'], 'last_at': row['last_at'],
                         'recovering': not bool(data['progress'].get('backfill_complete'))},
            'comparison': None if period == 'all' else comparison(repository, owner, account, chat, data, now)}


def comparison(repository, owner, account, chat, data, now):
    span = data['end'] - data['start']
    continuous = max(data['progress']['coverage_start'], data['progress'].get(f'continuous_{account}', 0))
    if data['start'] - span < continuous or not data['progress'].get('backfill_complete') or data['unknown']:
        return {'available': False}
    def count(start, end):
        return repository.db.execute('SELECT COUNT(*) FROM business_stats_seen WHERE account_id=? AND chat_id=? '
                                     'AND detailed=1 AND sent_at>=? AND sent_at<?', (account, chat, start, end)).fetchone()[0]
    current = count(data['start'], now + 1)
    previous = count(data['start'] - span, now + 1 - span)
    return {'available': True, 'current': current, 'previous': previous,
            'percent': round((current - previous) * 100 / previous, 1) if previous else None}
