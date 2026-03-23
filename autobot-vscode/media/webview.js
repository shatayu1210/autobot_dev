(function () {
  const vscode = acquireVsCodeApi();

  const issueNumber = document.getElementById("issueNumber");
  const repoPath = document.getElementById("repoPath");
  const pickFolder = document.getElementById("pickFolder");
  const btnAsk = document.getElementById("btnAsk");
  const btnPlan = document.getElementById("btnPlan");
  const btnPatch = document.getElementById("btnPatch");
  const btnOpenPr = document.getElementById("btnOpenPr");
  const status = document.getElementById("status");
  const contextOut = document.getElementById("contextOut");
  const patchOut = document.getElementById("patchOut");

  /** @type {any} */
  let lastIssuePayload = null;
  /** @type {any} */
  let lastPlanPayload = null;
  /** @type {any} */
  let lastPatchPayload = null;

  let reqId = 0;
  const pending = new Map();

  window.addEventListener("message", (event) => {
    const msg = event.data;
    if (msg.type === "config") {
      if (!repoPath.value && msg.defaultRepoPath) {
        repoPath.value = msg.defaultRepoPath;
      }
      return;
    }
    if (msg.type === "folderPicked" && msg.folder) {
      repoPath.value = msg.folder;
      return;
    }
    if (msg.type === "orchestrateResult") {
      const p = pending.get(msg.id);
      if (!p) return;
      pending.delete(msg.id);
      if (msg.ok) {
        p.resolve(msg.result);
      } else {
        p.reject(new Error(msg.error || "Unknown error"));
      }
    }
  });

  function setStatus(text) {
    status.textContent = text;
  }

  function pretty(obj) {
    try {
      return JSON.stringify(obj, null, 2);
    } catch {
      return String(obj);
    }
  }

  function parseIssueNumber() {
    const raw = (issueNumber.value || "").trim();
    if (!raw) {
      throw new Error("Enter an issue number.");
    }
    const n = Number(raw);
    if (!Number.isFinite(n)) {
      throw new Error("Issue number must be numeric.");
    }
    return n;
  }

  function repoPathValue() {
    const v = (repoPath.value || "").trim();
    if (!v) {
      throw new Error("Set repository path (or open a workspace folder).");
    }
    return v;
  }

  /** @param {Record<string, unknown>} payload */
  function callOrchestrate(payload) {
    const id = ++reqId;
    return new Promise((resolve, reject) => {
      pending.set(id, { resolve, reject });
      vscode.postMessage({ type: "orchestrate", id, payload });
    });
  }

  async function run(label, fn) {
    setStatus(label + "…");
    [
      btnAsk,
      btnPlan,
      btnPatch,
      btnOpenPr,
      pickFolder,
    ].forEach((b) => (b.disabled = true));
    try {
      await fn();
      setStatus("Done — " + label);
    } catch (e) {
      const m = e instanceof Error ? e.message : String(e);
      setStatus("Error: " + m);
      patchOut.textContent = m;
    } finally {
      [
        btnAsk,
        btnPlan,
        btnPatch,
        btnOpenPr,
        pickFolder,
      ].forEach((b) => (b.disabled = false));
    }
  }

  pickFolder.addEventListener("click", () => {
    vscode.postMessage({ type: "pickFolder" });
  });

  btnAsk.addEventListener("click", () =>
    run("Load issue", async () => {
      const n = parseIssueNumber();
      const body = await callOrchestrate({ command: "ask_issue", issue_number: n });
      lastIssuePayload = body;
      contextOut.textContent = pretty(body);
      patchOut.textContent = "";
    })
  );

  btnPlan.addEventListener("click", () =>
    run("Planner", async () => {
      const n = parseIssueNumber();
      const rp = repoPathValue();
      const body = await callOrchestrate({
        command: "plan_patch",
        issue_number: n,
        repo_path: rp,
      });
      lastPlanPayload = body;
      contextOut.textContent = pretty(body);
    })
  );

  btnPatch.addEventListener("click", () =>
    run("Patcher + Critic", async () => {
      if (!lastPlanPayload) {
        throw new Error('Run "Planner" first (need plan in memory).');
      }
      const n = parseIssueNumber();
      const plan =
        lastPlanPayload.plan !== undefined
          ? lastPlanPayload.plan
          : lastPlanPayload;
      const code_spans =
        lastPlanPayload.code_spans !== undefined
          ? lastPlanPayload.code_spans
          : lastPlanPayload.codeSpans;

      const body = await callOrchestrate({
        command: "accept_plan",
        issue_number: n,
        plan,
        code_spans,
      });
      lastPatchPayload = body;
      patchOut.textContent = pretty(body);
    })
  );

  btnOpenPr.addEventListener("click", () =>
    run("Open PR draft", async () => {
      if (!lastPatchPayload) {
        throw new Error('Run "Patcher + Critic" first (need diff).');
      }
      const diff =
        lastPatchPayload.diff !== undefined
          ? lastPatchPayload.diff
          : lastPatchPayload.patch;
      if (diff === undefined || diff === null) {
        throw new Error('No diff in last response; run "Patcher + Critic" again.');
      }
      const body = await callOrchestrate({ command: "open_pr", diff });
      contextOut.textContent = pretty(body);
    })
  );

  vscode.postMessage({ type: "ready" });
})();
