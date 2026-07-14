# ruff: noqa: E501
"""Bundled, dependency-free review run dashboard."""

REVIEWS_DASHBOARD_HTML = r'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Review 执行台账</title>
<style>
:root{--ink:#172033;--muted:#677287;--paper:#f3f6fb;--panel:#fff;--line:#d8dee9;--line-soft:#e9edf3;--blue:#2563eb;--blue-soft:#e9f0ff;--green:#16805c;--green-soft:#e5f5ee;--red:#c63c4a;--red-soft:#fce9eb;--amber:#a46107;--amber-soft:#fff2d9;--nav:#14243a;--shadow:0 14px 40px rgba(23,32,51,.07)}
*{box-sizing:border-box}
html{background:var(--paper)}
body{margin:0;color:var(--ink);background:var(--paper);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif}
button,input,select{font:inherit}
button,a,input,select{outline-offset:3px}
button:focus-visible,a:focus-visible,input:focus-visible,select:focus-visible{outline:2px solid var(--blue)}
.topline{height:4px;background:linear-gradient(90deg,var(--nav) 0 21%,var(--blue) 21% 63%,var(--green) 63% 100%)}
.page{width:min(1480px,calc(100% - 48px));margin:0 auto;padding:34px 0 48px}
.masthead{display:flex;align-items:flex-start;justify-content:space-between;gap:28px;margin-bottom:28px}
.identity{display:flex;align-items:flex-start;gap:16px}
.mark{display:grid;place-items:center;width:45px;height:45px;border:1px solid #bdc8d8;border-radius:10px;background:var(--panel);color:var(--nav);font:800 13px/1 ui-monospace,SFMono-Regular,Menlo,monospace;letter-spacing:-.04em;box-shadow:0 6px 18px rgba(20,36,58,.06)}
.eyebrow{margin:0 0 5px;color:var(--blue);font:700 10px/1.2 ui-monospace,SFMono-Regular,Menlo,monospace;letter-spacing:.16em;text-transform:uppercase}
h1{margin:0;font:750 30px/1.14 ui-rounded,"Arial Rounded MT Bold","PingFang SC",sans-serif;letter-spacing:-.035em}
.intro{margin:7px 0 0;color:var(--muted)}
.refresh-box{display:flex;align-items:center;gap:11px;min-height:45px}
.refresh-meta{text-align:right;color:var(--muted);font-size:12px}
.refresh-meta strong{display:block;color:var(--ink);font:600 12px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace}
.icon-button{display:inline-flex;align-items:center;gap:7px;min-height:38px;padding:8px 12px;border:1px solid var(--line);border-radius:8px;background:var(--panel);color:var(--ink);cursor:pointer;box-shadow:0 2px 8px rgba(23,32,51,.03)}
.icon-button:hover{border-color:#aeb9c9;background:#fbfcfe}
.icon-button[disabled]{cursor:wait;opacity:.65}
.icon-button svg{width:15px;height:15px}
.icon-button.is-loading svg{animation:spin .8s linear infinite}
.ledger{overflow:hidden;border:1px solid var(--line);border-radius:12px;background:var(--panel);box-shadow:var(--shadow)}
.status-tabs{display:flex;align-items:center;gap:2px;padding:11px 14px;border-bottom:1px solid var(--line);background:#fbfcfe;overflow-x:auto;scrollbar-width:thin}
.status-tab{white-space:nowrap;border:0;border-radius:7px;background:transparent;color:var(--muted);padding:7px 11px;cursor:pointer;font-weight:600}
.status-tab:hover{color:var(--ink);background:#eef2f7}
.status-tab[aria-pressed="true"]{color:#fff;background:var(--nav)}
.filters{display:grid;grid-template-columns:160px minmax(220px,1fr) minmax(150px,190px) auto;align-items:end;gap:12px;padding:16px;border-bottom:1px solid var(--line)}
.field{display:grid;gap:6px}
.field label{color:var(--muted);font-size:11px;font-weight:700;letter-spacing:.04em}
.field input,.field select{width:100%;height:39px;border:1px solid var(--line);border-radius:7px;background:#fff;color:var(--ink);padding:7px 10px}
.field input::placeholder{color:#9aa4b4}
.field input:hover,.field select:hover{border-color:#aeb9c9}
.filter-actions{display:flex;gap:8px}
.primary,.quiet{height:39px;border-radius:7px;padding:0 15px;cursor:pointer;font-weight:650}
.primary{border:1px solid var(--blue);background:var(--blue);color:#fff}
.primary:hover{background:#1e55cf}
.quiet{border:1px solid var(--line);background:#fff;color:var(--ink)}
.quiet:hover{background:#f5f7fa}
.list-meta{display:flex;align-items:center;justify-content:space-between;gap:14px;padding:11px 16px;border-bottom:1px solid var(--line-soft);color:var(--muted);font-size:12px}
.list-meta strong{color:var(--ink);font:700 12px ui-monospace,SFMono-Regular,Menlo,monospace}
.live-dot{display:inline-block;width:7px;height:7px;margin-right:7px;border-radius:50%;background:var(--green);box-shadow:0 0 0 3px var(--green-soft)}
.table-wrap{width:100%;overflow-x:auto}
table{width:100%;min-width:1120px;border-collapse:collapse}
th,td{text-align:left;border-bottom:1px solid var(--line-soft);padding:14px 16px;vertical-align:middle}
th{padding-top:10px;padding-bottom:10px;background:#fbfcfe;color:var(--muted);font:700 10px/1.3 ui-monospace,SFMono-Regular,Menlo,monospace;letter-spacing:.08em;text-transform:uppercase}
tbody tr:last-child td{border-bottom:0}
tbody tr:hover{background:#f8faff}
.mr{display:grid;grid-template-columns:32px minmax(0,1fr);gap:10px;align-items:start;max-width:330px}
.provider-mark{display:grid;place-items:center;width:30px;height:30px;border-radius:8px;background:#eef2f7;color:#3e4c60;font:800 10px ui-monospace,SFMono-Regular,Menlo,monospace}
.provider-mark.gitlab{background:#fff0e9;color:#9a3d17}
.mr-title{display:block;overflow:hidden;color:var(--ink);font-weight:700;text-decoration:none;text-overflow:ellipsis;white-space:nowrap}
a.mr-title:hover{color:var(--blue);text-decoration:underline;text-underline-offset:3px}
.mr-number{margin-top:2px;color:var(--muted);font:12px ui-monospace,SFMono-Regular,Menlo,monospace}
.repo{max-width:230px;overflow:hidden;text-overflow:ellipsis;font-weight:650}
.branch{max-width:230px;margin-top:3px;overflow:hidden;color:var(--muted);font:11px ui-monospace,SFMono-Regular,Menlo,monospace;text-overflow:ellipsis;white-space:nowrap}
.run-cell{min-width:260px}
.rail{display:grid;grid-template-columns:56px 1fr 56px 1fr 62px;align-items:center;width:248px}
.rail-step{display:flex;align-items:center;gap:6px;color:#8a95a5;font:650 10px ui-monospace,SFMono-Regular,Menlo,monospace}
.rail-step:last-child{justify-content:flex-end}
.rail-node{width:9px;height:9px;flex:0 0 9px;border:2px solid #bdc6d3;border-radius:50%;background:#fff}
.rail-line{height:2px;background:#dce2ea}
.rail-step.done{color:var(--green)}
.rail-step.done .rail-node{border-color:var(--green);background:var(--green)}
.rail-step.active{color:var(--blue)}
.rail-step.active .rail-node{border-color:var(--blue);background:#fff;box-shadow:0 0 0 3px var(--blue-soft)}
.rail-step.failed{color:var(--red)}
.rail-step.failed .rail-node{border-color:var(--red);background:var(--red);box-shadow:0 0 0 3px var(--red-soft)}
.rail-step.cancelled{color:var(--amber)}
.rail-step.cancelled .rail-node{border-color:var(--amber);background:var(--amber);box-shadow:0 0 0 3px var(--amber-soft)}
.rail-line.done{background:var(--green)}
.rail-line.active{background:linear-gradient(90deg,var(--green),var(--blue))}
.stage{margin-top:7px;color:var(--muted);font-size:11px}
.stage code{color:#44516a;font:600 11px ui-monospace,SFMono-Regular,Menlo,monospace}
.failure{max-width:310px;margin-top:5px;overflow:hidden;color:var(--red);font-size:11px;text-overflow:ellipsis;white-space:nowrap}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.sha{display:inline-block;border:1px solid var(--line-soft);border-radius:5px;background:#f5f7fa;padding:3px 6px;color:#44516a;font-size:11px}
.time strong{display:block;font:650 12px ui-monospace,SFMono-Regular,Menlo,monospace}
.time span{color:var(--muted);font-size:11px}
.state-panel{min-height:280px;display:grid;place-items:center;padding:45px 20px;text-align:center}
.state-panel strong{display:block;margin-bottom:6px;font-size:15px}
.state-panel p{max-width:480px;margin:0;color:var(--muted)}
.state-symbol{display:grid;place-items:center;width:42px;height:42px;margin:0 auto 13px;border:1px solid var(--line);border-radius:50%;color:var(--muted);font:700 18px ui-monospace,SFMono-Regular,Menlo,monospace}
.state-panel.error .state-symbol{border-color:#efc5ca;background:var(--red-soft);color:var(--red)}
.skeleton{width:100%;padding:20px}
.skeleton-row{height:56px;margin-bottom:10px;border-radius:8px;background:linear-gradient(90deg,#eef1f5 20%,#f8f9fb 38%,#eef1f5 56%);background-size:400% 100%;animation:shimmer 1.4s ease infinite}
.pagination{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:14px 16px;border-top:1px solid var(--line)}
.page-info{color:var(--muted);font-size:12px}
.page-info strong{color:var(--ink);font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.page-buttons{display:flex;gap:7px}
.page-buttons button{height:34px;border:1px solid var(--line);border-radius:7px;background:#fff;padding:0 12px;color:var(--ink);cursor:pointer}
.page-buttons button:hover:not(:disabled){border-color:#aeb9c9;background:#f5f7fa}
.page-buttons button:disabled{cursor:not-allowed;color:#a5adba;background:#fafbfc}
.visually-hidden{position:absolute!important;width:1px!important;height:1px!important;padding:0!important;margin:-1px!important;overflow:hidden!important;clip:rect(0,0,0,0)!important;white-space:nowrap!important;border:0!important}
@keyframes spin{to{transform:rotate(360deg)}}
@keyframes shimmer{to{background-position:-100% 0}}
@media(max-width:760px){.page{width:min(100% - 24px,1480px);padding-top:22px}.masthead{display:grid;gap:18px}.refresh-box{justify-content:space-between}.refresh-meta{text-align:left}.filters{grid-template-columns:1fr}.filter-actions{display:grid;grid-template-columns:1fr 1fr}.table-wrap{overflow:visible}table,thead,tbody,tr,td{display:block}table{min-width:0}thead{display:none}tbody{display:grid;gap:0}tbody tr{padding:16px;border-bottom:1px solid var(--line)}tbody tr:last-child{border-bottom:0}td{display:grid;grid-template-columns:88px minmax(0,1fr);border:0;padding:7px 0}td::before{content:attr(data-label);color:var(--muted);font:700 10px ui-monospace,SFMono-Regular,Menlo,monospace;letter-spacing:.06em;text-transform:uppercase}.mr,.repo,.branch{max-width:none}.run-cell{min-width:0}.pagination{align-items:flex-start}.page-buttons button{padding:0 10px}}
@media(max-width:420px){h1{font-size:25px}.identity{gap:11px}.mark{width:40px;height:40px}.refresh-box{align-items:flex-end}.icon-button span{display:none}.rail{width:100%;max-width:248px}.list-meta{align-items:flex-start;flex-direction:column}.pagination{flex-direction:column}.page-buttons{width:100%}.page-buttons button{flex:1}}
@media(prefers-reduced-motion:reduce){*,*::before,*::after{scroll-behavior:auto!important;animation-duration:.01ms!important;animation-iteration-count:1!important}}
</style>
</head>
<body>
<div class="topline"></div>
<main class="page">
  <header class="masthead">
    <div class="identity">
      <div class="mark" aria-hidden="true">RO</div>
      <div>
        <p class="eyebrow">Review orchestrator / live ledger</p>
        <h1>Review 执行台账</h1>
        <p class="intro">追踪每一次代码审查的执行状态，以及它对应的 MR / PR。</p>
      </div>
    </div>
    <div class="refresh-box">
      <div class="refresh-meta"><span id="refresh-status">30 秒后自动刷新</span><strong id="updated-at">尚未加载</strong></div>
      <button class="icon-button" id="refresh" type="button">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M20 11a8.1 8.1 0 0 0-15.5-2M4 4v5h5M4 13a8.1 8.1 0 0 0 15.5 2M20 20v-5h-5"/></svg>
        <span>立即刷新</span>
      </button>
    </div>
  </header>

  <section class="ledger" aria-labelledby="ledger-title">
    <h2 id="ledger-title" class="visually-hidden">Review 任务列表</h2>
    <div class="status-tabs" id="status-tabs" aria-label="按状态筛选">
      <button class="status-tab" type="button" data-status="" aria-pressed="true">全部</button>
      <button class="status-tab" type="button" data-status="queued" aria-pressed="false">排队中</button>
      <button class="status-tab" type="button" data-status="running" aria-pressed="false">运行中</button>
      <button class="status-tab" type="button" data-status="completed" aria-pressed="false">已完成</button>
      <button class="status-tab" type="button" data-status="failed" aria-pressed="false">失败</button>
      <button class="status-tab" type="button" data-status="cancelled" aria-pressed="false">已取消</button>
      <button class="status-tab" type="button" data-status="superseded" aria-pressed="false">已替代</button>
    </div>

    <form class="filters" id="filters">
      <div class="field"><label for="provider">代码平台</label><select id="provider" name="provider"><option value="">全部平台</option><option value="github">GitHub</option><option value="gitlab">GitLab</option></select></div>
      <div class="field"><label for="repository">仓库</label><input id="repository" name="repo_full_name" maxlength="512" placeholder="owner/repository" autocomplete="off"></div>
      <div class="field"><label for="request-number">MR / PR 编号</label><input id="request-number" name="pull_request_number" type="number" min="1" step="1" placeholder="例如 42" inputmode="numeric"></div>
      <div class="filter-actions"><button class="primary" type="submit">应用筛选</button><button class="quiet" id="reset" type="button">重置</button></div>
    </form>

    <div class="list-meta"><span><span class="live-dot" aria-hidden="true"></span><span id="result-summary">正在连接 review 服务</span></span><span>每页 <strong>25</strong> 条 · 最新任务优先</span></div>
    <div id="results" aria-live="polite" aria-busy="true"></div>
    <div class="pagination" id="pagination" hidden>
      <div class="page-info" id="page-info"></div>
      <div class="page-buttons"><button id="previous" type="button">← 上一页</button><button id="next" type="button">下一页 →</button></div>
    </div>
  </section>
  <div id="announcer" class="visually-hidden" aria-live="polite"></div>
</main>

<script>
const API='/api/v1/observability/review-runs';
const PAGE_SIZE=25;
const REFRESH_SECONDS=30;
const PROXY_TOKEN=new URLSearchParams(window.location.search).get('token')||'';
const statusNames={queued:'排队中',running:'运行中',completed:'已完成',failed:'失败',cancelled:'已取消',superseded:'已替代'};
const stageNames={start:'准备执行',waiting_for_openhands_start:'等待执行器',retrying_openhands_start:'重试执行器',waiting_for_result:'等待审查结果',collecting_result:'收集审查结果',publishing_summary:'发布审查摘要',cleanup:'清理工作区',completed:'执行完成'};
const state={status:'',provider:'',repo_full_name:'',pull_request_number:'',page:1,total:0,loading:false,controller:null,remaining:REFRESH_SECONDS};
const $=selector=>document.querySelector(selector);
const esc=value=>String(value??'').replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const scrollToTop=()=>window.scrollTo({top:0,behavior:window.matchMedia('(prefers-reduced-motion: reduce)').matches?'auto':'smooth'});
const parseDate=value=>{const date=value?new Date(value):null;return date&&!Number.isNaN(date.getTime())?date:null};
const exactTime=value=>{const date=parseDate(value);return date?new Intl.DateTimeFormat('zh-CN',{dateStyle:'medium',timeStyle:'medium'}).format(date):'—'};
function relativeTime(value){const date=parseDate(value);if(!date)return '—';const seconds=Math.round((date.getTime()-Date.now())/1000);const abs=Math.abs(seconds);let amount=seconds,unit='second';if(abs>=86400){amount=Math.round(seconds/86400);unit='day'}else if(abs>=3600){amount=Math.round(seconds/3600);unit='hour'}else if(abs>=60){amount=Math.round(seconds/60);unit='minute'}return new Intl.RelativeTimeFormat('zh-CN',{numeric:'auto'}).format(amount,unit)}
function duration(run){const start=parseDate(run.started_at||run.created_at);const end=parseDate(run.completed_at)||(run.status==='running'?new Date():parseDate(run.updated_at));if(!start||!end)return '—';const seconds=Math.max(0,Math.round((end-start)/1000));if(seconds<60)return `${seconds} 秒`;const minutes=Math.floor(seconds/60);if(minutes<60)return `${minutes} 分 ${seconds%60} 秒`;const hours=Math.floor(minutes/60);return `${hours} 小时 ${minutes%60} 分`}
function safeUrl(value){if(!value)return null;try{const url=new URL(value,window.location.origin);return ['http:','https:'].includes(url.protocol)?url.href:null}catch{return null}}
function statusRail(run){const status=run.status;let first='done',middle='',last='',lastLabel='结束',line1='done',line2='';if(status==='queued'){first='active';line1='';lastLabel='结束'}else if(status==='running'){middle='active';line2='active'}else if(status==='completed'){middle='done';last='done';line2='done'}else if(status==='failed'){middle='done';last='failed';lastLabel='失败';line2='done'}else if(status==='cancelled'){middle=run.started_at?'done':'';last='cancelled';lastLabel='取消';line2=run.started_at?'done':''}else if(status==='superseded'){middle=run.started_at?'done':'';last='cancelled';lastLabel='替代';line2=run.started_at?'done':''}return `<div class="rail" aria-label="${esc(statusNames[status]||status)}"><span class="rail-step ${first}"><span class="rail-node"></span>入队</span><span class="rail-line ${line1}"></span><span class="rail-step ${middle}"><span class="rail-node"></span>执行</span><span class="rail-line ${line2}"></span><span class="rail-step ${last}"><span class="rail-node"></span>${lastLabel}</span></div>`}
function mrCell(run){const context=run.pull_request_context||{};const provider=String(run.provider||'').toLowerCase();const prefix=provider==='gitlab'?'!':'#';const title=context.title||`${provider==='gitlab'?'Merge request':'Pull request'} ${prefix}${run.pull_request_number}`;const url=safeUrl(context.html_url);const titleHtml=url?`<a class="mr-title" href="${esc(url)}" target="_blank" rel="noopener noreferrer" title="${esc(title)}">${esc(title)}</a>`:`<span class="mr-title" title="${esc(title)}">${esc(title)}</span>`;return `<div class="mr"><span class="provider-mark ${provider==='gitlab'?'gitlab':''}" aria-hidden="true">${provider==='gitlab'?'GL':'GH'}</span><span>${titleHtml}<span class="mr-number">${prefix}${esc(run.pull_request_number)} · 第 ${esc(run.attempt)} 次执行</span></span></div>`}
function renderRows(items){return `<div class="table-wrap"><table><thead><tr><th>MR / PR</th><th>仓库</th><th>执行状态</th><th>版本</th><th>耗时</th><th>最近更新</th></tr></thead><tbody>${items.map(run=>{const context=run.pull_request_context||{};const branch=context.head_ref&&context.base_ref?`${context.head_ref} → ${context.base_ref}`:(context.head_ref||context.base_ref||'');const stage=stageNames[run.stage]||run.stage||'等待分配阶段';const failure=run.status==='failed'&&(run.failure_code||run.error)?`<div class="failure" title="${esc(run.error||run.failure_code)}">${esc(run.failure_code||'执行失败')}${run.error?` · ${esc(run.error)}`:''}</div>`:'';return `<tr><td data-label="MR / PR">${mrCell(run)}</td><td data-label="仓库"><div class="repo" title="${esc(run.repo_full_name)}">${esc(run.repo_full_name)}</div>${branch?`<div class="branch" title="${esc(branch)}">${esc(branch)}</div>`:''}</td><td data-label="执行状态" class="run-cell">${statusRail(run)}<div class="stage">当前阶段 · <code>${esc(stage)}</code></div>${failure}</td><td data-label="版本"><code class="sha" title="${esc(run.head_sha)}">${esc(String(run.head_sha||'').slice(0,8)||'—')}</code></td><td data-label="耗时"><div class="time"><strong>${esc(duration(run))}</strong><span>${run.started_at?'已开始':'从创建起'}</span></div></td><td data-label="最近更新"><div class="time" title="${esc(exactTime(run.updated_at))}"><strong>${esc(relativeTime(run.updated_at))}</strong><span>${esc(exactTime(run.updated_at))}</span></div></td></tr>`}).join('')}</tbody></table></div>`}
function renderLoading(){return `<div class="skeleton" aria-label="正在加载"><div class="skeleton-row"></div><div class="skeleton-row"></div><div class="skeleton-row"></div><div class="skeleton-row"></div></div>`}
function renderEmpty(){return `<div class="state-panel"><div><div class="state-symbol" aria-hidden="true">0</div><strong>没有符合条件的 review 任务</strong><p>调整状态或仓库筛选后再试，新的 review 任务也会在下次自动刷新时出现。</p></div></div>`}
function renderError(message){return `<div class="state-panel error"><div><div class="state-symbol" aria-hidden="true">!</div><strong>列表加载失败</strong><p>${esc(message)}</p></div></div>`}
function errorMessage(response){if(response.status===401||response.status===403)return '当前访问凭据无权查看 review 看板，请通过受保护的 operator 入口重新访问。';if(response.status===422)return '筛选条件不符合 API 要求，请检查仓库或 MR / PR 编号。';return `review 服务返回错误（${response.status}），请稍后刷新。`}
function requestHeaders(){const headers={Accept:'application/json'};if(PROXY_TOKEN)headers['X-Review-Token']=PROXY_TOKEN;return headers}
function params(){const query=new URLSearchParams({limit:String(PAGE_SIZE),offset:String((state.page-1)*PAGE_SIZE)});for(const key of ['status','provider','repo_full_name','pull_request_number'])if(state[key])query.set(key,state[key]);return query}
function syncUrl(){const query=new URLSearchParams();for(const key of ['status','provider','repo_full_name','pull_request_number'])if(state[key])query.set(key,state[key]);if(state.page>1)query.set('page',String(state.page));const suffix=query.toString()?`?${query}`:window.location.pathname;history.replaceState(null,'',suffix)}
function syncControls(){document.querySelectorAll('[data-status]').forEach(button=>button.setAttribute('aria-pressed',String(button.dataset.status===state.status)));$('#provider').value=state.provider;$('#repository').value=state.repo_full_name;$('#request-number').value=state.pull_request_number}
function readUrl(){const query=new URLSearchParams(location.search);state.status=statusNames[query.get('status')]?query.get('status'):'';state.provider=['github','gitlab'].includes(query.get('provider'))?query.get('provider'):'';state.repo_full_name=(query.get('repo_full_name')||'').slice(0,512);state.pull_request_number=/^[1-9]\d*$/.test(query.get('pull_request_number')||'')?query.get('pull_request_number'):'';state.page=Math.max(1,Number.parseInt(query.get('page')||'1',10)||1);syncControls()}
function updateRefreshLabel(){const text=document.hidden?'页面隐藏，自动刷新已暂停':`${state.remaining} 秒后自动刷新`;$('#refresh-status').textContent=text}
function updatePagination(){const pages=Math.max(1,Math.ceil(state.total/PAGE_SIZE));let shouldReload=false;if(state.page>pages&&state.total>0){state.page=pages;shouldReload=true}$('#pagination').hidden=state.total===0;$('#page-info').innerHTML=`第 <strong>${state.page}</strong> / <strong>${pages}</strong> 页`;$('#previous').disabled=state.page<=1;$('#next').disabled=state.page>=pages;return shouldReload}
async function loadRuns({background=false,announce=true}={}){if(state.loading&&background)return;state.controller?.abort();const controller=new AbortController();state.controller=controller;state.loading=true;$('#refresh').disabled=true;$('#refresh').classList.add('is-loading');$('#results').setAttribute('aria-busy','true');if(!background)$('#results').innerHTML=renderLoading();try{const response=await fetch(`${API}?${params()}`,{headers:requestHeaders(),signal:controller.signal});if(!response.ok)throw new Error(errorMessage(response));const data=await response.json();state.total=Number(data.total)||0;const items=Array.isArray(data.items)?data.items:[];$('#results').innerHTML=items.length?renderRows(items):renderEmpty();$('#result-summary').textContent=`共 ${state.total} 次执行，本页显示 ${items.length} 条`;if(updatePagination()){setTimeout(()=>loadRuns(),0);return}const now=new Date();$('#updated-at').textContent=`更新于 ${new Intl.DateTimeFormat('zh-CN',{hour:'2-digit',minute:'2-digit',second:'2-digit'}).format(now)}`;if(announce)$('#announcer').textContent=`Review 列表已更新，共 ${state.total} 条记录`;state.remaining=REFRESH_SECONDS;syncUrl()}catch(error){if(error.name==='AbortError')return;$('#results').innerHTML=renderError(error.message||'无法连接 review 服务，请检查网络后重试。');$('#result-summary').textContent='未能取得任务数据';$('#pagination').hidden=true;$('#announcer').textContent='Review 列表加载失败'}finally{if(state.controller===controller){state.loading=false;$('#refresh').disabled=false;$('#refresh').classList.remove('is-loading');$('#results').setAttribute('aria-busy','false');updateRefreshLabel()}}}
document.querySelectorAll('[data-status]').forEach(button=>button.addEventListener('click',()=>{state.status=button.dataset.status;state.page=1;syncControls();loadRuns()}));
$('#filters').addEventListener('submit',event=>{event.preventDefault();state.provider=$('#provider').value;state.repo_full_name=$('#repository').value.trim();state.pull_request_number=$('#request-number').value.trim();state.page=1;loadRuns()});
$('#reset').addEventListener('click',()=>{Object.assign(state,{status:'',provider:'',repo_full_name:'',pull_request_number:'',page:1});syncControls();loadRuns()});
$('#refresh').addEventListener('click',()=>loadRuns());
$('#previous').addEventListener('click',()=>{if(state.page>1){state.page--;loadRuns();scrollToTop()}});
$('#next').addEventListener('click',()=>{if(state.page*PAGE_SIZE<state.total){state.page++;loadRuns();scrollToTop()}});
window.addEventListener('popstate',()=>{readUrl();loadRuns()});
document.addEventListener('visibilitychange',updateRefreshLabel);
setInterval(()=>{if(document.hidden||state.loading)return;state.remaining--;if(state.remaining<=0)loadRuns({background:true,announce:false});else updateRefreshLabel()},1000);
readUrl();loadRuns();
</script>
</body>
</html>'''
