const TOKEN = document.querySelector('meta[name="azp-token"]').content;
const state = { installs: [], base: "", target: "", defaults: {}, lastPlan: null, channel: null, jobId: "", jobKind: "", timer: null };

const $ = (id) => document.getElementById(id);

function log(text) {
  const el = $("log");
  const line = `[${new Date().toLocaleTimeString()}] ${text}`;
  el.textContent = el.textContent ? `${el.textContent}\n${line}` : line;
  el.scrollTop = el.scrollHeight;
}

async function call(path, payload) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Azp-Token": TOKEN },
    body: JSON.stringify(payload || {}),
  });
  const data = await res.json();
  if (!data.ok) throw new Error(data.error || `请求失败：${res.status}`);
  return data;
}

function gib(n) {
  return `${(Number(n || 0) / 2 ** 30).toFixed(2)} GiB`;
}

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function renderCards() {
  const box = $("cards");
  box.innerHTML = "";
  if (!state.installs.length) {
    box.innerHTML = '<p class="empty">没有找到启动器。先安装任意一个区的官方启动器，装完点「重新检测」。'
      + '装在非标准位置时，用命令行 python -m azp check --launcher "launcher.exe 路径" 指定。</p>';
    return;
  }
  for (const item of state.installs) {
    const group = item.key === "bili" ? "cn" : item.key;
    const active = group === "cn"
      ? state.base === "cn" || state.base === "bili"
      : state.base === item.key;
    const card = document.createElement("article");
    card.className = "card" + (active ? " active" : "");
    const status = item.installed ? "游戏已安装" : "未安装";
    const installer = !item.installed && item.installer
      ? `<button class="ghost">打开启动器安装包</button>`
      : "";
    const home = !item.installed && !item.installer && item.homepage
      ? `<button class="ghost">打开官网安装</button>`
      : "";
    const currentChannel = state.channel ? state.channel.current : null;
    const variants = (item.variants || []).map((v) => (
      `<button class="seg${currentChannel === v.key ? " on" : ""}" data-key="${v.key}">${v.label}</button>`
    )).join("");
    let channelHint = "";
    if (item.variants && item.variants.length && state.channel) {
      const src = state.channel.sources || {};
      channelHint = `<p class="hint">当前：${escapeHtml(state.channel.current_label || "未知")}`
        + ` · 官服源 ${src.official ? "就绪" : "缺失"}`
        + ` · B服源 ${src.bilibili ? "就绪" : "缺失"}</p>`;
    }
    card.innerHTML = `
      <div class="card-head">
        <strong>${escapeHtml(item.label)}</strong>
        <span class="pill ${item.installed ? "ok" : "warn"}">${status}</span>
      </div>
      ${variants ? `<div class="segmented">${variants}</div>` : ""}
      <p class="path">${escapeHtml(item.game || item.root || "还没有安装启动器")}</p>
      ${channelHint}
      <div class="card-foot">
        <button class="ghost" ${item.installed ? "" : "disabled"}>用作源区</button>
        ${installer}${home}
      </div>`;
    card.querySelector(".card-foot button").addEventListener("click", () => {
      if (!item.installed) return;
      state.base = item.key;
      renderCards();
      renderTargets();
      log(`源区：${item.label}`);
    });
    const actionBtn = card.querySelectorAll(".card-foot button")[1];
    if (actionBtn) {
      actionBtn.addEventListener("click", () => {
        if (item.installer) openInstaller(item.installer);
        else openHomepage(item.homepage);
      });
    }
    card.querySelectorAll(".seg").forEach((btn) => {
      btn.addEventListener("click", () => switchChannel(btn.dataset.key));
    });
    box.appendChild(card);
  }
}

async function loadChannelInfo() {
  const item = state.installs.find(
    (x) => x.installed && (x.key === "cn" || x.key === "bili") && x.game);
  if (!item) {
    state.channel = null;
    return;
  }
  try {
    state.channel = await call("/api/channel-info", { target: item.game });
  } catch (err) {
    state.channel = null;
    log(`读取渠道状态失败：${err.message}`);
  }
}

async function switchChannel(to) {
  const info = state.channel;
  if (!info || !info.current) {
    log("没有识别到当前渠道，无法切换。");
    return;
  }
  if (info.current === to) return;
  const labels = info.labels || {};
  if (!info.sources || !info.sources[to]) {
    if (to === "bilibili") {
      $("maintHint").innerHTML =
        '还没有哔哩哔哩服的渠道源。需要先安装 B 服启动器：'
        + '<a href="https://www.biligame.com/detail/?id=111392" target="_blank" rel="noopener">'
        + '下载哔哩哔哩服启动器</a>，装好后在 B 服客户端点「提取当前渠道源」。';
      log("缺少 B 服渠道源：先安装 B 服启动器，装好后提取一次渠道源。");
    } else {
      log(`还没有${labels[to] || to}的渠道源。先在同渠道客户端里提取一次，或把源放进 out/cn_sources。`);
    }
    return;
  }
  const text = `切换渠道：${labels[info.current] || info.current} → ${labels[to] || to}\n\n`
    + "会替换客户端里的渠道配置与登录组件，游戏必须已关闭。继续？";
  if (!confirm(text)) return;
  runMaint("channel-switch", "切换渠道", "切换前会备份当前渠道文件。",
    { from: info.current, to });
}

function renderTargets() {
  const select = $("target");
  select.innerHTML = "";
  const baseGroup = state.base === "bili" ? "cn" : state.base;
  for (const item of state.installs) {
    if (!item.key || item.key === "bili"
        || item.key === baseGroup || item.kind === "unknown") continue;
    const option = document.createElement("option");
    option.value = item.key;
    option.textContent = item.installed
      ? `${item.label}（已安装，只补缺失）`
      : `${item.label}（需先安装启动器）`;
    select.appendChild(option);
  }
  if (!select.options.length) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "没有其他可选区服";
    select.appendChild(option);
  }
  state.target = select.value;
  updateTargetHint();
  select.onchange = () => {
    state.target = select.value;
    updateTargetHint();
  };
}

function updateTargetHint() {
  const dir = state.defaults[state.target] || "";
  const installed = state.installs.find((x) => x.key === state.target);
  $("targetDir").placeholder = dir || "留空则使用源区同盘的默认目录";
  if (installed && !installed.installed) {
    $("targetHint").textContent =
      `${installed.label}还没有安装启动器。先从官网下载安装，装完点「重新检测」。`;
    $("calc").disabled = true;
    return;
  }
  $("calc").disabled = false;
  $("targetHint").textContent = installed && installed.installed
    ? `目标区已有客户端：${installed.game}。已有且大小一致的文件不会重新下载。`
    : "目标区还没有游戏目录，将按源区复用文件，只下载差异部分。";
}

async function detect() {
  $("conn").textContent = "正在检测…";
  $("conn").className = "badge";
  try {
    const data = await call("/api/detect");
    state.installs = data.installs || [];
    state.defaults = data.defaults || {};
    if (!state.base) {
      const first = state.installs.find((x) => x.installed);
      state.base = first ? first.key : "";
    }
    await loadChannelInfo();
    renderCards();
    renderTargets();
    renderMaintDirs();
    refreshRegions();
    $("conn").textContent = `已找到 ${state.installs.length} 个客户端`;
    $("conn").className = "badge ok";
    log(`检测完成：${state.installs.map((x) => x.label).join("、") || "无"}`);
  } catch (err) {
    $("conn").textContent = "检测失败";
    $("conn").className = "badge fail";
    log(`检测失败：${err.message}`);
  }
}

function renderStats(stats) {
  const items = [
    ["硬链接", gib(stats.link_bytes), `${stats.link_files.toLocaleString()} 个文件`],
    ["复用", gib(stats.reuse_bytes), `${stats.reuse_files.toLocaleString()} 个文件`],
    ["已就绪", gib(stats.ready_bytes), `${stats.ready_files.toLocaleString()} 个文件`],
    ["需下载", gib(stats.download_bytes), `${stats.download_files.toLocaleString()} 个文件`],
    ["新增占用", gib(stats.download_bytes), `完整安装 ${gib(stats.total_bytes)}`],
  ];
  $("stats").innerHTML = items.map(([name, value, note]) => `
    <div class="stat">
      <span>${name}</span>
      <strong>${value}</strong>
      <em>${note}</em>
    </div>`).join("");
}

function showPlan(data) {
  $("planPanel").hidden = false;
  $("envPanel").hidden = false;
  $("paths").textContent = `源区 ${data.base.game} → 目标区 ${data.target.dir}`;
  renderStats(data.stats);
  $("planBadge").textContent = data.env_ok ? "环境检查通过" : "有致命项";
  $("planBadge").className = `badge ${data.env_ok ? "ok" : "fail"}`;
  $("notes").innerHTML = (data.notes || [])
    .map((n) => `<li>${escapeHtml(n)}</li>`).join("");
  $("env").innerHTML = (data.env || []).map((item) => `
    <li class="${item.status}">
      <span class="env-name">${escapeHtml(item.name)}</span>
      <span class="env-detail">${escapeHtml(item.detail || item.status)}</span>
    </li>`).join("");
  state.lastPlan = data;
  $("run").disabled = !data.env_ok;
  renderMaintDirs();
  $("maintDir").value = data.target.dir;
  $("maintBadge").textContent = data.target.label;
  $("maintBadge").className = "badge ok";
  $("calc").disabled = false;
  $("calc").textContent = "计算计划";
  log(`计划完成：需下载 ${gib(data.stats.download_bytes)}，新增占用 ${gib(data.need_bytes)}`);
}

async function calc() {
  const target = $("target").value;
  if (!state.base || !target) {
    log("先选择一个源区和一个目标区。");
    return;
  }
  $("calc").disabled = true;
  $("calc").textContent = "计算中…";
  $("progressBox").hidden = false;
  $("bar").style.width = "0%";
  $("jobMsg").textContent = "[计算计划] 读取清单并比对…";
  state.jobKind = "plan";
  try {
    const r = await call("/api/run", {
      action: "plan",
      base: state.base,
      target,
      target_dir: $("targetDir").value.trim(),
      out: "out",
    });
    state.jobId = r.id;
    log("已启动计划计算。日服首次核对可能需要几分钟。");
    pollJob();
  } catch (err) {
    log(`计算失败：${err.message}`);
    $("calc").disabled = false;
    $("calc").textContent = "计算计划";
  }
}

async function runJob() {
  const p = state.lastPlan;
  if (!p) {
    log("先计算计划。");
    return;
  }
  const lines = [
    `目标目录：${p.target.dir}`,
    `硬链接 ${p.stats.link_files.toLocaleString()} 个`,
    `已就绪 ${p.stats.ready_files.toLocaleString()} 个`,
    `下载 ${p.stats.download_files.toLocaleString()} 个 / ${gib(p.stats.download_bytes)}`,
  ];
  if (!confirm(`将向目标目录写入文件：\n\n${lines.join("\n")}\n\n继续？`)) return;
  state.jobKind = "switch";
  $("run").disabled = true;
  $("cancel").disabled = false;
  $("progressBox").hidden = false;
  $("bar").style.width = "0%";
  $("jobMsg").textContent = "正在启动…";
  try {
    const r = await call("/api/run", {
      base: state.base,
      target: $("target").value,
      target_dir: $("targetDir").value.trim(),
      out: "out",
    });
    state.jobId = r.id;
    log(`任务已启动：${r.id}`);
    pollJob();
  } catch (err) {
    log(`启动失败：${err.message}`);
    $("run").disabled = false;
    $("cancel").disabled = true;
  }
}

async function pollJob() {
  if (!state.jobId) return;
  try {
    const s = await call("/api/job", { id: state.jobId });
    const pct = s.total_files ? Math.min(100, (s.done_files / s.total_files) * 100) : 0;
    $("bar").style.width = `${pct.toFixed(1)}%`;
    $("jobMsg").textContent = `[${s.phase}] ${s.message || ""}`;
    $("log").textContent = s.logs.join("\n");
    $("log").scrollTop = $("log").scrollHeight;
    if (s.status === "running") {
      state.timer = setTimeout(pollJob, 600);
      return;
    }
    $("run").disabled = false;
    $("cancel").disabled = true;
    ["btnNofadeInstall", "btnNofadeRestore", "btnVerify", "btnVerifyFull",
     "btnRepair", "btnRepairFull", "btnRollback", "btnRemovePreview", "btnRemove",
     "btnJournalClean", "btnManifests", "btnChannelExtract"]
      .forEach((id) => { $(id).disabled = false; });
    refreshNofadeState();
    if (state.jobKind === "maint") {
      loadChannelInfo().then(renderCards);
    }
    if (s.status === "done") {
      if (state.jobKind === "plan" && s.result && s.result.plan) {
        showPlan(s.result.plan);
      } else if (state.jobKind === "maint" && s.result && s.result.rollback) {
        const r = s.result.rollback;
        const verb = r.dry ? "预览" : "回滚";
        $("journalHint").textContent =
          `${verb}：删除 ${r.deleted}，还原 ${r.restored}，保留 ${r.kept}`;
        log(`${verb}完成：删除 ${r.deleted}，还原 ${r.restored}，保留 ${r.kept}`);
        if (!r.dry) refreshJournals();
      } else if (state.jobKind === "maint" && s.result && s.result.verify) {
        const v = s.result.verify;
        const tag = v.full ? "全量校验" : "抽检";
        $("maintHint").textContent =
          `${tag}：缺失 ${v.missing}，大小不符 ${v.wrong}，校验不符 ${v.bad}，已核对 ${v.checked}`;
        log(`${tag}完成：缺失 ${v.missing}，大小不符 ${v.wrong}，校验不符 ${v.bad}`);
      } else if (state.jobKind === "maint" && s.result && s.result.repair) {
        const r = s.result.repair;
        $("maintHint").textContent =
          `修复：重建链接 ${r.linked}，下载 ${r.downloaded}，失败 ${r.failed}`;
        log(`修复完成：重建链接 ${r.linked}，下载 ${r.downloaded}，失败 ${r.failed}`);
      } else if (state.jobKind === "maint" && s.result && s.result.journal_clean) {
        const c = s.result.journal_clean;
        $("journalHint").textContent = `${c.dry ? "将删除" : "已删除"} ${c.removed} 条失效账本`;
        log(`失效账本：${c.dry ? "将删除" : "已删除"} ${c.removed} 条`);
        if (!c.dry) refreshJournals();
      } else if (state.jobKind === "maint" && s.result && s.result.remove) {
        const r = s.result.remove;
        $("maintHint").textContent = r.dry ? "删除预览完成，未改动文件。" : "已移入回收站。";
        log(r.dry ? "删除预览完成。" : "已移入回收站。");
      } else if (state.jobKind === "maint" && s.result && s.result.manifests) {
        $("maintHint").textContent = "清单已重抓，建议重新计算计划。";
        log("清单已重抓，建议重新计算计划。");
      } else {
        $("jobMsg").textContent = "完成";
        log("执行完成。");
      }
    } else {
      $("jobMsg").textContent = s.error || s.phase;
      log(`任务结束：${s.error || s.status}`);
    }
    state.jobKind = "";
    state.jobId = "";
  } catch (err) {
    log(`读取进度失败：${err.message}`);
  }
}

async function cancelJob() {
  if (!state.jobId) return;
  try {
    await call("/api/cancel", { id: state.jobId });
    log("已请求取消，当前阶段结束后停止。");
  } catch (err) {
    log(`取消失败：${err.message}`);
  }
}

async function openHomepage(url) {
  if (!url) return;
  try {
    await call("/api/open-url", { url });
    log(`已打开官网：${url}`);
  } catch (err) {
    log(`打开官网失败：${err.message}`);
  }
}

async function openInstaller(path) {
  if (!path) return;
  try {
    await call("/api/open-installer", { path });
    log("已启动安装包，装完点「重新检测」。");
  } catch (err) {
    log(`打开安装包失败：${err.message}`);
  }
}

function renderMaintDirs() {
  const select = $("maintDir");
  const items = [];
  for (const item of state.installs) {
    if (item.installed && item.game) items.push([item.game, `${item.label}  ${item.game}`]);
  }
  if (state.lastPlan && state.lastPlan.target.dir) {
    items.push([state.lastPlan.target.dir, `目标区  ${state.lastPlan.target.dir}`]);
  }
  const current = select.value;
  select.innerHTML = "";
  if (!items.length) {
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "没有可用目录";
    select.appendChild(opt);
    return;
  }
  for (const [value, text] of items) {
    const opt = document.createElement("option");
    opt.value = value;
    opt.textContent = text;
    select.appendChild(opt);
  }
  if (items.some(([value]) => value === current)) select.value = current;
  select.onchange = () => { refreshJournals(); refreshNofadeState(); };
  refreshJournals();
  refreshNofadeState();
}

function maintTarget() {
  return $("maintDir").value || (state.lastPlan ? state.lastPlan.target.dir : "");
}

async function nofadeStatus() {
  const dir = maintTarget();
  if (!dir) {
    log("先指定维护目录。");
    return;
  }
  try {
    const r = await call("/api/nofade", { target: dir });
    const map = {
      patched: "已安装补丁", original: "官方原版", missing: "文件不存在",
      unsupported: "仅支持国服", unknown: "无法判断",
    };
    const text = map[r.state] || r.state;
    $("maintHint").textContent = `补丁状态：${text}（${dir}）`;
    log(`补丁状态：${text}`);
  } catch (err) {
    log(`查询失败：${err.message}`);
  }
}

function setMaintDisabled(disabled) {
  ["btnNofadeInstall", "btnNofadeRestore", "btnVerify", "btnVerifyFull",
   "btnRepair", "btnRepairFull", "btnRollback", "btnRemovePreview", "btnRemove",
   "btnJournalClean", "btnManifests"]
    .forEach((id) => { $(id).disabled = disabled; });
}

async function refreshRegions() {
  try {
    const data = await call("/api/regions");
    const list = $("regionList");
    list.innerHTML = (data.regions || []).map((r) => `
      <li class="ok">
        <span class="env-name">${escapeHtml(r.key)} · ${escapeHtml(r.label)}</span>
        <span class="env-detail">${escapeHtml(r.app_id || "—")} / ${escapeHtml(r.folder || "—")}</span>
      </li>`).join("");
  } catch (err) {
    log(`读取区服表失败：${err.message}`);
  }
}

async function refreshNofadeState() {
  const installBtn = $("btnNofadeInstall");
  const restoreBtn = $("btnNofadeRestore");
  const dir = maintTarget();
  if (!dir) {
    installBtn.disabled = true;
    restoreBtn.disabled = true;
    return;
  }
  try {
    const s = await call("/api/nofade", { target: dir });
    if (s.state === "unsupported") {
      installBtn.disabled = true;
      restoreBtn.disabled = true;
      $("maintHint").textContent = "反虚化只支持国服客户端（AzurPromilia_Game）。";
    } else if (s.state === "patched") {
      installBtn.disabled = true;
      restoreBtn.disabled = false;
      $("maintHint").textContent = "已安装反虚化补丁；要恢复官方文件点「还原官方文件」。";
    } else if (s.state === "original") {
      installBtn.disabled = false;
      restoreBtn.disabled = true;
      $("maintHint").textContent = "当前是官方原版，可以安装补丁。";
    } else if (s.state === "missing") {
      installBtn.disabled = true;
      restoreBtn.disabled = true;
      $("maintHint").textContent = "这个目录里没有对应的资源文件，可能客户端未下载完整。";
    } else {
      installBtn.disabled = false;
      restoreBtn.disabled = true;
      $("maintHint").textContent = "补丁状态无法判断；安装前会再校验一次。";
    }
  } catch (err) {
    $("maintHint").textContent = `查询补丁状态失败：${err.message}`;
  }
}

function journalSelected() {
  return $("journalList").value || "";
}

async function refreshJournals() {
  const select = $("journalList");
  const dir = maintTarget();
  if (!dir) {
    select.innerHTML = '<option value="">没有可用目录</option>';
    $("journalHint").textContent = "先选择维护目录。";
    return;
  }
  try {
    const data = await call("/api/journals", { target: dir });
    const entries = data.entries || [];
    const current = select.value;
    select.innerHTML = "";
    if (!entries.length) {
      const opt = document.createElement("option");
      opt.value = "";
      opt.textContent = "这个目录没有账本";
      select.appendChild(opt);
      $("journalHint").textContent = "没有账本：本工具还没有向这个目录写过文件。";
      return;
    }
    for (const item of entries) {
      const opt = document.createElement("option");
      opt.value = item.id;
      const when = new Date(item.time * 1000).toLocaleString();
      opt.textContent = `${when} · ${item.cmd} · 新建 ${item.created_files} · 备份 ${item.backup_files}`;
      select.appendChild(opt);
    }
    if (entries.some((x) => x.id === current)) select.value = current;
    $("journalHint").textContent = `共 ${entries.length} 条账本，可先预览再回滚。`;
  } catch (err) {
    $("journalHint").textContent = `读取账本失败：${err.message}`;
  }
}

async function runManifests() {
  const base = state.base;
  const target = $("target").value;
  if (!base || !target) {
    log("先选择源区和目标区。");
    return;
  }
  if (!confirm(`重新抓取 ${base} 与 ${target} 的清单？会重新查询版本并下载清单。`)) return;
  state.jobKind = "maint";
  setMaintDisabled(true);
  $("progressBox").hidden = false;
  $("bar").style.width = "0%";
  $("jobMsg").textContent = "[重抓清单] 正在启动…";
  try {
    const r = await call("/api/run", { action: "manifests", keys: [base, target], out: "out" });
    state.jobId = r.id;
    pollJob();
  } catch (err) {
    log(`启动失败：${err.message}`);
    setMaintDisabled(false);
  }
}

async function runJournalClean() {
  if (!confirm("清理目标目录已不存在的账本？有效账本不会动。")) return;
  state.jobKind = "maint";
  setMaintDisabled(true);
  $("progressBox").hidden = false;
  $("bar").style.width = "0%";
  $("jobMsg").textContent = "[清理账本] 正在启动…";
  try {
    const r = await call("/api/run", { action: "journal-clean", out: "out" });
    state.jobId = r.id;
    pollJob();
  } catch (err) {
    log(`启动失败：${err.message}`);
    setMaintDisabled(false);
  }
}

async function runMaint(action, title, warning, extra) {
  const dir = maintTarget();
  if (!dir) {
    log("先指定维护目录。");
    return;
  }
  if (action.startsWith("nofade-")) {
    try {
      const s = await call("/api/nofade", { target: dir });
      if (s.state === "unsupported") {
        $("maintHint").textContent = "反虚化只支持国服客户端（AzurPromilia_Game）。";
        log("反虚化只支持国服，当前目录不能操作。");
        return;
      }
      if (action === "nofade-install" && s.state === "patched") {
        $("maintHint").textContent = "这个目录已经安装过反虚化补丁。";
        log("已安装补丁，无需重复安装；要恢复官方文件请点「还原官方文件」。");
        return;
      }
      if (action === "nofade-restore" && s.state !== "patched") {
        $("maintHint").textContent = "当前没有检测到已安装的补丁。";
        log("没有可还原的补丁。");
        return;
      }
    } catch (err) {
      log(`检查目录失败：${err.message}`);
      return;
    }
  }
  const lines = [title, "", `目录：${dir}`];
  if (warning) lines.push("", warning);
  if (!confirm(`${lines.join("\n")}\n\n继续？`)) return;

  state.jobKind = "maint";
  setMaintDisabled(true);
  $("progressBox").hidden = false;
  $("bar").style.width = "0%";
  $("jobMsg").textContent = "正在启动…";
  const p = state.lastPlan;
  try {
    const r = await call("/api/run", {
      action,
      target: dir,
      out: "out",
      base: p ? p.base.game : "",
      key: p ? p.target.key : "",
      ...(extra || {}),
    });
    state.jobId = r.id;
    log(`维护任务已启动：${r.id}`);
    pollJob();
  } catch (err) {
    log(`启动失败：${err.message}`);
    setMaintDisabled(false);
  }
}

$("refresh").addEventListener("click", detect);
$("calc").addEventListener("click", calc);
$("run").addEventListener("click", runJob);
$("cancel").addEventListener("click", cancelJob);
$("btnNofadeStatus").addEventListener("click", nofadeStatus);
$("btnNofadeInstall").addEventListener("click", () => runMaint(
  "nofade-install", "安装反虚化补丁",
  "会替换客户端文件并备份原版。该功能存在账号风险，自行判断。"));
$("btnNofadeRestore").addEventListener("click", () => runMaint(
  "nofade-restore", "还原官方文件", "从 .nofade-backup 恢复原版文件。"));
$("btnRepair").addEventListener("click", () => runMaint(
  "repair", "按清单修复", "会重新下载并覆盖校验不符的文件；已装补丁默认跳过。"));
$("btnRollback").addEventListener("click", () => runMaint(
  "rollback", "回滚最近一次写入", "按账本删除新增文件，并还原被覆盖的备份。"));
$("btnJournalRefresh").addEventListener("click", refreshJournals);
$("btnJournalPreview").addEventListener("click", () => {
  const id = journalSelected();
  if (!id) {
    log("没有可回滚的账本。");
    return;
  }
  runMaint("rollback", "预览回滚（不改动文件）",
    "只统计会删除与还原多少，不会改动文件。", { id, dry: true });
});
$("btnJournalRollback").addEventListener("click", () => {
  const id = journalSelected();
  if (!id) {
    log("没有可回滚的账本。");
    return;
  }
  runMaint("rollback", "回滚选中的账本",
    "会删除本工具新建的文件，并还原被覆盖的备份。", { id });
});
$("btnRemove").addEventListener("click", () => runMaint(
  "remove", "删除客户端目录", "整个目录会移入回收站，不是彻底删除。"));
$("btnManifests").addEventListener("click", runManifests);
$("btnVerify").addEventListener("click", () => {
  if (!state.lastPlan) { log("先计算计划。"); return; }
  runMaint("verify", "抽检目录", "按清单抽样核对 20 个文件，不改动文件。", { full: false });
});
$("btnVerifyFull").addEventListener("click", () => {
  if (!state.lastPlan) { log("先计算计划。"); return; }
  runMaint("verify", "全量校验", "逐个核对清单里的所有文件，耗时较长。", { full: true });
});
$("btnRepairFull").addEventListener("click", () => {
  if (!state.lastPlan) { log("先计算计划。"); return; }
  runMaint("repair", "全量修复", "重新下载所有与清单不符的文件。", { full: true });
});
$("btnRemovePreview").addEventListener("click", () => runMaint(
  "remove", "预览删除", "只列出会删除的内容，不会真的删除。", { dry: true }));
$("btnJournalClean").addEventListener("click", runJournalClean);
$("btnChannelExtract").addEventListener("click", () => {
  const info = state.channel;
  if (!info || !info.current) {
    log("没有识别到当前渠道。");
    return;
  }
  runMaint("channel-extract", "提取当前渠道源",
    "把当前客户端的渠道配置与登录组件缓存到 out/cn_sources，供之后切换使用。",
    { server: info.current });
});
$("clear").addEventListener("click", () => { $("log").textContent = ""; });

log("界面已就绪。计算计划是只读操作；点「开始执行」才会写入目标目录。");
detect();