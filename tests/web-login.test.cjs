'use strict';
// Exercise the shipped page and script together, including races between HTTP replies.
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {JSDOM,VirtualConsole}=require('jsdom');
const root=path.join(__dirname,'../vkiphunter/web/static');
const html=fs.readFileSync(path.join(root,'index.html'),'utf8');
const script=fs.readFileSync(path.join(root,'app.js'),'utf8');
const PASSWORD='test-password-that-must-not-enter-the-url';
const state=()=>({
  csrf:'test-csrf',status:'idle',running:false,connected:false,busy:false,cloud:'не подключено',
  stats:{attempts:0,matches:0,pool_empty:0,errors:0,quota:0,seen_ips:{}},
  attempts:[],found:[],current_attempt:0,
  settings:{mode:'port',network:'internet',prefix:'test-probe',interval:90,workers:1,
    max_attempts:0,port_timeout:120,server_timeout:1800,volume_size:10,volume_type:'',
    report_every:20,targets:['1.2.3.0/24'],excludes:[],subnets:[]},
});
const reply=(status,data)=>({status,ok:status>=200&&status<300,json:async()=>structuredClone(data)});
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
async function until(condition) {
  for(let n=0;n<300;n++){if(condition())return;await pause(5);}
  throw Error('Timed out waiting for '+condition.toString());
}
function deferred(){let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve};}
function mount(t,respond,options={}) {
  const errors=[], calls=[], pendingPolls=new Map();
  const virtualConsole=new VirtualConsole();
  virtualConsole.on('jsdomError',error=>errors.push(error.message));
  const dom=new JSDOM(html,{url:options.url||'http://panel.example/',runScripts:'outside-only',virtualConsole});
  const w=dom.window,d=w.document;
  const originalTimeout=w.setTimeout.bind(w), originalClear=w.clearTimeout.bind(w);
  let pollId=-1;
  w.setTimeout=(fn,ms,...args)=>{
    if(ms===3000){const id=pollId--;pendingPolls.set(id,()=>fn(...args));return id;}
    return originalTimeout(fn,options.fastTimeout&&ms===15000?10:ms,...args);
  };
  w.clearTimeout=id=>{if(id<0)pendingPolls.delete(id);else originalClear(id);};
  w.fetch=(url,init)=>{
    calls.push({url,init});
    const result=Promise.resolve().then(()=>respond(url,init,calls));
    if(options.ignoreAbort)return result;
    return new Promise((resolve,reject)=>{
      const abort=()=>reject(new w.DOMException('Aborted','AbortError'));
      init.signal.addEventListener('abort',abort,{once:true});
      if(init.signal.aborted)abort();
      result.then(resolve,reject).finally(()=>init.signal.removeEventListener('abort',abort));
    });
  };
  w.confirm=()=>true;
  w.eval(script);
  t.after(()=>{w.close();assert.deepEqual(errors,[]);});
  return {
    w,d,calls,pendingPolls,
    submit(submitter=true){
      const form=d.getElementById('login-form');form.elements.password.value=PASSWORD;
      const event=new w.SubmitEvent('submit',{bubbles:true,cancelable:true,
        submitter:submitter?form.querySelector('button[type=submit]'):null});
      assert.equal(form.dispatchEvent(event),false);
    },
    async poll(){const [id,fn]=pendingPolls.entries().next().value;pendingPolls.delete(id);await fn();},
  };
}
const ready=page=>until(()=>page.d.getElementById('login-status').textContent==='');
const visibleApp=page=>!page.d.getElementById('app-view').hidden;
const loginError=page=>page.d.getElementById('login-error').textContent;


test('login verifies the session, restores after reload, and logs out',async t=>{
  let signedIn=false;
  const respond=(url)=>{
    if(url==='/api/login'){signedIn=true;return reply(200,{csrf:'test-csrf'});}
    if(url==='/api/logout'){signedIn=false;return reply(200,{ok:true});}
    return signedIn?reply(200,state()):reply(401,{error:'Войдите в панель'});
  };
  const page=mount(t,respond);await ready(page);
  assert.equal(page.d.getElementById('login-form').method,'post');
  assert.equal(new URL(page.d.getElementById('login-form').action).pathname,'/login');
  page.submit();await until(()=>visibleApp(page));
  await until(()=>!page.d.querySelector('#login-form button').disabled);
  assert.equal(page.d.querySelector('#login-form input').value,'');
  const request=page.calls.find(c=>c.url==='/api/login');
  assert.equal(request.init.method,'POST');
  assert.equal(request.init.headers['X-VKH-Request'],'1');
  assert.equal(JSON.parse(request.init.body).password,PASSWORD);
  assert.ok(page.calls.every(c=>!c.url.includes(PASSWORD)));
  const reloaded=mount(t,respond);await until(()=>visibleApp(reloaded));
  assert.equal(reloaded.calls.some(c=>c.url==='/api/login'),false);
  page.d.getElementById('logout').click();await until(()=>!visibleApp(page));
  assert.match(loginError(page),/вышли/);
  assert.equal(page.pendingPolls.size,0);
});


test('wrong password and rate limits stay visible and the button can retry',async t=>{
  let attempts=0;
  const page=mount(t,url=>url==='/api/login'
    ?reply(++attempts===1?401:429,{error:attempts===1?'Неверный пароль':'Подождите минуту'})
    :reply(401,{error:'Войдите в панель'}));
  await ready(page);page.submit();await until(()=>loginError(page)==='Неверный пароль');
  assert.equal(page.d.querySelector('#login-form button').disabled,false);
  assert.equal(page.d.querySelector('#login-form input').value,PASSWORD);
  page.submit();await until(()=>loginError(page)==='Подождите минуту');
});


test('accepted password with a rejected cookie explains why the panel stays closed',async t=>{
  const page=mount(t,url=>url==='/api/login'?reply(200,{csrf:'new'}):reply(401,{error:'Войдите в панель'}));
  await ready(page);page.submit();await until(()=>loginError(page).includes('сессия не сохранилась'));
  assert.match(loginError(page),/WEB_SECURE_COOKIE/);
  assert.equal(visibleApp(page),false);
  assert.equal(page.d.querySelector('#login-form button').disabled,false);
});


test('failure loading state after accepted password is shown on the login screen',async t=>{
  let accepted=false;
  const page=mount(t,url=>{
    if(url==='/api/login'){accepted=true;return reply(200,{csrf:'new'});}
    return accepted?reply(500,{error:'Ошибка сервера'}):reply(401,{error:'Войдите в панель'});
  });
  await ready(page);page.submit();await until(()=>loginError(page).includes('не удалось открыть панель'));
  assert.match(loginError(page),/Ошибка сервера/);
  assert.equal(visibleApp(page),false);
});


test('Enter or a password manager can submit with a null submitter',async t=>{
  let accepted=false;
  const page=mount(t,url=>{
    if(url==='/api/login'){accepted=true;return reply(200,{csrf:'new'});}
    return accepted?reply(200,state()):reply(401,{error:'Войдите в панель'});
  });
  await ready(page);page.submit(false);await until(()=>visibleApp(page));
});


test('a delayed old unauthorized response cannot undo a successful login',async t=>{
  const old=deferred();let states=0;
  const page=mount(t,url=>url==='/api/login'?reply(200,{csrf:'new'}):++states===1?old.promise:reply(200,state()),{ignoreAbort:true});
  page.submit();await until(()=>visibleApp(page));
  old.resolve(reply(401,{error:'Войдите в панель'}));await pause(20);
  assert.equal(visibleApp(page),true);
});


test('duplicate clicks send one login and show progress while waiting',async t=>{
  const pending=deferred();let accepted=false;
  const page=mount(t,url=>{
    if(url==='/api/login')return pending.promise;
    return accepted?reply(200,state()):reply(401,{error:'Войдите в панель'});
  });
  await ready(page);page.submit();page.submit();await pause(5);
  assert.equal(page.calls.filter(c=>c.url==='/api/login').length,1);
  assert.equal(page.d.querySelector('#login-form button').disabled,true);
  assert.match(page.d.getElementById('login-status').textContent,/пароль/);
  accepted=true;pending.resolve(reply(200,{csrf:'new'}));await until(()=>visibleApp(page));
});


test('session expiry during polling gives a visible re-login message',async t=>{
  let expired=false;
  const page=mount(t,()=>expired?reply(401,{error:'Войдите в панель'}):reply(200,state()));
  await until(()=>visibleApp(page));expired=true;await page.poll();
  assert.equal(visibleApp(page),false);
  assert.match(loginError(page),/Сессия истекла/);
  assert.equal(page.pendingPolls.size,0);
});


test('a transient server error keeps a valid session and polling active',async t=>{
  let failed=false;
  const page=mount(t,()=>failed?reply(503,{error:'Временно недоступно'}):reply(200,state()));
  await until(()=>visibleApp(page));failed=true;await page.poll();
  assert.equal(visibleApp(page),true);
  assert.match(page.d.getElementById('notice').textContent,/Временно недоступно/);
  assert.equal(page.pendingPolls.size,1);
});


test('late state data cannot reopen the panel after logout',async t=>{
  const pending=deferred();let states=0;
  const page=mount(t,url=>url==='/api/logout'?reply(200,{ok:true}):++states===1?reply(200,state()):pending.promise,{ignoreAbort:true});
  await until(()=>visibleApp(page));
  const poll=page.poll();await pause(5);
  page.d.getElementById('logout').click();await until(()=>!visibleApp(page));
  pending.resolve(reply(200,state()));await poll;
  assert.equal(visibleApp(page),false);
});


test('network failure on login is visible and can be retried',async t=>{
  const page=mount(t,url=>{
    if(url==='/api/login')throw new TypeError('fetch failed');
    return reply(401,{error:'Войдите в панель'});
  });
  await ready(page);page.submit();await until(()=>loginError(page).includes('Не удалось связаться'));
  assert.equal(page.d.querySelector('#login-form button').disabled,false);
});


test('login timeout releases the button and shows a useful message',async t=>{
  const page=mount(t,url=>url==='/api/login'?new Promise(()=>{}):reply(401,{error:'Войдите в панель'}),{fastTimeout:true});
  await ready(page);page.submit();await until(()=>loginError(page).includes('15 секунд'));
  assert.equal(page.d.querySelector('#login-form button').disabled,false);
});


test('HTML proxy errors do not become obscure JSON parsing messages',async t=>{
  const page=mount(t,url=>url==='/api/login'?{status:502,ok:false,json:async()=>{throw new SyntaxError('Unexpected token <');}}:reply(401,{error:'Войдите в панель'}));
  await ready(page);page.submit();await until(()=>loginError(page).includes('HTTP 502'));
  assert.equal(visibleApp(page),false);
});


test('old password query is removed without trying to log in with it',async t=>{
  const page=mount(t,()=>reply(401,{error:'Войдите в панель'}),{url:'http://panel.example/?password=old-secret#keep'});
  assert.equal(page.w.location.href,'http://panel.example/#keep');
  assert.equal(page.calls.some(c=>c.url==='/api/login'),false);
  await ready(page);
});
