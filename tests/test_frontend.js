/* Small behavior regressions using the actual UI script, no browser packages. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../static/app.js"), "utf8");

class Element {
  constructor(tag = "div") {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.listeners = {};
    this.value = "";
    this.textContent = "";
    this.hidden = false;
  }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  setAttribute(name, value) { this[name] = value; }
  addEventListener(name, listener) { this.listeners[name] = listener; }
  focus() {}
}

function harness() {
  const elements = new Map();
  const get = (id) => {
    if (!elements.has(id)) elements.set(id, new Element());
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
  const context = vm.createContext({
    document: {
      getElementById: get,
      createElement: (tag) => new Element(tag),
      createTextNode: (text) => Object.assign(new Element("#text"), {textContent: text}),
    },
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
    get, initial,
    evaluate: (code) => vm.runInContext(code, context),
    respond: (callback) => {
      context.respond = callback;
      vm.runInContext("api=async (path,body)=>respond(path,body)", context);
    },
  };
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
