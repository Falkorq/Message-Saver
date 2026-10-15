const tg = window.Telegram?.WebApp;
const $ = id => document.getElementById(id);
const state = {screen:'chats', selected:null, section:'overview', period:'30', side:'both', wordPage:0, sequence:0, overview:null, detail:null, search:'', sort:'recent', rank:'total_messages', visible:50, controller:null};
const screenNames = {overview:'Обзор',chats:'Чаты',mine:'Моя активность',rankings:'Рейтинги',info:'Об учёте'};
const sections = {overview:'Общение',activity:'Ритм',content:'Медиа',events:'Изменения'};
const fmt = n => Number(n ?? 0).toLocaleString('ru-RU');
const pct = (n,total) => total ? `${Math.round(100*n/total)}%` : '0%';
const date = value => value ? new Date(typeof value==='number' ? value*1000 : `${value}T12:00:00+03:00`).toLocaleDateString('ru-RU',{timeZone:'Europe/Moscow',day:'numeric',month:'short'}) : 'нет данных';
function duration(seconds) {if(seconds==null)return 'нет данных';if(seconds<60)return `${Math.round(seconds)} с`;if(seconds<3600)return `${Math.round(seconds/60)} мин`;if(seconds<86400)return `${(seconds/3600).toLocaleString('ru-RU',{maximumFractionDigits:1})} ч`;return `${(seconds/86400).toLocaleString('ru-RU',{maximumFractionDigits:1})} дн`;}
function node(tag,text,cls){const el=document.createElement(tag);if(text!==undefined)el.textContent=text;if(cls)el.className=cls;return el;}
function button(text,action,cls){const b=node('button',text,cls);b.type='button';b.onclick=action;return b;}
function status(text,error=false){$('status').hidden=!text;$('status').replaceChildren(node('span',text));if(error){const code=state.errorCode; if(code==='auth_expired' || code==='invitation_required')$('status').append(button(code==='auth_expired'?'Открыть заново из бота':'Получить приглашение',()=>botAction('start')));else if(code==='chat_unavailable')$('status').append(button('Вернуться к чатам',()=>navigate('chats')));else $('status').append(button('Повторить',()=>{state.overviewAt=0;load();}),button('Помощь',()=>botAction('help')));}}
function botAction(command){const name=state.overview?.bot_username;if(name && /^[A-Za-z0-9_]{5,32}$/.test(name)){tg?.openTelegramLink?.(`https://t.me/${name}?start=${command}`);return;}const text=`Вернитесь в чат с Nyan и отправьте /${command}.`;if(tg?.showAlert)tg.showAlert(text,()=>tg.close());else window.alert(text);}
function freshness(){const stamp=state.detail?.generated_at || state.overview?.generated_at;if(!stamp)return '';return `Обновлено ${new Date(stamp*1000).toLocaleTimeString('ru-RU',{timeZone:'Europe/Moscow',hour:'2-digit',minute:'2-digit'})} МСК${state.overview?.coverage?.since?' · Учёт с '+date(state.overview.coverage.since):''}${state.overview?.connection_states?.some(r=>r.state==='paused')?' · Сбор остановлен':state.overview?.connection_states?.some(r=>r.state==='disconnected')?' · Соединение отключено':''}`;}

function theme(value){document.documentElement.dataset.theme=value;document.querySelector('meta[name=theme-color]').content=value==='dark'?'#181420':'#f8f7fc';$('theme').textContent=value==='dark'?'Включить светлую тему':'Включить тёмную тему';if(tg?.isVersionAtLeast?.('6.1')){tg.setHeaderColor(value==='dark'?'#181420':'#f8f7fc');tg.setBackgroundColor(value==='dark'?'#181420':'#f8f7fc');}}
let savedTheme;try{savedTheme=localStorage.getItem('stats-theme');}catch{}
theme(savedTheme || 'light');
$('theme').onclick=()=>{const value=document.documentElement.dataset.theme==='dark'?'light':'dark';theme(value);closeSheets();try{localStorage.setItem('stats-theme',value);}catch{}};
if(tg){tg.ready();tg.expand();tg.BackButton.onClick(()=>{const open=document.querySelector('dialog[open]');if(open)open.close();else navigate('chats');});}
function params(){return new URLSearchParams({period:state.period,account:$('account').value,q:state.screen==='chats'?state.search:'',sort:state.screen==='rankings'?(state.rank==='total_messages'?'messages':state.rank):state.sort,pins:state.screen==='chats'?pinned.slice(0,200).join(','):'',favorites:state.screen==='chats' && state.favoritesOnly?'1':'0'});}
async function api(path,query,signal){
  let response;
  try{response=await fetch(`${path}?${query}`,{headers:{Authorization:`tma ${tg?.initData || ''}`,Accept:'application/json'},cache:'no-store',signal:AbortSignal.any([...(signal?[signal]:[]),AbortSignal.timeout(12000)])});}
  catch(error){if(error.name==='AbortError')throw error;if(error.name==='TimeoutError')throw Error('Ответ задерживается. Последние загруженные данные сохранены; попробуйте ещё раз.');throw Error('Не удалось подключиться. Проверьте интернет и попробуйте ещё раз.');}
  const ray=response.headers.get('cf-ray');
  const reference=ray && /^[a-zA-Z0-9-]{1,80}$/.test(ray)?` Код проверки: ${ray}.`:'';
  if(response.headers.get('cf-mitigated')==='challenge')throw Error(`Статистика временно недоступна. Повторите позже или обратитесь в поддержку.${reference}`);
  let data;
  try{data=await response.json();}
  catch(error){
    if(error.name==='AbortError')throw error;
    if(response.status===403 && ray)throw Error(`Сервис временно отклонил запрос. Повторите позже.${reference}`);
    if(response.status>=500)throw Error(`Ошибка сервера (${response.status}). Попробуйте позже.${reference}`);
    throw Error(`Получен некорректный ответ статистики (HTTP ${response.status}).${reference}`);
  }
  if(!response.ok)throw Object.assign(Error(typeof data?.error==='string'?data.error:`Не удалось загрузить статистику (HTTP ${response.status}).`),{code:data.code,status:response.status});
  if(!data || typeof data!=='object' || Array.isArray(data))throw Error('Получен некорректный формат статистики.');
  return data;
}
const screenScroll=new Map();
function navigate(screen,selected=null,section='overview'){screenScroll.set(location.hash||'#chats',window.scrollY);const target=selected?`chat/${selected.account_id}/${selected.chat_id}/${section}`:screen;if(location.hash.slice(1)===target){route();}else location.hash=target;}
function route(){state.restoreScroll=screenScroll.get(location.hash||'#chats')||0;const parts=location.hash.slice(1).split('/');if(parts[3]==='replies')parts[3]='activity';if(parts[3]==='records')parts[3]='overview';const previous=state.selected;state.selected=null;state.section='overview';if(parts[0]==='chat' && /^\d+$/.test(parts[1]) && /^-?\d+$/.test(parts[2]) && sections[parts[3] || 'overview']){state.selected={account_id:Number(parts[1]),chat_id:Number(parts[2])};state.section=parts[3] || 'overview';state.screen='chats';}else state.screen=screenNames[parts[0]]?parts[0]:'chats';if(!previous || !state.selected || previous.chat_id!==state.selected.chat_id || previous.account_id!==state.selected.account_id)$('chat-context').replaceChildren();if(!previous || !state.selected || previous.account_id!==state.selected.account_id || previous.chat_id!==state.selected.chat_id){state.side='both';state.wordPage=0;state.detail=null;}tg?.BackButton[state.selected?'show':'hide']();load();}
window.addEventListener('hashchange',route);
document.querySelectorAll('[data-screen]').forEach(b=>b.onclick=()=>{closeSheets();navigate(b.dataset.screen);});
$('periods').querySelectorAll('button').forEach(b=>b.onclick=()=>{state.period=b.dataset.period;state.wordPage=0;state.overview=null;load();});
$('account').onchange=()=>{closeSheets();state.favoritesOnly=false;state.overview=null;state.wordPage=0;navigate('chats');};
$('refresh').onclick=()=>{closeSheets();state.overviewAt=0;state.wordPage=0;load();};
function updateChrome(){document.body.classList.toggle('in-chat',Boolean(state.selected));document.querySelector('.page-heading').hidden=Boolean(state.selected);$('chat-context').hidden=!state.selected;document.querySelectorAll('[data-screen]').forEach(b=>{if(b.dataset.screen===state.screen)b.setAttribute('aria-current','page');else b.removeAttribute('aria-current');});$('more-tab').setAttribute('aria-current',['mine','rankings','info'].includes(state.screen)?'page':'false');$('period-picker').value=state.period;$('period-scope').replaceChildren(icon('calendar-blank'),node('span',periodLabels[state.period]),icon('caret-down'));const option=$('account').selectedOptions[0];$('account-scope').replaceChildren(node('span',option?.textContent || 'Все аккаунты'),icon('caret-down'));$('breadcrumb').textContent=screenNames[state.screen];}
async function load(){
  stopMotion();const seq=++state.sequence;state.controller?.abort();state.controller=new AbortController();updateChrome();
  if(state.selected && !$('chat-context').childElementCount){const h=node('div',undefined,'contact-header');h.append(iconButton('arrow-left','Вернуться к чатам',()=>navigate('chats'),'back'),node('h1','Статистика чата'));$('chat-context').append(h);}
  if(!state.selected)heading(state.screen==='overview'?'Сводка':screenNames[state.screen],'');
  $('content').setAttribute('aria-busy','true');$('content').classList.add('loading');
  status($('content').childElementCount?'Обновляем показатели…':'Загружаем статистику…');
  try{
    if(!tg?.initData)throw Object.assign(Error('Откройте Nyan message из бота: /chatstats → «Открыть статистику».'),{code:'auth_expired'});
    if(!state.overview || Date.now()-(state.overviewAt||0)>20000 || state.overviewKey!==params().toString()){
      const data=await api('/api/overview',params(),state.controller.signal);if(seq!==state.sequence)return;
      state.overview=data;state.overviewAt=Date.now();state.overviewKey=params().toString();const current=$('account').value;const option=node('option','Все аккаунты');option.value='';
      $('account').replaceChildren(option,...data.accounts.map(a=>{const o=node('option',a.name);o.value=a.id;return o;}));
      $('account').value=current;$('chat-count').textContent=fmt(data.chats.length);
    }
    if(state.selected){
      const q=params();q.set('account',state.selected.account_id);q.set('side',state.side);
      q.set('section',state.section==='content'?'words':state.section);
      const data=await api(`/api/chats/${state.selected.chat_id}`,q,state.controller.signal);if(seq!==state.sequence)return;
      state.detail=data;state.wordPage=0;
    }
    if(seq===state.sequence){render();status(freshness());clearTimeout(state.staleTimer);state.staleTimer=setTimeout(()=>{if(seq===state.sequence){status(freshness()+' · Данные могут быть устаревшими');$('status').append(button('Обновить',()=>{state.overviewAt=0;load();}));}},21000);if(state.restoreScroll!=null){window.scrollTo(0,state.restoreScroll);state.restoreScroll=null;}}
  }catch(error){
    if(error.name!=='AbortError' && seq===state.sequence){state.errorCode=error.code;if(!state.overview || [401,403,404,409].includes(error.status)){$('content').replaceChildren();if([401,403,409].includes(error.status)){state.overview=null;state.detail=null;document.querySelectorAll('.avatar img').forEach(img=>img.remove());avatarMemory.clear();}}status(`${error.message} ${state.overview ? freshness()+' · сохранённые данные' : ''}`,true);}
  }finally{
    if(seq===state.sequence){$('content').setAttribute('aria-busy','false');$('content').classList.remove('loading');}
  }
}
function heading(title,subtitle){$('title').textContent=title;$('subtitle').textContent=subtitle || '';}
function panel(title,subtitle,action){const box=node('article',undefined,'panel');const head=node('div',undefined,'panel-head');const copy=node('div');copy.append(node('h2',title));if(subtitle)copy.append(node('p',subtitle));head.append(copy);if(action)head.append(button(action[0],action[1]));box.append(head);return box;}
function kpis(items){const grid=node('div',undefined,'kpis');items.forEach(([value,label,note],i)=>{const card=node('div',undefined,`kpi${i===0?' featured':''}`);card.append(node('p',label,'kpi-label'),node('strong',typeof value==='number'?fmt(value):value));if(note)card.append(node('small',note));grid.append(card);});return grid;}
function rows(items){const box=node('div',undefined,'stat-rows');items.forEach(([label,value])=>{const row=node('div',undefined,'stat-row');row.append(node('span',label),node('strong',value));box.append(row);});return box;}
function grid(...items){const box=node('div',undefined,'grid');box.append(...items);return box;}
function empty(target,title,text){const el=node('div',undefined,'empty');const mark=node('span',undefined,'empty-mark');mark.append(icon('chats'));el.append(mark,node('h3',title),node('p',text));target.append(el);}
function coverage(data){const box=node('details',undefined,'coverage-details');const title=data.recovering?'История ещё восстанавливается':data.partial_period?'Часть старых сообщений нельзя разнести по датам':data.detailed<data.total?`${fmt(data.total-data.detailed)} сообщений учтены только в итогах`:'По сохранённой истории';box.append(node('summary',title));const copy=node('div',undefined,'coverage-copy');copy.append(node('p',`В итогах: ${fmt(data.total??data.detailed)}. В подробной истории этого периода: ${fmt(data.detailed)}.`));if(data.since)copy.append(node('p',`Подробные данные с ${date(data.since)}. Графики, ответы и рекорды используют подробную историю. Общие слова и медиа могут включать накопленные итоги.`));if(data.undetailed)copy.append(node('p',`${fmt(data.undetailed)} старых сообщений имеют только накопленные итоги${data.undetailed_own!=null?`: от вас ${fmt(data.undetailed_own)}, от собеседника ${fmt(data.undetailed_peer)}`:'.'}`));if(data.partial_period)copy.append(node('p','Период охватывает только часть срока переписки. Старые итоги нельзя точно распределить внутри него: показанные цифры могут быть ниже реальных.'));if(data.basis==='retained')copy.append(node('p','Период охватывает весь известный срок переписки, поэтому итог включает старые накопленные счётчики.'));if(data.recovering)copy.append(node('p','Дайте восстановлению истории закончиться, затем обновите показатели.'));box.append(copy);return box;}
function legend(){const box=node('div',undefined,'legend');[['own','Вы'],['peer','Собеседник']].forEach(([cls,text])=>{const entry=node('span',undefined,cls);entry.append(node('i'),node('span',text));box.append(entry);});return box;}
function svgEl(tag,attrs){const el=document.createElementNS('http://www.w3.org/2000/svg',tag);Object.entries(attrs).forEach(([k,v])=>el.setAttribute(k,String(v)));return el;}
function activityChart(days,title='Динамика общения'){const box=panel(title,state.period==='all'?'До 90 дней с подробными данными':null);box.append(legend());if(!days.length || days.every(d=>d.own==null && d.peer==null)){empty(box,'Пока нет подробной истории','Сохранённые общие итоги могут быть доступны выше.');return box;}
const chartWidth=Math.max(240,Math.min(640,$('content').clientWidth));const svg=svgEl('svg',{viewBox:`0 0 ${chartWidth} 220`,preserveAspectRatio:'none',class:'chart-svg',role:'img','aria-label':'График сообщений от вас и собеседника по дням. Точные значения доступны под графиком.'});const max=Math.max(1,...days.flatMap(d=>[d.own||0,d.peer||0]));const left=Math.max(34,fmt(max).length*7.5),width=chartWidth-left-16,base=190,top=14;const x=i=>left+(days.length===1?width/2:i*width/(days.length-1));const y=n=>base-(base-top)*n/max;
[0,.5,1].forEach(f=>{svg.append(svgEl('line',{x1:left,x2:chartWidth-16,y1:y(max*f),y2:y(max*f),class:'chart-grid'}));const text=svgEl('text',{x:0,y:y(max*f)+4,fill:'var(--muted)','font-size':11});text.textContent=fmt(Math.round(max*f));svg.append(text);});
for(const side of ['own','peer']){let segments=[],current=[];days.forEach((d,i)=>{if(d[side]==null){if(current.length)segments.push(current);current=[];}else current.push([x(i),y(d[side])]);});if(current.length)segments.push(current);segments.forEach(points=>{const path=points.map(([a,b],i)=>`${i?'L':'M'}${a},${b}`).join(' ');if(side==='own')svg.append(svgEl('path',{d:`${path} L${points.at(-1)[0]},${base} L${points[0][0]},${base} Z`,class:'chart-area'}));svg.append(svgEl('path',{d:path,fill:'none',class:`chart-${side}`,'stroke-width':2.5,'stroke-linecap':'round','stroke-linejoin':'round','vector-effect':'non-scaling-stroke'}));if(points.length===1)svg.append(svgEl('circle',{cx:points[0][0],cy:points[0][1],r:3,fill:side==='own'?'var(--chart-own)':'var(--chart-peer)'}));});}
box.append(svg);const axis=node('div',undefined,'chart-axis');axis.append(node('span',date(days[0].day)),node('span',date(days[Math.floor(days.length/2)].day)),node('span',date(days.at(-1).day)));box.append(axis);
const marker=svgEl('line',{x1:0,x2:0,y1:top,y2:base,stroke:'var(--muted)','stroke-width':1,'stroke-dasharray':'4 4',opacity:.5});svg.append(marker);
const control=node('label',undefined,'chart-control');const text=node('span',undefined,'chart-detail');const input=node('input');input.type='range';input.min='0';input.max=days.length-1;input.value=days.length-1;input.setAttribute('aria-label','Выбрать день на графике');function selected(){const i=Number(input.value),d=days[i];marker.setAttribute('x1',x(i));marker.setAttribute('x2',x(i));text.textContent=`${date(d.day)} · ${d.own==null?'нет сохранённых данных':`вы: ${fmt(d.own)} · собеседник: ${fmt(d.peer)} · всего: ${fmt(d.own+d.peer)}`}`;}input.oninput=selected;svg.addEventListener('pointermove',event=>{const rect=svg.getBoundingClientRect();const coordinate=(event.clientX-rect.left)*chartWidth/rect.width;input.value=Math.min(days.length-1,Math.max(0,Math.round((coordinate-left)*(days.length-1)/width)));selected();});selected();control.append(text,input);box.append(control);
const disclosure=node('details');disclosure.append(node('summary','Все значения по дням'));disclosure.append(table(['Дата','Вы','Собеседник','Всего'],[...days].reverse().map(d=>[date(d.day),d.own==null?'—':fmt(d.own),d.peer==null?'—':fmt(d.peer),d.own==null?'нет данных':fmt(d.own+d.peer)])));box.append(disclosure);return box;}
function balance(own,peer){const box=panel('Баланс общения','Количество сообщений от каждой стороны');const total=own+peer;box.append(legend());const track=node('div',undefined,'split-track');const fill=node('span');fill.style.width=`${total?100*own/total:0}%`;track.append(fill);box.append(track);const nums=node('div',undefined,'split-numbers');[[own,'От вас'],[peer,'От собеседника']].forEach(([value,label])=>{const el=node('div');el.append(node('strong',fmt(value)),node('p',`${label} · ${pct(value,total)}`));nums.append(el);});box.append(nums,node('p',total?'Доли показывают количество сообщений, а не длительность разговоров.':'За выбранный период нет сообщений.','note'));return box;}
function table(headers,items){const wrap=node('div',undefined,'table-wrap');const t=node('table');const head=node('thead');const tr=node('tr');headers.forEach((h,i)=>{const cell=node('th',h,i?'numeric':'');cell.scope='col';tr.append(cell);});head.append(tr);const body=node('tbody');items.forEach(row=>{const r=node('tr');row.forEach((v,i)=>{const td=node('td',undefined,i?'numeric':'');td.append(v instanceof Node?v:node('span',v));r.append(td);});body.append(r);});t.append(head,body);wrap.append(t);return wrap;}
function chatName(r){return r.display_name || (r.username?`@${r.username}`:String(r.chat_id));}
function chatLink(r,i){const b=button('',()=>navigate('chats',r),'table-open');const copy=node('span');copy.append(node('span',(isPinned(r)?'• ':'')+chatName(r),'chat-name'),node('span',r.account_name || `Аккаунт ${String(r.account_id).slice(-4)}`,'chat-meta'));b.append(node('span',Array.from(chatName(r))[0].toUpperCase(),`avatar tone-${i%4}`),copy);return b;}
function chatRows(chats,limit=5,metric='total_messages'){const box=node('div',undefined,'list');chats.slice(0,limit).forEach((r,i)=>{const b=button('',()=>navigate('chats',r),'chat-row');const copy=node('span',undefined,'chat-copy');const name=node('span',chatName(r),'chat-name');if(isPinned(r)){name.append(icon('push-pin'));}const meta=node('span',r.last_at?`Общались ${date(r.last_at)}`:'Нет даты последнего сообщения','chat-meta');copy.append(name,meta);if(!$('account').value && state.overview.accounts.length>1)copy.append(node('span',r.account_name || `Аккаунт ${String(r.account_id).slice(-4)}`,'chat-account'));const amount=node('span',undefined,'chat-value');amount.append(node('strong',metric==='voice_seconds'?duration(r[metric]):fmt(r[metric])),node('small',['total_messages','peer_messages'].includes(metric)?'сообщений':metric==='voice'?'голосовых':metric==='voice_seconds'?'длительность':'удалений'));b.append(avatarElement(r,i),copy,amount);box.append(b);});if(!chats.length)empty(box,'Здесь появятся ваши чаты','За выбранный период нет сохранённых сообщений.');return box;}
function overview(){const d=state.overview,s=d.summary;heading('Сводка','');$('content').append(kpis([[s.messages,'Сообщений',''],[s.chats,'Чатов',''],[s.own,'От вас',pct(s.own,s.messages)],[s.peer,'От собеседников',pct(s.peer,s.messages)]]),coverage(d.coverage),activityChart(d.days,'Общение по дням'));const top=panel('Чаще всего общаетесь',null,['Все чаты',()=>navigate('chats')]);top.append(chatRows([...(d.top_chats||d.chats)].sort((a,b)=>b.total_messages-a.total_messages)));$('content').append(top,hoursPanel(d.hours));}
function bars(values,labels){const chart=node('div',undefined,'bars');const max=Math.max(1,...values);values.forEach((value,i)=>{const col=node('div',undefined,'bar');const fill=node('div',undefined,'bar-fill');fill.style.height=`${Math.max(2,106*value/max)}px`;col.title=`${labels[i]}: ${fmt(value)}`;col.setAttribute('aria-label',col.title);col.append(node('span',fmt(value),'bar-value'),fill,node('span',labels[i],'bar-label'));chart.append(col);});return chart;}
function hoursPanel(hours){const box=panel('Когда вы общаетесь','Сообщения по времени суток · МСК');const groups=[0,1,2,3].map(i=>hours.slice(i*6,i*6+6).reduce((a,b)=>a+b,0));box.append(bars(groups,['00–06','06–12','12–18','18–24']));const peak=Math.max(...hours);box.append(node('p',peak?`Самый активный час: ${String(hours.indexOf(peak)).padStart(2,'0')}:00 · ${fmt(peak)} сообщений`:'Пока нет данных об активности по часам.','note'));return box;}
function connectionEmpty(){const states=(state.overview.connection_states||[]).filter(r=>!$('account').value || String(r.account_id)===$('account').value);if(states.some(r=>r.state==='paused'))return {text:'Сбор остановлен. Сохранённая история доступна; новые сообщения не учитываются.',action:'Управление сбором в боте',command:'data'};if(!states.length || states.every(r=>r.state==='not_connected'))return {text:'Аккаунт ещё не подключён. Откройте инструкцию в боте.',action:'Подключить аккаунт',command:'connect'};if(states.every(r=>r.state==='disconnected'))return {text:'Соединение отключено. Подключите Nyan повторно в настройках Telegram.',action:'Подключить повторно',command:'connect'};if(states.every(r=>r.state==='waiting'))return {text:'Аккаунт подключён. Ждём первое событие: отправьте тестовое сообщение в разрешённом чате.',action:'Проверить подключение',command:'status'};return {text:'За выбранный период сообщений нет. Выберите другой период; «Всё время» показывает доступную историю Nyan.',action:'Проверить состояние',command:'status'};}
function chats(){heading('Чаты','');const search=node('input');search.type='search';search.placeholder='Найти собеседника';search.value=state.search;search.setAttribute('aria-label','Поиск по чатам');const searchBox=node('div',undefined,'search-field');const clear=iconButton('x','Очистить поиск',()=>{search.value='';state.search='';state.visible=50;state.overviewAt=0;load();});clear.hidden=!state.search;searchBox.append(icon('magnifying-glass'),search,clear);const filter=node('div',undefined,'chat-list-filter');let favoritesOnly=Boolean(state.favoritesOnly);const all=button('Все',()=>{favoritesOnly=false;state.favoritesOnly=false;state.overviewAt=0;load();});const pinnedOnly=button('Закреплённые',()=>{favoritesOnly=true;state.favoritesOnly=true;state.overviewAt=0;load();});filter.append(all,pinnedOnly);const row=node('div',undefined,'contact-list-heading');const count=node('span');const sort=button('Недавние',()=>{state.sort=state.sort==='recent'?'messages':'recent';state.overviewAt=0;load();});row.append(count,sort);const list=node('div',undefined,'mobile-list');const more=button('Ещё чаты',async()=>{more.disabled=true;try{const q=params();q.set('offset',state.overview.chats.length);const next=await api('/api/overview',q,state.controller.signal);state.overview.chats.push(...next.chats);state.overview.pagination=next.pagination;state.visible+=50;paint();}catch(e){status(e.message,true);}finally{more.disabled=false;}});function paint(){all.setAttribute('aria-pressed',String(!favoritesOnly));pinnedOnly.setAttribute('aria-pressed',String(favoritesOnly));const found=state.overview.chats.filter(r=>(!favoritesOnly || isPinned(r)) && `${chatName(r)} ${r.username || ''} ${r.chat_id}`.toLocaleLowerCase('ru').includes(state.search.toLocaleLowerCase('ru')));found.sort(state.sort==='messages'?(a,b)=>b.total_messages-a.total_messages:(a,b)=>(b.last_at||0)-(a.last_at||0));found.sort((a,b)=>Number(isPinned(b))-Number(isPinned(a)));clear.hidden=!state.search;const total=state.overview.pagination?.total??found.length;count.textContent=`${fmt(total)} ${found.length%100>=11 && found.length%100<=14?'чатов':found.length%10===1?'чат':found.length%10>=2 && found.length%10<=4?'чата':'чатов'}`;sort.replaceChildren(node('span',state.sort==='messages'?'По сообщениям':'Недавние'),icon('sort-descending'));list.replaceChildren(chatRows(found,state.visible));if(!found.length){list.replaceChildren();empty(list,favoritesOnly?'Пока ничего не закреплено':state.search?'Не нашли собеседника':'Пока нет чатов',favoritesOnly?'Откройте чат, нажмите «Ещё» и выберите «Закрепить чат».':state.search?'Попробуйте имя или username.':connectionEmpty().text);}if(!found.length && !state.search && !favoritesOnly)list.append(button(connectionEmpty().action,()=>botAction(connectionEmpty().command)));more.hidden=!state.overview.pagination?.has_more && found.length<=state.visible;observeAvatars();}search.oninput=()=>{state.search=search.value;state.visible=50;paint();clearTimeout(state.searchTimer);state.searchTimer=setTimeout(async()=>{state.overviewAt=0;await load();const field=document.querySelector('input[type=search]');if(field){field.focus();}},300);};$('content').append(searchBox,filter,row,list,more);paint();}
function mine(){const d=state.overview,m=d.mine;heading('Моя активность','Ваш вклад в переписку за выбранный период.','ВАША СТОРОНА');$('content').append(kpis([[d.summary.own,'Ваших сообщений',d.coverage.partial_period?'По доступной подробной истории':'Все учтённые в периоде'],[m.voice,'Голосовых','По подробной истории'],[duration(m.voice_seconds),'Голосом',m.voice_missing?`${fmt(m.voice_missing)} без длительности`:'Известная длительность'],[m.starts,'Начатых разговоров','После паузы более 4 часов']]));const events=panel('Изменения ваших сообщений','По доступной подробной истории');events.append(rows([['Удалённых сообщений',fmt(m.deleted)],['Обнаруженных правок',fmt(m.edits)],['Подробно учтённых сообщений',fmt(m.messages)]]));$('content').append(grid(events,balance(d.summary.own,d.summary.peer)),activityChart(d.days,'Ваше общение по дням'),coverage(d.coverage));}
function rankings(){heading('Рейтинги чатов','Сравните активность, голосовые и удаления.','СОБЕСЕДНИКИ');const box=panel('Кто в топе','Голосовые и удаления относятся к доступной подробной истории');const label=node('label','Показатель');const select=node('select');const choices={total_messages:'Все сообщения',peer_messages:'Сообщения собеседника',voice:'Голосовые собеседника',voice_seconds:'Длительность голосовых',deleted:'Удалённые сообщения собеседника',own_deleted:'Ваши удалённые сообщения'};Object.entries(choices).forEach(([k,v])=>{const o=node('option',v);o.value=k;select.append(o);});select.value=state.rank;label.append(select);const list=node('div');function update(){const data=[...state.overview.chats].sort((a,b)=>(b[state.rank]||0)-(a[state.rank]||0));list.replaceChildren(chatRows(data,state.visible,state.rank));observeAvatars();if(state.rank==='voice_seconds' && data.some(r=>r.voice_missing))list.append(node('p','Длительность неполная: часть голосовых сохранена без метаданных.','note'));}select.onchange=()=>{state.rank=select.value;state.overviewAt=0;load();};box.append(label,list);$('content').append(box);if(state.overview.chats.length>state.visible)box.append(button('Показать все чаты',()=>{state.visible=state.overview.chats.length;update();}));update();}
function info(){heading('Как устроена статистика','Что учитывается и где история может быть неполной.','ОБ УЧЁТЕ');const box=panel('Правила расчёта');const items=[['Доступная история','Бот учитывает полученные и сохранённые сообщения. Историю до подключения и пропущенные события восстановить нельзя. Когда период охватывает весь известный срок чата, итог включает накопленные счётчики. Для более короткого периода доступны только сообщения с датами; приложение отмечает неполноту.'],['Периоды по Москве','Сообщения, правки и удаления относятся к дате отправки исходного сообщения в МСК. Сегодня включает текущий неполный день.'],['Ответы и разговоры','Ответ считается от последнего сообщения серии до первого сообщения другой стороны. Паузы больше четырёх часов показаны отдельно. Первый доступный разговор не считается инициативой.'],['Удаления и правки','Telegram не сообщает, кто именно удалил сообщение. «Ваши» и «собеседника» означают автора сообщения. Время удаления означает время обнаружения ботом.'],['Медиа и слова','Длительность доступна только при наличии метаданных. Общие итоги медиа за всё время могут включать старые данные без разделения по авторам. Частые слова показываются без текстов сообщений.'],['Графики и сравнения','График показывает до 90 календарных дней доступной истории выбранного периода. Пропуск данных отличается от нулевой активности. Сравнение появляется только при достаточно полной истории обоих интервалов.']];const content=node('div',undefined,'info-grid');items.forEach(([title,text])=>{const card=node('div',undefined,'info-item');card.append(node('h3',title),node('p',text));content.append(card);});box.append(content);$('content').append(box,coverage(state.overview.coverage));}
function chatDetail(){const d=state.detail;const header=node('div',undefined,'contact-header');const avatar=avatarElement({...state.selected,display_name:d.name},0,'contact-avatar');const identity=node('div',undefined,'contact-identity');identity.append(node('h1',d.name),node('p',d.account_name));header.append(iconButton('arrow-left','Вернуться к чатам',()=>navigate('chats'),'back'),avatar,identity,iconButton('dots-three','Действия с чатом',()=>openActions(),'contact-menu'));$('chat-context').replaceChildren(header);const nav=node('nav',undefined,'detail-navigation');nav.setAttribute('aria-label','Разделы статистики чата');Object.entries(sections).forEach(([key,label])=>{const b=button(label,()=>navigate('chats',state.selected,key));b.setAttribute('aria-pressed',String(key===state.section));nav.append(b);});$('content').append(nav);if(state.section==='overview')detailOverview(d);if(state.section==='activity')detailActivity(d);if(state.section==='events')detailEvents(d);if(state.section==='content')detailContent(d);}
function detailOverview(d){const hero=node('article',undefined,'conversation-summary');const total=node('div',undefined,'conversation-total');total.append(node('span','Сообщений'),node('strong',fmt(d.own+d.peer)));const days=node('div',undefined,'conversation-days');days.append(node('strong',fmt(d.active_days)),node('span','дней общения'));const split=node('div',undefined,'conversation-sides');[['Вы',d.own,'own'],['Собеседник',d.peer,'peer']].forEach(([label,value,cls])=>{const item=node('div',undefined,cls);item.append(node('span',label),node('strong',fmt(value)),node('small',pct(value,d.own+d.peer)));split.append(item);});const head=node('div',undefined,'conversation-head');head.append(total,days);const track=node('div',undefined,'conversation-track');track.setAttribute('role','img');track.setAttribute('aria-label',`Вы: ${pct(d.own,d.own+d.peer)}, собеседник: ${pct(d.peer,d.own+d.peer)}`);const ownFill=node('span');ownFill.style.width=`${d.own+d.peer?100*d.own/(d.own+d.peer):0}%`;track.append(ownFill);hero.append(head,split,track);$('content').append(hero,coverage(d.coverage),activityChart(d.series,'По дням'));const last=node('div',undefined,'last-message');last.append(node('span',d.last?`Последним написал ${d.last.side==='o'?'вы':'собеседник'}`:'Нет полного контекста последнего сообщения'),node('strong',d.last?new Date(d.last.sent_at*1000).toLocaleString('ru-RU',{timeZone:'Europe/Moscow',day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'}):'—'));$('content').append(last);const notes=panel('Что запомнилось');notes.append(rows([['Самый активный день',d.records.day?`${date(d.records.day)}: ${fmt(d.records.day_messages)}`:'Нет данных'],['Дней подряд',fmt(d.records.streak)],['Ваша средняя длина',`${fmt(d.lengths.o.mean)} символов`],['Длина собеседника',`${fmt(d.lengths.p.mean)} символов`]]));$('content').append(notes);}
function detailActivity(d){
  const panels=['o','p'].map(side=>{
    const r=d.replies[side];
    const box=panel(side==='o'?'Как отвечаете вы':'Как отвечает собеседник');
    box.classList.add('reply-panel',side==='o'?'reply-own':'reply-peer');
    box.append(node('strong',duration(r.normal.median),'reply-time'),node('p','Обычное время ответа','reply-caption'));
    const more=node('details');more.append(node('summary','Все показатели ответов'));
    more.append(rows([['Среднее время',duration(r.normal.mean)],['Ответов до 4 часов',fmt(r.normal.count)],['Длинных пауз',fmt(r.long.count)],['Медиана длинной паузы',duration(r.long.median)],['Начатых разговоров',fmt(r.starts)]]));
    more.append(node('p','Считаем от последнего сообщения серии. Обычное время: медиана ответов в пределах четырёх часов.','note'));
    box.append(more);return box;
  });
  const pair=grid(...panels);pair.classList.add('equal');$('content').append(pair,coverage(d.coverage));
  const week=panel('Ритм недели','Сообщения в каждый день недели · подробная история');week.append(bars(d.weekdays,['Пн','Вт','Ср','Чт','Пт','Сб','Вс']));
  $('content').append(grid(week,hoursPanel(d.hours)));
  const comparison=panel('По сравнению с прошлым периодом','Равные интервалы до текущего времени');
  if(d.comparison?.available){const c=d.comparison;comparison.append(rows([['Раньше',fmt(c.previous)],['Сейчас',fmt(c.current)],['Изменение',c.percent==null?'База равна нулю':`${c.percent>0?'+':''}${c.percent}%`]]));}
  else comparison.append(node('p',state.period==='all'?'Выберите 7 или 30 дней для сравнения.':'Не хватает подробной истории за предыдущий период.','note'));
  $('content').append(comparison);
}
function detailEvents(d){const panels=['o','p'].map(side=>{const e=d.events[side],n=side==='o'?d.own:d.peer;const box=panel(side==='o'?'Ваши сообщения':'Сообщения собеседника','По дате отправки исходного сообщения');box.append(rows([['Удалено',`${fmt(e.deleted)} · ${pct(e.deleted,n)}`],['Сообщений с правками',fmt(e.edited)],['Всего правок',fmt(e.edits)],['До обнаружения удаления, среднее',duration(e.delay.mean)],['До обнаружения, медиана',duration(e.delay.median)]]));return box;});const pair=grid(...panels);pair.classList.add('equal');$('content').append(pair);const note=panel('Что означает удаление');note.append(node('p','Автор сообщения и тот, кто его удалил, могут быть разными людьми. Telegram не сообщает, кто нажал «Удалить». Время показано до обнаружения события ботом.','note'));if(d.coverage.unknown)note.append(node('p',`${fmt(d.coverage.unknown)} сообщений без исходного контекста. Такие события не распределяются по авторам.`,'note'));$('content').append(note);}
function sideSelector(){const box=node('div',undefined,'side-selector');[['both','Оба автора'],['o','Вы'],['p','Собеседник']].forEach(([key,label])=>{const b=button(label,()=>{state.side=key;state.wordPage=0;load();});b.setAttribute('aria-pressed',String(state.side===key));box.append(b);});return box;}
function detailContent(d){$('content').append(sideSelector());const media=panel('Медиа','Все типы на одном экране');const value=m=>state.side==='o'?m.own:state.side==='p'?m.peer:m.total;const selectedMedia=d.media.filter(m=>value(m)>0);const max=Math.max(1,...selectedMedia.map(value));selectedMedia.forEach(m=>{const row=node('div',undefined,'media-row');const label=node('div');label.append(node('span',m.name),node('strong',fmt(value(m))));const track=node('div',undefined,`media-track${state.side==='p'?' peer':''}`);const fill=node('span');fill.style.width=`${100*value(m)/max}%`;track.append(fill);row.append(label,track);media.append(row);});if(!selectedMedia.length)empty(media,'Пока нет медиа','За этот период не сохранены медиафайлы.');if(d.totals.complete_scope && state.side==='both')media.append(node('p','Общие итоги включают старую историю. Разделение по авторам доступно только для подробных данных.','note'));const lengths=panel('Голосовые и видеосообщения','Известная длительность · подробная история');const sides=state.side==='both'?['o','p']:[state.side];sides.forEach(s=>{lengths.append(node('h3',s==='o'?'Вы':'Собеседник'));lengths.append(rows([['Голосовые',duration(d.durations[s].voice.seconds)],['Видеосообщения',duration(d.durations[s].video_note.seconds)],['Без длительности',fmt(d.durations[s].voice.missing+d.durations[s].video_note.missing)]]));});$('content').append(grid(media,lengths));const words=panel('Частые слова',`${fmt(d.word_total)} слов · ${state.side==='both'?'оба автора':state.side==='o'?'ваши слова':'слова собеседника'} · ${d.words_basis==='retained'?'накопленные итоги':'подробная история'}`);const list=node('div',undefined,'word-list');d.words.forEach((w,i)=>{const row=node('div',undefined,'word-item');row.append(node('span',String(i+1),'word-rank'),node('span',w.word),node('strong',fmt(w.count),'word-count'));list.append(row);});words.append(list);if(!d.words.length)empty(words,'Пока нет слов','За этот период нет сохранённой статистики слов.');if(d.words.length<d.word_total){const more=button(`Показать ещё ${Math.min(50,d.word_total-d.words.length)}`,async()=>{more.disabled=true;more.textContent='Загружаем…';try{const selected=state.selected,sequence=state.sequence;const q=params();q.set('account',selected.account_id);q.set('side',state.side);q.set('section','words');q.set('page',state.wordPage+1);const next=await api(`/api/chats/${selected.chat_id}`,q,state.controller.signal);if(sequence!==state.sequence)return;state.wordPage++;state.detail.words.push(...next.words);render();}catch(e){if(e.name!=='AbortError'){status(e.message,true);more.disabled=false;more.textContent='Повторить загрузку';}}});words.append(node('p',`Показано ${fmt(d.words.length)} из ${fmt(d.word_total)}`,'note'),more);}$('content').append(words);}
const reducedMotion=window.matchMedia('(prefers-reduced-motion: reduce)');
let motionView=null;
const motionAnimations=new Set();
function stopMotion(){motionAnimations.forEach(a=>a.cancel());motionAnimations.clear();}
reducedMotion.addEventListener('change',()=>{if(reducedMotion.matches)stopMotion();});
function animateUI(element,frames,duration=220,delay=0){
  if(!element || reducedMotion.matches || !element.animate)return;
  const animation=element.animate(frames,{duration,delay,easing:'cubic-bezier(.2,.8,.2,1)'});
  motionAnimations.add(animation);
  animation.finished.then(()=>motionAnimations.delete(animation),()=>motionAnimations.delete(animation));
}
function render(){
  stopMotion();
  const current={screen:state.screen,chat:state.selected?`${state.selected.account_id}:${state.selected.chat_id}`:null,section:state.section};
  $('content').replaceChildren();
  if(state.selected){chatDetail();chatDetailContext();}else({overview,chats,mine,rankings,info})[state.screen]();
  // Animate the screen once; keep the sticky tabs and contact header still.
  const body=node('div',undefined,'view-body');
  Array.from($('content').children).filter(el=>!el.classList.contains('detail-navigation')).forEach(el=>body.append(el));
  $('content').append(body);
  if(motionView){
    let x=0,y=0;
    if(current.chat && current.chat===motionView.chat && current.section!==motionView.section){
      const order=Object.keys(sections);x=order.indexOf(current.section)>order.indexOf(motionView.section)?10:-10;
    }else if(current.chat!==motionView.chat)x=current.chat?12:-12;
    else if(current.screen!==motionView.screen)y=6;
    animateUI(body,[{opacity:.45,transform:`translate(${x}px,${y}px)`},{opacity:1,transform:'translate(0,0)'}],200);
    animateUI(body.querySelector('.chart-svg'),[{opacity:.25},{opacity:1}],240);
    animateUI(body.querySelector('.conversation-track span'),[{transform:'scaleX(.9)'},{transform:'scaleX(1)'}],260);
    if(current.section!==motionView.section)animateUI($('content').querySelector('.detail-navigation [aria-pressed=true]'),[{transform:'scale(.96)'},{transform:'scale(1)'}],220);
    if(current.screen!==motionView.screen)animateUI(document.querySelector('#navigation [aria-current=page] .ui-icon'),[{transform:'scale(.9)'},{transform:'scale(1)'}],220);
  }
  motionView=current;observeAvatars();
}

let pinned=[];
try{pinned=JSON.parse(localStorage.getItem('nyan-pins')||'[]');if(!Array.isArray(pinned))pinned=[];}catch{}
function pinKey(chat){return `${chat.account_id}:${chat.chat_id}`;}
function isPinned(chat){return pinned.includes(pinKey(chat));}
function togglePin(chat){const key=pinKey(chat);pinned=isPinned(chat)?pinned.filter(k=>k!==key):[...pinned,key];try{localStorage.setItem('nyan-pins',JSON.stringify(pinned));}catch{}}
function chatDetailContext(){if(!$('action-sheet').open)return;const b=$('contact-actions').querySelector('.pin-button');if(b){b.textContent=isPinned(state.selected)?'Открепить чат':'Закрепить чат';b.setAttribute('aria-pressed',String(isPinned(state.selected)));}}
function exportChat(d){const quote=value=>{let content=String(value??'');if(typeof value==='string' && /^[=+@\-\t\r]/.test(content))content="'"+content;return '"'+content.replaceAll('"','""')+'"';};const data=[['Nyan message',d.name],['Период',state.period],['Основа итога',d.totals.basis==='retained'?'Накопленные итоги':'Подробная история'],['Всего сообщений',d.own+d.peer],['От вас',d.own],['От собеседника',d.peer],['Подробно учтено',d.coverage.detailed],['Ряд по дням','Доступная подробная история, не более 90 дней'],[],['Дата','От вас','От собеседника']];d.series.forEach(day=>data.push([day.day,day.own??'нет данных',day.peer??'нет данных']));const blob=new Blob(['\uFEFF'+data.map(row=>row.map(quote).join(';')).join('\r\n')],{type:'text/csv;charset=utf-8'});const url=URL.createObjectURL(blob);const link=node('a');link.href=url;link.download=`nyan-message-${state.selected.chat_id}-${state.period}.csv`;document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);}


function icon(name){const svg=svgEl('svg',{viewBox:'0 0 256 256',class:'ui-icon','aria-hidden':'true'});const use=svgEl('use',{href:`/icons.svg#${name}`});svg.append(use);return svg;}
function iconButton(name,label,action,cls=''){const b=button('',action,`icon-button ${cls}`);b.setAttribute('aria-label',label);b.append(icon(name));return b;}
function showSheet(id){$(id).showModal();document.body.classList.add('sheet-open');tg?.BackButton.show();}
function closeSheets(){document.querySelectorAll('dialog[open]').forEach(d=>d.close());}
function openActions(){const area=$('contact-actions');area.hidden=!state.selected;area.replaceChildren();$('sheet-title').textContent=state.selected?'Этот чат':'Nyan message';if(state.selected){const pin=button(isPinned(state.selected)?'Открепить чат':'Закрепить чат',()=>{togglePin(state.selected);closeSheets();},'pin-button');pin.setAttribute('aria-pressed',String(isPinned(state.selected)));const csv=button('Скачать CSV',()=>{exportChat(state.detail);closeSheets();},'export-button');csv.disabled=!state.detail;area.append(pin,csv);}showSheet('action-sheet');}
// Avatar requests carry Telegram auth in a header; tokens never enter image URLs.
const avatarMemory=new Map(),avatarJobs=new Map();
let avatarQueue=[],avatarActive=0;
function avatarKey(el){return `${el.dataset.account}:${el.dataset.chat}`;}
function avatarElement(chat,index=0,extra=''){
  const el=node('span',Array.from(chatName(chat).replace(/^@/,''))[0]?.toUpperCase()||'?',`avatar tone-${index%4} ${extra}`);
  el.dataset.account=chat.account_id;el.dataset.chat=chat.chat_id;return el;
}
function paintAvatar(el,blob){
  if(!el.isConnected || !blob)return;
  const url=URL.createObjectURL(blob),img=new Image();img.alt='';img.width=48;img.height=48;
  img.onload=()=>{URL.revokeObjectURL(url);if(el.isConnected){el.replaceChildren(img);el.classList.add('has-photo');}};
  img.onerror=()=>URL.revokeObjectURL(url);img.src=url;
}
function avatarVisible(el){const r=el.getBoundingClientRect();return el.isConnected && r.width>0 && r.height>0 && r.bottom>0 && r.top<innerHeight;}
const avatarObserver=new IntersectionObserver(entries=>{
  entries.forEach(entry=>{if(entry.isIntersecting){queueAvatar(entry.target);avatarObserver.unobserve(entry.target);}});
},{threshold:.01});
function observeAvatars(){
  avatarObserver.disconnect();
  document.querySelectorAll('.avatar[data-chat]:not(.has-photo)').forEach(el=>avatarObserver.observe(el));
  // Work for screens that have gone away must not occupy the two browser slots.
  avatarJobs.forEach(job=>{if(!Array.from(job.targets).some(el=>el.isConnected))job.controller.abort();});
}
function queueAvatar(el){
  const key=avatarKey(el),cached=avatarMemory.get(key);
  if(cached && cached.until>Date.now()){paintAvatar(el,cached.blob);return;}
  let job=avatarJobs.get(key);
  if(job?.controller.signal.aborted){avatarJobs.delete(key);job=null;}
  if(job){job.targets.add(el);return;}
  job={key,account:el.dataset.account,chat:el.dataset.chat,targets:new Set([el]),controller:new AbortController()};
  avatarJobs.set(key,job);avatarQueue.push(job);pumpAvatars();
}
function avatarPause(ms,signal){return new Promise((resolve,reject)=>{
  if(signal.aborted){reject(new DOMException('Aborted','AbortError'));return;}
  const stop=()=>{clearTimeout(timer);reject(new DOMException('Aborted','AbortError'));};
  const timer=setTimeout(()=>{signal.removeEventListener('abort',stop);resolve();},ms);
  signal.addEventListener('abort',stop,{once:true});
});}
async function downloadAvatar(job){
  const signal=job.controller.signal;
  for(let attempt=0;attempt<12;attempt++){
    if(!Array.from(job.targets).some(avatarVisible))return;
    const response=await fetch(`/api/avatars/${job.chat}?account=${job.account}`,{
      headers:{Authorization:`tma ${tg?.initData || ''}`},cache:'no-store',signal});
    if(response.status===200 && /^image\/(jpeg|png|webp)(;|$)/.test(response.headers.get('content-type')||'')){
      const blob=await response.blob();if(blob.size>128*1024)return;
      avatarMemory.delete(job.key);avatarMemory.set(job.key,{blob,until:Date.now()+15*60*1000});
      job.targets.forEach(el=>paintAvatar(el,blob));break;
    }
    if(response.status===204){avatarMemory.set(job.key,{blob:null,until:Date.now()+15*60*1000});break;}
    if(response.status!==202 && response.status!==429)return;
    const delay=Math.min(10,Math.max(3,Number(response.headers.get('retry-after'))||3));
    await avatarPause(delay*1000,signal);
  }
  while(avatarMemory.size>80)avatarMemory.delete(avatarMemory.keys().next().value);
}
function pumpAvatars(){
  while(avatarActive<2 && avatarQueue.length){
    const job=avatarQueue.shift();
    if(job.controller.signal.aborted || !Array.from(job.targets).some(avatarVisible)){
      job.targets.forEach(el=>{if(el.isConnected && !avatarVisible(el))avatarObserver.observe(el);});
      if(avatarJobs.get(job.key)===job)avatarJobs.delete(job.key);continue;
    }
    avatarActive++;
    downloadAvatar(job).catch(()=>{}).finally(()=>{
      job.targets.forEach(el=>{if(el.isConnected && !el.classList.contains('has-photo') && !avatarVisible(el))avatarObserver.observe(el);});
      avatarActive--;if(avatarJobs.get(job.key)===job)avatarJobs.delete(job.key);pumpAvatars();
    });
  }
}

const periodLabels={today:'Сегодня','7':'7 дней','30':'30 дней',all:'Всё время'};
function choiceSheet(kind){closeSheets();const isPeriod=kind==='period';const select=$(isPeriod?'period-picker':'account');const container=$(isPeriod?'period-options':'account-options');container.replaceChildren();container.setAttribute('role','group');container.setAttribute('aria-label',isPeriod?'Период':'Аккаунт');Array.from(select.options).forEach(option=>{const b=button('',()=>{select.value=option.value;select.dispatchEvent(new Event('change'));});b.className='choice-option';b.setAttribute('aria-pressed',String(option.selected));const copy=node('span');copy.append(node('strong',option.textContent));if(isPeriod)copy.append(node('small',option.value==='all'?'Вся сохранённая история':option.value==='today'?'С начала дня, включая сейчас':`Последние ${option.value} календарных дней`));b.append(copy,icon(option.selected?'check':'caret-right'));container.append(b);});showSheet(isPeriod?'period-sheet':'account-sheet');}
function setupMobile(){new ResizeObserver(entries=>{const height=entries[0].contentRect.height;if(height>0)document.documentElement.style.setProperty('--contact-header-height',height+'px');}).observe($('chat-context'));[['#navigation [data-screen=chats]','chats','Чаты'],['#navigation [data-screen=overview]','chart-bar','Сводка'],['#more-tab','dots-three','Ещё']].forEach(([selector,name,label])=>{document.querySelector(selector).replaceChildren(icon(name),node('span',label));});$('app-menu').replaceChildren(icon('dots-three'));$('app-menu').onclick=openActions;$('more-tab').onclick=openActions;$('account-scope').onclick=()=>choiceSheet('account');$('period-scope').onclick=()=>choiceSheet('period');$('period-picker').onchange=()=>{closeSheets();state.period=$('period-picker').value;state.wordPage=0;state.overview=null;load();};document.querySelectorAll('.sheet-close').forEach(b=>{b.replaceChildren(icon('x'));b.onclick=()=>b.closest('dialog').close();});document.querySelectorAll('dialog').forEach(d=>d.addEventListener('close',()=>{if(!document.querySelector('dialog[open]')){document.body.classList.remove('sheet-open');tg?.BackButton[state.selected?'show':'hide']();}}));document.querySelectorAll('dialog').forEach(d=>d.addEventListener('click',e=>{const r=d.getBoundingClientRect();if(e.target===d && (e.clientY<r.top || e.clientX<r.left || e.clientX>r.right))d.close();}));}
setupMobile();

route();

let backgroundAt=0;document.addEventListener('visibilitychange',()=>{if(document.hidden){backgroundAt=Date.now();return;}if(Date.now()-backgroundAt>20000){state.overviewAt=0;load();}});
for(const [id,command] of [['bot-notifications','notifications'],['bot-connection','connect'],['bot-data','data'],['bot-help','help']]){const el=$(id);if(el)el.onclick=()=>{closeSheets();botAction(command);};}
