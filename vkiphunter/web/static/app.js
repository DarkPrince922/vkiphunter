'use strict';
const $ = (id) => document.getElementById(id);
let csrf = '', current = null, settingsLoaded = false, pollTimer = null;
const labels = {idle:'Остановлен',running:'Поиск идёт',stopping:'Завершаем попытку',match:'Найден',reject:'Не подходит',pool_empty:'Пул пуст',error:'Ошибка',quota:'Квота'};
const date = (ts) => ts ? new Date(ts * 1000).toLocaleString('ru-RU') : '—';
function notify(message, error=false) { $('notice').textContent=message; $('notice').classList.toggle('error',error); $('notice').hidden=false; }
function showLogin() { clearTimeout(pollTimer); $('app-view').hidden=true; $('login-view').hidden=false; csrf=''; }
async function api(path, data) {
  const response=await fetch('/api/'+path,{method:data===undefined?'GET':'POST',credentials:'same-origin',headers:data===undefined?{}:{'Content-Type':'application/json','X-VKH-Request':'1','X-CSRF-Token':csrf},body:data===undefined?undefined:JSON.stringify(data)});
  const result=await response.json();
  if (!response.ok) { if(response.status===401 && path!=='login') showLogin(); throw new Error(result.error || `HTTP ${response.status}`); }
  return result;
}
function table(container, columns, rows) {
  container.replaceChildren();
  if(!rows.length) { const p=document.createElement('p'); p.className='muted'; p.textContent='Пока нет данных.'; container.append(p); return; }
  const t=document.createElement('table'),head=t.createTHead().insertRow();
  columns.forEach(([title])=>{const th=document.createElement('th'); th.textContent=title; head.append(th);});
  const body=t.createTBody();
  rows.forEach(item=>{const tr=body.insertRow(); columns.forEach(([,value])=>{const cell=tr.insertCell(); const v=typeof value==='function'?value(item):item[value]; cell.textContent=v==null?'—':(typeof v==='object'?JSON.stringify(v):String(v));});});
  container.append(t);
}
function options(select, items, text, value) {
  const old=select.value; select.replaceChildren();
  for(const item of items) { const option=document.createElement('option'); option.value=value(item); option.textContent=text(item); select.append(option); }
  if([...select.options].some(o=>o.value===old)) select.value=old;
}
function fillSettings(settings) {
  const form=$('settings-form');
  for(const [key,value] of Object.entries(settings)) {
    if(key==='subnets') continue;
    if(form.elements[key]) form.elements[key].value=Array.isArray(value)?value.join('\n'):value;
  }
  $('selected-subnets').textContent='Сохранённый выбор: '+(settings.subnets.map(s=>s.cidr||s.id).join(', ')||'любая подсеть');
  $('subnet-options').replaceChildren();
  delete $('subnet-options').dataset.loaded;
  settingsLoaded=true;
}
function render(data) {
  current=data; csrf=data.csrf;
  $('login-view').hidden=true; $('app-view').hidden=false;
  $('status').textContent=labels[data.status]||data.status; $('status').classList.toggle('running',data.running);
  $('account').textContent=data.account || data.cloud;
  $('connection-label').textContent=data.connected?'Облако подключено':'Облако не подключено';
  $('cloud-description').textContent=data.cloud;
  ['attempts','matches','pool_empty'].forEach(k=>$('stat-'+k).textContent=data.stats[k]);
  $('stat-errors').textContent=`${data.stats.errors} / ${data.stats.quota}`;
  $('next-attempt').textContent=data.next_attempt_at?'Следующая попытка: '+date(data.next_attempt_at):`Попыток в текущем запуске: ${data.current_attempt}`;
  if(data.last_error) $('next-attempt').textContent+=' · '+data.last_error;
  $('start').disabled=data.running||!data.connected||data.busy;
  $('stop').disabled=!data.running||data.busy||data.status==='stopping';
  $('settings-fields').disabled=data.running||data.busy;
  $('cloud-form').querySelector('button[type=submit]').disabled=data.running||data.busy;
  $('disconnect').disabled=data.running||data.busy;
  $('cleanup').disabled=data.running||data.busy;
  $('vm-form').querySelector('button[type=submit]').disabled=data.running||data.busy;
  $('attach-form').querySelector('button[type=submit]').disabled=data.running||data.busy;
  if(!settingsLoaded) fillSettings(data.settings);
  table($('found-table'),[['Адрес','ip'],['Тип',x=>x.kind==='port'?'Порт':'Floating IP'],['Диапазон','matched_cidr'],['Ресурс','port_id'],['Найден',x=>date(x.found_at)],['ВМ',x=>x.server_name||x.server_id||'Не подключён']],data.found.slice().reverse());
  table($('history-table'),[['Время',x=>date(x.ts)],['IP','ip'],['Результат',x=>labels[x.outcome]||x.outcome],['Подсеть','subnet'],['Причина','detail']],data.attempts.slice().reverse());
  table($('seen-table'),[['IP','ip'],['Число выдач','count']],Object.entries(data.stats.seen_ips).map(([ip,count])=>({ip,count})).sort((a,b)=>b.count-a.count));
  const free=data.found.filter(f=>!f.server_id);
  options($('vm-port'),free.filter(f=>f.kind==='port'),f=>`${f.ip} · ${f.port_id}`,f=>f.port_id);
  options($('attach-ip'),free.filter(f=>f.kind==='floatingip'),f=>`${f.ip} · ${f.port_id}`,f=>f.port_id);
}
async function refresh() { render(await api('state')); }
async function poll() {
  clearTimeout(pollTimer);
  try { await refresh(); } catch(e) { if(!$('app-view').hidden) notify('Не удалось обновить статус: '+e.message,true); }
  if(!$('app-view').hidden) pollTimer=setTimeout(poll,3000);
}
async function action(fn) { try { await fn(); } catch(e) { notify(e.message,true); } }
function busyButton(button, fn) { return action(async()=>{button.disabled=true; try { await fn(); } finally { button.disabled=false; }}); }
$('login-form').addEventListener('submit',async e=>{e.preventDefault(); const button=e.submitter; button.disabled=true; $('login-error').textContent=''; try { const result=await api('login',{password:e.target.elements.password.value}); csrf=result.csrf; e.target.reset(); settingsLoaded=false; await poll(); } catch(err) { $('login-error').textContent=err.message; } finally { button.disabled=false; }});
$('logout').addEventListener('click',()=>action(async()=>{await api('logout',{}); showLogin();}));
document.querySelectorAll('[data-tab]').forEach(b=>b.addEventListener('click',()=>{document.querySelectorAll('.tab').forEach(x=>x.classList.toggle('active',x===b)); document.querySelectorAll('.tab-panel').forEach(x=>x.hidden=x.id!=='tab-'+b.dataset.tab); }));
$('start').addEventListener('click',e=>busyButton(e.target,async()=>{await api('hunt/start',{}); notify('Поиск запущен. Статус обновляется автоматически.'); await refresh();}));
$('stop').addEventListener('click',e=>busyButton(e.target,async()=>{await api('hunt/stop',{}); notify('Останавливаем поиск после текущей попытки.'); await refresh();}));
$('settings-form').addEventListener('submit',e=>{e.preventDefault(); busyButton(e.submitter,async()=>{
  const form=e.target, data={};
  for(const [key,value] of new FormData(form)) data[key]=value;
  ['interval','workers','max_attempts','port_timeout','server_timeout','volume_size','report_every'].forEach(k=>data[k]=Number(data[k]));
  ['targets','excludes'].forEach(k=>data[k]=data[k].split(/[\s,;]+/).filter(Boolean));
  data.subnets=current.settings.subnets;
  if($('subnet-options').dataset.loaded==='1') data.subnets=[...$('subnet-options').querySelectorAll('input:checked')].map(i=>({id:i.value,cidr:i.dataset.cidr}));
  await api('settings',data); settingsLoaded=false; delete $('subnet-options').dataset.loaded; await refresh(); notify('Настройки сохранены.');
});});
$('load-subnets').addEventListener('click',e=>busyButton(e.target,async()=>{
  if($('settings-form').elements.network.value!==current.settings.network) throw new Error('Сначала сохраните новое имя сети, затем загрузите её подсети.');
  const subnets=await api('cloud/subnets'); let availability=[];
  try { availability=await api('cloud/availability'); } catch(err) { notify('Подсети загружены, но доступность пулов не получена: '+err.message,true); }
  const all=new Map(subnets.map(s=>[s.id,s])); availability.forEach(a=>{if(!all.has(a.subnet_id)) all.set(a.subnet_id,{id:a.subnet_id,name:a.name,cidr:a.cidr});});
  // Preserve selected pools hidden by cloud policy as well.
  current.settings.subnets.forEach(s=>{if(!all.has(s.id)) all.set(s.id,{...s,name:'сохранённая подсеть'});});
  $('subnet-options').replaceChildren(); $('subnet-options').dataset.loaded='1';
  for(const s of all.values()) { const label=document.createElement('label'), input=document.createElement('input'), text=document.createElement('span'); input.type='checkbox'; input.value=s.id; input.dataset.cidr=s.cidr; input.checked=current.settings.subnets.some(v=>v.id===s.id); const a=availability.find(x=>x.subnet_id===s.id); text.textContent=`${s.cidr} · ${s.name||s.id}${a?' · свободно '+a.free+' из '+a.total:''}`; label.append(input,text); $('subnet-options').append(label); }
}));
function cloudMethod() { const method=$('cloud-form').elements.method.value; $('real-cloud-fields').hidden=method==='simulate'; document.querySelectorAll('[data-method]').forEach(el=>el.hidden=el.dataset.method!==method); }
$('cloud-form').elements.method.addEventListener('change',cloudMethod);
$('cloud-form').addEventListener('submit',e=>{e.preventDefault(); busyButton(e.submitter,async()=>{
  const form=e.target, method=form.elements.method.value, data={method};
  if(method!=='simulate') {
    for(const name of ['openrc','auth_url','region','username','password','project_id','project_name','user_domain_name','app_cred_id','app_cred_secret']) data[name]=form.elements[name].value;
  }
  await api('connect',data); form.reset(); cloudMethod(); await refresh(); notify('Подключение проверено и сохранено.');
});});
$('disconnect').addEventListener('click',e=>busyButton(e.target,async()=>{if(!confirm('Забыть сохранённые доступы к облаку? Ресурсы останутся в проекте.')) return; await api('disconnect',{confirm:true}); await refresh(); notify('Сохранённые доступы удалены.');}));
const resourceNames={id:'ID',name:'Название',ip:'IP',cidr:'CIDR',free:'Свободно',total:'Всего',used:'Занято',status:'Статус',device_id:'Подключено к',description:'Описание',external:'Внешняя',project_name:'Проект',region:'Регион'};
function renderResources(data) { const rows=Array.isArray(data)?data:[data]; const keys=[...new Set(rows.flatMap(x=>Object.keys(x)))]; table($('resource-output'),keys.map(k=>[resourceNames[k]||k,k]),rows); }
document.querySelectorAll('[data-load]').forEach(b=>b.addEventListener('click',()=>busyButton(b,async()=>renderResources(await api('cloud/'+b.dataset.load)))));
$('cleanup').addEventListener('click',e=>busyButton(e.target,async()=>{if(!confirm('Освободить свои неподключённые пробные ресурсы по текущему префиксу? Найденные будут сохранены.')) return; const result=await api('cleanup',{confirm:true}); notify(`Удалено: ${result.deleted.length}. Не удалось удалить: ${result.failed.length}.`,result.failed.length>0); }));
$('load-logs').addEventListener('click',e=>busyButton(e.target,async()=>{$('logs').textContent=(await api('logs')).text||'Журнал пока пуст.';}));
$('load-catalog').addEventListener('click',e=>busyButton(e.target,async()=>{
  const [flavors,images,keys]=await Promise.all([api('cloud/flavors'),api('cloud/images'),api('cloud/keypairs')]);
  options($('vm-flavor'),flavors,x=>`${x.name} · ${x.vcpus} vCPU · ${x.ram} МБ`,x=>x.id);
  options($('vm-image'),images,x=>x.name,x=>x.id);
  options($('vm-key'),['',...keys],x=>x||'Без ключа',x=>x); notify('Каталог ВМ загружен.');
}));
$('load-servers').addEventListener('click',e=>busyButton(e.target,async()=>{options($('attach-server'),await api('cloud/servers'),x=>`${x.name} · ${x.status}`,x=>x.id); notify('Список ВМ загружен.');}));
$('vm-form').addEventListener('submit',e=>{e.preventDefault(); busyButton(e.submitter,async()=>{
  const data=Object.fromEntries(new FormData(e.target));
  if(!confirm(`Создать платную ВМ «${data.name}» на выбранном порту?\nФлейвор: ${$('vm-flavor').selectedOptions[0]?.textContent}\nОбраз: ${$('vm-image').selectedOptions[0]?.textContent}\nДиск: ${current.settings.volume_size} ГБ`)) return;
  notify('Создаём ВМ. Дождитесь результата; операция может занять до 30 минут.');
  try { const result=await api('vm',{...data,confirm:true}); await refresh(); notify(`ВМ создана: ${result.name} · ${result.status}`); }
  catch(err) { throw new Error(err.message+' Порт сохранён. Перед повтором проверьте список ВМ в облаке: создание могло завершиться после тайм-аута.'); }
});});
$('attach-form').addEventListener('submit',e=>{e.preventDefault(); busyButton(e.submitter,async()=>{const data=Object.fromEntries(new FormData(e.target)); if(!confirm('Привязать выбранный Floating IP к выбранной ВМ?')) return; await api('attach',{...data,confirm:true}); await refresh(); notify('Адрес привязан.');});});
poll();
