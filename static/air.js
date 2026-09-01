(() => {
  const q = (selector) => document.querySelector(selector);
  const fileInput = q("#file-input");
  const drop = q("#drop-zone");
  const selected = q("#selected-file");
  const run = q("#run");
  const error = q("#error");
  const notice = q("#notice");
  const jobsRoot = q("#jobs");
  const empty = q("#empty");
  const worker = q("#worker");
  let chosen = null;
  let refreshTimer = null;
  let countdownTimer = null;

  function makeId() {
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    return [...bytes].map((value) => value.toString(16).padStart(2, "0")).join("");
  }

  const clientId = localStorage.getItem("amex_air_client_id") || makeId();
  localStorage.setItem("amex_air_client_id", clientId);

  function message(element, text) {
    element.textContent = text || "";
    element.hidden = !text;
  }

  function choose(file) {
    message(error, "");
    message(notice, "");
    if (!file) {
      chosen = null;
      selected.textContent = "No file selected";
      run.disabled = true;
      return;
    }
    if (!/\.(xlsx|csv|txt)$/i.test(file.name)) {
      choose(null);
      message(error, "Choose an .xlsx, .csv, or .txt file.");
      return;
    }
    chosen = file;
    selected.textContent = file.name;
    run.disabled = false;
  }

  async function payload(response) {
    const text = await response.text();
    try { return JSON.parse(text); }
    catch { return { error: text || `HTTP ${response.status}` }; }
  }

  async function api(path, options = {}) {
    const headers = new Headers(options.headers || {});
    headers.set("X-AIR-Client-ID", clientId);
    const response = await fetch(path, { ...options, headers, cache: "no-store" });
    const data = await payload(response);
    if (!response.ok) throw new Error(data.error || `Server returned HTTP ${response.status}.`);
    return data;
  }

  function formatTime(value) {
    try { return new Date(value).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }); }
    catch { return value; }
  }

  function jobCard(job) {
    const card = document.createElement("article");
    card.className = "job";
    const top = document.createElement("div");
    top.className = "job-top";
    const identity = document.createElement("div");
    const name = document.createElement("h3");
    name.className = "job-name";
    name.textContent = job.filename;
    const time = document.createElement("p");
    time.className = "job-time";
    time.textContent = `${job.total} ${job.total === 1 ? "query" : "queries"} · ${formatTime(job.created_at)}`;
    identity.append(name, time);
    const state = document.createElement("span");
    state.className = `state ${job.state}`;
    state.textContent = job.state === "processing" ? "Processing" : job.state;
    top.append(identity, state);

    const detail = document.createElement("p");
    detail.className = "job-message";
    detail.textContent = job.error || job.message;
    card.append(top, detail);
    if (job.current_query) {
      const current = document.createElement("p");
      current.className = "job-query";
      current.textContent = `Current: ${job.current_query}`;
      card.append(current);
    }
    if (job.cooldown_until) {
      const cooldown = document.createElement("span");
      cooldown.className = "cooldown";
      cooldown.dataset.cooldownUntil = job.cooldown_until;
      card.append(cooldown);
    }

    const progress = document.createElement("div");
    progress.className = "progress-track";
    const fill = document.createElement("div");
    fill.style.width = `${job.total ? Math.round(job.completed / job.total * 100) : 0}%`;
    progress.append(fill);
    card.append(progress);

    const bottom = document.createElement("div");
    bottom.className = "job-bottom";
    const counts = document.createElement("span");
    counts.className = "counts";
    counts.textContent = ["completed", "cancelled", "paused", "timed_out", "failed"].includes(job.state) && job.download_url
      ? `${job.success_count} successful · ${job.failed_count} failed`
      : `${job.completed} of ${job.total} completed`;
    bottom.append(counts);

    const downloads = document.createElement("div");
    downloads.className = "downloads";
    for (const batch of job.batch_downloads || []) {
      const link = document.createElement("a");
      link.className = "action secondary";
      link.textContent = `Batch ${batch.batch_number}`;
      link.href = batch.download_url;
      downloads.append(link);
    }
    if (job.download_url) {
      const download = document.createElement("a");
      download.className = "action";
      download.textContent = job.state === "completed" ? "Download combined Excel" : "Download combined partial Excel";
      download.href = job.download_url;
      downloads.append(download);
    }
    if (downloads.children.length) {
      bottom.append(downloads);
    } else if (["queued", "processing"].includes(job.state)) {
      const cancel = document.createElement("button");
      cancel.className = "action cancel";
      cancel.textContent = job.cancel_requested ? "Stopping…" : "Cancel request";
      cancel.disabled = job.cancel_requested;
      cancel.dataset.cancel = job.run_id;
      bottom.append(cancel);
    }
    card.append(bottom);
    return card;
  }

  function render(data) {
    worker.className = `worker ${data.worker.online ? "online" : "offline"}`;
    worker.querySelector("strong").textContent = data.worker.online ? "Processing engine online" : "Processing engine offline";
    jobsRoot.replaceChildren(...data.jobs.map(jobCard));
    empty.hidden = data.jobs.length > 0;
    updateCountdowns();
  }

  function updateCountdowns() {
    document.querySelectorAll("[data-cooldown-until]").forEach((element) => {
      const remaining = Math.max(0, Math.ceil((new Date(element.dataset.cooldownUntil) - Date.now()) / 1000));
      const minutes = Math.floor(remaining / 60).toString().padStart(2, "0");
      const seconds = (remaining % 60).toString().padStart(2, "0");
      element.textContent = remaining ? `Cooldown ${minutes}:${seconds}` : "Cooldown ending⬦";
    });
  }

  async function loadJobs() {
    clearTimeout(refreshTimer);
    try {
      render(await api("/jobs"));
    } catch (caught) {
      message(error, `AMEX AIR could not refresh request status. Reason: ${caught.message}`);
    } finally {
      refreshTimer = setTimeout(loadJobs, 5000);
    }
  }

  fileInput.onchange = () => choose(fileInput.files[0]);
  ["dragenter", "dragover"].forEach((name) => drop.addEventListener(name, (event) => {
    event.preventDefault();
    drop.classList.add("drag");
  }));
  ["dragleave", "drop"].forEach((name) => drop.addEventListener(name, (event) => {
    event.preventDefault();
    drop.classList.remove("drag");
  }));
  drop.addEventListener("drop", (event) => choose(event.dataTransfer.files[0]));

  run.onclick = async () => {
    if (!chosen) return;
    run.disabled = true;
    run.textContent = "Queueing request…";
    message(error, "");
    message(notice, "");
    const form = new FormData();
    form.append("file", chosen);
    try {
      const data = await api("/jobs", { method: "POST", body: form });
      message(notice, data.job.state === "queued"
        ? "Request queued. It will start automatically when the laptop processing engine is online."
        : "Request accepted.");
      fileInput.value = "";
      choose(null);
      await loadJobs();
    } catch (caught) {
      message(error, `AMEX AIR could not queue the request. Reason: ${caught.message}`);
      run.disabled = !chosen;
    } finally {
      run.textContent = "Queue AMEX AIR request";
    }
  };

  jobsRoot.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-cancel]");
    if (!button) return;
    button.disabled = true;
    button.textContent = "Stopping…";
    try {
      await api(`/jobs/${button.dataset.cancel}/cancel`, { method: "POST" });
      await loadJobs();
    } catch (caught) {
      message(error, `AMEX AIR could not cancel the request. Reason: ${caught.message}`);
      button.disabled = false;
      button.textContent = "Cancel request";
    }
  });

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) loadJobs();
  });
  countdownTimer = setInterval(updateCountdowns, 1000);
  loadJobs();
})();
