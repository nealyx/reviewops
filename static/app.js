const $ = (id) => document.getElementById(id);
const labels = {needs_review:"Needs review",approved:"Approved",changes_requested:"Changes requested",blocked:"Blocked",verified:"Verified",missing:"Missing",changed:"Changed",empty:"No evidence"};
const limits = {files:20, each:256 * 1024, total:1024 * 1024};
let state = {runs:[],events:[]}, selected = null, detail = null, busy = false, requestId = 0, reviewStale = false;
let draft = null;
const drafts = new Map();
const reviewDrafts = new Map();
const fields = {id:"submission-id",title:"submission-run-title",summary:"submission-summary",owner:"submission-owner",actor:"submission-actor",note:"submission-note"};
function el(tag, cls, value) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (value !== undefined) n.textContent = String(value);
  return n;
}
function badge(value) { return el("span", "badge " + (labels[value] ? value : ""), labels[value] || value); }
function when(value) { const d=new Date(value); return Number.isNaN(d.valueOf()) ? value : d.toLocaleString(); }
function shortId(value) { return String(value).slice(0, 10); }
function sizeLabel(bytes) { return bytes < 1024 ? `${bytes} B` : `${(bytes / 1024).toFixed(1)} KiB`; }
function message(text, error = false) {
  $("message").hidden = !text; $("message").textContent = text; $("message").className = error ? "error" : "";
}
function submissionMessage(text, error=false) {
  $("submission-message").hidden=!text; $("submission-message").textContent=text;
  $("submission-message").className="form-message"+(error?" error":"");
  // Errors must be visible even when the submit button is at the bottom of a
  // scrolled dialog. This target is outside the temporarily disabled fieldset.
  if(text&&error&&$("submission-dialog").open)$("submission-message").focus();
}
async function api(path, body) {
  const options = body === undefined ? {} : {method:"POST",headers:{"Content-Type":"application/json","X-ReviewOps-Token":state.csrf_token},body:JSON.stringify(body)};
  let response;
  try { response=await fetch(path, options); }
  catch { throw new Error("Could not reach the local workspace. Check that it is running, then try again."); }
  let value;
  try { value=await response.json(); }
  catch { throw new Error("The workspace returned an unreadable response. Refresh before trying again."); }
  if (!response.ok) { const e=new Error(value.error||value.message||"Request failed."); e.status=response.status; throw e; }
  return value;
}
function syncControls() {
  $("refresh").disabled=busy; $("export").disabled=busy||!state.csrf_token;
  $("add-experiment").disabled=busy||!state.csrf_token;
  $("submit-revision").disabled=busy||!detail;
  $("approve").disabled=busy||reviewStale||!detail||detail.run.evidence_status!=="verified";
  $("changes").disabled=busy||reviewStale||!detail; $("assign").disabled=busy||reviewStale||!detail;
  $("submission-fields").disabled=busy;
  $("submission-close").disabled=busy; $("submission-cancel").disabled=busy;
  $("submission-refresh").disabled=busy;
  $("submission-save").disabled=busy||Boolean(draft&&(draft.stale||fileIssue(draft)||draft.files.some(f=>f.reading||f.content===undefined)));
  if(draft) $("submission-save").textContent=busy?"Saving…":draft.mode==="revision"?"Submit revision":"Add experiment";
}
function setBusy(value) { busy=value; syncControls(); }
function renderWorkspaceNotice() {
  const demo=state.runs.filter(r=>r.is_demo).length, real=state.runs.length-demo;
  const historicalUserData=demo&&!real&&state.demo===false;
  $("workspace-kind").textContent=!state.runs.length?"Empty workspace":demo&&real||historicalUserData?"Mixed workspace":demo?"Synthetic demo":"Local workspace";
  const lead=!state.runs.length?"This workspace is empty. Add an experiment and its text evidence to begin.":historicalUserData?"Current records are marked synthetic, but retained history contains user submissions. Review the full history before sharing an export.":demo&&real?"This workspace contains synthetic examples and user submissions. Each record is labeled.":demo?"The current experiments are invented examples. Add an experiment to review your own text evidence.":"This local workspace contains user-submitted experiments.";
  $("workspace-notice").textContent=lead+" A recorded approval is a reviewer’s decision, not a scientific validation or proof.";
}
function renderQueue(restoreFocus=true) {
  renderWorkspaceNotice();
  $("count-total").textContent=state.runs.length;
  $("count-review").textContent=state.runs.filter(r=>r.status==="needs_review"||r.status==="changes_requested").length;
  $("count-blocked").textContent=state.runs.filter(r=>r.status==="blocked").length;
  $("count-approved").textContent=state.runs.filter(r=>r.status==="approved").length;
  const term=$("search").value.toLowerCase(), filter=$("status-filter").value;
  const runs=state.runs.filter(r=>(!filter||r.status===filter)&&`${r.id} ${r.title} ${r.owner||""}`.toLowerCase().includes(term));
  $("queue-count").textContent=`${runs.length} shown`;
  const list=$("run-list");
  const focusedId=restoreFocus&&list.contains(document.activeElement)?document.activeElement.dataset.runId:null;
  let focusTarget=null; list.replaceChildren();
  if(!runs.length) {
    list.append(el("p","empty",state.runs.length?"No experiments match your filters.":"Add your first experiment to begin a review."));
    if(focusedId)$("search").focus();
    return;
  }
  for(const run of runs) {
    const button=el("button","run"+(selected===run.id?" selected":""));
    button.dataset.runId=run.id;
    if(run.id===focusedId)focusTarget=button;
    button.type="button"; button.setAttribute("aria-pressed",String(selected===run.id));
    button.setAttribute("aria-label",`${run.title}, ${labels[run.status]||run.status}, ${run.is_demo?"synthetic example":"user submission"}`);
    const top=el("div","run-top"); top.append(el("span","run-code",run.id),badge(run.status));
    const bottom=el("div","run-bottom"), owner=el("span","");
    owner.append(el("span","avatar",(run.owner||"?").slice(0,1).toUpperCase()),document.createTextNode(run.owner||"Unassigned"));
    const count=run.evidence_count||0;
    bottom.append(owner,el("span","",`${count} ${count===1?"file":"files"} · ${run.is_demo?"Demo":"Submitted"}`));
    button.append(top,el("div","run-title",run.title),bottom);
    button.addEventListener("click",()=>{if(!busy)return selectRun(run.id,button);}); list.append(button);
  }
  // A response may arrive after the user has advanced to another queue item.
  if(focusedId) {
    if(focusTarget)focusTarget.focus({preventScroll:true});
    else $("search").focus();
  }
}
function evidencePreview(path, label="Read evidence") {
  const preview=el("details","evidence-preview"), summary=el("summary","",label), content=el("pre","","Loading…");
  preview.append(summary,content); let loaded=false, loading=false;
  preview.addEventListener("toggle",async()=>{
    if(!preview.open||loaded||loading)return;
    loading=true;
    try {
      const value=await api(path);
      const warning=value.status&&value.status!=="verified"?`Evidence status: ${labels[value.status]||value.status}.\n\n`:"";
      content.textContent=warning+value.text+(value.truncated?"\n\n[Preview limited to the first 32 KiB.]":""); loaded=true;
    } catch(e) {content.textContent=e.message+" Close and reopen to retry.";}
    finally {loading=false;}
  });
  return preview;
}
function hashRow(label, hash) {
  const row=el("div","hash-row"); row.append(el("span","",label),el("code","",hash||"File unavailable")); return row;
}
function compareEvidence(previous, current) {
  const before=new Map((previous.evidence||[]).map((file,index)=>[file.name,{...file,index}]));
  const after=new Map((current.evidence||[]).map((file,index)=>[file.name,{...file,index}]));
  return [...new Set([...before.keys(),...after.keys()])].sort().map(name=>{
    const oldFile=before.get(name), newFile=after.get(name);
    const change=!oldFile?"added":!newFile?"removed":oldFile.sha256===newFile.sha256?"unchanged":"changed";
    return {name,change,before:oldFile,after:newFile};
  });
}
function renderComparison() {
  const section=$("revision-comparison"), list=$("comparison-files");
  list.replaceChildren(); $("comparison-metadata").replaceChildren();
  const snapshots=detail.snapshots||[];
  const current=snapshots.find(snapshot=>snapshot.id===detail.run.snapshot_id);
  const previous=current&&snapshots.filter(snapshot=>snapshot.run_version<current.run_version)
    .sort((a,b)=>b.run_version-a.run_version)[0];
  section.hidden=!previous;
  if(!previous)return;
  const before=previous.manifest||{}, after=current.manifest||{};
  const changes=compareEvidence(before,after), counts={added:0,changed:0,removed:0,unchanged:0};
  for(const file of changes)counts[file.change]++;
  $("comparison-summary").textContent=`${counts.changed} changed · ${counts.added} added · ${counts.removed} removed · ${counts.unchanged} unchanged`;
  const reference=$("comparison-reference");
  reference.textContent=`Submission at record version ${previous.run_version} → ${current.run_version}`;
  reference.title=`${previous.id} → ${current.id}`;
  for(const [key,label] of [["title","Title"],["summary","Summary"],["owner","Reviewer at submission"],["is_demo","Evidence label"]]) {
    if(before[key]===after[key])continue;
    const values=key==="is_demo"
      ? [before.is_demo?"Synthetic example":"User submission",after.is_demo?"Synthetic example":"User submission"]
      : [before[key]||"Not set",after[key]||"Not set"];
    const item=el("li","metadata-change");
    item.append(el("strong","",`${label}: `),el("span","",values[0]),el("span","change-arrow"," → "),el("span","",values[1]));
    $("comparison-metadata").append(item);
  }
  if(!changes.length)list.append(el("p","small muted","Both submissions contain no evidence files."));
  for(const file of changes) {
    const item=el("div","comparison-file"), heading=el("div","comparison-file-heading");
    heading.append(el("strong","evidence-name",file.name),el("span",`change-label ${file.change}`,file.change));
    item.append(heading);
    const path=(snapshot,index)=>`/api/runs/${encodeURIComponent(detail.run.id)}/snapshots/${encodeURIComponent(snapshot.id)}/evidence/${index}`;
    if(file.before&&file.change!=="unchanged")item.append(evidencePreview(path(previous,file.before.index),"Read previous file"));
    if(file.after)item.append(evidencePreview(path(current,file.after.index),"Read current file"));
    list.append(item);
  }
}
function renderSnapshots() {
  const list=$("snapshot-list"); list.replaceChildren();
  const snapshots=detail.snapshots||[];
  $("snapshot-count").textContent=`${snapshots.length} saved`;
  if(!snapshots.length) {list.append(el("p","snapshot-empty","No submitted snapshots yet. These seeded files remain the current evidence until a revision is submitted.")); return;}
  for(const snapshot of snapshots) {
    const current=snapshot.id===detail.run.snapshot_id, card=el("details","snapshot"+(current?" current":""));
    const summary=el("summary","snapshot-heading"), title=el("span","snapshot-title",`${current?"Current":"Previous"} evidence · ${shortId(snapshot.id)}`);
    title.title=snapshot.id; summary.append(title,el("span","small muted",when(snapshot.created_at))); card.append(summary);
    const body=el("div","snapshot-body"), manifest=snapshot.manifest||{};
    body.append(el("h4","",manifest.title||"Saved evidence"),el("p","small muted",manifest.summary||""));
    body.append(el("p","small",`${manifest.legacy_capture?"Captured existing evidence at":"Submitted at"} record version ${snapshot.run_version} · ${manifest.is_demo?"Synthetic demonstration":"User submission"}`));
    if(manifest.owner!==undefined)body.append(el("p","small muted",`Reviewer at ${manifest.legacy_capture?"capture":"submission"}: ${manifest.owner||"Unassigned"}`));
    if(manifest.legacy_capture)body.append(el("p","small muted","This snapshot captures an existing record. Earlier history entries may not identify an evidence snapshot."));
    body.append(hashRow("Snapshot",snapshot.id),hashRow("Manifest",snapshot.manifest_sha256));
    for(const [index,file] of (manifest.evidence||[]).entries()) {
      const item=el("div","snapshot-file"), head=el("div","snapshot-file-heading");
      head.append(el("strong","evidence-name",file.name),el("span","small muted",file.size_bytes==null?"Size not captured":sizeLabel(file.size_bytes))); item.append(head,hashRow("SHA-256",file.sha256));
      item.append(evidencePreview(`/api/runs/${encodeURIComponent(detail.run.id)}/snapshots/${encodeURIComponent(snapshot.id)}/evidence/${index}`,"Read saved snapshot evidence")); body.append(item);
    }
    if(!manifest.evidence?.length) body.append(el("p","small muted","This snapshot contains no evidence files."));
    const raw=el("details","manifest-preview"), pre=el("pre","",JSON.stringify(manifest,null,2));
    raw.append(el("summary","","View full manifest"),pre); body.append(raw); card.append(body); list.append(card);
  }
}
function renderDetail() {
  if(!detail)return;
  $("detail-empty").hidden=true; $("detail-content").hidden=false;
  const run=detail.run;
  $("run-id").textContent=run.id; $("run-status").replaceChildren(badge(run.status));
  $("detail-title").textContent=run.title; $("run-summary").textContent=run.summary;
  $("run-kind").textContent=run.is_demo?"Synthetic example":"User submission";
  $("version").textContent=`Record version ${run.version}`; $("owner").value=run.owner||"";
  $("current-snapshot").textContent=run.snapshot_id?`Current evidence snapshot: ${shortId(run.snapshot_id)}`:"Seeded evidence · no submitted snapshot";
  $("current-snapshot").title=run.snapshot_id||"";
  $("evidence-summary").replaceChildren(badge(run.evidence_status)); $("evidence-list").replaceChildren();
  for(const [index,file] of (detail.evidence||[]).entries()) {
    const item=el("div","evidence-item"), head=el("header","");
    head.append(el("span","evidence-name",file.name),badge(file.status)); item.append(head,hashRow("Expected",file.expected_sha256),hashRow("Current",file.actual_sha256));
    if(file.actual_sha256) {
      // A later revision must not replace the content beneath this displayed
      // filename/hash while the reviewer is still inspecting the older detail.
      const path=run.snapshot_id
        ? `/api/runs/${encodeURIComponent(run.id)}/snapshots/${encodeURIComponent(run.snapshot_id)}/evidence/${index}`
        : `/api/runs/${encodeURIComponent(run.id)}/evidence/${index}`;
      item.append(evidencePreview(path));
    }
    $("evidence-list").append(item);
  }
  if(!detail.evidence?.length) $("evidence-list").append(el("p","empty","No evidence has been attached. Submit a revision to add files."));
  $("approval-reason").textContent=run.evidence_status==="verified"?"Record what you checked before approving.":"Approval is blocked until every required evidence file is present and matches its recorded hash.";
  renderComparison(); renderSnapshots(); $("history").replaceChildren();
  if(!detail.events?.length) $("history").append(el("li","muted","No actions recorded yet."));
  for(const event of detail.events||[]) {
    const item=el("li","");
    item.append(el("div","event-title",`${event.actor||"System"} · ${(event.action||"event").replaceAll("_"," ")}`));
    item.append(el("div","event-time",when(event.created_at)+(event.run_version?` · Record version ${event.run_version}`:"")));
    if(event.snapshot_id) {const reference=el("div","event-snapshot",`Evidence snapshot ${shortId(event.snapshot_id)}`); reference.title=event.snapshot_id; item.append(reference);}
    if(event.note)item.append(el("div","event-note",event.note)); $("history").append(item);
  }
  renderDraftWarning(); syncControls();
}
function rememberReviewDraft() {
  if(!selected||!detail)return;
  const note=$("note").value;
  if(!note){reviewDrafts.delete(selected);return;}
  const prior=reviewDrafts.get(selected);
  reviewDrafts.set(selected,{note,snapshotId:prior?prior.snapshotId:detail.run.snapshot_id});
}
function renderDraftWarning() {
  const saved=reviewDrafts.get(selected);
  $("review-draft-warning").hidden=!(saved&&saved.note&&detail&&saved.snapshotId!==detail.run.snapshot_id);
}
function restoreReviewDraft() {
  $("note").value=reviewDrafts.get(selected)?.note||"";
  renderDraftWarning();
}
async function selectRun(id, activatedButton=null) {
  if(selected!==id)message(""); const ticket=++requestId;
  try {
    const result=await api(`/api/runs/${encodeURIComponent(id)}`); if(ticket!==requestId)return;
    // Rebuilding the queue removes its focused button. Move an explicit
    // activation into the detail, but never interrupt focus moved during loading.
    const focusDetail=activatedButton&&document.activeElement===activatedButton;
    // Capture at response time as the reviewer may keep typing while it loads.
    rememberReviewDraft();
    selected=id; detail=result; reviewStale=false; restoreReviewDraft(); renderQueue(!focusDetail); renderDetail();
    if(focusDetail)$("detail-title").focus();
    return true;
  } catch(e) {message(e.message,true);return false;}
}
async function refresh(showMessage=false) {
  state=await api("/api/state"); renderQueue();
  if(selected&&state.runs.some(r=>r.id===selected)) {
    if(await selectRun(selected)===false)throw new Error("The selected experiment could not be refreshed.");
  } else if(state.runs.length) {
    if(await selectRun(state.runs[0].id)===false)throw new Error("The selected experiment could not be loaded.");
  }
  else {rememberReviewDraft();selected=null; detail=null;restoreReviewDraft(); $("detail-content").hidden=true; $("detail-empty").hidden=false;}
  syncControls(); if(showMessage)message("Evidence refreshed against the recorded hashes. Any decision note is kept.");
}
async function mutate(action) {
  if(busy||reviewStale||!detail)return;
  const runId=selected, originalNote=$("note").value;
  const actor=$("actor").value.trim(), note=$("note").value.trim(), owner=$("owner").value.trim();
  if(!actor){message("Enter your name to record this action.",true);$("actor").focus();return;}
  if(action!=="assign"&&!note){message("Add a decision note explaining what you checked.",true);$("note").focus();return;}
  setBusy(true); let saved=false;
  try {
    const body={actor,expected_version:detail.run.version};
    if(action==="assign")body.owner=owner; else {body.decision=action;body.note=note;}
    await api(`/api/runs/${encodeURIComponent(runId)}/${action==="assign"?"assign":"decision"}`,body);
    saved=true;
    // Clear only the note that was actually recorded, retaining any new typing.
    if(action!=="assign") {
      if(reviewDrafts.get(runId)?.note===originalNote)reviewDrafts.delete(runId);
      if(selected===runId&&$("note").value===originalNote)$("note").value="";
    }
    await refresh();
    message(action==="assign"?"Reviewer assignment saved.":action==="approved"?"Review approved and recorded in the audit history.":"Changes requested and recorded in the audit history.");
  } catch(e) {
    if(saved){reviewStale=true;message("Your action was saved, but the workspace could not refresh. Use Refresh evidence before recording another action.",true);}
    else if(e.status===409){reviewStale=true;message("This record changed elsewhere. Refresh evidence and inspect the latest record before trying again. Your note is kept.",true);}
    else message(e.message,true);
  } finally {setBusy(false);}
}
function captureDraft() {
  if(!draft)return;
  for(const [key,id] of Object.entries(fields))draft[key]=$(id).value;
  draft.is_demo=$("submission-demo").checked;
}
function fileIssue(value) {
  if(value.files.length>limits.files)return "Choose no more than 20 files. Remove extras below.";
  if(value.files.reduce((n,f)=>n+f.file.size,0)>limits.total)return "Selected evidence exceeds 1 MiB. Remove files to continue.";
  const names=new Set();
  for(const item of value.files) {
    const name=item.file.name;
    if(!/^[A-Za-z0-9][A-Za-z0-9_. -]{0,119}$/.test(name)||name.endsWith("."))return `Rename “${name}” on your computer: use a simple filename starting with a letter or number.`;
    if(names.has(name.toLowerCase()))return `“${name}” appears more than once. Filenames must be unique, ignoring letter case.`;
    names.add(name.toLowerCase());
    if(item.file.size>limits.each)return `“${name}” exceeds 256 KiB. Remove it or choose a smaller file.`;
    if(item.error)return item.error;
  }
  return "";
}
function renderFiles() {
  if(!draft)return;
  const list=$("submission-file-list");
  const focusedIndex=Array.from(list.children).findIndex(row=>row.contains(document.activeElement));
  const removeButtons=[]; list.replaceChildren();
  const total=draft.files.reduce((n,item)=>n+item.file.size,0);
  $("file-summary").textContent=draft.files.length?`${draft.files.length}/20 files · ${sizeLabel(total)}`:"No files selected";
  $("no-evidence-note").hidden=draft.files.length>0;
  for(const item of draft.files) {
    const row=el("div","selected-file"), info=el("div","selected-file-info");
    info.append(el("strong","",item.file.name),el("span","small muted",`${sizeLabel(item.file.size)} · ${item.error?"Needs attention":item.reading?"Reading…":item.content===undefined?"Waiting":"UTF-8 text"}`));
    const remove=el("button","remove-file","Remove"); remove.type="button"; remove.disabled=busy; remove.setAttribute("aria-label",`Remove ${item.file.name}`);
    remove.addEventListener("click",()=>{if(busy)return;draft.files=draft.files.filter(f=>f!==item);draft.fileMessage="";renderFiles();return decodeFiles(draft);});
    removeButtons.push(remove); row.append(info,remove);list.append(row);
  }
  const issue=fileIssue(draft);
  if(issue)list.append(el("p","file-error",issue));
  syncControls();
  // Retain keyboard position through removal and asynchronous decoding redraws.
  // After removal choose the next file, then the previous one, then the picker.
  if(focusedIndex>=0)(removeButtons[Math.min(focusedIndex,removeButtons.length-1)]||$("submission-files")).focus();
}
async function decodeFiles(value) {
  if(value.files.length>limits.files||value.files.reduce((n,f)=>n+f.file.size,0)>limits.total)return;
  const pending=value.files.filter(item=>item.content===undefined&&!item.reading&&!item.error&&item.file.size<=limits.each);
  for(const item of pending)item.reading=true;
  if(draft===value)renderFiles();
  await Promise.all(pending.map(async item=>{
    try {
      const data=await item.file.arrayBuffer();
      item.content=new TextDecoder("utf-8",{fatal:true,ignoreBOM:true}).decode(data);
    } catch {item.error=`“${item.file.name}” could not be read as UTF-8 text. Remove it and choose a UTF-8 text file.`;}
    finally {item.reading=false;}
  }));
  if(draft===value)renderFiles();
}
function renderSubmissionContext() {
  if(!draft)return;
  $("submission-conflict").hidden=!draft.stale;
  $("submission-context").textContent=draft.mode==="revision"?`Replaces evidence for ${draft.id} · Based on record version ${draft.expectedVersion}`:"Creates a new experiment and its first evidence snapshot.";
  syncControls();
}
function showLatestRecord(value) {
  const content=$("submission-latest-content"); content.replaceChildren();
  const run=value.run;
  content.append(el("strong","",run.title),el("p","small muted",run.summary));
  content.append(el("p","small",`Record version ${run.version} · ${labels[run.status]||run.status} · ${run.is_demo?"Synthetic demonstration":"User submission"}`));
  content.append(el("p","small muted",`Assigned reviewer: ${run.owner||"Unassigned"}`));
  const list=el("ul","latest-files");
  for(const file of value.evidence||[])list.append(el("li","",`${file.name} · ${labels[file.status]||file.status}`));
  if(!value.evidence?.length)list.append(el("li","","No current evidence files."));
  content.append(list); $("submission-latest").hidden=false;
}
function openSubmission(mode) {
  if(busy||!state.csrf_token||mode==="revision"&&!detail)return;
  const run=mode==="revision"?detail.run:null, key=run?`revision:${run.id}`:"new";
  draft=drafts.get(key);
  if(!draft) {
    draft={key,mode,id:run?.id||"",title:run?.title||"",summary:run?.summary||"",owner:run?.owner||"",actor:$("actor").value,note:"",is_demo:run?Boolean(run.is_demo):false,expectedVersion:run?.version,files:[],stale:false};
    drafts.set(key,draft);
  }
  if(run&&draft.expectedVersion!==run.version)draft.stale=true;
  for(const [name,id] of Object.entries(fields))$(id).value=draft[name];
  $("submission-demo").checked=draft.is_demo; $("submission-id").readOnly=mode==="revision";
  $("submission-title").textContent=mode==="revision"?"Submit revision":"Add experiment";
  $("submission-description").textContent=mode==="revision"?"Submit a complete replacement evidence set and explain what changed. Earlier snapshots and decisions remain in the history.":"Bring a question and its text evidence into the review queue. You decide whether the record is a synthetic example.";
  $("revision-file-note").hidden=mode!=="revision";
  $("submission-latest").hidden=true; $("submission-latest").open=false;
  if(run)showLatestRecord(detail);
  $("submission-files").value=""; submissionMessage(""); renderFiles(); renderSubmissionContext();
  $("submission-dialog").showModal();
  (mode==="revision"?$("submission-run-title"):$("submission-id")).focus();
}
function closeSubmission() {if(busy)return;captureDraft();$("submission-dialog").close();}
async function refreshSubmission() {
  if(busy||!draft||draft.mode!=="revision")return;
  captureDraft();setBusy(true);
  try {
    const latest=await api(`/api/runs/${encodeURIComponent(draft.id)}`);
    draft.expectedVersion=latest.run.version; draft.stale=false;
    showLatestRecord(latest); $("submission-latest").open=true;
    if(selected===draft.id){detail=latest;reviewStale=false;renderDetail();}
    state=await api("/api/state");renderQueue();
    submissionMessage("Latest record loaded below. Review it before submitting; your draft and selected files are unchanged.");
  } catch(e) {submissionMessage(e.message,true);}
  finally {setBusy(false);renderSubmissionContext();}
}
async function submitExperiment(event) {
  event.preventDefault(); if(busy||!draft||draft.stale)return;
  captureDraft(); if(!$("submission-form").reportValidity())return;
  const issue=fileIssue(draft);
  if(issue){submissionMessage(issue,true);return;}
  if(draft.files.some(f=>f.reading||f.content===undefined)){submissionMessage("Wait for the selected files to finish reading.",true);return;}
  const body={};for(const key of Object.keys(fields))body[key]=draft[key].trim();
  if(draft.mode==="new"&&state.runs.some(run=>run.id===body.id)) {
    submissionMessage("An experiment already uses this ID. Choose a different ID, or close this draft and use Submit revision on the existing record.",true);
    $("submission-id").focus();return;
  }
  body.is_demo=draft.is_demo;body.evidence=draft.files.map(item=>({name:item.file.name,content:item.content}));
  if(draft.mode==="revision")body.expected_version=draft.expectedVersion;
  const current=draft;setBusy(true);submissionMessage("");
  try {
    const result=await api("/api/submissions",body);
    drafts.delete(current.key);$("submission-dialog").close();draft=null;
    rememberReviewDraft();
    selected=result.run?.id||body.id;detail=result.run?result:null;restoreReviewDraft();
    $("search").value="";$("status-filter").value="";
    await refresh();
    message(current.mode==="revision"?"Revision submitted. The new evidence is ready for review, prior approval has reset, and earlier snapshots are retained.":"Experiment added. Its first evidence snapshot is ready for review.");
  } catch(e) {
    if(draft===current) {
      if(e.status===409&&current.mode==="revision") {current.stale=true;submissionMessage("This record changed elsewhere. Refresh the latest record before submitting. Your draft and files are kept.",true);}
      else submissionMessage(e.message+" Your draft and selected files are kept.",true);
      renderSubmissionContext();
    } else message("Submission was saved, but the workspace could not refresh: "+e.message+" Use Refresh evidence to load it.",true);
  } finally {setBusy(false);}
}
$("search").addEventListener("input",()=>renderQueue());
$("status-filter").addEventListener("change",()=>renderQueue());
$("refresh").addEventListener("click",async()=>{if(busy)return;setBusy(true);try{await refresh(true);}catch(e){message(e.message,true);}finally{setBusy(false);}});
$("note").addEventListener("input",()=>{rememberReviewDraft();renderDraftWarning();});
$("assign").addEventListener("click",()=>mutate("assign"));
$("approve").addEventListener("click",()=>mutate("approved"));
$("changes").addEventListener("click",()=>mutate("changes_requested"));
$("add-experiment").addEventListener("click",()=>openSubmission("new"));
$("submit-revision").addEventListener("click",()=>openSubmission("revision"));
$("submission-close").addEventListener("click",closeSubmission);
$("submission-cancel").addEventListener("click",closeSubmission);
$("submission-dialog").addEventListener("cancel",event=>{if(busy)event.preventDefault();else captureDraft();});
$("submission-form").addEventListener("input",captureDraft);
$("submission-form").addEventListener("submit",submitExperiment);
$("submission-refresh").addEventListener("click",refreshSubmission);
$("submission-files").addEventListener("change",()=>{
  if(!draft||busy)return;
  for(const file of $("submission-files").files)draft.files.push({file,reading:false,content:undefined,error:""});
  $("submission-files").value="";renderFiles();decodeFiles(draft);
});
$("export").addEventListener("click",async()=>{
  if(busy)return;setBusy(true);
  try {
    const snapshot=await api("/api/export"), url=URL.createObjectURL(new Blob([JSON.stringify(snapshot,null,2)],{type:"application/json"}));
    const a=el("a","");a.href=url;a.download="reviewops-audit.json";a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
    message(!state.runs.length?"Audit export downloaded for an empty workspace.":snapshot.demo?"Audit export downloaded with synthetic records.":"Audit export downloaded, including user-submitted records.");
  }catch(e){message(e.message,true);}finally{setBusy(false);}
});
syncControls();refresh().catch(e=>message("Could not load the local workspace: "+e.message,true));
