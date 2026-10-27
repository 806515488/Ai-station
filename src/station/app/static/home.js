/* 工作台目录由服务端提供；这里负责渲染和编辑输入，原聊天链路负责实际执行。 */
(() => {
  const root = document.getElementById('stationHome');
  const messages = document.getElementById('msgs');
  const title = document.getElementById('workspaceTitle');
  const input = document.getElementById('inp');
  const drafts = {};
  let tasks = new Map(), draftLimit = 0, viewVersion = 0, preparation = null, caseView = null;
  const icons = {
    archive: '<path d="M3 7h7l2-3h9v15H3z"/><path d="M7 11h10M7 15h7"/>',
    interview: '<rect x="9" y="3" width="6" height="12" rx="3"/><path d="M6 11v1a6 6 0 0 0 12 0v-1M12 18v3M9 21h6"/>',
    weekly: '<path d="M14 3H5v18h14V8zM14 3v6h5M8 13h8M8 17h6"/>',
    video: '<rect x="3" y="5" width="18" height="14" rx="3"/><path d="m10 9 5 3-5 3z"/>',
    task: '<rect x="4" y="4" width="16" height="16" rx="3"/><path d="M8 9h8M8 13h8M8 17h5"/>'
  };
  const escape = value => String(value??'').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
  // 后端只返回图标标识，不把 SVG/HTML 当作可以执行的界面内容。
  const icon = key => '<span class="home-icon"><svg viewBox="0 0 24 24" aria-hidden="true">'+(icons[key]||icons.task)+'</svg></span>';
  function cancelPreparation() {
    if (!preparation) return;
    const previous=preparation;preparation=null;previous.controller.abort();
    if (previous.button.isConnected) previous.button.disabled=false;
    if (previous.field?.isConnected) previous.field.disabled=false;
  }
  function setVisible(visible) {
    viewVersion++;cancelPreparation();caseView=null;
    root.hidden=!visible;messages.hidden=visible;
    title.textContent=visible?'工作台':'对话';
    document.getElementById('btnHome').classList.toggle('is-chat',!visible);
    input.placeholder=visible?'也可以直接告诉我，你想完成什么…':'继续输入你的需求…';
  }
  async function show() {
    setVisible(true);
    const version=viewVersion;
    root.innerHTML='<div class="home-inner"><p class="home-eyebrow">工作台</p><h1>你的云端 AI 工作站</h1><p class="home-guide-note" role="status">正在加载任务入口…</p></div>';
    try {
      const response=await fetch('/api/workbench',{credentials:'same-origin',cache:'no-store'});
      if (!response.ok) throw Error(response.status===401?'登录已失效，请刷新页面后重新登录。':'任务入口暂时无法加载，请稍后重试。');
      const data=await response.json();
      if (!Array.isArray(data.tasks)||!Number.isInteger(data.draft_limit)||data.draft_limit<1) throw Error('任务目录格式不正确，请刷新后重试。');
      // 切到历史会话或再次点新对话后，迟到的目录不能盖住当前页面。
      if (version!==viewVersion||root.hidden) return;
      tasks=new Map(data.tasks.map(task=>[task.id,task]));draftLimit=data.draft_limit;
      renderHome();
    } catch (error) {
      if (version!==viewVersion||root.hidden) return;
      root.innerHTML='<div class="home-inner"><h1>你的云端 AI 工作站</h1><p class="home-guide-note" role="status">'+escape(error.message)+'</p><button class="home-guide-secondary" data-home-retry>重新加载任务入口</button><p class="home-guide-note">你仍然可以在下方直接输入需求。</p></div>';
    }
  }
  function renderHome() {
    const entries=[...tasks.values()], quick=entries.find(task=>task.input_kind==='text'&&task.sample);
    root.innerHTML='<div class="home-inner"><p class="home-eyebrow"><i aria-hidden="true"></i> 浏览器访问 · 云端执行</p>'
      +'<div class="home-hero"><div><h1>你的云端 AI 工作站</h1><p>选择一个任务，提供材料，在对话中核对并带走结果文件。<br>也可以直接在下方告诉我你的需求。</p></div>'
      +'<div class="home-flow" aria-label="工作流程"><span><b>01</b> 提出需求，提供材料</span><span><b>02</b> 对话核对，查看进度</span><span><b>03</b> 预览结果，下载文件</span></div></div>'
      +'<div class="home-section-head"><h2>从一件具体的事开始</h2><span>'+entries.length+' 个任务入口</span></div>'
      +(entries.length?'<div class="home-grid">'+entries.map(task=>
        '<article class="home-card" data-task="'+escape(task.icon)+'"><div class="home-card-top">'+icon(task.icon)+'<span class="home-type">'+escape(task.category)+'</span></div>'
        +'<h3>'+escape(task.name)+'</h3><p class="home-description">'+escape(task.description)+'</p>'
        +'<div class="home-output"><span>你会得到</span><span>'+escape(task.output)+'</span></div>'
        +'<div class="home-card-actions"><button class="home-start" data-home-task="'+escape(task.id)+'" aria-label="开始使用：'+escape(task.name)+'">开始使用 <span aria-hidden="true">↗</span></button>'
        +'<button class="home-case" '+(task.case?.available?'data-home-case="'+escape(task.id)+'"':'disabled')+' title="'+escape(task.case?.reason)+'">'+escape(task.case?.label||'案例准备中')+'</button></div></article>'
      ).join('')+'</div>':'<p class="home-guide-note">暂时没有已加载的任务入口，你仍然可以直接对话。</p>')
      +'<div class="home-footer"><span>任务可跟踪 · 结果可下载 · 对话可回看</span>'
      +(quick?'<button class="home-text-button" data-home-task="'+escape(quick.id)+'">没有材料？先试试'+escape(quick.name)+' →</button>':'')+'</div></div>';
  }
  function guide(key) {
    const task=tasks.get(key);if (!task) return;
    setVisible(true);
    const textInput=task.input_kind==='text';
    root.innerHTML='<div class="home-guide" data-task="'+escape(task.icon)+'"><button class="home-back" data-home-back>← 返回工作台</button>'
      +'<div class="home-guide-head">'+icon(task.icon)+'<h1>'+escape(task.name)+'</h1></div><p>'+escape(task.description)+'</p>'
      +'<div class="home-steps" aria-label="操作步骤">'+task.steps.map((step,i)=>
        '<span class="home-step"><b>'+(i+1)+'</b>'+escape(step)+'</span>'+(i<task.steps.length-1?'<span class="home-step-arrow" aria-hidden="true">→</span>':'')).join('')+'</div>'
      +'<div class="home-guide-input">'+(textInput
        ?'<label for="homeDraft">'+escape(task.label)+'</label><textarea id="homeDraft" maxlength="'+draftLimit+'" data-draft="'+escape(key)+'" placeholder="'+escape(task.placeholder)+'">'+escape(drafts[key]||'')+'</textarea>'
        :'<label>先准备好你的材料</label><p>'+escape(task.need)+'</p>')
      +'<div class="home-guide-actions"><button class="home-guide-primary" data-home-begin="'+escape(key)+'">'+escape(task.action)+' →</button>'
      +(task.case?.materials_url?'<a class="home-guide-secondary" href="'+escape(task.case.materials_url)+'" download>下载 '+task.case.input_count+' 张示例照片</a>':task.sample?'<button class="home-guide-secondary" data-home-sample="'+escape(key)+'">填入'+escape(task.sample_label)+'</button>':task.sample_notice?'<span class="home-guide-note">'+escape(task.sample_notice)+'</span>'
        :'<button class="home-guide-secondary" disabled>'+escape(task.sample_label)+'准备中</button>')
      +'</div><div class="home-guide-error" id="homeGuideError" role="status"></div></div><p class="home-guide-note">'+escape(task.pending)+(task.case?.materials_url?' 下载图片包并解压后，开始对话，再选择这些照片上传即可。':'')+'</p></div>';
    root.scrollTop=0;root.querySelector('.home-back').focus({preventScroll:true});
  }
  function feedback(text,error=false) {
    const node=root.querySelector('#homeGuideError');
    if (node) {node.textContent=text;node.classList.toggle('is-error',error);}
  }
  async function showCase(key) {
    if(!tasks.get(key)?.case?.available)return;
    setVisible(true);const version=viewVersion;
    root.innerHTML='<div class="home-inner"><p role="status">正在加载案例…</p></div>';
    try {
      const response=await fetch('/api/workbench/'+encodeURIComponent(key)+'/case',{cache:'no-store'});
      const data=await response.json();
      if(!response.ok)throw Error(typeof data.detail==='string'?data.detail:'案例暂时无法加载');
      if(version!==viewVersion||root.hidden)return;
      caseView={key,data};renderCase('result');
    } catch(error) {
      if(version!==viewVersion||root.hidden)return;
      root.innerHTML='<div class="home-inner"><button class="home-back" data-home-back>← 返回工作台</button><p role="status">'+escape(error.message)+'</p></div>';
    }
  }
  function renderCase(tab) {
    if(!caseView)return;
    const {key,data}=caseView, inputs=(data.files||[]).filter(file=>file.role==='input'&&file.preview_url);
    const tabs=['result','process',...(inputs.length?['materials']:[])], labels={result:'最终结果',process:'查看使用过程',materials:'示例照片'};
    const tools=new Map();
    (data.turns||[]).forEach(turn=>turn.messages.forEach(message=>{
      if(message.role==='tool')tools.set(message.content,message.label||message.content);
    }));
    const links=(data.files||[]).filter(file=>file.role==='result').map(file=>
      '<a class="case-download" href="'+escape(file.url)+'" download="'+escape(file.filename)+'">'+escape(file.label)+' <span aria-hidden="true">↓</span></a>').join('');
    let content='';
    if(tab==='result')content='<section class="case-report" id="caseReport" aria-label="案例最终结果"></section>';
    else if(tab==='materials') {
      content='<section aria-label="示例照片"><p class="case-process-hint">这些照片是本案例实际使用的虚构材料。可点击查看大图，或下载图片包后重新上传体验。</p>'
        +'<div class="case-gallery">'+inputs.map(file=>'<a href="'+escape(file.preview_url)+'" target="_blank" rel="noopener noreferrer"><img src="'+escape(file.preview_url)+'" alt="'+escape(file.label)+'" loading="lazy"><span>'+escape(file.label)+'</span></a>').join('')+'</div></section>';
    } else {
      content='<section class="case-process" aria-label="使用过程"><p class="case-process-hint">以下是这次示例的实际对话与工具调用。展开每一步，可以查看用户如何提出要求、修改内容和取得文件。</p>'
        +'<details class="case-tool-coverage"><summary>本案例调用了 '+tools.size+' 个工具，展开查看</summary><div>'+[...tools].map(([name,label])=>'<span title="'+escape(name)+'">'+escape(label)+'</span>').join('')+'</div></details>'
        +(data.turns||[]).map((turn,i)=>'<details class="case-turn"><summary><span class="case-step-number">'+(i+1)+'</span><b>'+escape(turn.title)+'</b><span>'+Math.round(turn.duration_ms/100)/10+' 秒</span></summary><div class="case-turn-body">'
          +turn.messages.map(message=>{
            const pictures=(message.images||[]).map(id=>(data.files||[]).find(file=>file.id===id&&file.preview_url)).filter(Boolean);
            return '<div class="case-message case-message-'+escape(message.role)+'"><span class="case-speaker">'+escape(message.role==='user'?(data.user_label||'示例用户'):message.role==='tool'?(message.label||'工具调用'):(message.label||'工作站'))+'</span><div class="case-message-content" '+(message.role==='assistant'?'data-case-markdown':'')+'>'+escape(message.content)+'</div>'
              +(pictures.length?'<div class="case-inline-images">'+pictures.map(file=>'<a href="'+escape(file.preview_url)+'" target="_blank" rel="noopener noreferrer"><img src="'+escape(file.preview_url)+'" alt="'+escape(file.label)+'" loading="lazy"></a>').join('')+'</div>':'')+'</div>';
          }).join('')+'</div></details>').join('')+'</section>';
    }
    root.innerHTML='<div class="case-inner"><button class="home-back" data-home-back>← 返回工作台</button>'
      +'<p class="home-eyebrow">演示案例 · '+escape(data.material_label||'虚构材料')+' · 实际执行记录</p><h1>'+escape(data.title)+'</h1><p class="case-summary">'+escape(data.summary)+'</p>'
      +'<div class="case-actions">'+links+(data.materials_url?'<a class="case-download" href="'+escape(data.materials_url)+'" download>下载示例图片包 ↓</a>':'')
      +'<button class="home-guide-secondary" data-case-replay="'+escape(key)+'">'+escape(data.replay_label||'用这份材料重新体验 →')+'</button></div>'
      +'<div class="case-tabs" role="tablist" aria-label="案例内容">'+tabs.map(id=>'<button id="caseTab'+id+'" role="tab" data-case-tab="'+id+'" aria-controls="caseContent" aria-selected="'+(tab===id)+'">'+labels[id]+'</button>').join('')+'</div>'
      +'<div id="caseContent" role="tabpanel" aria-labelledby="caseTab'+tab+'">'+content+'</div></div>';
    if(tab==='result')window.renderMsgText(root.querySelector('#caseReport'),data.report,true);
    else if(tab==='process')root.querySelectorAll('[data-case-markdown]').forEach(node=>window.renderMsgText(node,node.textContent,true));
    root.scrollTop=0;
  }
  async function begin(key) {
    const task=tasks.get(key), button=root.querySelector('[data-home-begin]'), field=root.querySelector('#homeDraft');
    if (!task||input.disabled||preparation||!button) return;
    const draft=field?field.value.trim():'';
    if (task.input_kind==='text'&&!draft) {feedback('请先填写内容，也可以使用示例。',true);field.focus();return;}
    const ticket={controller:new AbortController(),version:viewVersion,button,field};
    preparation=ticket;button.disabled=true;if(field)field.disabled=true;feedback('正在准备本次需求…');
    try {
      const response=await fetch('/api/workbench/'+encodeURIComponent(key)+'/prepare',{
        method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({draft}),signal:ticket.controller.signal
      });
      const data=await response.json();
      if (!response.ok) throw Error(typeof data.detail==='string'?data.detail:'无法准备本次需求，请检查输入后重试。');
      if (typeof data.message!=='string'||!data.message.trim()) throw Error('没有取得有效的需求内容，请重试。');
      if(preparation!==ticket||ticket.version!==viewVersion||root.hidden)return;
      if(field)drafts[key]=draft;
      preparation=null;input.value='';
      // 准备接口只返回文字；实际消息、批准和工具执行仍交给原 send，不强制指定技能。
      window.send(data.message);
    } catch(error) {
      if(preparation===ticket&&ticket.version===viewVersion&&error.name!=='AbortError')feedback(error.message,true);
    } finally {
      if(preparation===ticket) {
        preparation=null;
        if(button.isConnected)button.disabled=false;
        if(field?.isConnected)field.disabled=false;
      }
    }
  }
  root.addEventListener('click',event=>{
    const button=event.target.closest('button');
    if(!button||button.disabled)return;
    if(button.hasAttribute('data-home-back')||button.hasAttribute('data-home-retry'))show();
    else if(button.dataset.homeCase)showCase(button.dataset.homeCase);
    else if(button.dataset.caseTab)renderCase(button.dataset.caseTab);
    else if(button.dataset.caseReplay&&caseView) {const key=caseView.key;drafts[key]=caseView.data.input;guide(key);}
    else if(button.dataset.homeTask)guide(button.dataset.homeTask);
    else if(button.dataset.homeBegin)begin(button.dataset.homeBegin);
    else if(button.dataset.homeSample) {
      const key=button.dataset.homeSample,task=tasks.get(key),field=root.querySelector('#homeDraft');
      if(!field||field.disabled||!task?.sample)return;
      if(field.value.trim()&&field.value!==task.sample){feedback('已有你填写的内容，请先清空后再填入示例。',true);return;}
      field.value=drafts[key]=task.sample;feedback('已填入示例，你可以修改后再开始。');field.focus();
    }
  });
  root.addEventListener('input',event=>{
    if(event.target.dataset.draft){drafts[event.target.dataset.draft]=event.target.value;feedback('');}
  });
  root.addEventListener('keydown',event=>{
    if(event.target.matches('[data-case-tab]')&&['ArrowLeft','ArrowRight'].includes(event.key)){
      event.preventDefault();const tabs=[...root.querySelectorAll('[data-case-tab]')].map(node=>node.dataset.caseTab),index=tabs.indexOf(event.target.dataset.caseTab),next=tabs[(index+(event.key==='ArrowRight'?1:-1)+tabs.length)%tabs.length];renderCase(next);root.querySelector('[data-case-tab="'+next+'"]').focus();
    }
  });
  window.StationHome={show,hide:()=>setVisible(false)};

  // 设置入口复用原面板，支持键盘导航、Esc 和点击外部关闭。
  const settings = document.getElementById('settingsButton');
  const menu = document.getElementById('settingsMenu');
  function closeSettings(focus=false) {
    menu.hidden=true;
    settings.setAttribute('aria-expanded','false');
    if (focus) settings.focus();
  }
  function openSettings() {
    menu.hidden=false;
    settings.setAttribute('aria-expanded','true');
    menu.querySelector('[role="menuitem"]').focus();
  }
  settings.addEventListener('click', () => menu.hidden ? openSettings() : closeSettings());
  settings.addEventListener('keydown', event => {
    if (event.key==='ArrowDown') {event.preventDefault();openSettings();}
  });
  menu.addEventListener('click', event => {
    const item=event.target.closest('[data-settings]');
    if (!item) return;
    closeSettings();
    if (item.dataset.settings==='models') window.toggleConfig(true);
    else if (item.dataset.settings==='mcp') window.toggleMcp(true);
  });
  menu.addEventListener('keydown', event => {
    if (!['ArrowDown','ArrowUp','Home','End'].includes(event.key)) return;
    event.preventDefault();
    const items=[...menu.querySelectorAll('[role="menuitem"]')], current=items.indexOf(document.activeElement);
    const next=event.key==='Home'?0:event.key==='End'?items.length-1:(current+(event.key==='ArrowDown'?1:-1)+items.length)%items.length;
    items[next].focus();
  });
  document.addEventListener('click', event => {
    if (!event.target.closest('.settings-control')) closeSettings();
    else if (event.target.closest('#btnLogout2')) closeSettings();
  });
  document.addEventListener('keydown', event => {
    if (event.key==='Escape' && !menu.hidden) {event.preventDefault();closeSettings(true);}
  });

  // 窄屏把原来的会话侧栏收成抽屉，仍可访问工作台和历史，不复制另一套导航。
  const sidebar=document.getElementById('sidebar'), scrim=document.getElementById('sideScrim');
  const mobileNav=document.getElementById('mobileNav');
  function closeNavigation(focus=false) {
    sidebar.classList.remove('is-open');scrim.hidden=true;
    mobileNav.setAttribute('aria-expanded','false');
    if (focus) mobileNav.focus();
  }
  mobileNav.addEventListener('click', () => {
    if (sidebar.classList.contains('is-open')) {closeNavigation();return;}
    closeSettings();sidebar.classList.add('is-open');scrim.hidden=false;
    mobileNav.setAttribute('aria-expanded','true');
    document.getElementById('btnHome').focus();
  });
  scrim.addEventListener('click', () => closeNavigation(true));
  sidebar.addEventListener('click', event => {
    if (event.target.closest('#btnHome,#btnNewSide,.hitem')) closeNavigation();
  });
  window.addEventListener('resize', () => {if (window.innerWidth>760) closeNavigation();});
  document.addEventListener('keydown', event => {
    if (!sidebar.classList.contains('is-open')) return;
    if (event.key==='Escape') {event.preventDefault();closeNavigation(true);}
    if (event.key==='Tab') {
      const items=[...sidebar.querySelectorAll('button,a,[tabindex="0"]')].filter(item=>!item.disabled&&item.getClientRects().length);
      const first=items[0],last=items.at(-1);
      if (event.shiftKey&&document.activeElement===first) {event.preventDefault();last?.focus();}
      else if (!event.shiftKey&&document.activeElement===last) {event.preventDefault();first?.focus();}
    }
  });
})();
