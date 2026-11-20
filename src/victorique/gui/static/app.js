// Victorique local interface. Talks only to the server that served this page.
"use strict";

(function () {
  const token = document.querySelector('meta[name="victorique-token"]').content;
  const views = ["new", "runs", "run"];
  let options = null;
  let generation = 0;
  let pollTimer = null;
  let openRun = null;
  let undecidedChoice = "abstain";
  const openState = new Map();
  // The run whose Stop run confirmation is open, and the latest detail of the open run.
  let confirmingStop = null;
  let shownRun = null;

  // Chinese captions under the English status labels.
  const STATUS_CAPTIONS = {starting: "启动中", running: "运行中", stopping: "停止中", delivered: "已交付",
    abstained: "未交付", failed: "失败", interrupted: "中途停止", stopped: "已停止"};
  const CALL_CAPTIONS = {completed: "完成", timed_out: "超时", stopped: "已停止", failed: "失败",
    returned_invalid: "回答无效", "in-progress": "进行中", unrecorded: "无回答"};
  // Steps of the time scales, in seconds.
  const TIME_STEPS = [5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400];
  // Anthropic's terms (code.claude.com/docs/en/legal-and-compliance, checked 2026-11-19).
  const CLAUDE_LOGIN = "Log Claude Code in with an Anthropic Console account: run claude, type /login and choose " +
    "the Anthropic Console account (API usage billing). Anthropic's terms keep the Claude.ai subscription login " +
    "(Free, Pro, Max, Ultra) for ordinary use of its own apps and ask developers who build products to use an " +
    "API key. Victorique removes ANTHROPIC_API_KEY from the Claude Code process environment, so Claude Code " +
    "runs with the login stored by /login; Victorique cannot see which login that is.";

  // ---- small helpers -------------------------------------------------------

  function el(tag, attributes, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attributes || {})) {
      if (value === null || value === undefined || value === false) continue;
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else if (key === "vars") setVars(node, value);
      else node.setAttribute(key, value === true ? "" : String(value));
    }
    for (const child of children.flat()) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  // Bar, gauge and timeline values are custom properties set through the CSS object model;
  // the page's content security policy refuses style attributes.
  function setVars(node, vars) {
    for (const [name, value] of Object.entries(vars)) node.style.setProperty(name, String(value));
  }

  function cap(text) {
    return text ? el("span", {class: "cap", lang: "zh-CN", text: text}) : null;
  }

  async function api(path, init) {
    let response;
    try {
      response = await fetch(path, Object.assign({cache: "no-store"}, init || {}));
    } catch (error) {
      document.getElementById("offline").classList.remove("hidden");
      throw new Error("The interface is not responding.");
    }
    document.getElementById("offline").classList.add("hidden");
    let data = {};
    try {
      data = await response.json();
    } catch (error) {
      data = {};
    }
    if (!response.ok) throw new Error(data.error || ("Request failed (" + response.status + ")."));
    return data;
  }

  function when(iso) {
    if (!iso) return "";
    const date = new Date(iso);
    return isNaN(date) ? iso : date.toLocaleString();
  }

  function duration(seconds) {
    if (seconds === null || seconds === undefined || isNaN(seconds)) return "";
    seconds = Math.max(0, Math.round(seconds));
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const rest = seconds % 60;
    if (hours) return hours + " h " + minutes + " min";
    if (minutes) return minutes + " min " + rest + " s";
    return rest + " s";
  }

  // A time span for scales and axes, without zero parts: "0", "45 s", "15 min", "1 h 30 min".
  function shortDuration(seconds) {
    seconds = Math.round(seconds);
    if (!(seconds > 0)) return "0";
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const rest = seconds % 60;
    const parts = [];
    if (hours) parts.push(hours + " h");
    if (minutes) parts.push(minutes + " min");
    if (rest && !hours) parts.push(rest + " s");
    return parts.join(" ");
  }

  // The smallest scale step that divides ``total`` seconds into at most ``most`` parts.
  function timeStep(total, most) {
    for (const step of TIME_STEPS) {
      if (total / step <= most) return step;
    }
    return 86400 * Math.ceil(total / (86400 * most));
  }

  // A share between 0 and 1, rounded for a custom property.
  function fraction(value) {
    if (typeof value !== "number" || !isFinite(value)) return 0;
    return Math.round(Math.min(1, Math.max(0, value)) * 10000) / 10000;
  }

  function percent(share) {
    return Math.round(share * 100) + "%";
  }

  function number(value) {
    return typeof value === "number" ? value.toLocaleString() : "";
  }

  function tokens(value) {
    if (!value) return "not reported";
    const parts = [];
    if (typeof value.input === "number") parts.push(number(value.input) + " in");
    if (typeof value.output === "number") parts.push(number(value.output) + " out");
    return parts.length ? parts.join(" / ") : "not reported";
  }

  // Token counts for a table cell or fact; "not reported" is set in the body font.
  function tokensCell(value) {
    const text = tokens(value);
    return text === "not reported" ? el("span", {class: "na", text: text}) : text;
  }

  function size(bytes) {
    if (bytes < 1024) return bytes + " bytes";
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
    return (bytes / 1024 / 1024).toFixed(1) + " MB";
  }

  function badge(status, label) {
    return el("span", {class: "badge status-" + status}, label, cap(STATUS_CAPTIONS[status]));
  }

  function calls(run) {
    if (run.calls_used === null || run.calls_used === undefined) return "";
    return run.calls_limit ? run.calls_used + " of " + run.calls_limit : String(run.calls_used);
  }

  function schedule(callback, delay) {
    clearTimeout(pollTimer);
    const mine = generation;
    pollTimer = setTimeout(() => { if (mine === generation) callback(); }, delay);
  }

  // ---- routing --------------------------------------------------------------

  function show(view) {
    for (const name of views) {
      document.getElementById("view-" + name).classList.toggle("hidden", name !== view);
    }
    for (const link of document.querySelectorAll("[data-nav]")) {
      const current = link.dataset.nav === view || (view === "run" && link.dataset.nav === "runs");
      link.classList.toggle("current", current);
      if (current) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    }
  }

  function route() {
    generation += 1;
    clearTimeout(pollTimer);
    const hash = location.hash || "#/new";
    let run = null;
    if (hash.startsWith("#/run/")) {
      try {
        run = decodeURIComponent(hash.slice("#/run/".length));
      } catch (error) {
        run = null;
      }
    }
    if (run) {
      show("run");
      loadRun(run, true);
    } else if (hash === "#/runs") {
      show("runs");
      loadRuns();
    } else {
      show("new");
      document.title = "New problem - Victorique";
    }
  }

  // ---- new problem ----------------------------------------------------------

  function setupForm() {
    const effort = document.getElementById("effort");
    for (const value of options.reasoning_efforts) effort.append(el("option", {value: value, text: value}));
    const checkEffort = document.getElementById("check-effort");
    for (const value of options.reasoning_efforts) checkEffort.append(el("option", {value: value, text: value}));
    // Older solve commands check with the solver's model service and repeat no timed-out call.
    const checks = options.checks || {configurable: false};
    document.getElementById("check-backend").disabled = !checks.configurable;
    setupRetries();
    const timeout = document.getElementById("call-timeout");
    const timeoutHelp = document.getElementById("call-timeout-help");
    if (options.call_timeout.configurable) {
      const range = options.call_timeout.range;
      timeout.disabled = false;
      timeout.min = String(range[0]);
      timeout.max = String(range[1]);
      timeout.placeholder = "Empty: the command's default";
      timeoutHelp.textContent = "How long one model call may take before it counts as failed, from " + range[0] +
        " to " + range[1] + " seconds. Empty uses the command's default: " + options.call_timeout.default_seconds +
        " seconds, or call_timeout_seconds from the settings file named by VICTORIQUE_CONFIG.";
    } else {
      timeoutHelp.textContent = "Every model call may take up to " + options.call_timeout.fixed_seconds +
        " seconds (less if the whole run's time limit is shorter). This version does not let you change it.";
    }
    // The preselected choice is the solve command's own setting: undecided_checks from the settings file
    // named by VICTORIQUE_CONFIG, else Abstain.
    const undecided = document.getElementById("undecided");
    undecidedChoice = options.undecided_checks.default || "abstain";
    undecided.replaceChildren(...options.undecided_checks.choices.map((choice) => el("option", {value: choice.value,
      text: choice.label + (choice.value === undecidedChoice ? " (default)" : "")})));
    undecided.value = undecidedChoice;
    setupMinorFindings();
    const backend = document.getElementById("backend");
    backend.addEventListener("change", updateBackend);
    document.getElementById("check-backend").addEventListener("change", updateChecks);
    updateBackend();
    for (const input of document.querySelectorAll('input[name="policy"]')) {
      input.addEventListener("change", updatePolicy);
    }
    updatePolicy();
    document.getElementById("new-form").addEventListener("submit", submit);
  }

  // The number of retries after a call timeout: the solve command's own setting, timeout_retries from the
  // settings file named by VICTORIQUE_CONFIG, else 1.
  function setupRetries() {
    const field = document.getElementById("timeout-retries");
    const help = document.getElementById("timeout-retries-help");
    const setting = options.timeout_retries;
    if (!setting || !setting.configurable) {
      field.disabled = true;
      field.value = "";
      help.textContent = "This version of Victorique does not send a call again after it reaches its time limit.";
      return;
    }
    field.disabled = false;
    field.min = String(setting.range[0]);
    field.max = String(setting.range[1]);
    field.value = String(setting.default);
    let source = " The value shown is the command's default.";
    if (setting.settings_error) {
      source = " The settings file named by VICTORIQUE_CONFIG could not be read, so the built-in default is shown: " +
        setting.settings_error;
    } else if (setting.source === "settings file") {
      source = " The value shown is timeout_retries from the settings file named by VICTORIQUE_CONFIG.";
    }
    help.textContent = "How often a model call that reaches its time limit is sent again, from " + setting.range[0] +
      " to " + setting.range[1] + ". Each repetition is a new call: it counts toward the maximum model calls and " +
      "gets at most the run's remaining time. 0 ends the run at the first timeout." + source;
  }

  // The choice for undecided checks applies to ladder, and only when the solve command has the option.
  function updatePolicy() {
    const policy = document.querySelector('input[name="policy"]:checked').value;
    const undecided = document.getElementById("undecided");
    const help = document.getElementById("undecided-help");
    const setting = options.undecided_checks;
    if (!setting.configurable) {
      undecided.disabled = true;
      undecided.value = "abstain";
      help.textContent = "This version of Victorique always stops without a proof when a check is undecided.";
      return;
    }
    // The reader's ladder choice comes back when ladder is chosen again.
    if (!undecided.disabled) undecidedChoice = undecided.value;
    undecided.disabled = policy !== "ladder";
    undecided.value = undecided.disabled ? "abstain" : undecidedChoice;
    let source = "";
    if (setting.settings_error) {
      source = " The settings file named by VICTORIQUE_CONFIG could not be read, so Abstain is preselected: " +
        setting.settings_error;
    } else if (setting.source === "settings file") {
      source = " The preselected choice is undecided_checks from the settings file named by VICTORIQUE_CONFIG.";
    }
    help.textContent = policy === "ladder"
      ? "A check is undecided when it neither passes nor reports a serious or fatal problem, or when it returns " +
        "no usable report. Abstain stops the run without a proof. Repair once sends the draft to one repair, " +
        "as after a failed check; a second undecided check stops the run." + source
      : "Only the approach Solve, check and repair (ladder) uses this choice.";
  }

  // Every approach uses this choice. Its preselected value is the solve command's own setting:
  // minor_findings from the settings file named by VICTORIQUE_CONFIG, else Count it as passed.
  function setupMinorFindings() {
    const minor = document.getElementById("minor");
    const help = document.getElementById("minor-help");
    const setting = options.minor_findings;
    if (!setting || !setting.configurable) {
      minor.disabled = true;
      minor.value = "nonblocking";
      help.textContent = "This version of Victorique has no choice for this: a check that says pass with only " +
        "minor findings counts as passed.";
      return;
    }
    const chosen = setting.default || "nonblocking";
    minor.replaceChildren(...setting.choices.map((choice) => el("option", {value: choice.value,
      text: choice.label + (choice.value === chosen ? " (default)" : "")})));
    minor.value = chosen;
    minor.disabled = false;
    let source = "";
    if (setting.settings_error) {
      source = " The settings file named by VICTORIQUE_CONFIG could not be read, so Count it as passed is " +
        "preselected: " + setting.settings_error;
    } else if (setting.source === "settings file") {
      source = " The preselected choice is minor_findings from the settings file named by VICTORIQUE_CONFIG.";
    }
    help.textContent = "Count it as passed lets such a check pass and only records its minor findings; Count it " +
      "as not passed treats them like serious problems, so single delivers no proof and ladder and full repair " +
      "the draft. The web interface guide (docs/gui.md) gives the results of a replay on 500 proofs graded by " +
      "people." + source;
  }

  function updateBackend() {
    const backend = document.getElementById("backend").value;
    const effort = document.getElementById("effort");
    effort.disabled = backend !== "codex";
    if (effort.disabled) effort.value = "";
    document.getElementById("effort-help").textContent = backend === "codex"
      ? "How long the model may think on each call."
      : "Only the Codex CLI lets you choose this.";
    document.getElementById("model").disabled = backend === "fake";
    document.getElementById("backend-help").textContent = backend === "fake"
      ? "No model is called; fixed example answers run through the same steps."
      : "Uses the command-line tool installed on this computer, with the account you logged in to yourself. " +
        "Victorique never asks for or reads passwords or keys." + (backend === "claude" ? " " + CLAUDE_LOGIN : "");
    updateChecks();
  }

  // The checks use the solver's service unless another one is chosen; model and effort follow the service.
  function updateChecks() {
    const configurable = Boolean(options.checks && options.checks.configurable);
    const backend = document.getElementById("backend").value;
    const choice = document.getElementById("check-backend").value;
    const service = choice || backend;
    const same = service === backend;
    const model = document.getElementById("check-model");
    const effort = document.getElementById("check-effort");
    model.disabled = !configurable || service === "fake";
    if (model.disabled) model.value = "";
    model.placeholder = same ? "Empty: the solver's model" : "Empty: the tool's default model";
    effort.disabled = !configurable || service !== "codex";
    if (effort.disabled) effort.value = "";
    effort.options[0].textContent = same ? "Same as the solver" : "The tool's default";
    document.getElementById("check-effort-help").textContent = service === "codex"
      ? "How long the model may think on each check."
      : "Only the Codex CLI lets you choose this.";
    let help = "The statement check and the proof check use this service; every other step uses the model " +
      "service above. Same as the solver is the default; another service is optional.";
    if (!configurable) help = "This version of Victorique checks with the solver's model service.";
    else if ((backend === "fake") !== (service === "fake")) {
      help = "A practice run calls no model for solving or for the checks: choose Practice run for both model " +
        "services or for neither.";
    } else if (service === "claude") help += " " + CLAUDE_LOGIN;
    document.getElementById("check-backend-help").textContent = help;
  }

  async function submit(event) {
    event.preventDefault();
    const form = event.target;
    const error = document.getElementById("form-error");
    const button = document.getElementById("start");
    error.textContent = "";
    const statement = document.getElementById("statement").value;
    if (!statement.trim()) {
      error.textContent = "Write or paste the problem statement.";
      document.getElementById("statement").focus();
      return;
    }
    const body = {
      title: document.getElementById("title").value,
      statement: statement,
      policy: form.querySelector('input[name="policy"]:checked').value,
      backend: document.getElementById("backend").value,
      model: document.getElementById("model").disabled ? "" : document.getElementById("model").value,
      reasoning_effort: document.getElementById("effort").value || null,
      call_timeout: document.getElementById("call-timeout").disabled ? null
        : (document.getElementById("call-timeout").value || null),
      undecided_checks: document.getElementById("undecided").disabled ? null
        : document.getElementById("undecided").value,
      minor_findings: document.getElementById("minor").disabled ? null : document.getElementById("minor").value,
      check_backend: document.getElementById("check-backend").disabled ? ""
        : document.getElementById("check-backend").value,
      check_model: document.getElementById("check-model").disabled ? "" : document.getElementById("check-model").value,
      check_reasoning_effort: document.getElementById("check-effort").value || null,
      timeout_retries: document.getElementById("timeout-retries").disabled ? null
        : (document.getElementById("timeout-retries").value || null),
      calls: document.getElementById("calls").value,
      hours: document.getElementById("hours").value,
    };
    button.disabled = true;
    button.textContent = "Starting...";
    try {
      const result = await api("/api/runs", {
        method: "POST",
        headers: {"Content-Type": "application/json", "X-Victorique-Token": token},
        body: JSON.stringify(body),
      });
      location.hash = "#/run/" + encodeURIComponent(result.name);
    } catch (failure) {
      error.textContent = failure.message;
    } finally {
      button.disabled = false;
      button.textContent = "Start";
    }
  }

  // ---- runs list ------------------------------------------------------------

  async function loadRuns() {
    const mine = generation;
    const target = document.getElementById("runs-table");
    document.title = "Runs - Victorique";
    let data;
    try {
      data = await api("/api/runs");
    } catch (failure) {
      if (mine === generation) schedule(loadRuns, 5000);
      return;
    }
    if (mine !== generation) return;
    document.getElementById("runs-dir").textContent = data.runs_dir;
    // Rebuild only when something changed, so clicks and selections are not interrupted.
    const snapshot = JSON.stringify(data.runs);
    if (snapshot !== target.dataset.snapshot || !target.firstChild) {
      target.dataset.snapshot = snapshot;
      target.replaceChildren(runsTable(data.runs));
    }
    schedule(loadRuns, data.runs.some((run) => !run.finished) ? 2500 : 6000);
  }

  function runsTable(rows) {
    if (!rows.length) {
      return el("p", {class: "box"}, "No runs yet. Start one on the ", el("a", {href: "#/new", text: "New problem"}),
        " page.");
    }
    const body = rows.map((run) => el("tr", {},
      el("td", {}, el("a", {href: "#/run/" + encodeURIComponent(run.name), text: run.title})),
      el("td", {class: "status-cell"}, badge(run.status, run.status_label)),
      el("td", {"data-label": "Approach", text: run.policy_label || ""}),
      el("td", {"data-label": "Model service", text: run.backend_label || ""}),
      el("td", {class: "when", "data-label": "Started", text: when(run.started_at)}),
      el("td", {class: "number", "data-label": "Model calls"}, callsValue(run)),
      el("td", {class: "number tok", "data-label": "Tokens"}, tokensCell(run.tokens))));
    return el("div", {class: "table-wrap"}, el("table", {class: "runs"},
      el("thead", {}, el("tr", {},
        ["Run", "Status", "Approach", "Model service", "Started"].map((label) => el("th", {scope: "col", text: label})),
        ["Model calls", "Tokens"].map((label) => el("th", {scope: "col", class: "number", text: label})))),
      el("tbody", {}, body)));
  }

  // The call count, with a thin bar for the share of the call budget used.
  function callsValue(run) {
    const text = calls(run);
    const used = run.calls_used;
    const limit = run.calls_limit;
    if (!text || typeof used !== "number" || typeof limit !== "number" || limit <= 0) return text;
    return el("span", {class: "val"},
      el("span", {class: "bar", "aria-hidden": "true", vars: {"--fill": fraction(used / limit)}}), text);
  }

  // ---- run detail -----------------------------------------------------------

  async function loadRun(name, first) {
    const mine = generation;
    const target = document.getElementById("view-run");
    if (first) target.replaceChildren(el("p", {class: "muted", text: "Loading..."}));
    let run;
    try {
      run = await api("/api/runs/" + encodeURIComponent(name));
    } catch (failure) {
      if (mine !== generation) return;
      if (first) target.replaceChildren(el("p", {class: "box warn", text: failure.message}),
        el("p", {}, el("a", {href: "#/runs", text: "Back to all runs"})));
      schedule(() => loadRun(name, false), 5000);
      return;
    }
    if (mine !== generation) return;
    if (openRun !== name) {
      openRun = name;
      openState.clear();
      confirmingStop = null;
      delete target.dataset.snapshot;
    }
    // A confirmation left open belongs to a run that is still running.
    if (run.status !== "running") confirmingStop = null;
    shownRun = run;
    // Rebuild only when the run changed, so reading, selecting and clicking are not interrupted.
    const snapshot = JSON.stringify(run);
    if (first || snapshot !== target.dataset.snapshot) {
      target.dataset.snapshot = snapshot;
      target.replaceChildren(...runView(run));
    } else {
      updateElapsed(run);
    }
    document.title = run.title + " - Victorique";
    if (!run.finished) schedule(() => loadRun(name, false), 2000);
  }

  // Rebuild the open run's page after a change made on the page itself.
  function redraw() {
    if (shownRun) document.getElementById("view-run").replaceChildren(...runView(shownRun));
  }

  // POST /api/runs/NAME/stop or /resume, with the page token like the form.
  function runAction(run, action) {
    return api("/api/runs/" + encodeURIComponent(run.name) + "/" + action, {
      method: "POST",
      headers: {"Content-Type": "application/json", "X-Victorique-Token": token},
      body: "{}",
    });
  }

  // The Stop run button of a running run, and its confirmation.
  function stopControls(run) {
    if (confirmingStop !== run.name) {
      const button = el("button", {type: "button", class: "secondary stop"}, "Stop run");
      button.addEventListener("click", () => {
        confirmingStop = run.name;
        redraw();
      });
      return el("div", {class: "run-actions"}, button);
    }
    const error = el("p", {class: "error", role: "alert"});
    const confirm = el("button", {type: "button", class: "stop"}, "Stop the run");
    const keep = el("button", {type: "button", class: "secondary"}, "Keep running");
    confirm.addEventListener("click", async () => {
      confirm.disabled = true;
      keep.disabled = true;
      confirm.textContent = "Stopping...";
      try {
        await runAction(run, "stop");
        confirmingStop = null;
        loadRun(run.name, false);
      } catch (failure) {
        error.textContent = failure.message;
        confirm.disabled = false;
        keep.disabled = false;
        confirm.textContent = "Stop the run";
      }
    });
    keep.addEventListener("click", () => {
      confirmingStop = null;
      redraw();
    });
    return el("div", {class: "run-actions confirm", role: "group", "aria-label": "Stop this run"},
      el("p", {}, el("strong", {text: "Stop this run? "}), "The current model call is abandoned, the run ends " +
        "without a proof, and the best draft is kept. No further model call is made."),
      el("div", {class: "buttons"}, confirm, keep), error);
  }

  // The Resume button of a run whose process ended without a summary.
  function resumeControls(run) {
    const error = el("p", {class: "error", role: "alert"});
    const button = el("button", {type: "button"}, "Resume");
    button.addEventListener("click", async () => {
      button.disabled = true;
      button.textContent = "Starting...";
      try {
        await runAction(run, "resume");
        loadRun(run.name, false);
      } catch (failure) {
        error.textContent = failure.message;
        button.disabled = false;
        button.textContent = "Resume";
      }
    });
    return el("div", {class: "run-actions"}, el("p", {}, "Resume continues this run with its original call " +
      "budget and time limit, as victorique resume does. A run past its time limit ends at once and writes " +
      "its summary."), el("div", {class: "buttons"}, button), error);
  }

  function elapsedSeconds(run) {
    if (run.wall_seconds !== null && run.wall_seconds !== undefined) return run.wall_seconds;
    return run.started_at ? (Date.now() - new Date(run.started_at)) / 1000 : null;
  }

  // The time used against the run's own time limit, or null when either is unknown.
  function timeShare(run) {
    const total = (Date.parse(run.deadline) - Date.parse(run.started_at)) / 1000;
    if (!(total > 0)) return null;
    if (run.finished && typeof run.wall_seconds !== "number") return null;
    const elapsed = elapsedSeconds(run);
    if (typeof elapsed !== "number" || isNaN(elapsed)) return null;
    return {total: total, elapsed: elapsed, share: fraction(elapsed / total)};
  }

  // Between rebuilds, only the running time changes.
  function updateElapsed(run) {
    const elapsed = document.getElementById("elapsed");
    if (elapsed) elapsed.textContent = duration(elapsedSeconds(run));
    const time = timeShare(run);
    const value = document.getElementById("time-used");
    const share = document.getElementById("time-share");
    const bar = document.getElementById("time-bar");
    if (time && value && share && bar) {
      value.textContent = duration(time.elapsed);
      share.textContent = percent(time.share);
      setVars(bar, {"--fill": time.share});
    }
  }

  // Sections keep the open or closed state the reader chose while the page updates.
  function details(key, summary, caption, defaultOpen, ...content) {
    const remembered = openState.get(key);
    const label = el("summary", {}, summary, cap(caption));
    const node = el("details", {"data-key": key, open: remembered === undefined ? defaultOpen : remembered},
      label, ...content);
    // A click on the summary toggles the section after this handler runs.
    label.addEventListener("click", () => openState.set(key, !node.open));
    return node;
  }

  function fact(label, value, extra) {
    if (value === null || value === undefined || value === "") return null;
    const more = extra || {};
    return el("div", {class: more.wide ? "fact wide" : "fact"}, el("dt", {text: label}),
      el("dd", {id: more.id, class: more.num ? "num" : null}, value));
  }

  function runView(run) {
    const parts = [];
    parts.push(el("p", {class: "crumbs"}, el("a", {href: "#/runs", text: "All runs"})));
    parts.push(el("p", {class: "run-name", text: run.name}));
    parts.push(el("h1", {class: "run-title"}, run.title, " ", badge(run.status, run.status_label)));
    parts.push(outcomeBox(run));
    if (run.progress) {
      parts.push(el("p", {class: "progress"}, el("span", {class: "spinner", "aria-hidden": "true"}),
        "Now: " + run.progress.step + ", " + run.progress.phase + ". Model calls used: " +
        run.progress.calls_used + " of " + run.progress.calls_limit + "."));
    } else if (run.status === "running" || run.status === "starting" || run.status === "stopping") {
      parts.push(el("p", {class: "progress"}, el("span", {class: "spinner", "aria-hidden": "true"}),
        run.started_here ? "Waiting for the first progress report." :
          "This run was not started from this window; its state is read from its files."));
    }
    parts.push(statusSection(run));
    // On wide screens the proof (or best draft) and the checks sit side by side when the run has both.
    const paired = Boolean(run.proof) && run.checks.length > 0;
    if (run.proof) parts.push(proofSection(run, paired));
    if (run.checks.length) parts.push(checksSection(run, paired));
    if (run.problem) parts.push(details("problem", "Problem statement", "题面", !run.proof,
      el("pre", {class: "statement", text: run.problem})));
    if (run.calls.length) parts.push(details("calls", "Model calls (" + run.calls.length + ")", "模型调用",
      !run.finished, callsTable(run)));
    parts.push(filesSection(run));
    if (run.console.length) parts.push(details("log", "Technical log", "技术日志", false,
      el("pre", {class: "log", text: run.console.join("\n")})));
    return parts;
  }

  function statusSection(run) {
    const gauges = [callsGauge(run), timeGauge(run)].filter(Boolean);
    return el("section", {class: "run-status", "aria-labelledby": "status-heading"},
      el("h2", {id: "status-heading", class: "label"}, "Run status", cap("运行状态")),
      gauges.length ? el("div", {class: "gauges"}, gauges) : null,
      el("dl", {class: "facts"}, runFacts(run)));
  }

  function runFacts(run) {
    const model = run.backend === "fake" ? null : run.reported_model || run.model;
    return [
      fact("Approach", run.policy_label),
      fact("Model service", run.backend_label),
      fact("Model", run.backend === "fake" ? "none (practice run)" : model || "the tool's default",
        {num: Boolean(model)}),
      fact("Reasoning effort", run.reasoning_effort || (run.backend === "codex" ? "the tool's default" : null),
        {num: Boolean(run.reasoning_effort)}),
      fact("Time limit per model call", typeof run.call_timeout_seconds === "number"
        ? number(run.call_timeout_seconds) + " seconds" : null, {num: true}),
      fact("When a check is undecided", run.undecided_checks_label),
      fact("When a check passes with minor findings", run.minor_findings_label),
      // The checks' model service is shown only when it differs from the solver's.
      fact("Check model service", run.check_separate ? run.check_backend_label : null),
      fact("Check model", run.check_separate ? (run.check_reported_model || run.check_model || "the tool's default")
        : null, {num: Boolean(run.check_reported_model || run.check_model)}),
      fact("Check reasoning effort", run.check_separate ? (run.check_reasoning_effort ||
        (run.check_backend === "codex" ? "the tool's default" : null)) : null, {num: Boolean(run.check_reasoning_effort)}),
      fact("Retries after a call timeout", typeof run.timeout_retries === "number"
        ? run.timeout_retries + " per call; " + (run.timeout_retries_used ? run.timeout_retries_used + " used" : "none used")
        : null, {num: true}),
      fact("Model calls", calls(run), {num: true}),
      fact("Tokens", tokensCell(run.tokens), {num: true}),
      fact("Started", when(run.started_at), {num: true}),
      fact(run.finished ? "Time used" : "Running for", duration(elapsedSeconds(run)), {num: true, id: "elapsed"}),
      fact("Time limit", when(run.deadline), {num: true}),
      fact("Run folder", run.run_directory ? el("code", {text: run.run_directory}) : null, {wide: true}),
    ];
  }

  // A budget gauge: label and share, the amount used of the total, a bar and a scale.
  function gauge(parts) {
    const ids = parts.ids || {};
    return el("div", {class: "gauge"},
      el("div", {class: "gauge-head"}, el("p", {class: "label"}, parts.label, cap(parts.caption)),
        el("span", {class: "pct", id: ids.share, text: percent(parts.share)})),
      el("p", {class: "gauge-value"}, el("span", {class: "big", id: ids.value, text: parts.value}),
        " of " + parts.total),
      el("span", {class: parts.segments ? "gauge-bar segmented" : "gauge-bar", id: ids.bar, "aria-hidden": "true",
        vars: parts.segments ? {"--fill": parts.share, "--segments": parts.segments} : {"--fill": parts.share}}),
      el("p", {class: "gauge-scale", "aria-hidden": "true", vars: {"--ticks": parts.ticks}},
        el("span", {text: "0"}), el("span", {text: parts.total})));
  }

  function callsGauge(run) {
    const used = run.calls_used;
    const limit = run.calls_limit;
    if (typeof used !== "number" || typeof limit !== "number" || limit <= 0) return null;
    // One segment per allowed call; a continuous bar for large budgets.
    return gauge({label: "Model calls", caption: "调用次数", share: fraction(used / limit), value: String(used),
      total: String(limit), segments: limit <= 40 ? limit : null,
      ticks: [4, 5, 3, 2].find((count) => limit % count === 0) || 1});
  }

  function timeGauge(run) {
    const time = timeShare(run);
    if (!time) return null;
    return gauge({label: "Time", caption: "时间", share: time.share, value: duration(time.elapsed),
      total: shortDuration(time.total), ticks: Math.round(time.total / timeStep(time.total, 6) * 100) / 100,
      ids: {value: "time-used", share: "time-share", bar: "time-bar"}});
  }

  function outcomeBox(run) {
    if (run.status === "delivered") {
      return el("div", {class: "box good"}, el("strong", {text: "Proof delivered. "}),
        "Both checks passed on the same final version of the proof.");
    }
    if (run.status === "running" || run.status === "starting") {
      return el("div", {class: "box"}, "The run is in progress. This page updates by itself.",
        run.can_stop ? stopControls(run) : null);
    }
    if (run.status === "stopping") {
      return el("div", {class: "box warn"}, el("strong", {text: "Stopping. "}),
        "The run sends no new model call; the call in progress is being abandoned. The run then ends without " +
        "a proof and keeps its best draft. This page updates by itself.");
    }
    const box = el("div", {class: "box " + (run.status === "abstained" || run.status === "stopped" ? "warn" : "bad")});
    const heading = {abstained: "No proof delivered. ", failed: "The run failed. ",
      interrupted: "The run stopped before it finished. ", stopped: "Stopped by you. "}[run.status] || "";
    box.append(el("strong", {text: heading}), run.reason || "");
    const a = run.abstention;
    if (a) {
      if (a.unresolved_issues.length) box.append(el("h3", {text: "Problems the checks found"}),
        findingsList(a.unresolved_issues));
      if (a.unresolved_questions.length) box.append(el("h3", {text: "Open questions from the checks"}),
        el("ul", {}, a.unresolved_questions.map((item) => el("li", {text: item}))));
      if (a.incomplete_checks.length) box.append(el("h3", {text: "Checks that could not finish"}),
        el("ul", {}, a.incomplete_checks.map((item) => el("li", {text: item}))));
      const inconclusive = a.inconclusive_checks || [];
      // The marker shows what the check said: no verdict, or pass or fail without a blocking finding.
      if (inconclusive.length) box.append(el("h3", {text: "Checks that neither passed nor found a serious problem"}),
        el("ul", {}, inconclusive.map((item) => el("li", {class: item.outcome === "unknown" ? "v-unknown"
          : item.outcome === "pass" || item.outcome === "fail" ? "v-undecided" : null, text: item.text}))));
      if (a.checks_not_run.length) box.append(el("p", {},
        "Did not run: " + a.checks_not_run.join(" and ") + "."));
      if (!a.unresolved_issues.length && !inconclusive.length && a.failed_checks.length) box.append(el("h3", {text: "Checks that did not pass"}),
        el("ul", {}, a.failed_checks.map((call) => el("li", {text: call.step + (call.note ? " (" + call.note + ")" : "") +
          ": " + verdict(call.review).text}))));
      if (a.minor_findings.length) box.append(el("h3", {text: "Minor remarks (these do not block delivery)"}),
        findingsList(a.minor_findings));
    }
    if (run.can_resume) box.append(resumeControls(run));
    if ((run.status === "interrupted" || run.can_resume) && run.run_directory) {
      box.append(el("p", {}, run.can_resume ? "Or run this in a terminal:"
        : "To continue this run with its original budget and time limit, run this in a terminal:"),
        el("pre", {class: "command", text: 'victorique resume "' + run.run_directory + '"'}));
    }
    return box;
  }

  function proofSection(run, paired) {
    const delivered = run.proof_kind === "delivered";
    const download = el("a", {class: "button", href: fileUrl(run, "proof.md"), download: true,
      text: delivered ? "Download proof.md" : "Download the draft"});
    const content = [el("p", {class: "muted", text: delivered
      ? "Shown as plain text; LaTeX is not rendered."
      : "This draft did not pass both checks and was not delivered."}), download,
    el("pre", {class: "proof", text: run.proof})];
    if (delivered) {
      return el("section", {class: paired ? "proof-section paired" : "proof-section", "aria-labelledby": "proof-heading"},
        el("h2", {id: "proof-heading"}, "Delivered proof", cap("已交付证明")), ...content);
    }
    const draft = details("draft", "Best draft (not delivered)", "最佳草稿（未交付）", false, ...content);
    if (paired) draft.classList.add("paired");
    return draft;
  }

  // The tone (verdict-*), the marker shape (v-*), the label and its caption of a check or critique.
  function verdict(review) {
    if (review.type === "critique") {
      const count = review.findings.filter((item) => item.blocking).length;
      return count
        ? {cls: "warn", shape: "problems", text: count + " problem(s) reported", cap: "发现问题"}
        : {cls: "good", shape: "pass", text: "No problem reported", cap: "未发现问题"};
    }
    if (review.passed === true) return {cls: "good", shape: "pass", text: "Passed", cap: "通过"};
    if (review.passed === false) {
      if (review.outcome === "unknown") return {cls: "bad", shape: "unknown", text: "No clear verdict", cap: "无结论"};
      // Said pass with open questions, or fail with only minor remarks.
      if (review.undecided) return {cls: "warn", shape: "undecided", text: "Undecided", cap: "未决"};
      return {cls: "bad", shape: "fail", text: "Did not pass", cap: "未通过"};
    }
    return {cls: "muted", shape: "unread", text: "Could not be read", cap: "无法读取"};
  }

  function checksSection(run, paired) {
    const cards = run.checks.map((call) => {
      const review = call.review;
      const v = verdict(review);
      return el("article", {class: "check"},
        el("h3", {}, el("span", {class: "call-no", text: "#" + call.index}),
          (review.type === "critique" ? "Critique" : call.step) + (call.note ? " (" + call.note + ")" : ""), " ",
          el("span", {class: "badge verdict-" + v.cls + " v-" + v.shape}, v.text, cap(v.cap))),
        review.findings.length ? findingsList(review.findings) :
          el("p", {class: "muted", text: "No findings."}),
        review.questions.length ? [el("h4", {text: "Open questions"}),
          el("ul", {}, review.questions.map((item) => el("li", {text: item})))] : null);
    });
    return el("section", {class: paired ? "checks paired" : "checks", "aria-labelledby": "checks-heading"},
      el("h2", {id: "checks-heading"}, "Checks and critiques", cap("审核意见")),
      el("p", {class: "muted", text: run.minor_findings === "blocking"
        ? "Serious and fatal findings block delivery. This run counts a check that says pass with minor findings " +
          "as not passed, so those minor findings block delivery too."
        : "Serious and fatal findings block delivery; minor ones are recorded only."}),
      ...cards);
  }

  function findingsList(findings) {
    return el("ul", {class: "findings"}, findings.map((item) => {
      const severity = ["fatal", "material", "minor"].includes(item.severity) ? item.severity : "none";
      return el("li", {class: "sev-" + severity},
        el("span", {class: "badge severity-" + severity, text: item.severity_label}), " ",
        el("strong", {text: item.kind_label}), item.claim ? ": " + item.claim : "",
        item.quote ? el("blockquote", {text: item.quote}) : null);
    }));
  }

  // The class of a call's status tag; an unknown status is in progress while the run runs.
  function callStatus(status, running) {
    if (status === "unknown") return running ? "in-progress" : "unrecorded";
    if (status === "returned_invalid_model") return "returned_invalid";
    return String(status).replace(/[^\w-]/g, "_");
  }

  // Where each call falls on the run's time line, in seconds from the run's start.
  function timeline(run) {
    const origin = Date.parse(run.started_at);
    if (isNaN(origin)) return null;
    let total = run.finished ? (typeof run.wall_seconds === "number" ? run.wall_seconds : 0)
      : (Date.now() - origin) / 1000;
    const spans = run.calls.map((call) => {
      const start = Date.parse(call.started_at);
      if (isNaN(start)) return null;
      let end = Date.parse(call.ended_at);
      if (isNaN(end)) end = run.finished ? start : Math.max(start, Date.now());
      const span = {start: Math.max(0, (start - origin) / 1000), end: Math.max(0, (end - origin) / 1000)};
      total = Math.max(total, span.end);
      return span;
    });
    if (!(total > 0)) return null;
    return {total: total, step: timeStep(total, 5), spans: spans};
  }

  function callsTable(run) {
    const line = timeline(run);
    const running = !run.finished;
    const axis = el("span", {class: "axis", "aria-hidden": "true"});
    if (line) {
      for (let index = 0; index * line.step <= line.total * 0.95; index += 1) {
        const share = fraction(index * line.step / line.total);
        const text = shortDuration(index * line.step);
        // A label that would pass the right edge on a narrow screen ends at its tick instead.
        axis.append(el("span", {class: index && share > 1 - text.length * 0.0132 ? "end" : null, text: text,
          vars: {"--at": share}}));
      }
    }
    const rows = run.calls.map((call, position) => {
      const status = callStatus(call.status, running);
      // A call that timed out or was stopped by a stop request ended without an answer: outlined bar.
      const timedOut = call.status === "timed_out" || call.status === "stopped";
      const track = el("span", {class: "track"});
      const span = line ? line.spans[position] : null;
      if (span) {
        const start = fraction(span.start / line.total);
        track.append(el("span", {class: timedOut ? "seg out" : "seg",
          vars: {"--start": start, "--span": fraction(Math.min((span.end - span.start) / line.total, 1 - start))}}));
      }
      // A call made by the run's separate check service names that service.
      const service = run.check_separate && call.service === "checks" ? run.check_backend_label : null;
      const note = [call.note, service].filter(Boolean).join("; ");
      return el("tr", {class: timedOut ? "timed-out" : null},
        el("td", {class: "number idx", text: String(call.index).padStart(2, "0")}),
        el("td", {class: "step"}, call.step, note ? el("span", {class: "muted", text: " (" + note + ")"}) : null),
        el("td", {class: "status"}, el("span", {class: "badge call-status call-" + status}, call.status_label,
          cap(CALL_CAPTIONS[status]))),
        el("td", {class: "number dur", text: duration(call.seconds)}),
        el("td", {class: "tl"}, track),
        el("td", {class: "number tok"}, tokensCell(call.tokens)));
    });
    return el("div", {class: "table-wrap"}, el("table", {class: "calls",
      vars: line ? {"--tick": fraction(line.step / line.total)} : null},
      el("thead", {}, el("tr", {},
        el("th", {scope: "col", class: "idx number", text: "#"}),
        el("th", {scope: "col", class: "c-step", text: "Step"}),
        el("th", {scope: "col", class: "c-status", text: "Status"}),
        el("th", {scope: "col", class: "c-time number", text: "Time"}),
        el("th", {scope: "col", class: "tl-head"}, el("span", {class: "visually-hidden", text: "Timeline"}), axis),
        el("th", {scope: "col", class: "c-tok number", text: "Tokens"}))),
      el("tbody", {}, rows)));
  }

  function fileUrl(run, name) {
    return "/api/runs/" + encodeURIComponent(run.name) + "/files/" + encodeURIComponent(name);
  }

  function filesSection(run) {
    const descriptions = {
      "proof.md": "the delivered proof, or the best draft",
      "summary.json": "outcome, calls, tokens and time",
      "calls.jsonl": "one line per model call, with the check reports",
      "invocation.json": "the settings the run started with",
      "problem.md": "the statement as you typed it",
      "console.log": "the command's progress messages",
    };
    const items = run.files.map((file) => el("li", {},
      el("a", {href: fileUrl(run, file.name), download: true, text: file.name}),
      el("span", {class: "desc", text: descriptions[file.name] || ""}),
      el("span", {class: "size", text: size(file.size)})));
    if (run.archive_available) {
      items.push(el("li", {class: "zip"}, el("a", {href: "/api/runs/" + encodeURIComponent(run.name) + "/archive.zip",
        download: true, text: "All run files (zip)"}),
        el("span", {class: "desc", text: "the whole run folder"}), el("span", {class: "size"})));
    }
    return el("section", {class: "files-section", "aria-labelledby": "files-heading"},
      el("h2", {id: "files-heading"}, "Files", cap("文件")),
      items.length ? el("ul", {class: "files"}, items) : el("p", {class: "muted", text: "No files yet."}));
  }

  // ---- start ----------------------------------------------------------------

  async function start() {
    try {
      options = await api("/api/options");
    } catch (failure) {
      document.getElementById("main").prepend(el("p", {class: "box bad", text: failure.message}));
      return;
    }
    setupForm();
    window.addEventListener("hashchange", route);
    route();
  }

  start();
})();
