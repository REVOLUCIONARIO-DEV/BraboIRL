/* ═══════════════════════════════════════════════════
   Brabo IRL — Lógica do Frontend
   ═══════════════════════════════════════════════════ */

// ── Estado global ──────────────────────────────────
let ws = null;
let selectedServer = null;
let mode = 'server';
let streaming = false;
let uptimeInterval = null;
let uptimeStart = null;

// ── Dispositivos ───────────────────────────────────
let selectedCameraId = null;
let selectedCameraName = null;
let selectedMicId = null;
let selectedMicName = null;
let selectedSource = 'camera'; // 'camera' | 'screen'
let devicesLoaded = false;

// ══════════════════════════════════════════════════
// PERSISTÊNCIA DE SESSÃO
// Sobrevive a reloads sem perder dados. Não persiste
// ao fechar a aba (usa sessionStorage, não localStorage).
// ══════════════════════════════════════════════════

const SESSION_KEY = 'braboirl_session';

function saveSession() {
  try {
    sessionStorage.setItem(SESSION_KEY, JSON.stringify({
      selectedServer,
      mode,
      streaming,
      uptimeStart,
      servers: window._servers || {},
      logs: Array.from(document.getElementById('logArea').children).map(el => el.outerHTML),
    }));
  } catch(e) {}
}

function restoreSession() {
  try {
    const raw = sessionStorage.getItem(SESSION_KEY);
    if (!raw) return null;
    return JSON.parse(raw);
  } catch(e) { return null; }
}

function applySession(session) {
  if (!session) return;
  if (session.servers) window._servers = session.servers;
  if (session.logs && session.logs.length) {
    const area = document.getElementById('logArea');
    area.innerHTML = session.logs.join('');
    area.scrollTop = area.scrollHeight;
    addLog('— sessão restaurada após reload —', 'warn');
  }
  if (session.selectedServer) selectedServer = session.selectedServer;
}

// Salva automaticamente a cada 2s e antes de fechar/recarregar
setInterval(saveSession, 2000);
window.addEventListener('beforeunload', saveSession);

// ══════════════════════════════════════════════════
// WEBSOCKET
// ══════════════════════════════════════════════════

function connectWS() {
  const host = window.location.host;
  ws = new WebSocket(`ws://${host}/ws`);

  ws.onopen = () => {
    document.getElementById('wsDot').className = 'ws-dot connected';
    document.getElementById('wsLabel').textContent = 'online';
    addLog('Conectado ao Brabo IRL', 'ok');
  };

  ws.onclose = () => {
    document.getElementById('wsDot').className = 'ws-dot error';
    document.getElementById('wsLabel').textContent = 'desconectado';
    addLog('Conexão perdida. Reconectando...', 'warn');
    setTimeout(connectWS, 3000);
  };

  ws.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    handleMessage(msg);
  };
}

function handleMessage(msg) {
  if (msg.type === 'hello') {
    mode = msg.mode;
    streaming = msg.streaming;
    document.getElementById('modeBadge').textContent = mode === 'server' ? 'SERVIDOR' : 'TRANSMISSOR';
    document.getElementById('serversCard').style.display = mode === 'server' ? 'none' : '';
    document.getElementById('streamControls').style.display = mode === 'server' ? 'none' : '';
    if (mode === 'server') {
      updateServerUI(msg.client_connected, msg.relay_active);
    } else {
      updateStreamUI(streaming);
    }
  }

  if (msg.type === 'stats') { updateStats(msg.stats); }

  if (msg.type === 'servers_updated') { renderServers(msg.servers); }

  // ── Eventos do TRANSMISSOR ──
  if (msg.type === 'stream_started') {
    streaming = true;
    uptimeStart = Date.now();
    updateStreamUI(true);
    addLog('Enviando stream para o servidor!', 'ok');
  }
  if (msg.type === 'stream_stopped') {
    streaming = false;
    uptimeStart = null;
    updateStreamUI(false);
    addLog('Stream encerrado.' + (msg.reason === 'ffmpeg_exited' ? ' (FFmpeg encerrou)' : ''), 'info');
  }

  // ── Reconexão automática ──
  if (msg.type === 'reconnecting') {
    addLog(`⚡ Reconectando (tentativa ${msg.attempt}) em ${msg.delay}s → ${msg.host}:${msg.port}`, 'warn');
    showReconnectBanner(msg.attempt, msg.delay);
  }
  if (msg.type === 'reconnect_ok') {
    addLog(`✓ Reconectado com sucesso (tentativa ${msg.attempt})`, 'ok');
    hideReconnectBanner();
  }

  // ── Eventos do SERVIDOR ──
  if (msg.type === 'relay_ready') {
    addLog('Relay ativo — aguardando transmissor na porta SRT ' + msg.port, 'ok');
    updateServerUI(false, true);
  }
  if (msg.type === 'client_connected') {
    uptimeStart = Date.now();
    updateServerUI(true, true);
    addLog('Transmissor conectado! Recebendo stream.', 'ok');
  }
  if (msg.type === 'client_disconnected') {
    uptimeStart = null;
    updateServerUI(false, true);
    addLog('Transmissor desconectado.', 'warn');
  }
  if (msg.type === 'relay_stopped') {
    updateServerUI(false, false);
    addLog('Relay encerrado. Reiniciando...', 'warn');
  }
  if (msg.type === 'error') {
    addLog('ERRO: ' + msg.msg, 'err');
    showToast(msg.msg, true);
  }
}

// ══════════════════════════════════════════════════
// UI — STREAM
// ══════════════════════════════════════════════════

function updateStreamUI(live) {
  const hero      = document.getElementById('streamHero');
  const title     = document.getElementById('streamTitle');
  const sub       = document.getElementById('streamSub');
  const indicator = document.getElementById('liveIndicator');
  const btnStart  = document.getElementById('btnStart');
  const btnStop   = document.getElementById('btnStop');

  hero.className      = 'stream-hero' + (live ? ' live' : '');
  title.className     = 'stream-title ' + (live ? 'live' : 'idle');
  title.textContent   = live ? 'TRANSMITINDO' : 'AGUARDANDO';
  sub.textContent     = live ? 'Stream ativo via SRT → RTMP' : 'Nenhuma transmissão ativa';
  indicator.className = 'live-indicator' + (live ? ' visible' : '');
  btnStart.disabled   = live;
  btnStop.disabled    = !live;

  if (live) {
    uptimeStart = uptimeStart || Date.now();
    clearInterval(uptimeInterval);
    uptimeInterval = setInterval(updateUptime, 1000);
    document.getElementById('uptimeLabel').textContent = 'transmitindo ao vivo';
  } else {
    clearInterval(uptimeInterval);
    document.getElementById('uptimeDisplay').textContent = '00:00:00';
    document.getElementById('uptimeLabel').textContent   = 'sem transmissão ativa';
  }
}

function updateServerUI(clientConnected, relayActive) {
  const hero  = document.getElementById('streamHero');
  const title = document.getElementById('streamTitle');
  const sub   = document.getElementById('streamSub');
  const ind   = document.getElementById('liveIndicator');

  if (clientConnected) {
    hero.className  = 'stream-hero live';
    title.className = 'stream-title live';
    title.textContent = 'RECEBENDO';
    sub.textContent   = 'Transmissor conectado → relay ativo → OBS';
    ind.className     = 'live-indicator visible';
    uptimeStart = uptimeStart || Date.now();
    clearInterval(uptimeInterval);
    uptimeInterval = setInterval(updateUptime, 1000);
    document.getElementById('uptimeLabel').textContent = 'recebendo stream';
  } else if (relayActive) {
    hero.className  = 'stream-hero';
    title.className = 'stream-title idle';
    title.textContent = 'AGUARDANDO';
    sub.textContent   = 'Relay SRT ativo — esperando o transmissor conectar';
    ind.className     = 'live-indicator';
    clearInterval(uptimeInterval);
    document.getElementById('uptimeDisplay').textContent = '00:00:00';
    document.getElementById('uptimeLabel').textContent   = 'aguardando transmissor';
  } else {
    hero.className  = 'stream-hero';
    title.className = 'stream-title idle';
    title.textContent = 'OFFLINE';
    sub.textContent   = 'Relay SRT inativo';
    ind.className     = 'live-indicator';
    clearInterval(uptimeInterval);
    document.getElementById('uptimeDisplay').textContent = '00:00:00';
    document.getElementById('uptimeLabel').textContent   = 'sem relay ativo';
  }
}

function updateUptime() {
  if (!uptimeStart) return;
  const sec = Math.floor((Date.now() - uptimeStart) / 1000);
  const h = String(Math.floor(sec / 3600)).padStart(2, '0');
  const m = String(Math.floor((sec % 3600) / 60)).padStart(2, '0');
  const s = String(sec % 60).padStart(2, '0');
  document.getElementById('uptimeDisplay').textContent = `${h}:${m}:${s}`;
}

function updateStats(stats) {
  const br  = stats.bitrate_kbps || 0;
  const el  = document.getElementById('statBitrate');
  el.textContent = br > 0 ? br : '—';
  el.className   = 'stat-value' + (br > 8000 ? ' warn' : br > 0 ? ' good' : '');

  const lat = stats.latency_ms || 0;
  const el2 = document.getElementById('statLatency');
  el2.textContent = lat > 0 ? lat : '—';
  el2.className   = 'stat-value' + (lat > 300 ? ' crit' : lat > 150 ? ' warn' : lat > 0 ? ' good' : '');

  const dr  = stats.dropped_packets || 0;
  const el3 = document.getElementById('statDropped');
  el3.textContent = dr > 0 ? dr : '—';
  el3.className   = 'stat-value' + (dr > 50 ? ' crit' : dr > 0 ? ' warn' : '');

  const cpu = stats.cpu_percent || 0;
  const mem = stats.mem_percent || 0;
  const el4 = document.getElementById('statCpu');
  el4.textContent = (cpu > 0 || mem > 0) ? `${cpu}/${mem}` : '—';
  el4.className   = 'stat-value' + (cpu > 80 ? ' crit' : cpu > 50 ? ' warn' : cpu > 0 ? ' good' : '');
}

// ══════════════════════════════════════════════════
// BANNER DE RECONEXÃO
// ══════════════════════════════════════════════════

let _reconnectCountdown = null;

function showReconnectBanner(attempt, delay) {
  let banner = document.getElementById('reconnectBanner');
  if (!banner) {
    banner = document.createElement('div');
    banner.id = 'reconnectBanner';
    banner.style.cssText = `
      position:fixed; top:0; left:0; right:0; z-index:9998;
      background:#1a0a00; border-bottom:2px solid var(--amber);
      padding:10px 20px; display:flex; align-items:center; gap:14px;
      font-family:var(--mono); font-size:12px; color:var(--amber);
    `;
    document.body.prepend(banner);
  }
  clearInterval(_reconnectCountdown);
  let remaining = delay;
  function tick() {
    banner.innerHTML = `
      <span style="font-size:16px">⚡</span>
      <span>Conexão perdida — reconectando (tentativa ${attempt}) em <strong>${remaining}s</strong>…</span>
      <span style="margin-left:auto;color:var(--muted);font-size:10px">O uptime da live continua sendo contado</span>
    `;
    if (remaining > 0) { remaining--; _reconnectCountdown = setTimeout(tick, 1000); }
  }
  tick();
}

function hideReconnectBanner() {
  clearInterval(_reconnectCountdown);
  const banner = document.getElementById('reconnectBanner');
  if (banner) banner.remove();
}

// ══════════════════════════════════════════════════
// SERVIDORES
// ══════════════════════════════════════════════════

function renderServers(servers) {
  const list = document.getElementById('serverList');
  if (!servers || servers.length === 0) {
    list.innerHTML = `<div class="empty-state">nenhum servidor encontrado<div class="scanning-anim"></div></div>`;
    return;
  }
  list.innerHTML = servers.map(s => `
    <div class="server-item ${selectedServer === s.id ? 'selected' : ''}" onclick="selectServer('${s.id}', '${s.name}', '${s.host}')">
      <div>
        <div class="server-name">${s.name}</div>
        <div class="server-meta">${s.host}:${s.srt_port} · ${s.platform} · ${s.manual ? 'manual' : 'v' + s.version}</div>
      </div>
      <div class="server-ping" style="color:${s.manual ? 'var(--amber)' : 'var(--green)'}">
        ${s.manual ? '◎ manual' : '● online'}
      </div>
    </div>
  `).join('');
}

function selectServer(id, name, host) {
  selectedServer = id;
  renderServers(Object.values(window._servers || {}));
  addLog(`Servidor selecionado: ${name} (${host})`, 'info');
}

function addManualServer() {
  const ip   = document.getElementById('manualIp').value.trim();
  const port = parseInt(document.getElementById('manualPort').value) || 9999;

  if (!ip) { showToast('Digite o IP do servidor', true); return; }

  const ipRegex = /^(\d{1,3}\.){3}\d{1,3}$/;
  if (!ipRegex.test(ip)) { showToast('IP inválido. Ex: 192.168.191.1', true); return; }

  const id  = 'manual-' + ip.replace(/\./g, '-');
  const srv = {
    id, name: 'Manual (' + ip + ')', host: ip,
    srt_port: port, version: '?', platform: 'manual',
    last_seen: Date.now() / 1000, manual: true,
  };

  if (!window._servers) window._servers = {};
  window._servers[id] = srv;
  selectedServer = id;
  renderServers(Object.values(window._servers));
  addLog('Servidor manual adicionado: ' + ip + ':' + port, 'ok');
  showToast('Servidor ' + ip + ' adicionado!');
  document.getElementById('manualIp').value = '';
}

async function scanServers() {
  const res     = await fetch('/api/servers');
  const servers = await res.json();

  // Preserva servidores manuais — não apaga o que o usuário adicionou
  const manuals = Object.values(window._servers || {}).filter(s => s.manual);
  window._servers = Object.fromEntries(servers.map(s => [s.id, s]));
  manuals.forEach(s => { window._servers[s.id] = s; });

  renderServers(Object.values(window._servers));
}

// ══════════════════════════════════════════════════
// AÇÕES — STREAM
// ══════════════════════════════════════════════════

async function startStream() {
  const url = mode === 'transmitter' && selectedServer
    ? `/api/stream/start?server_id=${selectedServer}`
    : '/api/stream/start';

  const res  = await fetch(url, { method: 'POST' });
  const data = await res.json();
  if (!data.ok) {
    showToast(data.error || 'Erro ao iniciar stream', true);
    addLog('Erro: ' + (data.error || 'Falha desconhecida'), 'err');
  }
}

async function stopStream() {
  const res  = await fetch('/api/stream/stop', { method: 'POST' });
  const data = await res.json();
  if (!data.ok) showToast(data.error, true);
}

// ══════════════════════════════════════════════════
// CONFIGURAÇÕES
// ══════════════════════════════════════════════════

async function saveConfig() {
  const config = {
    codec:         document.getElementById('cfgCodec').value,
    resolution:    document.getElementById('cfgRes').value,
    fps:           parseInt(document.getElementById('cfgFps').value),
    audio_bitrate: parseInt(document.getElementById('cfgAudio').value),
    bitrate:       parseInt(document.getElementById('cfgBitrate').value),
    drop_buffer:   document.getElementById('cfgBuffer').value === 'true', // <--- ADICIONE AQUI
  };
  const res  = await fetch('/api/config', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(config),
  });
  const data = await res.json();
  if (data.ok) {
    showToast('Configurações salvas!');
    addLog('Config atualizada: ' + config.resolution + ' / ' + config.codec + ' / ' + config.bitrate + 'kbps', 'ok');
  }
}

function updateBitrateLabel() {
  const v = document.getElementById('cfgBitrate').value;
  document.getElementById('bitrateLabel').textContent = v + ' kbps';
}

// ══════════════════════════════════════════════════
// DISPOSITIVOS
// ══════════════════════════════════════════════════

function jsEscape(value) {
  return String(value)
    .replace(/\\/g, '\\\\')
    .replace(/'/g, "\\'");
}

async function openDeviceModal() {
  document.getElementById('deviceModal').classList.add('open');
  if (!devicesLoaded) await loadDevices({ askPermission: true });
}

function closeDeviceModal() {
  document.getElementById('deviceModal').classList.remove('open');
}

async function loadDevices({ askPermission = false } = {}) {
  try {
    if (askPermission) {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true, video: true });
      stream.getTracks().forEach(track => track.stop());
    }

    const devices = await navigator.mediaDevices.enumerateDevices();

    const data = {
      cameras: devices.filter(d => d.kind === 'videoinput').map(d => ({ id: d.deviceId, name: d.label || 'Câmera Padrão' })),
      mics:    devices.filter(d => d.kind === 'audioinput').map(d => ({ id: d.deviceId, name: d.label || 'Microfone Padrão' })),
    };

    // Câmeras
    const camList = document.getElementById('cameraList');
    if (data.cameras.length > 0) {
      camList.innerHTML = data.cameras.map(c => `
        <div class="device-option ${selectedCameraId === c.id ? 'selected' : ''}"
             onclick="selectCamera('${jsEscape(c.id)}', '${jsEscape(c.name)}', this)">
          <input type="radio" name="camera" ${selectedCameraId === c.id ? 'checked' : ''}>
          <div>
            <div class="device-option-label">${c.name}</div>
            <div class="device-option-sub">${c.id}</div>
          </div>
        </div>
      `).join('');
    } else {
      camList.innerHTML = '<div class="device-loading">nenhuma câmera encontrada</div>';
    }

    // Microfones
    const micList = document.getElementById('micList');
    if (data.mics.length > 0) {
      micList.innerHTML = data.mics.map(m => `
        <div class="device-option ${selectedMicId === m.id ? 'selected' : ''}"
             onclick="selectMic('${jsEscape(m.id)}', '${jsEscape(m.name)}', this)">
          <input type="radio" name="mic" ${selectedMicId === m.id ? 'checked' : ''}>
          <div>
            <div class="device-option-label">${m.name}</div>
            <div class="device-option-sub">${m.id}</div>
          </div>
        </div>
      `).join('');
    } else {
      micList.innerHTML = '<div class="device-loading">nenhum microfone encontrado</div>';
    }

    devicesLoaded = true;
    updateDeviceSummary();

  } catch (e) {
    console.error('Erro ao carregar dispositivos:', e);
    document.getElementById('cameraList').innerHTML =
      '<div class="device-loading" style="color:var(--accent)">Erro: Permissão negada ou hardware não encontrado</div>';
  }
}

function selectCamera(id, name, el) {
  selectedCameraId   = id;
  selectedCameraName = name;
  selectedSource     = 'camera';

  document.querySelectorAll('#cameraList .device-option').forEach(e => e.classList.remove('selected'));
  document.querySelectorAll('#cameraList input[type=radio]').forEach(e => e.checked = false);
  el.classList.add('selected');
  el.querySelector('input').checked = true;

  document.getElementById('screenRadio').checked = false;
  document.getElementById('screenOpt').classList.remove('selected');

  updateDeviceSummary();
}

function selectMic(id, name, el) {
  selectedMicId   = id;
  selectedMicName = name;

  document.querySelectorAll('#micList .device-option').forEach(e => e.classList.remove('selected'));
  document.querySelectorAll('#micList input[type=radio]').forEach(e => e.checked = false);
  el.classList.add('selected');
  el.querySelector('input').checked = true;

  updateDeviceSummary();
}

function selectScreen() {
  selectedSource     = 'screen';
  selectedCameraId   = null;
  selectedCameraName = null;

  document.querySelectorAll('#cameraList .device-option').forEach(e => e.classList.remove('selected'));
  document.querySelectorAll('#cameraList input[type=radio]').forEach(e => e.checked = false);
  document.getElementById('screenOpt').classList.add('selected');
  document.getElementById('screenRadio').checked = true;

  updateDeviceSummary();
}

function updateDeviceSummary() {
  const src = selectedSource === 'screen' ? 'tela inteira' : (selectedCameraName || 'nenhuma câmera');
  const mic = selectedMicName || 'nenhum microfone';
  document.getElementById('deviceSummary').textContent = 'Vídeo: ' + src + ' · Áudio: ' + mic;
}

async function saveDevices() {
  const res = await fetch('/api/devices', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      source:      selectedSource,
      camera_id:   selectedCameraId,
      mic_id:      selectedMicId,
      camera_name: selectedCameraName,
      mic_name:    selectedMicName,
    }),
  });

  const data = await res.json();
  if (data.ok) {
    showToast('Dispositivos salvos!');
    addLog(
      'Fonte: ' + (selectedSource === 'screen' ? 'tela' : (selectedCameraName || selectedCameraId || 'nenhuma câmera')) +
      ' · Mic: ' + (selectedMicName || selectedMicId || 'nenhum'),
      'ok'
    );
    closeDeviceModal();
  } else {
    showToast(data.error || 'Erro ao salvar', true);
  }
}

// ══════════════════════════════════════════════════
// LOG & TOAST
// ══════════════════════════════════════════════════

function addLog(msg, type = 'info') {
  const area = document.getElementById('logArea');
  const now  = new Date().toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  const line = document.createElement('div');
  line.className = 'log-line';
  line.innerHTML = `<span class="log-time">${now}</span><span class="log-msg ${type}">${msg}</span>`;
  area.appendChild(line);
  area.scrollTop = area.scrollHeight;
  if (area.children.length > 100) area.removeChild(area.firstChild);
}

function clearLog() {
  document.getElementById('logArea').innerHTML = '';
}

function showToast(msg, error = false) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className   = 'toast show' + (error ? ' error' : '');
  setTimeout(() => t.className = 'toast' + (error ? ' error' : ''), 3000);
}

// ══════════════════════════════════════════════════
// INIT
// ══════════════════════════════════════════════════

// 1. Restaura sessão imediatamente (antes do WS chegar)
const _session = restoreSession();
applySession(_session);

// 2. Conecta WebSocket
connectWS();

// 3. Busca status atual via REST e sincroniza com o backend
fetch('/api/status').then(r => r.json()).then(data => {
  mode      = data.mode;
  streaming = data.streaming;

  document.getElementById('modeBadge').textContent         = mode === 'server' ? 'SERVIDOR' : 'TRANSMISSOR';
  document.getElementById('serversCard').style.display     = mode === 'server' ? 'none' : '';
  updateStreamUI(streaming);

  // Sincroniza uptime com start_time real do backend (preserva entre reconexões e reloads)
  if (streaming && data.start_time) {
    uptimeStart = Date.now() - ((Date.now() / 1000 - data.start_time) * 1000);
    clearInterval(uptimeInterval);
    uptimeInterval = setInterval(updateUptime, 1000);
  }

  if (data.config) {
    document.getElementById('cfgCodec').value   = data.config.codec;
    document.getElementById('cfgRes').value     = data.config.resolution;
    document.getElementById('cfgFps').value     = data.config.fps;
    document.getElementById('cfgBitrate').value = data.config.bitrate;
    document.getElementById('cfgAudio').value   = data.config.audio_bitrate;
    // ADICIONE ESTAS 3 LINHAS:
    if (document.getElementById('cfgBuffer')) {
      document.getElementById('cfgBuffer').value = data.config.drop_buffer ? 'true' : 'false';
    }

    updateBitrateLabel();
  }

  // === ADICIONE ESTE BLOCO AQUI ===
  // Oculta as configurações de transmissão se estiver no modo Servidor
  const txSettings = document.getElementById('txSettingsArea');
  if (txSettings) {
    if (mode === 'server') {
      txSettings.style.display = 'none';
    } else {
      txSettings.style.display = 'block'; // ou 'grid', dependendo do seu CSS original
    }
  }
  // ==================================

  // Restaura lista de servidores (incluindo manuais salvos na sessão)
  if (window._servers && Object.keys(window._servers).length > 0) {
    renderServers(Object.values(window._servers));
  }

  addLog(`Modo: ${mode.toUpperCase()} · Plataforma: ${data.platform}`, 'info');

  if (streaming && data.reconnect_attempt > 0) {
    addLog(`⚡ Em reconexão (tentativa ${data.reconnect_attempt})`, 'warn');
  }
  if (mode === 'server') updateServerUI(data.client_connected, data.relay_active);

}).catch(() => addLog('Não foi possível conectar ao backend', 'err'));

// 4. Scan automático no modo transmissor (silencioso, preserva manuais)
setInterval(() => {
  if (mode === 'transmitter' && !streaming) scanServers();
}, 5000);
