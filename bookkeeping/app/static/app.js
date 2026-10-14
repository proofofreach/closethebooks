// Progressive enhancement for the Books app. Every form also works without it,
// except Ask, which needs streaming.
(() => {
  const token = document.querySelector('meta[name="books-token"]')?.content || "";

  // Copy buttons
  document.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-copy]");
    if (!button) return;
    const original = button.textContent;
    try {
      await navigator.clipboard.writeText(button.dataset.copy);
      button.textContent = "Copied";
    } catch {
      button.textContent = "Select and copy";
    }
    setTimeout(() => { button.textContent = original; }, 1600);
  });

  // Disclosure buttons
  document.querySelectorAll("[data-disclosure]").forEach((button) => {
    button.addEventListener("click", () => {
      const target = document.getElementById(button.getAttribute("aria-controls"));
      const open = button.getAttribute("aria-expanded") === "true";
      button.setAttribute("aria-expanded", String(!open));
      if (target) target.hidden = open;
    });
  });

  // Statement dropzones: drag a file or choose one; upload starts immediately.
  document.querySelectorAll("[data-dropzone]").forEach((zone) => {
    const input = zone.querySelector('input[type="file"]');
    const status = zone.querySelector(".dropzone-status");
    const accepted = (input.accept || "").split(",").map((value) => value.trim().toLowerCase()).filter(Boolean);
    const say = (text, tone = "") => { status.textContent = text; status.className = `dropzone-status ${tone}`; };

    const send = async (file) => {
      const extension = `.${file.name.split(".").pop().toLowerCase()}`;
      if (accepted.length && !accepted.includes(extension)) {
        say(`That file type is not accepted for this account. Use ${accepted.join(", ").toUpperCase()}.`, "error");
        return;
      }
      const data = new FormData(zone);
      data.set("statement", file);
      zone.classList.add("is-busy");
      say(`Uploading ${file.name}…`);
      try {
        const response = await fetch(zone.action, { method: "POST", body: data, headers: { "X-Books-Token": token } });
        const result = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(result.error || "The upload failed. Try again.");
        zone.classList.add("is-done");
        say(`Saved ${result.filename} to the inbox. Next: prepare proposals on the Import page.`, "success");
      } catch (error) {
        say(error.message, "error");
      } finally {
        zone.classList.remove("is-busy");
        input.value = "";
      }
    };

    input.addEventListener("change", () => input.files[0] && send(input.files[0]));
    ["dragenter", "dragover"].forEach((name) => zone.addEventListener(name, (event) => {
      event.preventDefault();
      zone.classList.add("is-dragging");
    }));
    ["dragleave", "drop"].forEach((name) => zone.addEventListener(name, (event) => {
      event.preventDefault();
      zone.classList.remove("is-dragging");
    }));
    zone.addEventListener("drop", (event) => {
      const file = event.dataTransfer?.files?.[0];
      if (file) send(file);
    });
  });

  // Import form: the file picker follows the chosen account's formats.
  document.querySelectorAll("[data-accept-map]").forEach((select) => {
    const map = JSON.parse(select.dataset.acceptMap);
    const input = select.form.querySelector('input[type="file"]');
    const update = () => { input.accept = map[select.value] || ""; };
    select.addEventListener("change", update);
    update();
  });

  // Long-running forms show progress and cannot be submitted twice.
  document.querySelectorAll("[data-busy-form]").forEach((form) => {
    form.addEventListener("submit", () => {
      const button = form.querySelector("[data-busy-label]");
      if (!button) return;
      button.textContent = button.dataset.busyLabel;
      button.setAttribute("aria-disabled", "true");
      setTimeout(() => { button.disabled = true; }, 0);
    });
  });

  // Invoice line items and running total.
  document.querySelectorAll("[data-invoice-form]").forEach((form) => {
    const list = form.querySelector("[data-line-items]");
    const total = form.querySelector("[data-invoice-total]");
    const renumber = () => {
      list.querySelectorAll(".line-item").forEach((row, index) => {
        row.querySelector('[name="item"]').setAttribute("aria-label", `Description, line ${index + 1}`);
        row.querySelector('[name="amount"]').setAttribute("aria-label", `Amount, line ${index + 1}`);
        row.querySelector("[data-remove-line]").setAttribute("aria-label", `Remove line ${index + 1}`);
      });
    };
    const recalc = () => {
      let sum = 0;
      let valid = true;
      list.querySelectorAll('[name="amount"]').forEach((input) => {
        const value = input.value.replace(/,/g, "").trim();
        if (!value) return;
        const number = Number(value);
        if (Number.isFinite(number)) sum += number; else valid = false;
      });
      total.textContent = valid ? sum.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : "Check amounts";
    };
    form.querySelector("[data-add-line]").addEventListener("click", () => {
      const row = list.querySelector(".line-item").cloneNode(true);
      row.querySelectorAll("input").forEach((input) => { input.value = ""; });
      list.append(row);
      renumber();
      row.querySelector('[name="item"]').focus();
    });
    list.addEventListener("click", (event) => {
      const button = event.target.closest("[data-remove-line]");
      if (!button) return;
      const rows = list.querySelectorAll(".line-item");
      if (rows.length === 1) {
        rows[0].querySelectorAll("input").forEach((input) => { input.value = ""; });
      } else {
        button.closest(".line-item").remove();
      }
      renumber();
      recalc();
    });
    list.addEventListener("input", recalc);
    recalc();
  });

  // Cash chart crosshair and tooltip.
  document.querySelectorAll("[data-chart]").forEach((figure) => {
    const svg = figure.querySelector("svg");
    const points = JSON.parse(figure.querySelector("[data-chart-points]").textContent);
    const crosshair = svg.querySelector("[data-crosshair]");
    const dot = svg.querySelector("[data-hover-dot]");
    const tooltip = document.createElement("div");
    tooltip.className = "chart-tooltip";
    tooltip.hidden = true;
    const value = document.createElement("strong");
    const month = document.createElement("span");
    tooltip.append(value, month);
    figure.append(tooltip);
    const viewBox = svg.viewBox.baseVal;

    const show = (clientX) => {
      const box = svg.getBoundingClientRect();
      const x = ((clientX - box.left) / box.width) * viewBox.width;
      let nearest = points[0];
      for (const point of points) if (Math.abs(point.x - x) < Math.abs(nearest.x - x)) nearest = point;
      crosshair.setAttribute("x1", nearest.x);
      crosshair.setAttribute("x2", nearest.x);
      dot.setAttribute("cx", nearest.x);
      dot.setAttribute("cy", nearest.y);
      crosshair.hidden = dot.hidden = tooltip.hidden = false;
      value.textContent = nearest.value;
      month.textContent = nearest.month;
      const scale = box.width / viewBox.width;
      tooltip.style.left = `${svg.offsetLeft + nearest.x * scale}px`;
      tooltip.style.top = `${svg.offsetTop + nearest.y * scale}px`;
    };
    const hide = () => { crosshair.hidden = dot.hidden = tooltip.hidden = true; };
    svg.addEventListener("pointermove", (event) => show(event.clientX));
    svg.addEventListener("pointerleave", hide);
  });

  // Xero backup progress: poll while the background job runs.
  const backup = document.querySelector("[data-backup-status]");
  if (backup && backup.dataset.state === "running") {
    const message = backup.querySelector(".backup-message");
    const progress = backup.querySelector(".backup-progress");
    const poll = async () => {
      try {
        const response = await fetch("/migrate/status", { headers: { Accept: "application/json" } });
        const data = await response.json();
        message.textContent = data.message;
        const phase = data.progress.phase;
        const meter = backup.querySelector("[data-meter]");
        if (meter && phase) {
          meter.hidden = false;
          meter.querySelector("[data-meter-label]").textContent = `Step ${phase.step} of 3 · ${phase.label}`;
          meter.querySelector("[data-meter-count]").textContent = `${phase.done.toLocaleString()} of ${phase.total.toLocaleString()} ${phase.unit}`;
          const bar = meter.querySelector("[data-meter-bar]");
          bar.max = phase.total || 1;
          bar.value = phase.done;
          meter.querySelectorAll(".meter-steps li").forEach((item, index) => {
            item.className = index + 1 < phase.step ? "done" : index + 1 === phase.step ? "now" : "";
          });
        }
        if (progress && data.progress.entities !== undefined) {
          progress.textContent = `${data.progress.entities.toLocaleString()} records · ${data.progress.attachments_downloaded.toLocaleString()} of ${data.progress.attachments_queued.toLocaleString()} attachments`;
        }
        if (data.state === "running") setTimeout(poll, 3000); else location.reload();
      } catch { setTimeout(poll, 10000); }
    };
    setTimeout(poll, 3000);
  }

  // Ask: streamed, read-only answers.
  const askForm = document.querySelector("[data-ask-form]");
  if (askForm) {
    const transcript = document.querySelector("[data-transcript]");
    const starters = document.querySelector("[data-starters]");
    const textarea = askForm.querySelector("textarea");
    const send = askForm.querySelector("[data-send]");
    const stop = askForm.querySelector("[data-stop]");
    const clear = document.querySelector("[data-clear-chat]");
    const storageKey = `books-ask:${location.host}`;
    let history = [];
    try { history = JSON.parse(sessionStorage.getItem(storageKey) || "[]"); } catch { history = []; }
    let controller = null;

    const save = () => sessionStorage.setItem(storageKey, JSON.stringify(history.slice(-20)));
    const nearBottom = () => window.innerHeight + window.scrollY >= document.body.scrollHeight - 160;

    const addTurn = (role, text) => {
      const turn = document.createElement("article");
      turn.className = `turn ${role}`;
      const who = document.createElement("p");
      who.className = "who";
      who.textContent = role === "user" ? "You" : "Books";
      const body = document.createElement("div");
      body.className = "text";
      body.textContent = text;
      turn.append(who, body);
      transcript.append(turn);
      starters.hidden = true;
      clear.hidden = false;
      return turn;
    };
    history.forEach((turn) => addTurn(turn.role, turn.text));

    const setBusy = (busy) => {
      send.disabled = busy;
      textarea.disabled = busy;
      stop.hidden = !busy;
    };

    const ask = async (question) => {
      const follow = nearBottom();
      addTurn("user", question);
      const turn = addTurn("assistant", "");
      const body = turn.querySelector(".text");
      const progress = document.createElement("p");
      progress.className = "progress";
      progress.setAttribute("role", "status");
      progress.textContent = "Working…";
      turn.append(progress);
      if (follow) turn.scrollIntoView({ block: "end" });
      setBusy(true);
      controller = new AbortController();
      let answer = "";
      let finished = false;
      try {
        const response = await fetch("/ask", {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-Books-Token": token },
          body: JSON.stringify({ question, history }),
          signal: controller.signal,
        });
        if (!response.ok) {
          const result = await response.json().catch(() => ({}));
          throw new Error(result.error || "The question could not be sent.");
        }
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          let index;
          while ((index = buffer.indexOf("\n")) >= 0) {
            const line = buffer.slice(0, index).trim();
            buffer = buffer.slice(index + 1);
            if (!line) continue;
            const message = JSON.parse(line);
            if (message.type === "status") progress.textContent = message.text;
            if (message.type === "delta") { answer += message.text; body.textContent = answer; }
            if (message.type === "final") {
              answer = message.text || answer;
              body.textContent = answer;
              progress.textContent = `Answered by ${message.engine === "codex" ? "Codex" : "Claude"} from the ledger.`;
              finished = true;
            }
            if (message.type === "error") throw new Error(message.text);
          }
        }
        if (!finished) throw new Error("The answer stopped early. Try again.");
        history.push({ role: "user", text: question }, { role: "assistant", text: answer });
        save();
      } catch (error) {
        turn.classList.add("failed");
        const stopped = error.name === "AbortError";
        progress.textContent = stopped ? "Stopped. Nothing in the books changed." : error.message;
        if (!stopped && !answer) body.textContent = "No answer.";
        const retry = document.createElement("button");
        retry.type = "button";
        retry.className = "button small";
        retry.textContent = "Try again";
        retry.addEventListener("click", () => { turn.previousElementSibling?.remove(); turn.remove(); ask(question); });
        turn.append(retry);
      } finally {
        controller = null;
        setBusy(false);
        textarea.focus();
      }
    };

    askForm.addEventListener("submit", (event) => {
      event.preventDefault();
      const question = textarea.value.trim();
      if (!question || controller) return;
      textarea.value = "";
      ask(question);
    });
    textarea.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        askForm.requestSubmit();
      }
    });
    stop.addEventListener("click", () => controller?.abort());
    starters.addEventListener("click", (event) => {
      const button = event.target.closest("[data-starter]");
      if (!button || controller) return;
      ask(button.dataset.starter);
    });
    clear.addEventListener("click", () => {
      if (!window.confirm("Start a new conversation? The current one will be cleared from this tab.")) return;
      history = [];
      save();
      transcript.replaceChildren();
      starters.hidden = false;
      clear.hidden = true;
      textarea.focus();
    });
  }
})();
