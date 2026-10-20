const $ = (id) => document.getElementById(id);
const labels = {needs_review:"Needs review",approved:"Approved",changes_requested:"Changes requested",blocked:"Blocked",verified:"Verified",missing:"Missing",changed:"Changed",empty:"No evidence"};
let state = {runs:[],events:[]}, selected = null, detail = null, busy = false, requestId = 0;
function el(tag, cls, value) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (value !== undefined) n.textContent = String(value);
  return n;
}
function badge(value) { return el("span", "badge " + (labels[value] ? value : ""), labels[value] || value); }
function message(text, error = false) {
  $("message").hidden = !text;
  $("message").textContent = text;
  $("message").className = error ? "error" : "";
}
async function api(path, body) {
  const options = body ? {method:"POST",headers:{"Content-Type":"application/json","X-ReviewOps-Token":state.csrf_token},body:JSON.stringify(body)} : {};
  const response = await fetch(path, options);
  const value = await response.json();
  if (!response.ok) { const e = new Error(value.error || value.message || "Request failed."); e.status = response.status; throw e; }
  return value;
}
function renderQueue() {
  $("count-total").textContent = state.runs.length;
  $("count-review").textContent = state.runs.filter(r=>r.status==="needs_review"||r.status==="changes_requested").length;
  $("count-blocked").textContent = state.runs.filter(r=>r.status==="blocked").length;
  $("count-approved").textContent = state.runs.filter(r=>r.status==="approved").length;
  const term = $("search").value.toLowerCase();
  const filter = $("status-filter").value;
  const runs = state.runs.filter(r=>(!filter||r.status===filter)&&`${r.id} ${r.title} ${r.owner||""}`.toLowerCase().includes(term));
  $("queue-count").textContent = `${runs.length} shown`;
  const list = $("run-list"); list.replaceChildren();
  if (!runs.length) {list.append(el("p","empty","No experiments match your filters.")); return;}
  for (const run of runs) {
    const button = el("button","run"+(selected===run.id?" selected":""));
    button.type = "button"; button.setAttribute("aria-pressed",String(selected===run.id));
    button.setAttribute("aria-label",`${run.title}, ${labels[run.status]||run.status}`);
    const top=el("div","run-top");top.append(el("span","run-code",run.id),badge(run.status));
    const bottom=el("div","run-bottom"), owner=el("span","");
    owner.append(el("span","avatar",(run.owner||"?").slice(0,1).toUpperCase()),document.createTextNode(run.owner||"Unassigned"));
    const count=run.evidence_count||0;
    bottom.append(owner,el("span","",`${count} evidence ${count===1?"file":"files"}`));
    button.append(top,el("div","run-title",run.title),bottom);
    button.addEventListener("click",()=>{ if (!busy) selectRun(run.id); });
    list.append(button);
  }
}
function renderDetail() {
  if(!detail) return;
  $("detail-empty").hidden=true; $("detail-content").hidden=false;
  const run=detail.run;
  $("run-id").textContent=run.id; $("run-status").replaceChildren(badge(run.status));
  $("detail-title").textContent=run.title; $("run-summary").textContent=run.summary;
  $("version").textContent=`Revision ${run.version}`;
  $("owner").value=run.owner||"";
  $("evidence-summary").replaceChildren(badge(run.evidence_status));
  $("evidence-list").replaceChildren();
  for(const [index,file] of (detail.evidence||[]).entries()) {
    const item=el("div","evidence-item"), head=el("header","");
    head.append(el("span","evidence-name",file.name),badge(file.status)); item.append(head);
    for(const [label,hash] of [["Expected",file.expected_sha256],["Current",file.actual_sha256]]) {
      const row=el("div","hash-row");row.append(el("span","",label),el("code","",hash||"File unavailable"));item.append(row);
    }
    if(file.actual_sha256) {
      const preview=el("details","evidence-preview"), summary=el("summary","","Read evidence");
      const content=el("pre","","Loading…");preview.append(summary,content);
      const runId=run.id;let loaded=false;
      preview.addEventListener("toggle",async()=>{
        if(!preview.open||loaded)return;loaded=true;
        try{
          const value=await api(`/api/runs/${encodeURIComponent(runId)}/evidence/${index}`);
          content.textContent=value.text+(value.truncated?"\n\n[Preview limited to the first 32 KiB.]":"");
        }catch(e){content.textContent=e.message;loaded=false;}
      });
      item.append(preview);
    }
    $("evidence-list").append(item);
  }
  if(!detail.evidence?.length) $("evidence-list").append(el("p","empty","No evidence has been attached."));
  $("approve").disabled=busy||run.evidence_status!=="verified";
  $("changes").disabled=busy; $("assign").disabled=busy;
  $("approval-reason").textContent=run.evidence_status==="verified" ? "Record what you checked before approving." : "Approval is blocked until every required evidence file is present and matches its recorded hash.";
  $("history").replaceChildren();
  const events=[...(detail.events||[])];
  if(!events.length) $("history").append(el("li","muted","No decisions recorded yet."));
  for(const event of events) {
    const item=el("li","");
    item.append(el("div","event-title",`${event.actor||"System"} · ${(event.action||"event").replaceAll("_"," ")}`));
    const date=new Date(event.created_at);
    const when=Number.isNaN(date.valueOf()) ? event.created_at : date.toLocaleString();
    item.append(el("div","event-time",when+(event.run_version ? ` · Revision ${event.run_version}` : "")));
    if(event.note) item.append(el("div","event-note",event.note));
    $("history").append(item);
  }
}
async function selectRun(id) {
  if(selected!==id)message("");
  const ticket=++requestId;
  try {
    const result=await api(`/api/runs/${encodeURIComponent(id)}`);
    if(ticket!==requestId)return;
    selected=id; detail=result; $("note").value="";renderQueue();renderDetail();
  }catch(e){message(e.message,true);}
}
async function refresh(showMessage=false) {
  state=await api("/api/state");renderQueue();
  if(selected && state.runs.some(r=>r.id===selected)) await selectRun(selected);
  else if(state.runs.length) await selectRun(state.runs[0].id);
  if(showMessage)message("Evidence refreshed against the recorded hashes.");
}
async function mutate(action) {
  if(busy||!detail)return;
  const actor=$("actor").value.trim();
  if(!actor){message("Enter your name to record this action.",true);$("actor").focus();return;}
  const note=$("note").value.trim(), owner=$("owner").value.trim();
  if(action==="assign"&&!owner){message("Enter a reviewer name.",true);$("owner").focus();return;}
  if(action!=="assign"&&!note){message("Add a decision note explaining what you checked.",true);$("note").focus();return;}
  busy=true;renderDetail();
  try {
    const body={actor,expected_version:detail.run.version};
    if(action==="assign")body.owner=owner;else{body.decision=action;body.note=note;}
    await api(`/api/runs/${encodeURIComponent(selected)}/${action==="assign"?"assign":"decision"}`,body);
    await refresh();
    message(action==="assign"?"Reviewer assignment saved.":action==="approved"?"Review approved and recorded in the audit history.":"Changes requested and recorded in the audit history.");
  }catch(e){
    if(e.status===409){await refresh();message("This experiment changed. The latest revision is loaded; inspect it before trying again.",true);}
    else message(e.message,true);
  }finally{busy=false;renderDetail();}
}
$("search").addEventListener("input",renderQueue);
$("status-filter").addEventListener("change",renderQueue);
$("refresh").addEventListener("click",()=>{if(!busy)refresh(true).catch(e=>message(e.message,true));});
$("assign").addEventListener("click",()=>mutate("assign"));
$("approve").addEventListener("click",()=>mutate("approved"));
$("changes").addEventListener("click",()=>mutate("changes_requested"));
$("export").addEventListener("click",async()=>{
  try{
    const snapshot=await api("/api/export");
    const url=URL.createObjectURL(new Blob([JSON.stringify(snapshot,null,2)],{type:"application/json"}));
    const a=el("a","");a.href=url;a.download="reviewops-audit.json";a.click();
    setTimeout(()=>URL.revokeObjectURL(url),1000);message("Audit export downloaded.");
  }catch(e){message(e.message,true);}
});
refresh().catch(e=>message("Could not load the local workspace: "+e.message,true));
