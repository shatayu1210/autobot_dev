(function () {
  "use strict";
  const vscode = acquireVsCodeApi();

  // ── DOM refs ──────────────────────────────────────────────
  const chatFeed = document.getElementById("ab-chat");
  const inputEl = document.getElementById("ab-input");
  const sendBtn = document.getElementById("ab-send");
  const repoInput = document.getElementById("ab-repo");
  const pickBtn = document.getElementById("ab-pick");

  // ── State ─────────────────────────────────────────────────
  let pending = new Map();
  let reqId = 0;
  let lastPlan = null;       // stored plan JSON after plan_patch
  let lastDiff = null;       // stored diff after accept_plan
  let isBusy = false;

  // ── Message bus ───────────────────────────────────────────
  window.addEventListener("message", (event) => {
    const msg = event.data;

    if (msg.type === "config") {
      if (!repoInput.value && msg.defaultRepoPath) {
        repoInput.value = msg.defaultRepoPath;
      }
      return;
    }

    if (msg.type === "folderPicked" && msg.folder) {
      repoInput.value = msg.folder;
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
      return;
    }

    // Streaming step events from orchestrator
    if (msg.type === "step") {
      appendStep(msg.stepId, msg.text, msg.done);
    }
  });

  // ── Orchestrator call ─────────────────────────────────────
  function callOrchestrate(payload) {
    const id = ++reqId;
    return new Promise((resolve, reject) => {
      pending.set(id, { resolve, reject });
      vscode.postMessage({ type: "orchestrate", id, payload });
    });
  }

  // ── Folder picker ─────────────────────────────────────────
  pickBtn.addEventListener("click", () => {
    vscode.postMessage({ type: "pickFolder" });
  });

  // ── Auto-resize textarea ──────────────────────────────────
  inputEl.addEventListener("input", () => {
    inputEl.style.height = "auto";
    inputEl.style.height = Math.min(inputEl.scrollHeight, 120) + "px";
  });

  // ── Send on Enter (Shift+Enter = newline) ─────────────────
  inputEl.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  });

  sendBtn.addEventListener("click", handleSend);

  // ── Parse intent via LLM (with regex fallback) ────────────────
  async function parseIntent(text) {
    text = text.trim();
    
    // Attempt LLM-based intent detection
    try {
      const result = await callOrchestrate({ command: "detect_intent", text });
      if (result && result.intent) {
        return { 
          intent: result.intent, 
          issueNum: result.issue_number ? parseInt(result.issue_number) : null, 
          prNum: result.pr_number ? parseInt(result.pr_number) : null,
          text 
        };
      }
    } catch (e) {
      console.warn("LLM intent detection failed, falling back to heuristics:", e);
    }

    // Fallback heuristic if LLM fails
    const numMatch = text.match(/#?(\d+)/);
    const isFixRequest = /\b(fix|plan|analyze|patch|implement)\b/i.test(text);
    const isPrRequest = /\bpr\b/i.test(text);

    if (numMatch) {
      const num = parseInt(numMatch[1]);
      if (isFixRequest) {
        return { intent: "plan_patch", issueNum: num, text };
      }
      if (text.match(/^(pr\s*)?#?\d+$/i)) {
        return { intent: "ask_pr", prNum: num, text };
      }
      if (text.match(/^(issue\s*)?#?\d+$/i)) {
        return { intent: "ask_issue", issueNum: num, text };
      }
    }
    return { intent: "query", text };
  }

  function appendAgentBubble() {
    const wrap = el("div", "ab-msg", "ab-msg-agent");
    const label = el("div", "ab-msg-label");
    label.innerHTML = `AutoBot <span style="opacity:0.65;margin-left:4px;display:inline-flex;align-items:center;">
      <svg width="10" height="10" viewBox="0 0 16 16" fill="currentColor" style="margin-right:3px;margin-left:4px;"><path fill-rule="evenodd" clip-rule="evenodd" d="M8 1.5a6.5 6.5 0 100 13 6.5 6.5 0 000-13zM0 8a8 8 0 1116 0A8 8 0 010 8zm8.5-4a.5.5 0 00-1 0v4.207L10.146 11.5l.708-.707L8.5 8.414V4z"/></svg>
      <span class="ab-timer" style="font-variant-numeric: tabular-nums;">0s</span>
    </span>`;
    const bubble = el("div", "ab-bubble");
    wrap.appendChild(label);
    wrap.appendChild(bubble);
    chatFeed.appendChild(wrap);
    return wrap;
  }

  // ── Main send handler ─────────────────────────────────────
  let currentAbortController = null;
  let currentTimerInterval = null;

  async function handleSend() {
    // If it's currently busy, this is a STOP action
    if (isBusy) {
      if (currentAbortController) {
        currentAbortController.abort();
        currentAbortController = null;
      }
      return;
    }

    const raw = inputEl.value.trim();
    if (!raw) return;

    inputEl.value = "";
    inputEl.style.height = "auto";
    inputEl.disabled = true; // Disable textbox
    removeWelcome();
    appendUserMessage(raw);
    
    // Switch to STOP mode
    setBusy(true);
    sendBtn.innerHTML = "⏹";
    sendBtn.classList.add("ab-btn-stop");

    currentAbortController = new AbortController();
    const signal = currentAbortController.signal;

    const repo = repoInput.value.trim();
    
    // Add a fast initial loading state for intent detection
    const agentEl = appendAgentBubble();
    const timerEl = agentEl.querySelector(".ab-timer");
    const startTime = Date.now();
    currentTimerInterval = setInterval(() => {
      timerEl.textContent = `${Math.floor((Date.now() - startTime) / 1000)}s`;
    }, 1000);

    const stepsEl = appendStepsContainer(agentEl);
    const initStep = addStep(stepsEl, "Understanding intent...");
    scrollToBottom();

    let parsed;
    try {
      parsed = await parseIntent(raw);
      if (signal.aborted) throw new Error("Aborted by user");
    } catch (e) {
      markStepDone(initStep, "🛑");
      if (e.name === "AbortError" || e.message.includes("aborted")) {
        agentEl.querySelector(".ab-bubble").appendChild(textNode(`🛑 Request aborted by user.`));
      } else {
        agentEl.querySelector(".ab-bubble").appendChild(textNode(`❌ Failed: ${e.message}`));
      }
      resetInput();
      return;
    }
    
    markStepDone(initStep);
    const { intent, issueNum, prNum, text } = parsed;

    try {
      if (signal.aborted) throw new Error("Aborted by user");
      
      if (intent === "ask_issue") {
        await doAskIssue(issueNum, agentEl, signal);
      } else if (intent === "ask_pr") {
        await doAskPr(prNum, agentEl, signal);
      } else if (intent === "plan_patch") {
        if (!repo) {
          agentEl.querySelector(".ab-bubble").appendChild(textNode("⚠️ Please set the **Repository path** above before planning."));
          resetInput();
          return;
        }
        if (!issueNum) {
          agentEl.querySelector(".ab-bubble").appendChild(textNode("⚠️ Please include an issue number in your message, e.g. `fix issue #45123`."));
          resetInput();
          return;
        }
        await doPlanPatch(issueNum, repo, agentEl, signal);
      } else if (intent === "query") {
        await doQuery(text, agentEl, signal);
      } else {
        agentEl.querySelector(".ab-bubble").appendChild(textNode("I didn't quite understand that. Try: **fix issue #45123** or **check PR #123**."));
      }
    } catch (err) {
      if (err.name === "AbortError" || err.message.includes("aborted")) {
        agentEl.querySelector(".ab-bubble").appendChild(textNode(`🛑 Request aborted by user.`));
      } else {
        agentEl.querySelector(".ab-bubble").appendChild(textNode(`❌ ${err.message}`));
      }
    } finally {
      resetInput();
    }
  }

  function resetInput() {
    setBusy(false);
    currentAbortController = null;
    if (currentTimerInterval) {
      clearInterval(currentTimerInterval);
      currentTimerInterval = null;
    }
    inputEl.disabled = false;
    sendBtn.innerHTML = "➤";
    sendBtn.classList.remove("ab-btn-stop");
    inputEl.focus();
    scrollToBottom();
  }

  // ── Step 0: General Query ──────────────────────────────────
  async function doQuery(text, agentEl, signal) {
    const bubble = agentEl.querySelector(".ab-bubble");
    const stepsEl = appendStepsContainer(agentEl);
    
    let currentStep = null;
    scrollToBottom();

    try {
      const response = await fetch("http://127.0.0.1:5000/api/orchestrate_stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ command: "query", query: text }),
        signal
      });

      if (!response.ok) {
        throw new Error(`HTTP Error: ${response.status}`);
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder("utf-8");
      let buffer = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        
        buffer += decoder.decode(value, { stream: true });
        
        // Parse SSE lines
        const lines = buffer.split("\n\n");
        buffer = lines.pop(); // Keep incomplete chunk in buffer
        
        for (const line of lines) {
          if (line.startsWith("data: ")) {
            const dataStr = line.slice(6);
            const data = JSON.parse(dataStr);
            
            if (data.type === "step") {
              if (currentStep) markStepDone(currentStep);
              currentStep = addStep(stepsEl, data.msg);
              scrollToBottom();
            } else if (data.type === "done") {
              if (currentStep) { markStepDone(currentStep); currentStep = null; }

              const card = el("div", "ab-query-card");
              const answerEl = el("div", "ab-query-answer");
              answerEl.innerHTML = data.answer;
              card.appendChild(answerEl);

              if (data.tools_called && data.tools_called.length > 0) {
                const footer = el("div", "ab-query-footer");
                footer.textContent = `Tools: ${data.tools_called.join(", ")}`;
                card.appendChild(footer);
              }
              bubble.appendChild(card);
              scrollToBottom();
            }
          }
        }
      }
    } catch (e) {
      if (currentStep) markStepDone(currentStep, "❌");
      throw e;
    }
    scrollToBottom();
  }

  // ── Step 1: Load Issue ────────────────────────────────────
  async function doAskIssue(n, agentEl, signal) {
    const stepsEl = appendStepsContainer(agentEl);
    const s1 = addStep(stepsEl, "Fetching issue from GitHub...");

    const result = await callOrchestrate({ command: "ask_issue", issue_number: n });
    if (signal.aborted) throw new Error("Aborted by user");
    
    markStepDone(s1);

    const body = renderIssueCard(result);
    agentEl.querySelector(".ab-bubble").appendChild(body);
    scrollToBottom();
  }

  // ── Step 1b: Load PR ──────────────────────────────────────
  async function doAskPr(n, agentEl, signal) {
    const stepsEl = appendStepsContainer(agentEl);
    const s1 = addStep(stepsEl, "Fetching pull request from GitHub...");

    const result = await callOrchestrate({ command: "ask_pr", pr_number: n });
    if (signal.aborted) throw new Error("Aborted by user");
    
    markStepDone(s1);

    const body = renderPrCard(result);
    agentEl.querySelector(".ab-bubble").appendChild(body);
    scrollToBottom();
  }

  // ── Step 2: Plan ──────────────────────────────────────────
  async function doPlanPatch(n, repo, agentEl, signal) {
    const stepsEl = appendStepsContainer(agentEl);
    const s1 = addStep(stepsEl, "Fetching issue...");

    let issueResult;
    try {
      issueResult = await callOrchestrate({ command: "ask_issue", issue_number: n });
      if (signal.aborted) throw new Error("Aborted by user");
      markStepDone(s1);
    } catch (e) {
      if (e.message === "Aborted by user") throw e;
      markStepDone(s1, "⚠️");
      issueResult = null;
    }

    let currentStep = null;
    scrollToBottom();

    try {
      const response = await fetch("http://127.0.0.1:5000/api/orchestrate_stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ command: "plan_patch", issue_number: n, repo_path: repo }),
        signal
      });

      if (!response.ok) throw new Error(`HTTP Error: ${response.status}`);

      const reader = response.body.getReader();
      const decoder = new TextDecoder("utf-8");
      let buffer = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n\n");
        buffer = lines.pop();
        
        for (const line of lines) {
          if (line.startsWith("data: ")) {
            const dataStr = line.slice(6);
            const data = JSON.parse(dataStr);
            
            if (data.type === "step") {
              if (currentStep) markStepDone(currentStep);
              currentStep = addStep(stepsEl, data.msg);
              scrollToBottom();
            } else if (data.type === "plan_done") {
              if (currentStep) markStepDone(currentStep);
              
              lastPlan = data;
              const planCard = renderPlanCard(data, n);
              agentEl.querySelector(".ab-bubble").appendChild(planCard);
              scrollToBottom();
            } else if (data.type === "error") {
              if (currentStep) { markStepDone(currentStep, "❌"); currentStep = null; }
              throw new Error(data.msg);
            }
          }
        }
      }
    } catch (e) {
      if (currentStep) markStepDone(currentStep, "❌");
      throw e;
    }
  }

  // ── Approval: Approve Plan ────────────────────────────────
  async function doApprovePlan(plan, n) {
    const repo = repoInput.value.trim();
    const agentEl = appendAgentBubble();
    const stepsEl = appendStepsContainer(agentEl);
    const s1 = addStep(stepsEl, "Reading target file contents from workspace...");
    await delay(350); markStepDone(s1);
    const s2 = addStep(stepsEl, "Building patcher context (code spans)...");
    await delay(300); markStepDone(s2);
    const s3 = addStep(stepsEl, "Calling Patcher model...");

    let result;
    try {
      result = await callOrchestrate({
        command: "approve_plan",
        issue_number: n,
        plan: plan.plan || plan,
        repo_path: repo,
      });
      markStepDone(s3);
    } catch (err) {
      markStepDone(s3, "❌");
      agentEl.querySelector(".ab-bubble").appendChild(textNode(`❌ Patcher error: ${err.message}`));
      scrollToBottom();
      return;
    }

    if (result.status === "stopped_at_planner" || result.status === "context_assembled") {
      markStepDone(s3);
      agentEl.querySelector(".ab-bubble").appendChild(textNode("🛑 " + (result.note || "Stopped at planner.")));
      
      const payloadWrap = el("div", "ab-diff-wrap");
      const label = el("div", "ab-diff-label");
      label.textContent = "ASSEMBLED PATCHER CONTEXT (JSON)";
      payloadWrap.appendChild(label);
      
      const pre = el("pre", "ab-diff");
      pre.textContent = JSON.stringify(result.patcher_input, null, 2);
      payloadWrap.appendChild(pre);
      
      agentEl.querySelector(".ab-bubble").appendChild(payloadWrap);
      scrollToBottom();
      return;
    }

    if (result.status === "stopped_at_patcher") {
      markStepDone(s3);
      agentEl.querySelector(".ab-bubble").appendChild(textNode("🛑 " + (result.note || "Stopped at patcher.")));
      
      const diffCard = renderDiffCard({ diff: result.generated_diff, verdict: "STOPPED", reasoning: "Stopped before Critic." }, n);
      agentEl.querySelector(".ab-bubble").appendChild(diffCard);
      
      const payloadWrap = el("div", "ab-diff-wrap");
      const label = el("div", "ab-diff-label");
      label.textContent = "MOCK CRITIC INPUT (JSON)";
      payloadWrap.appendChild(label);
      const pre = el("pre", "ab-diff");
      pre.textContent = JSON.stringify(result.critic_input, null, 2);
      payloadWrap.appendChild(pre);
      
      agentEl.querySelector(".ab-bubble").appendChild(payloadWrap);
      scrollToBottom();
      return;
    }

    const s4 = addStep(stepsEl, `Critic review (${result.iterations_used ?? 1} iteration(s))...`);
    markStepDone(s4);

    lastDiff = result;
    const diffCard = renderDiffCard(result, n);
    agentEl.querySelector(".ab-bubble").appendChild(diffCard);
    scrollToBottom();
  }

  // ── Helpers for time calculation ────────────────────────
  function formatTimeDelta(startStr, endStr) {
    if (!startStr) return "";
    const start = new Date(startStr);
    const end = endStr ? new Date(endStr) : new Date();
    const diffMs = end.getTime() - start.getTime();
    if (diffMs < 0) return "";
    const hours = Math.floor(diffMs / (1000 * 60 * 60));
    if (hours < 24) return `${hours} hours`;
    return `${Math.floor(hours / 24)} days`;
  }

  function getTimingInfo(data, isPr) {
    const state = (isPr && data.merged) ? "merged" : (data.state || "open").toLowerCase();
    const createdStr = data.created_at;
    let timingHtml = "";
    if (createdStr) {
      const openDate = new Date(createdStr).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
      if (state === "closed" || state === "merged") {
        const closedStr = isPr ? (data.merged_at || data.closed_at) : data.closed_at;
        const diff = formatTimeDelta(createdStr, closedStr);
        timingHtml = `<span>Resolution: <strong>${diff}</strong></span>`;
      } else {
        timingHtml = `<span>Opened: <strong>${openDate}</strong></span>`;
      }
    }
    return timingHtml;
  }

  // ── UI renderers ──────────────────────────────────────────

  function renderIssueCard(data) {
    const wrap = el("div", "ab-card");
    const state = (data.state || "open").toLowerCase();
    
    // Header (always visible)
    const header = el("div", "ab-card-header");
    header.innerHTML = `
      <div class="ab-card-title-group">
        <span class="ab-card-badge ${state}">${state}</span>
        <strong>#${data.issue_number}</strong>
        <span class="ab-card-title-text" title="${data.title?.replace(/"/g, '&quot;') || "(no title)"}">
          ${data.title || "(no title)"}
        </span>
      </div>
      <div style="display:flex; align-items:center; gap:8px;">
        <a href="${data.html_url || "#"}" target="_blank" onclick="event.stopPropagation()" style="font-size:0.85em; text-decoration:none;">↗ GitHub</a>
        <div class="ab-card-expand-icon">▼</div>
      </div>
    `;
    wrap.appendChild(header);

    // Body (collapsible)
    const bodyEl = el("div", "ab-card-body");
    
    const desc = el("div", "ab-card-desc");
    desc.textContent = data.body ? (data.body.length > 800 ? data.body.slice(0, 800) + "..." : data.body) : "No description provided.";
    bodyEl.appendChild(desc);

    const meta = el("div", "ab-card-meta");
    meta.style.flexDirection = "column";
    meta.style.alignItems = "flex-start";
    meta.style.gap = "4px";

    let openDateStr = data.created_at ? new Date(data.created_at).toLocaleDateString("en-US", {month:"2-digit", day:"2-digit", year:"2-digit"}) : "Unknown";
    let assigneeStr = (data.assignee && data.assignee.login) ? data.assignee.login : "Nobody";

    meta.innerHTML = `
      <div><strong>Opened On:</strong> ${openDateStr}</div>
      <div><strong>Assignee:</strong> ${assigneeStr}</div>
    `;
    bodyEl.appendChild(meta);

    wrap.appendChild(bodyEl);

    // Toggle logic
    header.addEventListener("click", () => {
      wrap.classList.toggle("expanded");
    });

    return wrap;
  }

  function renderPrCard(data) {
    const wrap = el("div", "ab-card");
    let state = data.merged ? "merged" : (data.state || "open").toLowerCase();
    
    // Header (always visible)
    const header = el("div", "ab-card-header");
    const prNum = data.pr_number || data.number;
    header.innerHTML = `
      <div class="ab-card-title-group">
        <span class="ab-card-badge ${state}">${state}</span>
        <strong>PR #${prNum}</strong>
        <span class="ab-card-title-text" title="${data.title?.replace(/"/g, '&quot;') || "(no title)"}">
          ${data.title || "(no title)"}
        </span>
      </div>
      <div style="display:flex; align-items:center; gap:8px;">
        <a href="${data.html_url || "#"}" target="_blank" onclick="event.stopPropagation()" style="font-size:0.85em; text-decoration:none;">↗ GitHub</a>
        <div class="ab-card-expand-icon">▼</div>
      </div>
    `;
    wrap.appendChild(header);

    // Body (collapsible)
    const bodyEl = el("div", "ab-card-body");
    
    const desc = el("div", "ab-card-desc");
    desc.textContent = data.body ? (data.body.length > 800 ? data.body.slice(0, 800) + "..." : data.body) : "No description provided.";
    bodyEl.appendChild(desc);

    const meta = el("div", "ab-card-meta");
    meta.style.flexDirection = "column";
    meta.style.alignItems = "flex-start";
    meta.style.gap = "4px";

    let openDateStr = data.created_at ? new Date(data.created_at).toLocaleDateString("en-US", {month:"2-digit", day:"2-digit", year:"2-digit"}) : "Unknown";
    let assigneeStr = (data.assignee && data.assignee.login) ? data.assignee.login : "Nobody";
    let reviewsCount = data.reviews !== undefined ? data.reviews : "Unknown";

    meta.innerHTML = `
      <div><strong>Opened On:</strong> ${openDateStr}</div>
      <div><strong>Assignee:</strong> ${assigneeStr}</div>
      <div><strong>Reviews Count:</strong> ${reviewsCount}</div>
    `;
    bodyEl.appendChild(meta);

    wrap.appendChild(bodyEl);

    // Toggle logic
    header.addEventListener("click", () => {
      wrap.classList.toggle("expanded");
    });

    return wrap;
  }

  function renderPlanCard(result, issueNum) {
    const plan = result.plan || result;
    const requiresChange = result.requires_code_change ??
      (typeof plan === "object" ? plan.requires_code_change : null) ??
      String(result.raw_model_text || "").toUpperCase().includes("YES");
    const reason = plan.summary || result.reason || "(see plan below)";
    const files = plan.files || result.files || [];
    const confidence = result.confidence || "unknown";
    const refinementUsed = result.refinement_used || false;

    const card = el("div", "ab-plan-card");

    // Header
    const header = el("div", "ab-plan-header");
    const decision = el("span", "ab-plan-decision " + (requiresChange ? "yes" : "no"));
    decision.textContent = requiresChange ? "REQUIRES CHANGE" : "NO CHANGE PREDICTED";
    header.appendChild(decision);

    const conf = el("span", "ab-confidence");
    conf.style.marginLeft = "auto";
    let strength = "Low";
    if (typeof confidence === "number") {
      if (confidence >= 0.75) strength = "High";
      else if (confidence >= 0.65) strength = "Medium";
    } else {
      strength = confidence;
    }
    conf.textContent = `Evidence Strength: ${strength}`;
    header.appendChild(conf);

    if (issueNum) {
      const ghLink = el("a");
      ghLink.href = `https://github.com/apache/airflow/issues/${issueNum}`;
      ghLink.target = "_blank";
      ghLink.style.marginLeft = "12px";
      ghLink.style.fontSize = "0.9em";
      ghLink.style.textDecoration = "none";
      ghLink.style.color = "var(--vscode-textLink-foreground)";
      ghLink.textContent = "↗ GitHub";
      header.appendChild(ghLink);
    }

    card.appendChild(header);

    // Body
    const body = el("div", "ab-plan-body");

    if (!requiresChange) {
      // No-change path: clear explanation, no action buttons
      const noChangeMsg = el("div", "ab-plan-reason");
      noChangeMsg.style.cssText = "line-height:1.6;";
      noChangeMsg.innerHTML = `
        <strong>The Planner does not believe this issue (#${issueNum}) requires a code fix.</strong><br><br>
        Planner's reasoning: <em>${reason}</em><br><br>
        <span style="opacity:0.7;font-size:0.88em;">
          ⚠️ Note: This prediction may be incorrect, especially on smaller local models.
          If you believe this issue <em>does</em> require a code change, try re-planning with
          more specific instructions, e.g.:<br>
          <code>re-plan issue #${issueNum} focusing on the triggerer serialization module</code>
        </span>
      `;
      body.appendChild(noChangeMsg);
      card.appendChild(body);
      return card;
    }

    // Requires-change path: show summary, files, and action buttons
    if (reason) {
      const r = el("div", "ab-plan-reason");
      r.textContent = reason;
      body.appendChild(r);
    }

    if (files.length > 0) {
      const fileLabel = el("div");
      fileLabel.style.cssText = "font-size:0.78em;font-weight:600;opacity:0.6;letter-spacing:0.04em;margin-top:2px;";
      fileLabel.textContent = "FILES TO MODIFY";
      body.appendChild(fileLabel);

      const fileList = el("ul", "ab-plan-files");
      files.forEach((f) => {
        const li = el("li", "ab-plan-file");
        const path = el("span", "ab-plan-file-path");
        path.textContent = typeof f === "string" ? f : f.path || f;
        path.style.cursor = "pointer";
        path.style.textDecoration = "underline";
        path.style.color = "var(--vscode-textLink-foreground)";
        path.addEventListener("click", () => {
          const repo = document.getElementById("ab-repo").value.trim();
          const filePath = typeof f === "string" ? f : f.path || f;
          vscode.postMessage({ type: "openFile", repo: repo, file: filePath });
        });
        li.appendChild(path);
        if (f.change || f.intent) {
          const change = el("span", "ab-plan-file-change");
          change.textContent = f.change || f.intent || "";
          li.appendChild(change);
        }
        fileList.appendChild(li);
      });
      body.appendChild(fileList);
    }

    if (plan.code_spans && plan.code_spans.length > 0) {
      const spanLabel = el("div");
      spanLabel.style.cssText = "font-size:0.78em;font-weight:600;opacity:0.6;letter-spacing:0.04em;margin-top:10px;";
      spanLabel.textContent = "TARGET FUNCTIONS / SPANS";
      body.appendChild(spanLabel);

      const spanList = el("ul", "ab-plan-files");
      plan.code_spans.forEach((span) => {
        const li = el("li", "ab-plan-file");
        const path = el("span", "ab-plan-file-path");
        const lineStr = span.start_line ? ` (line ${span.start_line})` : "";
        path.textContent = `${span.symbol || 'code span'} in ${span.file}${lineStr}`;
        path.style.cursor = "pointer";
        path.style.textDecoration = "underline";
        path.style.color = "var(--vscode-textLink-foreground)";
        path.addEventListener("click", () => {
          const repo = document.getElementById("ab-repo").value.trim();
          vscode.postMessage({ type: "openFile", repo: repo, file: span.file, line: span.start_line });
        });
        li.appendChild(path);
        spanList.appendChild(li);
      });
      body.appendChild(spanList);
    }

    // Approval buttons — only shown when requiresChange is true
    const actions = el("div", "ab-actions");

    const approveBtn = el("button", "ab-btn ab-btn-approve");
    approveBtn.textContent = "✓ Approve Plan";
    approveBtn.addEventListener("click", async () => {
      approveBtn.disabled = true;
      rejectBtn.disabled = true;
      appendUserMessage("✓ Plan approved — generating patch...");
      setBusy(true);
      try {
        await doApprovePlan(result, issueNum);
      } catch (e) {
        appendAgentText(`❌ ${e.message}`);
      } finally {
        setBusy(false);
      }
    });

    const rejectBtn = el("button", "ab-btn ab-btn-reject");
    rejectBtn.textContent = "✗ Reject";
    rejectBtn.addEventListener("click", () => {
      approveBtn.disabled = true;
      rejectBtn.disabled = true;
      appendUserMessage("✗ Plan rejected.");
      appendAgentText("Plan rejected. You can ask me to re-plan with different instructions, e.g. _\"re-plan issue #45123 focusing on the serialization module\"_.");
      lastPlan = null;
    });

    actions.appendChild(approveBtn);
    actions.appendChild(rejectBtn);
    body.appendChild(actions);
    card.appendChild(body);
    return card;
  }

  function renderDiffCard(result, issueNum) {
    const wrap = el("div");

    // Critic verdict
    const verdict = (result.verdict || "").toUpperCase();
    if (verdict) {
      const vbadge = el("div", "ab-verdict " + verdict.toLowerCase());
      vbadge.textContent = verdict === "ACCEPT" ? "✓ Critic Accepted" : verdict === "REJECT" ? "✗ Critic Rejected" : "⟳ Critic: Revise";
      wrap.appendChild(vbadge);
    }

    if (result.reasoning) {
      const fb = el("div");
      fb.style.cssText = "font-size:0.82em;opacity:0.75;margin-bottom:6px;line-height:1.45;";
      fb.textContent = result.reasoning;
      wrap.appendChild(fb);
    }

    if (result.diff) {
      const diffWrap = el("div", "ab-diff-wrap");
      const diffLabel = el("div", "ab-diff-label");
      diffLabel.textContent = "UNIFIED DIFF";
      diffWrap.appendChild(diffLabel);
      const diffPre = el("pre", "ab-diff");
      diffPre.innerHTML = colorDiff(result.diff);
      diffWrap.appendChild(diffPre);
      wrap.appendChild(diffWrap);
    }

    // Action buttons
    if (verdict === "ACCEPT" && result.diff) {
      const actions = el("div", "ab-actions");

      const applyBtn = el("button", "ab-btn ab-btn-apply");
      applyBtn.textContent = "Apply to Workspace";
      applyBtn.addEventListener("click", () => {
        applyBtn.disabled = true;
        vscode.postMessage({ type: "applyDiff", diff: result.diff, repo_path: repoInput.value.trim() });
        appendAgentText("📋 Diff sent to workspace. Use `git apply` or the VS Code diff editor to review it.");
      });

      const prBtn = el("button", "ab-btn ab-btn-pr");
      prBtn.textContent = "Open PR Draft";
      prBtn.addEventListener("click", async () => {
        prBtn.disabled = true;
        setBusy(true);
        try {
          const pr = await callOrchestrate({ command: "open_pr", diff: result.diff, issue_number: issueNum });
          appendAgentText(`🔗 PR draft created: [${pr.title}](${pr.html_url})`);
        } catch (e) {
          appendAgentText(`❌ PR creation failed: ${e.message}`);
        } finally {
          setBusy(false);
        }
      });

      actions.appendChild(applyBtn);
      actions.appendChild(prBtn);
      wrap.appendChild(actions);
    }

    return wrap;
  }

  function colorDiff(text) {
    return text
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .split("\n")
      .map((line) => {
        if (line.startsWith("+") && !line.startsWith("+++")) return `<span class="diff-add">${line}</span>`;
        if (line.startsWith("-") && !line.startsWith("---")) return `<span class="diff-del">${line}</span>`;
        if (line.startsWith("@@")) return `<span class="diff-hunk">${line}</span>`;
        return line;
      })
      .join("\n");
  }

  // ── DOM helpers ───────────────────────────────────────────
  function el(tag, cls) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    return e;
  }

  function textNode(text) {
    const d = el("div");
    d.style.cssText = "font-size:0.86em;line-height:1.5;margin-top:3px;";
    d.textContent = text;
    return d;
  }

  function removeWelcome() {
    const w = document.getElementById("ab-welcome");
    if (w) w.remove();
  }

  function appendUserMessage(text) {
    const msg = el("div", "ab-msg ab-msg-user");
    const label = el("div", "ab-msg-label");
    label.textContent = "You";
    const bubble = el("div", "ab-bubble");
    bubble.textContent = text;
    msg.appendChild(label);
    msg.appendChild(bubble);
    chatFeed.appendChild(msg);
    scrollToBottom();
    return msg;
  }

  function appendAgentText(text) {
    const msg = appendAgentBubble();
    const bubble = msg.querySelector(".ab-bubble");
    // Simple markdown-lite: **bold** and `code` and _italic_
    bubble.innerHTML = text
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
      .replace(/`(.+?)`/g, "<code>$1</code>")
      .replace(/_(.+?)_/g, "<em>$1</em>");
    return msg;
  }

  function appendStepsContainer(agentMsgEl) {
    const steps = el("div", "ab-steps");
    agentMsgEl.querySelector(".ab-bubble").appendChild(steps);
    return steps;
  }

  function addStep(stepsEl, text) {
    const step = el("div", "ab-step active");
    const spinner = el("span", "ab-spinner");
    const txt = el("span");
    txt.textContent = text;
    step.appendChild(spinner);
    step.appendChild(txt);
    stepsEl.appendChild(step);
    scrollToBottom();
    return step;
  }

  function markStepDone(stepEl, icon = "✓") {
    if (!stepEl) return;
    stepEl.className = "ab-step done";
    const spinner = stepEl.querySelector(".ab-spinner");
    if (spinner) {
      spinner.replaceWith(
        Object.assign(el("span", "ab-step-icon"), { textContent: icon })
      );
    }
  }

  function appendStep(stepId, text, done) {
    // Used by SSE streaming (Phase 2)
    let stepEl = document.getElementById("step-" + stepId);
    if (!stepEl) {
      const lastAgent = chatFeed.querySelector(".ab-msg-agent:last-child .ab-steps");
      if (lastAgent) {
        stepEl = addStep(lastAgent, text);
        stepEl.id = "step-" + stepId;
      }
    } else {
      stepEl.querySelector("span:last-child").textContent = text;
    }
    if (done && stepEl) markStepDone(stepEl);
  }

  function scrollToBottom() {
    chatFeed.scrollTop = chatFeed.scrollHeight;
  }

  function setBusy(busy) {
    isBusy = busy;
    inputEl.disabled = busy;
  }

  function delay(ms) {
    return new Promise((r) => setTimeout(r, ms));
  }

  // ── Boot ──────────────────────────────────────────────────
  vscode.postMessage({ type: "ready" });
})();
