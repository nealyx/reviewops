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
}

function harness() {
  const elements = new Map();
  const document = {};
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
    console, TextDecoder, URL, Blob, setTimeout,
    initial,
  });
  vm.runInContext(source, context);
  vm.runInContext(
    'state={runs:[initial.run],events:[],csrf_token:"test",demo:false};'
    + 'selected=initial.run.id; detail=initial; renderDetail();', context);
  return {
    get, initial, document,
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
