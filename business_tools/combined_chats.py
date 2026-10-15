"""One contact across the current owner's accounts, without rewriting history.

    Replies remain measured within each dialogue: switching accounts must not
    create an invented reply or conversation start between unrelated streams.
"""
import json
from collections import Counter
from datetime import datetime

from .analytics import histogram_summary, day_of


def merge_metrics(values):
    combined = Counter()
    for metrics in values:
        for key,value in metrics.items():
            combined[key] = max(combined[key],value) if key.endswith('_max') else combined[key]+value
    return combined


def combine_chat_rows(rows):
    groups = {}
    for row in rows:
        groups.setdefault(row['chat_id'],[]).append(row)
    result = []
    for group in groups.values():
        latest = max(group,key=lambda r:(r.get('last_at') or 0,r['account_id']))
        row = dict(latest)
        row['account_ids'] = sorted(r['account_id'] for r in group)
        row['avatar_account_id'] = latest['account_id']
        if len(group)>1:
            row['account_id'] = 0  # Presentation key only; never a database account.
            for key in ('total_messages','owner_messages','peer_messages','undetailed'):
                row[key] = sum(r.get(key,0) for r in group)
            row['metrics'] = merge_metrics(r['metrics'] for r in group)
            bases={r['totals_basis'] for r in group}
            row['totals_basis'] = next(iter(bases)) if len(bases)==1 else 'mixed'
        result.append(row)
    return result


def chat_pin_keys(row):
    return {f"{a}:{row['chat_id']}" for a in row.get('account_ids',[row['account_id']])} | {f"0:{row['chat_id']}"}


def combined_detail(repository, owner, chat, period, now, side, page, section, single):
    allowed = set(repository.check_account(owner))
    accounts = sorted(r[0] for r in repository.db.execute(
        'SELECT account_id FROM business_chat_stats WHERE owner_id=? AND chat_id=?',(owner,chat)) if r[0] in allowed)
    if not accounts:
        return None
    raw = [repository.analytics(owner,account,chat,period,now=now,side=side,page=0,word_limit=0) for account in accounts]
    parts = [single(repository,owner,account,chat,period,now,side,page,section,analytics_data=data)
             for account,data in zip(accounts,raw)]
    metrics = merge_metrics(data['metrics'] for data in raw)
    days = Counter()
    for data in raw:
        days.update(data['days'])
    # Build words from complete source tables, then paginate once after grouping.
    queries, args = [], []
    active_days = set()
    for account, part, data in zip(accounts,parts,raw):
        if part['words_basis']=='retained':
            queries.append('SELECT word,count FROM business_stats_words WHERE account_id=? AND chat_id=?')
            args.extend((account,chat))
        else:
            clauses=['account_id=?','chat_id=?','day<?']
            values=[account,chat,day_of(data['end'])]
            if data['start'] is not None:
                clauses.append('day>=?');values.append(day_of(data['start']))
            if side in ('o','p'):
                clauses.append('side=?');values.append(side)
            queries.append('SELECT word,count FROM business_stats_side_words WHERE '+' AND '.join(clauses))
            args.extend(values)
        if part['totals']['complete_scope']:
            active_days.update(r[0] for r in repository.db.execute(
                'SELECT day FROM business_stats_days WHERE account_id=? AND chat_id=?',(account,chat)))
        else:
            active_days.update(day for day,count in data['days'].items() if count)
    union=' UNION ALL '.join(queries)
    words=repository.db.execute('SELECT word,SUM(count) AS count FROM ('+union+') GROUP BY word '
        'ORDER BY count DESC,word LIMIT 50 OFFSET ?',(*args,page*50)).fetchall()
    word_total=repository.db.execute('SELECT COUNT(DISTINCT word) FROM ('+union+')',args).fetchone()[0]
    own,peer=sum(p['own'] for p in parts),sum(p['peer'] for p in parts)
    chart={}
    for part in parts:
        for entry in part['series']:
            values=chart.setdefault(entry['day'],dict(day=entry['day'],own=0,peer=0,known_own=0,known_peer=0))
            for key in ('own','peer'):
                if entry[key] is not None:
                    values[key]+=entry[key]
                    values['known_'+key]+=1
    for entry in chart.values():
        for key in ('own','peer'):
            if entry.pop('known_'+key)<len(parts) and not entry[key]:
                entry[key]=None
    media={}
    for part in parts:
        for item in part['media']:
            target=media.setdefault(item['kind'],dict(kind=item['kind'],name=item['name'],own=0,peer=0,total=0))
            for key in ('own','peer','total'):target[key]+=item[key]
    streak=longest=0;previous=None
    for day in sorted(d for d,n in days.items() if n):
        date=datetime.fromisoformat(day)
        streak=streak+1 if previous and (date-previous).days==1 else 1
        longest=max(longest,streak);previous=date
    peak=max(days,key=days.get) if days else None
    coverage={key:sum(p['coverage'].get(key,0) for p in parts)
              for key in ('detailed','unknown','undetailed','undetailed_own','undetailed_peer')}
    retained=all(p['totals']['complete_scope'] for p in parts)
    bases={p['totals']['basis'] for p in parts}
    basis=next(iter(bases)) if len(bases)==1 else 'mixed'
    def bound(key, reducer):
        values=[p['coverage'].get(key) for p in parts if p['coverage'].get(key) is not None]
        return reducer(values) if values else None
    coverage.update(total=own+peer,since=bound('since',min),first_at=bound('first_at',min),last_at=bound('last_at',max),
        recovering=any(p['coverage']['recovering'] for p in parts),basis=basis,
        partial_period=any(p['coverage']['partial_period'] for p in parts))
    lengths={}
    for author,n in (('o',own),('p',peer)):
        chars=sum(data['legacy'][0]['owner_chars' if author=='o' else 'peer_chars']
                  if part['totals']['complete_scope'] else data['metrics'][f'{author}_chars']
                  for part,data in zip(parts,raw))
        lengths[author]=dict(mean=round(chars/n,1) if n else 0,max=max(data['lengths'][author] for data in raw))
    last=[dict(data['last']) for data in raw if data['last']]
    comparison=None
    if period!='all':
        comparison={'available':False}
        if all(p['comparison'] and p['comparison']['available'] for p in parts):
            current=sum(p['comparison']['current'] for p in parts)
            previous=sum(p['comparison']['previous'] for p in parts)
            comparison=dict(available=True,current=current,previous=previous,
                percent=round((current-previous)*100/previous,1) if previous else None)
    result=dict(parts[0])
    latest=max(zip(accounts,raw),key=lambda item:item[1]['legacy'][0]['last_at'] or 0)[0]
    result.update(name=parts[accounts.index(latest)]['name'],account_ids=accounts,avatar_account_id=latest,
        account_name=f'Все ваши аккаунты · {len(accounts)}' if len(accounts)>1 else parts[0]['account_name'],
        own=own,peer=peer,imported_messages=sum(p['imported_messages'] for p in parts),
        series=[chart[day] for day in sorted(chart)],days=sorted(days.items()),
        hours=[sum(p['hours'][i] for p in parts) for i in range(24)],
        weekdays=[sum(p['weekdays'][i] for p in parts) for i in range(7)],active_days=len(active_days),
        totals=dict(own=own,peer=peer,total=own+peer,basis=basis,complete_scope=retained),lengths=lengths,
        replies={s:dict(normal=histogram_summary(metrics,f'reply_{s}_'),long=histogram_summary(metrics,f'long_{s}_'),starts=metrics[f'{s}_starts']) for s in ('o','p')},
        events={s:dict(deleted=metrics[f'{s}_deleted'],edited=metrics[f'{s}_edited'],edits=metrics[f'{s}_edits'],delay=histogram_summary(metrics,f'delete_{s}_')) for s in ('o','p')},
        durations={s:{kind:dict(seconds=metrics[f'{s}_{kind}_seconds'],missing=metrics[f'{s}_{kind}_missing']) for kind in ('voice','video_note')} for s in ('o','p')},
        media=list(media.values()),media_count=sum(bool(m['total'] if side=='both' else m['own' if side=='o' else 'peer']) for m in media.values()),
        words=[dict(w) for w in words],word_total=word_total,word_count=word_total,
        words_basis=parts[0]['words_basis'] if len({p['words_basis'] for p in parts})==1 else 'mixed',
        records=dict(day=peak,day_messages=days[peak] if peak else 0,streak=longest),
        last=max(last,key=lambda row:row['sent_at']) if last and not coverage['unknown'] else None,
        coverage=coverage,comparison=comparison)
    return result
