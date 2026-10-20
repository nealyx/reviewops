/* Small behavior regressions using the actual UI script, no browser packages. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../static/app.js"), "utf8");

class Element {
  constructor(tag = "div", ownerDocument = null) {
    this.ownerDocument = ownerDocument;
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.listeners = {};
    this.dataset = {};
    this.value = "";
    this.textContent = "";
    this.hidden = false;
  }
  append(...children) { this.children.push(...children); }
  contains(node) {
    return this === node || this.children.some((child) => child.contains(node));
  }
  replaceChildren(...children) {
    // Model the browser's lost focus when a focused descendant is removed.
    const losesFocus = this.children.some((child) => child.contains(this.ownerDocument?.activeElement));
    this.children = children;
    if (losesFocus) this.ownerDocument.activeElement = this.ownerDocument.body;
  }
  setAttribute(name, value) { this[name] = value; }
  addEventListener(name, listener) { this.listeners[name] = listener; }
  focus() { this.ownerDocument.activeElement = this; }
  showModal() { this.open = true; }
  close() { this.open = false; }
  reportValidity() { return true; }
  click() {
    if(this.tagName === "A") this.ownerDocument.downloads.push({href: this.href, filename: this.download});
  }
}

function harness() {
  const elements = new Map();
  const document = {};
  document.downloads = [];
  const blobs = new Map();
  document.body = new Element("body", document);
  document.activeElement = document.body;
  const get = (id) => {
    if (!elements.has(id)) elements.set(id, new Element("div", document));
    return elements.get(id);
  };
  const initial = {
    run: {
      id: "RUN-1", title: "Original package", summary: "Review this evidence.",
      owner: "Original reviewer", version: 1, snapshot_id: "snapshot-a",
      is_demo: false, status: "needs_review", evidence_status: "verified",
      evidence_count: 1,
    },
    evidence: [{
      name: "original.txt", status: "verified",
      expected_sha256: "a".repeat(64), actual_sha256: "a".repeat(64),
    }],
    events: [], snapshots: [],
  };
  Object.assign(document, {
    getElementById: get,
    createElement: (tag) => new Element(tag, document),
    createTextNode: (text) => Object.assign(new Element("#text", document), {textContent: text}),
  });
  const context = vm.createContext({
    document,
    // Hold startup refresh; each test supplies deterministic API responses.
    fetch: () => new Promise(() => {}),
    console, TextDecoder, URL: class extends URL {
      static createObjectURL(blob) {const url=`blob:test-${blobs.size}`; blobs.set(url,blob); return url;}
      static revokeObjectURL() {}
    }, Blob, setTimeout,
    initial,
  });
  vm.runInContext(source, context);
  vm.runInContext(
    'state={runs:[initial.run],events:[],csrf_token:"test",demo:false};'
    + 'selected=initial.run.id; detail=initial; renderDetail();', context);
  return {
    get, initial, document,
    blobs,
    fetchWith: (callback) => {context.fetch = callback;},
    evaluate: (code) => vm.runInContext(code, context),
    respond: (callback) => {
      context.respond = callback;
      vm.runInContext("api=async (path,body)=>respond(path,body)", context);
    },
  };
}

test("queue activation moves keyboard focus to the loaded experiment heading", async () => {
  const ui = harness();
  ui.respond(() => ui.initial);
  ui.evaluate("renderQueue()");
  const button = ui.get("run-list").children[0];
  button.focus();
  await button.listeners.click();
  assert.equal(ui.document.activeElement, ui.get("detail-title"));
  assert.equal(ui.get("detail-title").textContent, "Original package");
  assert.notEqual(ui.get("run-list").children[0], button);
});

test("a delayed queue response does not take focus from another control", async () => {
  const ui = harness();
  let finish;
  ui.respond(() => new Promise((resolve) => { finish = resolve; }));
  ui.evaluate("renderQueue()");
  const button = ui.get("run-list").children[0];
  button.focus();
  const pending = button.listeners.click();
  ui.get("search").focus();
  finish(ui.initial);
  await pending;
  assert.equal(ui.document.activeElement, ui.get("search"));
});

test("a delayed queue response retains the different queue item the user advanced to", async () => {
  const ui = harness();
  let finish;
  ui.respond(() => new Promise((resolve) => { finish = resolve; }));
  ui.evaluate('state.runs.push({...initial.run,id:"RUN-2",title:"Second package"}); renderQueue()');
  const [first, second] = ui.get("run-list").children;
  first.focus();
  const pending = first.listeners.click();
  second.focus();
  finish(ui.initial);
  await pending;
  assert.equal(ui.document.activeElement, ui.get("run-list").children[1]);
  assert.equal(ui.document.activeElement.dataset.runId, "RUN-2");
  assert.equal(ui.get("detail-title").textContent, "Original package");
  assert.notEqual(ui.document.activeElement, second);
});

test("queue redraw focuses search when the formerly focused run is no longer visible", () => {
  const ui = harness();
  ui.evaluate("renderQueue()");
  ui.get("run-list").children[0].focus();
  ui.get("search").value = "No matching experiment";
  ui.evaluate("renderQueue()");
  assert.equal(ui.document.activeElement, ui.get("search"));
  assert.match(ui.get("run-list").children[0].textContent, /No experiments match/);
});

test("background refresh does not move focus into the experiment heading", async () => {
  const ui = harness();
  ui.respond((url) => url === "/api/state"
    ? {runs: [ui.initial.run], events: [], csrf_token: "test", demo: false}
    : ui.initial);
  ui.get("note").focus();
  await ui.evaluate("refresh()");
  assert.equal(ui.document.activeElement, ui.get("note"));
});

test("a failed queue load leaves focus on the still-present activation button", async () => {
  const ui = harness();
  ui.respond(() => { throw new Error("Could not load this experiment."); });
  ui.evaluate("renderQueue()");
  const button = ui.get("run-list").children[0];
  button.focus();
  await button.listeners.click();
  assert.equal(ui.document.activeElement, button);
  assert.equal(ui.get("run-list").children[0], button);
  assert.match(ui.get("message").textContent, /Could not load/);
});

function fillDraft(ui, mode = "new") {
  ui.evaluate(`openSubmission("${mode}")`);
  for (const [id, value] of Object.entries({
    "submission-id": mode === "new" ? "NEW-1" : "RUN-1",
    "submission-run-title": "Draft experiment",
    "submission-summary": "Draft summary",
    "submission-actor": "Researcher",
    "submission-note": "Review these files",
  })) ui.get(id).value = value;
  ui.evaluate(`captureDraft(); draft.files=["methods.txt","results.csv"].map(name=>({
    file:{name,size:3},reading:false,content:"ok\\n",error:""
  })); renderFiles();`);
}

test("removing files keeps focus on a neighboring remove button, then the picker", async () => {
  const ui = harness();
  fillDraft(ui);
  const firstRemove = ui.get("submission-file-list").children[0].children[1];
  firstRemove.focus();
  await firstRemove.listeners.click();
  const remainingRemove = ui.get("submission-file-list").children[0].children[1];
  assert.equal(remainingRemove["aria-label"], "Remove results.csv");
  assert.equal(ui.document.activeElement, remainingRemove);
  await remainingRemove.listeners.click();
  assert.equal(ui.document.activeElement, ui.get("submission-files"));
  assert.equal(ui.get("submission-file-list").children.length, 0);
});

for (const movesFocus of [false, true]) {
  test(`file decoding ${movesFocus ? "leaves another control focused" : "preserves the focused remove button"}`, async () => {
    const ui = harness();
    fillDraft(ui);
    const pending = ui.evaluate(`draft.files[1].content=undefined;
      draft.files[1].file.arrayBuffer=()=>new Promise(resolve=>{globalThis.finishFile=resolve});
      decodeFiles(draft)`);
    ui.get("submission-file-list").children[0].children[1].focus();
    if (movesFocus) ui.get("submission-note").focus();
    ui.evaluate("finishFile(new Uint8Array([111,107,10]).buffer)");
    await pending;
    assert.equal(ui.document.activeElement, movesFocus
      ? ui.get("submission-note")
      : ui.get("submission-file-list").children[0].children[1]);
    assert.equal(ui.evaluate("draft.files[1].content"), "ok\n");
  });
}

for (const status of [400, 409]) {
  test(`submission ${status} failure focuses its explanation and preserves the draft and files`, async () => {
    const ui = harness();
    fillDraft(ui, status === 409 ? "revision" : "new");
    // Native required validation allows whitespace, which the API rejects.
    if (status === 400) ui.get("submission-run-title").value = "   ";
    const requests = [];
    ui.respond((url, body) => {
      requests.push({url, body});
      const error = new Error(status === 400 ? "title is required." : "Stale version.");
      error.status = status;
      throw error;
    });
    ui.get("submission-save").focus();
    await ui.evaluate("submitExperiment({preventDefault(){}})");
    assert.equal(requests.length, 1);
    assert.equal(requests[0].url, "/api/submissions");
    assert.equal(ui.document.activeElement, ui.get("submission-message"));
    assert.equal(ui.get("submission-message").hidden, false);
    assert.match(ui.get("submission-message").textContent, status === 400 ? /title is required/ : /changed elsewhere/);
    assert.equal(ui.get("submission-dialog").open, true);
    assert.equal(ui.get("submission-note").value, "Review these files");
    assert.equal(ui.evaluate("draft.files.map(item=>item.file.name).join(',')"), "methods.txt,results.csv");
    assert.equal(ui.evaluate("draft.files[0].content"), "ok\n");
    assert.equal(ui.get("submission-save").disabled, status === 409);
    if (status === 409) {
      assert.equal(ui.get("submission-conflict").hidden, false);
      assert.equal(ui.evaluate("draft.expectedVersion"), 1);
    }
  });
}

test("current evidence preview stays bound to the snapshot displayed before a revision", async () => {
  const ui = harness();
  const requested = [];
  ui.respond(async (url) => {
    requested.push(url);
    // Another tab has replaced current evidence, while this detail still shows A.
    return {
      text: url.includes("/snapshots/snapshot-a/") ? "Original A" : "Replacement B",
      status: "verified", truncated: false,
    };
  });
  const preview = ui.get("evidence-list").children[0].children
    .find((child) => child.tagName === "DETAILS");
  preview.open = true;
  await preview.listeners.toggle();
  assert.deepEqual(requested, ["/api/runs/RUN-1/snapshots/snapshot-a/evidence/0"]);
  assert.equal(preview.children[1].textContent, "Original A");
});

test("snapshot preview failure never falls back to mutable current evidence", async () => {
  const ui = harness();
  const requested = [];
  ui.respond(async (url) => {
    requested.push(url);
    throw new Error("The saved evidence is unavailable.");
  });
  const preview = ui.get("evidence-list").children[0].children
    .find((child) => child.tagName === "DETAILS");
  preview.open = true;
  await preview.listeners.toggle();
  assert.deepEqual(requested, ["/api/runs/RUN-1/snapshots/snapshot-a/evidence/0"]);
  assert.match(preview.children[1].textContent, /unavailable/);
});

for (const owner of ["New reviewer", ""]) {
  test(`saving ${owner ? "an assignee" : "Unassigned"} preserves a draft decision note`, async () => {
    const ui = harness();
    const saved = [];
    const updated = structuredClone(ui.initial);
    ui.respond(async (url, body) => {
      if (body !== undefined) {
        assert.equal(url, "/api/runs/RUN-1/assign");
        saved.push(JSON.parse(JSON.stringify(body)));
        updated.run.owner = body.owner;
        updated.run.version = 2;
        return updated;
      }
      if (url === "/api/state") {
        return {runs: [updated.run], events: [], csrf_token: "test", demo: false};
      }
      assert.equal(url, "/api/runs/RUN-1");
      return updated;
    });
    ui.get("actor").value = "Actual reviewer";
    ui.get("owner").value = owner;
    ui.get("note").value = "Unsubmitted review reasoning that must not disappear.";
    await ui.evaluate('mutate("assign")');
    assert.deepEqual(saved, [{
      actor: "Actual reviewer", expected_version: 1, owner,
    }]);
    assert.equal(ui.get("owner").value, owner);
    assert.equal(ui.get("note").value, "Unsubmitted review reasoning that must not disappear.");
    assert.equal(ui.get("version").textContent, "Record version 2");
  });
}

function comparisonFixture(ui) {
  ui.evaluate(`detail.run.version=9; detail.run.snapshot_id="snapshot-b";
    detail.run.owner="Assigned after submission";
    detail.snapshots=[
      {id:"snapshot-old",run_version:1,manifest:{evidence:[{name:"unrelated.txt",sha256:"old"}]}},
      {id:"snapshot-b",run_version:6,manifest:{
        title:"Revised package",summary:"Same summary",owner:"Reviewer",is_demo:true,
        evidence:[{name:"added.txt",sha256:"new"},{name:"changed.txt",sha256:"b"},{name:"same.txt",sha256:"same"}]}},
      {id:"snapshot-a",run_version:3,manifest:{
        title:"Original package",summary:"Same summary",owner:"Reviewer",is_demo:true,
        evidence:[{name:"changed.txt",sha256:"a"},{name:"removed.txt",sha256:"gone"},{name:"same.txt",sha256:"same"}]}}
    ]; renderDetail();`);
}

test("revision comparison identifies file changes against the preceding submission, not record version", () => {
  const ui = harness();
  comparisonFixture(ui);
  assert.equal(ui.get("revision-comparison").hidden, false);
  assert.equal(ui.get("comparison-reference").textContent, "Submission at record version 3 → 6");
  assert.equal(ui.get("comparison-reference").title, "snapshot-a → snapshot-b");
  assert.equal(ui.get("comparison-summary").textContent, "1 changed · 1 added · 1 removed · 1 unchanged");
  const rows = ui.get("comparison-files").children.map(row => row.children[0].children.map(x => x.textContent));
  assert.deepEqual(rows, [
    ["added.txt", "added"], ["changed.txt", "changed"],
    ["removed.txt", "removed"], ["same.txt", "unchanged"],
  ]);
  // Assignment at record version 9 is not a new submission or evidence change.
  const metadata = ui.get("comparison-metadata").children;
  assert.equal(metadata.length, 1);
  assert.deepEqual(metadata[0].children.map(x => x.textContent), [
    "Title: ", "Original package", " → ", "Revised package",
  ]);
});

test("comparison previews remain attached to both displayed snapshots after another revision", async () => {
  const ui = harness();
  comparisonFixture(ui);
  const requested = [];
  ui.respond(url => {
    requested.push(url);
    return {text: "Evidence", status: "verified", truncated: false};
  });
  ui.evaluate('detail.run.snapshot_id="snapshot-c"');
  const rows = ui.get("comparison-files").children;
  for (const row of rows) {
    for (const preview of row.children.filter(x => x.tagName === "DETAILS")) {
      preview.open = true;
      await preview.listeners.toggle();
    }
  }
  assert.deepEqual(requested, [
    "/api/runs/RUN-1/snapshots/snapshot-b/evidence/0",
    "/api/runs/RUN-1/snapshots/snapshot-a/evidence/0",
    "/api/runs/RUN-1/snapshots/snapshot-b/evidence/1",
    "/api/runs/RUN-1/snapshots/snapshot-a/evidence/1",
    "/api/runs/RUN-1/snapshots/snapshot-b/evidence/2",
  ]);
});

test("renames including letter case appear as removal and addition, not unchanged evidence", () => {
  const ui = harness();
  const rows = ui.evaluate(`compareEvidence(
    {evidence:[{name:"Results.csv",sha256:"same"},{name:"old.txt",sha256:"same"}]},
    {evidence:[{name:"results.csv",sha256:"same"},{name:"new.txt",sha256:"same"}]}
  )`);
  assert.deepEqual(Array.from(rows, x => [x.name, x.change]), [
    ["Results.csv", "removed"], ["new.txt", "added"],
    ["old.txt", "removed"], ["results.csv", "added"],
  ]);
});

test("unchanged submitted hashes do not conceal a later stored-file integrity failure", async () => {
  const ui = harness();
  comparisonFixture(ui);
  ui.respond(() => ({text: "Modified outside the app", status: "changed", truncated: false}));
  const sameRow = ui.get("comparison-files").children[3];
  assert.equal(sameRow.children[0].children[1].textContent, "unchanged");
  const preview = sameRow.children.find(x => x.tagName === "DETAILS");
  preview.open = true;
  await preview.listeners.toggle();
  assert.match(preview.children[1].textContent, /^Evidence status: Changed\./);
});

test("comparison disappears for first submissions and never substitutes another record's snapshots", () => {
  const ui = harness();
  comparisonFixture(ui);
  ui.evaluate('detail.snapshots=detail.snapshots.filter(x=>x.id==="snapshot-b"); renderDetail()');
  assert.equal(ui.get("revision-comparison").hidden, true);
  assert.equal(ui.get("comparison-files").children.length, 0);
  comparisonFixture(ui);
  ui.evaluate('detail.run.snapshot_id="unavailable"; renderDetail()');
  assert.equal(ui.get("revision-comparison").hidden, true);
});

test("selecting the same experiment keeps its decision note", async () => {
  const ui = harness();
  ui.respond(() => ui.initial);
  ui.get("note").value = "I have checked the methods; results are next.";
  await ui.evaluate('selectRun("RUN-1")');
  assert.equal(ui.get("note").value, "I have checked the methods; results are next.");
});

test("switching experiments retains separate decision notes and does not copy them between records", async () => {
  const ui = harness();
  const second = structuredClone(ui.initial);
  second.run.id = "RUN-2";
  second.run.snapshot_id = "second-a";
  ui.respond(url => url.endsWith("RUN-2") ? second : ui.initial);
  ui.get("note").value = "First experiment draft.";
  await ui.evaluate('selectRun("RUN-2")');
  assert.equal(ui.get("note").value, "");
  ui.get("note").value = "Second experiment draft.";
  await ui.evaluate('selectRun("RUN-1")');
  assert.equal(ui.get("note").value, "First experiment draft.");
  await ui.evaluate('selectRun("RUN-2")');
  assert.equal(ui.get("note").value, "Second experiment draft.");
});

test("a draft retained across a new evidence snapshot is labeled until cleared", async () => {
  const ui = harness();
  const revised = structuredClone(ui.initial);
  revised.run.snapshot_id = "snapshot-b";
  revised.run.version = 3;
  ui.get("note").value = "Reasoning about the previous files.";
  ui.respond(url => url === "/api/state"
    ? {runs: [revised.run], events: [], csrf_token: "test"}
    : revised);
  await ui.evaluate("refresh()");
  assert.equal(ui.get("note").value, "Reasoning about the previous files.");
  assert.equal(ui.get("review-draft-warning").hidden, false);
  ui.get("note").value = "";
  await ui.get("note").listeners.input();
  assert.equal(ui.get("review-draft-warning").hidden, true);
});

test("typing while another experiment loads is retained on returning to the first", async () => {
  const ui = harness();
  let finish;
  ui.respond(() => new Promise(resolve => { finish = resolve; }));
  ui.get("note").value = "Beginning";
  const pending = ui.evaluate('selectRun("RUN-2")');
  ui.get("note").value = "Finished while waiting";
  const second = structuredClone(ui.initial);
  second.run.id = "RUN-2";
  finish(second);
  await pending;
  ui.respond(() => ui.initial);
  await ui.evaluate('selectRun("RUN-1")');
  assert.equal(ui.get("note").value, "Finished while waiting");
});

for (const keepTyping of [false, true]) {
  test(`a saved decision ${keepTyping ? "preserves newer typing" : "clears only the recorded draft"}`, async () => {
    const ui = harness();
    const updated = structuredClone(ui.initial);
    updated.run.version = 2;
    updated.run.status = "approved";
    ui.get("actor").value = "Reviewer";
    ui.get("note").value = "Checked the current files.";
    let finish, saved;
    ui.respond((url, body) => {
      if (body) {
        saved = body;
        return new Promise(resolve => { finish = resolve; });
      }
      return url === "/api/state"
        ? {runs: [updated.run], events: [], csrf_token: "test"}
        : updated;
    });
    const pending = ui.evaluate('mutate("approved")');
    if (keepTyping) ui.get("note").value = "New follow-up thought.";
    finish(updated);
    await pending;
    assert.equal(saved.note, "Checked the current files.");
    assert.equal(ui.get("note").value, keepTyping ? "New follow-up thought." : "");
    // Reselecting must not resurrect the already-submitted text.
    await ui.evaluate('selectRun("RUN-1")');
    assert.equal(ui.get("note").value, keepTyping ? "New follow-up thought." : "");
  });
}

test("a successful write followed by failed refresh is labeled saved and prevents duplicate decisions", async () => {
  const ui = harness();
  ui.get("actor").value = "Reviewer";
  ui.get("note").value = "This was recorded.";
  let writes = 0;
  ui.respond((url, body) => {
    if (body) { writes++; return ui.initial; }
    throw new Error("Disconnected after write");
  });
  await ui.evaluate('mutate("approved")');
  assert.equal(ui.get("note").value, "");
  assert.match(ui.get("message").textContent, /action was saved/);
  assert.equal(ui.get("approve").disabled, true);
  await ui.evaluate('mutate("approved")');
  assert.equal(writes, 1);
});

test("switching records while a decision saves cannot resurrect the recorded draft", async () => {
  const ui = harness();
  const second = structuredClone(ui.initial);
  second.run.id = "RUN-2";
  const approved = structuredClone(ui.initial);
  approved.run.version = 2;
  approved.run.status = "approved";
  let finishSelection, finishDecision, loads = 0;
  ui.get("actor").value = "Reviewer";
  ui.get("note").value = "Note actually submitted for RUN-1.";
  ui.respond((url, body) => {
    if (body) return new Promise(resolve => { finishDecision = resolve; });
    if (url === "/api/state") return {runs: [approved.run, second.run], events: [], csrf_token: "test"};
    if (url.endsWith("RUN-2")) {
      if (++loads === 1) return new Promise(resolve => { finishSelection = resolve; });
      return second;
    }
    return approved;
  });
  const selection = ui.evaluate('selectRun("RUN-2")');
  const decision = ui.evaluate('mutate("approved")');
  finishSelection(second);
  await selection;
  finishDecision(approved);
  await decision;
  await ui.evaluate('selectRun("RUN-1")');
  assert.equal(ui.get("note").value, "");
});

test("review summary downloads the displayed version without submitting a draft note", async () => {
  const ui = harness(), requests = [];
  ui.get("note").value = "Unfinished private reasoning";
  ui.fetchWith(async (...args) => {
    requests.push(args);
    return {ok: true, text: async () => "<!doctype html><title>Saved review</title>"};
  });
  await ui.get("download-summary").listeners.click();
  assert.deepEqual(requests, [["/api/runs/RUN-1/summary?expected_version=1"]]);
  const [download] = ui.document.downloads;
  assert.equal(download.filename, "reviewops-RUN-1-v1.html");
  assert.equal(ui.blobs.get(download.href).type, "text/html; charset=utf-8");
  assert.equal(await ui.blobs.get(download.href).text(), "<!doctype html><title>Saved review</title>");
  assert.equal(ui.get("note").value, "Unfinished private reasoning");
  assert.match(ui.get("message").textContent, /RUN-1, record version 1/);
  assert.equal(ui.get("download-summary").disabled, false);
});

test("a pending selection cannot relabel a downloaded summary as a different experiment", async () => {
  const ui = harness();
  let finishSelection, finishDownload;
  const second = structuredClone(ui.initial);
  second.run.id = "RUN-2"; second.run.version = 7;
  ui.respond(() => new Promise(resolve => {finishSelection=resolve;}));
  ui.fetchWith(() => new Promise(resolve => {finishDownload=resolve;}));
  const selection = ui.evaluate('selectRun("RUN-2")');
  const download = ui.get("download-summary").listeners.click();
  assert.equal(ui.get("download-summary").disabled, true);
  finishSelection(second); await selection;
  ui.get("note").value = "Second experiment draft";
  finishDownload({ok: true, text: async () => "RUN-1 saved report"}); await download;
  assert.equal(ui.document.downloads[0].filename, "reviewops-RUN-1-v1.html");
  assert.match(ui.get("message").textContent, /RUN-1, record version 1/);
  assert.equal(ui.evaluate("selected"), "RUN-2");
  assert.equal(ui.get("note").value, "Second experiment draft");
});

test("a stale summary produces no file and keeps the draft until the latest record is refreshed", async () => {
  const ui = harness();
  ui.get("note").value = "Still checking this evidence";
  ui.fetchWith(async () => ({ok: false, status: 409, json: async () => ({error:"Stale version"})}));
  await ui.get("download-summary").listeners.click();
  assert.equal(ui.document.downloads.length, 0);
  assert.equal(ui.get("note").value, "Still checking this evidence");
  assert.match(ui.get("message").textContent, /RUN-1 changed elsewhere\. Refresh evidence/);
  assert.equal(ui.get("download-summary").disabled, true);
  assert.equal(ui.get("approve").disabled, true);
  assert.equal(ui.get("refresh").disabled, false);
  const latest = structuredClone(ui.initial); latest.run.version = 2;
  ui.respond(url => url === "/api/state" ? {runs:[latest.run],events:[],csrf_token:"test"} : latest);
  await ui.evaluate("refresh()");
  assert.equal(ui.get("note").value, "Still checking this evidence");
  assert.equal(ui.get("download-summary").disabled, false);
});

test("a stale summary for the departing record does not block a newly loaded experiment", async () => {
  const ui = harness();
  let finishSelection, finishDownload;
  const second = structuredClone(ui.initial); second.run.id="RUN-2";
  ui.respond(() => new Promise(resolve => {finishSelection=resolve;}));
  ui.fetchWith(() => new Promise(resolve => {finishDownload=resolve;}));
  const selection=ui.evaluate('selectRun("RUN-2")');
  const download=ui.get("download-summary").listeners.click();
  finishSelection(second); await selection;
  finishDownload({ok:false,status:409,json:async()=>({error:"Stale version"})}); await download;
  assert.equal(ui.document.downloads.length, 0);
  assert.equal(ui.get("download-summary").disabled, false);
  assert.equal(ui.get("approve").disabled, false);
  assert.match(ui.get("message").textContent, /RUN-1 changed elsewhere/);
});

for (const operation of ["summary", "decision"]) {
  test(`an older pending detail cannot clear a known-stale version after a ${operation} conflict`, async () => {
    const ui = harness();
    let finishSelection;
    ui.respond((url,body) => {
      if(body){const error=new Error("Stale version"); error.status=409; throw error;}
      return new Promise(resolve => {finishSelection=resolve;});
    });
    ui.fetchWith(async()=>({ok:false,status:409,json:async()=>({error:"Stale version"})}));
    ui.get("actor").value="Reviewer"; ui.get("note").value="Keep this draft";
    const selection=ui.evaluate('selectRun("RUN-1")');
    if(operation === "summary") await ui.get("download-summary").listeners.click();
    else await ui.evaluate('mutate("approved")');
    assert.equal(ui.get("approve").disabled,true);
    finishSelection(ui.initial); await selection;
    assert.equal(ui.get("approve").disabled,true);
    assert.equal(ui.get("download-summary").disabled,true);
    assert.equal(ui.get("note").value,"Keep this draft");
    const latest=structuredClone(ui.initial); latest.run.version=2;
    ui.respond(()=>latest);
    await ui.evaluate('selectRun("RUN-1")');
    assert.equal(ui.get("approve").disabled,false);
    assert.equal(ui.get("download-summary").disabled,false);
  });
}

for(const failure of ["connection", "body"]) {
  test(`summary ${failure} failure stays visible and can be retried without losing the note`, async () => {
    const ui=harness(); ui.get("note").value="Keep my draft";
    ui.fetchWith(async () => {
      if(failure === "connection") throw new Error("Disconnected");
      return {ok:true,text:async()=>{throw new Error("Download interrupted");}};
    });
    await ui.get("download-summary").listeners.click();
    assert.equal(ui.document.downloads.length, 0);
    assert.equal(ui.get("message").hidden, false);
    assert.equal(ui.get("message").className, "error");
    assert.match(ui.get("message").textContent, /Could not download the review summary/);
    assert.equal(ui.get("note").value,"Keep my draft");
    assert.equal(ui.get("download-summary").disabled,false);
  });
}

test("the workspace audit remains a JSON download with the existing filename", async () => {
  const ui=harness();
  const audit={schema_version:2,demo:false,runs:[],events:[]};
  ui.respond(url=>{assert.equal(url,"/api/export"); return audit;});
  await ui.get("export").listeners.click();
  const [download]=ui.document.downloads;
  assert.equal(download.filename,"reviewops-audit.json");
  assert.equal(ui.blobs.get(download.href).type,"application/json");
  assert.deepEqual(JSON.parse(await ui.blobs.get(download.href).text()),audit);
});
