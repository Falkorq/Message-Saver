/* Import an explicit user-selected JSON file; filenames and text never enter HTML. */
(() => {
  const dialog=$('import-sheet'), form=$('import-form'), file=$('import-file'), account=$('import-account');
  const progress=$('import-progress'), preview=$('import-preview'), error=$('import-error');
  let job=null, busy=false, timer=null;
  const importDate=value=>new Date(value*1000).toLocaleDateString('ru-RU',{timeZone:'Europe/Moscow',day:'numeric',month:'short',year:'numeric'});
  function message(text){error.textContent=text || '';error.hidden=!text;}
  async function request(path,method='GET'){
    const deadline=requestDeadline(null,20000);
    try{
      const response=await fetch(path,{method,headers:{Authorization:`tma ${tg?.initData || ''}`},cache:'no-store',signal:deadline.signal});
      let data;try{data=await response.json();}catch{throw Error('Сервис временно недоступен. Повторите позже.');}
      if(!response.ok)throw Error(data.error || 'Не удалось выполнить запрос.');
      return data;
    }catch(e){if(e.name==='AbortError')throw Error('Ответ задерживается. Откройте импорт ещё раз, чтобы проверить состояние.');throw e;}
    finally{deadline.clear();}
  }
  function paint(){
    clearTimeout(timer);
    const active=job && ['reading','preview','importing'].includes(job.state);
    form.hidden=Boolean(job);
    $('import-start').hidden=job?.state!=='preview';
    $('import-cancel').hidden=!active;
    $('import-cancel').textContent=job?.state==='importing'?'Остановить импорт':'Отменить';
    $('import-result').hidden=!job || !['done','stopped','error'].includes(job.state) || !job.preview;
    $('import-another').hidden=!job || active;
    $('import-upload').disabled=busy || !file.files.length;
    preview.replaceChildren();
    if(!job){progress.textContent='';return;}
    const p=job.preview;
    if(p){
      preview.append(node('h3',p.name),rows([['Сообщений',fmt(p.messages)],['От вас',fmt(p.own)],['От собеседника',fmt(p.peer)],['Период',`${importDate(p.first_at)} — ${importDate(p.last_at)}`]]));
      if(p.service)preview.append(node('p',`${fmt(p.service)} служебных записей не входят в число сообщений.`,'note'));
    }
    if(job.state==='reading')progress.textContent='Проверяем экспорт…';
    if(job.state==='preview')progress.textContent='Файл проверен. Подтвердите добавление в статистику.';
    if(job.state==='importing')progress.textContent=`Обработано ${fmt(job.processed)} из ${fmt(p?.messages)} · добавлено ${fmt(job.added)}`;
    if(job.state==='done')progress.textContent=`Готово. Добавлено ${fmt(job.added)}, уже были учтены: ${fmt(job.skipped)}.`;
    if(['stopped','error'].includes(job.state)){progress.textContent=`Импорт остановлен. Добавлено ${fmt(job.added)}.`;message(job.error);}
    if(dialog.open && ['reading','importing'].includes(job.state))timer=setTimeout(poll,3500);
  }
  async function poll(){
    if(!dialog.open)return;
    try{const next=await request('/api/import');job=next.id?next:null;message('');paint();}
    catch(e){message(e.message);if(dialog.open)timer=setTimeout(poll,10000);}
  }
  $('import-open').onclick=async()=>{
    closeSheets();showSheet('import-sheet');message('');
    const accounts=state.overview?.accounts || [];
    account.replaceChildren(...accounts.map(a=>{const o=node('option',a.name);o.value=a.id;return o;}));
    const selected=$('account').value;if(selected)account.value=selected;
    if(!accounts.length){message('Сначала дождитесь загрузки статистики и списка аккаунтов.');$('import-upload').disabled=true;return;}
    if(!busy)await poll();
  };
  file.onchange=()=>{message('');$('import-upload').disabled=busy || !file.files.length;};
  $('import-upload').onclick=()=>{
    const selected=file.files[0];
    if(!selected || busy)return;
    if(!/\.json$/i.test(selected.name)){message('Выберите result.json из экспорта одного чата.');return;}
    if(selected.size>128*1024*1024){message('Максимальный размер файла — 128 МБ. Экспортируйте один чат без медиа.');return;}
    if(!account.value){message('Выберите свой аккаунт.');return;}
    busy=true;message('');$('import-upload').disabled=true;progress.textContent='Загружаем файл…';
    const xhr=new XMLHttpRequest();xhr.open('POST',`/api/import/preview?account=${encodeURIComponent(account.value)}`);
    xhr.setRequestHeader('Authorization',`tma ${tg?.initData || ''}`);xhr.timeout=180000;
    xhr.upload.onprogress=e=>{progress.textContent=e.lengthComputable?(e.loaded===e.total?'Проверяем экспорт…':`Загружаем файл: ${Math.round(100*e.loaded/e.total)}%`):'Загружаем файл…';};
    const fail=text=>{busy=false;job=null;paint();message(text);};
    xhr.onload=()=>{
      let data;try{data=JSON.parse(xhr.responseText);}catch{fail('Сервис не принял файл. Проверьте соединение и попробуйте ещё раз.');return;}
      if(xhr.status<200 || xhr.status>=300){fail(data.error || 'Не удалось проверить экспорт.');return;}
      busy=false;job=data;paint();
    };
    xhr.onerror=()=>fail('Не удалось загрузить файл. Проверьте соединение и откройте импорт ещё раз.');
    xhr.ontimeout=()=>fail('Загрузка заняла слишком долго. Откройте импорт заново, чтобы проверить состояние.');
    xhr.send(selected);
  };
  $('import-start').onclick=async()=>{
    if(!job)return;
    $('import-start').disabled=true;message('');
    try{job=await request(`/api/import/${encodeURIComponent(job.id)}/start`,'POST');paint();}
    catch(e){message(e.message);}finally{$('import-start').disabled=false;}
  };
  async function cancel(){
    if(!job)return;
    message('');
    try{
      await request(`/api/import/${encodeURIComponent(job.id)}`,'DELETE');
      if(job.state==='importing'){progress.textContent='Останавливаем импорт…';timer=setTimeout(poll,1000);}
      else{job=null;file.value='';paint();}
    }catch(e){message(e.message);}
  }
  $('import-cancel').onclick=cancel;$('import-another').onclick=cancel;
  $('import-result').onclick=()=>{
    if(!job?.preview)return;
    state.period='all';state.overview=null;state.overviewAt=0;$('account').value='';
    closeSheets();navigate('chats',{account_id:0,chat_id:job.preview.chat_id});
  };
  dialog.addEventListener('close',()=>clearTimeout(timer));
})();
