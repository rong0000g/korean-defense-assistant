const el = (id) => document.getElementById(id);
const status = el('status');
const live = el('live');
const results = el('results');
const startButton = el('start');
const stopButton = el('stop');
const sensitivity = el('sensitivity');
const sensitivityValue = el('sensitivity-value');
const micLevel = el('mic-level');
const latency = el('latency');
const quickTestButton = el('quick-test');

// 从 URL 获取 Token 并保存在 sessionStorage，防止 Safari 跨域或重载时丢失
const urlParams = new URLSearchParams(window.location.search);
let sessionToken = urlParams.get('token') || sessionStorage.getItem('defense_token') || '';
if (sessionToken) {
  sessionStorage.setItem('defense_token', sessionToken);
}
if (location.search.includes('token=')) {
  history.replaceState(null, '', location.pathname);
}

let stream = null;
let audioContext = null;
let source = null;
let processor = null;
let wakeLock = null;
let listening = false;
let utterance = null;
let preRoll = [];
let preRollSamples = 0;
let speechFrames = 0;
let silenceMs = 0;
let asrQueue = Promise.resolve();
let queued = 0;
let lastMeterUpdate = 0;
let cooldownUntil = 0;
const MIN_SPEECH_MS = 350;
const CHUNK_SECONDS = 5.5;

function cooldownMessage() {
  const seconds = Math.max(1, Math.ceil((cooldownUntil - Date.now()) / 1000));
  return `Groq 限流中，约 ${seconds} 秒后再按“开始收音”；无需充值。`;
}

function setStatus(text) { status.textContent = text; }
function updateSensitivity() {
  const n = Number(sensitivity.value);
  sensitivityValue.textContent = n < 0.009 ? '高' : n > 0.02 ? '低' : '中';
}
sensitivity.addEventListener('input', updateSensitivity);

function makeCard() {
  const card = document.createElement('article');
  card.className = 'card pending';
  const time = document.createElement('div');
  time.className = 'time';
  time.textContent = new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  const output = document.createElement('pre');
  output.textContent = '正在识别韩语…';
  card.append(time, output);
  results.prepend(card);
  return { card, output };
}

function newUtterance() {
  const display = makeCard();
  return { ...display, chunks: [], sampleCount: 0, speechMs: 0, korean: [], partialRequested: false, partialZh: '', finalized: false };
}

function concatSamples(chunks, total) {
  const merged = new Float32Array(total);
  let offset = 0;
  for (const chunk of chunks) { merged.set(chunk, offset); offset += chunk.length; }
  return merged;
}

function encodeWav(samples, sourceRate) {
  const targetRate = 16000;
  const ratio = sourceRate / targetRate;
  const count = Math.floor(samples.length / ratio);
  const buffer = new ArrayBuffer(44 + count * 2);
  const view = new DataView(buffer);
  const write = (offset, text) => [...text].forEach((c, i) => view.setUint8(offset + i, c.charCodeAt(0)));
  write(0, 'RIFF'); view.setUint32(4, 36 + count * 2, true); write(8, 'WAVE');
  write(12, 'fmt '); view.setUint32(16, 16, true); view.setUint16(20, 1, true);
  view.setUint16(22, 1, true); view.setUint32(24, targetRate, true);
  view.setUint32(28, targetRate * 2, true); view.setUint16(32, 2, true);
  view.setUint16(34, 16, true); write(36, 'data'); view.setUint32(40, count * 2, true);
  for (let i = 0; i < count; i++) {
    const sample = Math.max(-1, Math.min(1, samples[Math.floor(i * ratio)]));
    view.setInt16(44 + i * 2, sample < 0 ? sample * 32768 : sample * 32767, true);
  }
  return new Blob([buffer], { type: 'audio/wav' });
}

async function jsonPost(url, body, contentType) {
  if (Date.now() < cooldownUntil) throw new Error(cooldownMessage());
  const headers = { 'Content-Type': contentType };
  if (sessionToken) {
    headers['X-Defense-Token'] = sessionToken;
  }
  let response;
  try {
    response = await fetch(url, {
      method: 'POST',
      headers,
      body,
      cache: 'no-store',
    });
  } catch (err) {
    throw new Error(`网络连接异常（${err.message || '请检查手机网络'}）`);
  }
  const raw = await response.text();
  let data;
  try {
    data = JSON.parse(raw);
  } catch {
    if (response.status === 401) {
      throw new Error('API 密钥失效（401），请在电脑窗口重新输入有效 Groq 密钥（gsk_）');
    } else if (response.status === 403) {
      throw new Error('授权已过期或失效，请重新扫描电脑窗口中的二维码');
    } else {
      throw new Error(`服务器响应异常（HTTP ${response.status}）`);
    }
  }
  if (response.status === 429) {
    const seconds = Number.parseFloat(data?.retry_after || response.headers?.get?.('Retry-After') || '');
    cooldownUntil = Date.now() + (Number.isFinite(seconds) && seconds > 0 ? Math.ceil(seconds) : 60) * 1000;
    utterance = null;
    if (listening) void stopListening(cooldownMessage());
    throw new Error(cooldownMessage());
  }
  if (!response.ok) throw new Error(data?.error || `HTTP ${response.status}`);
  if (data && data.error) throw new Error(data.error);
  return data;
}

function queueChunk(item, samples, final, speechMs) {
  // Ignore brief noise while keeping utterances with at least 350 ms of speech.
  const blob = speechMs >= MIN_SPEECH_MS && samples.length >= 1600
    ? encodeWav(samples, audioContext.sampleRate) : null;
  queued += 1;
  setStatus(`正在处理；队列 ${queued} 段`);
  let stage = '转写';
  const queuedAt = Date.now();
  const asrJob = asrQueue.then(async () => {
    if (blob) {
      const result = await jsonPost('/api/transcribe', blob, 'audio/wav');
      if (result.korean) item.korean.push(result.korean);
    }
    return { korean: item.korean.join(' ').trim(), asrMs: Date.now() - queuedAt };
  });
  // Keep audio chunks in order, while a partial translation can run alongside
  // the next transcription and the final answer.
  asrQueue = asrJob.catch(() => {});
  asrJob.then(async ({ korean, asrMs }) => {
    if (!korean || [...korean].filter((char) => char >= '가' && char <= '힣').length < 3) {
      if (final) {
        item.output.textContent = '识别失败';
        item.card.classList.remove('pending');
        item.finalized = true;
        live.textContent = blob
          ? '录音已送去转写，但没有听清完整韩语。请靠近手机说完整问句。'
          : `有效声音仅 ${(speechMs / 1000).toFixed(1)} 秒，没有送出。请靠近手机说完整问句。`;
      }
      return;
    }
    if (final) {
      stage = '生成回答';
      item.output.textContent = `韩语：${korean}\n正在生成回答…`;
      const answerStarted = Date.now();
      const answer = await jsonPost('/api/answer', JSON.stringify({ korean, partial: false }), 'application/json');
      item.output.textContent = answer.output || '识别失败';
      item.card.classList.remove('pending');
      item.finalized = true;
      latency.textContent = `上次处理：转写 ${(asrMs / 1000).toFixed(1)} 秒，生成结果 ${((Date.now() - answerStarted) / 1000).toFixed(1)} 秒`;
      live.textContent = answer.output === '识别失败'
        ? `转写到的韩文：${korean}\n回答模型未确认内容，请查看识别是否准确。`
        : answer.output?.includes('【回复韩文】：无')
          ? `转写到的韩文：${korean}\n这段被当作非问句，因此只翻译。若评委在提问，请检查转写是否准确。`
          : '等待下一段韩语…';
    } else {
      if (item.partialRequested) {
        if (!item.finalized) {
          item.output.textContent = `韩语：${korean}\n中文：${item.partialZh || '正在翻译…'}\n（讲话中）`;
        }
        return;
      }
      item.partialRequested = true;
      stage = '翻译片段';
      const answer = await jsonPost('/api/answer', JSON.stringify({ korean, partial: true }), 'application/json');
      item.partialZh = answer.translation || '';
      if (!item.finalized) {
        item.output.textContent = `韩语：${korean}\n中文：${item.partialZh || '正在翻译…'}\n（讲话中）`;
        live.textContent = `韩语：${korean}\n中文：${item.partialZh}`;
      }
    }
  }).catch((error) => {
    if (final || !item.finalized) {
      item.output.textContent = `${stage}失败：${error.message}`;
      item.card.classList.remove('pending');
      if (final) item.finalized = true;
    }
  }).finally(() => {
    queued -= 1;
    if (Date.now() < cooldownUntil) setStatus(cooldownMessage());
    else if (queued) setStatus(`正在处理；队列 ${queued} 段`);
    else setStatus(listening ? '正在收音' : '已停止');
  });
}

function flushChunk(final) {
  if (!utterance) return;
  const item = utterance;
  const samples = concatSamples(item.chunks, item.sampleCount);
  const speechMs = item.speechMs;
  item.chunks = [];
  item.sampleCount = 0;
  item.speechMs = 0;
  if (samples.length || final) queueChunk(item, samples, final, speechMs);
  if (final) utterance = null;
}

function processAudio(event) {
  event.outputBuffer.getChannelData(0).fill(0);
  if (!listening) return;
  const input = event.inputBuffer.getChannelData(0);
  const copy = new Float32Array(input);
  const durationMs = (copy.length / audioContext.sampleRate) * 1000;
  let sum = 0;
  for (let i = 0; i < copy.length; i++) sum += copy[i] * copy[i];
  const rms = Math.sqrt(sum / copy.length);
  const active = rms > Number(sensitivity.value);
  if (Date.now() - lastMeterUpdate >= 250) {
    micLevel.textContent = active
      ? '麦克风：检测到声音'
      : '麦克风：当前声音较小；说话时仍显示这句，请把灵敏度向左调';
    lastMeterUpdate = Date.now();
  }
  if (!utterance) {
    preRoll.push(copy); preRollSamples += copy.length;
    const maxPreRoll = audioContext.sampleRate * 0.45;
    while (preRollSamples > maxPreRoll && preRoll.length > 1) preRollSamples -= preRoll.shift().length;
    speechFrames = active ? speechFrames + 1 : 0;
    if (speechFrames >= 2) {
      utterance = newUtterance();
      utterance.chunks.push(...preRoll); utterance.sampleCount += preRollSamples;
      utterance.speechMs = 2 * durationMs;
      preRoll = []; preRollSamples = 0; speechFrames = 0; silenceMs = 0;
      live.textContent = '听到韩语，正在分段识别…';
    }
    return;
  }
  utterance.chunks.push(copy);
  utterance.sampleCount += copy.length;
  if (active) utterance.speechMs += durationMs;
  silenceMs = active ? 0 : silenceMs + durationMs;
  if (silenceMs >= 700) {
    flushChunk(true);
    silenceMs = 0;
  } else if (utterance.sampleCount / audioContext.sampleRate >= CHUNK_SECONDS) {
    flushChunk(false);
  }
}

async function startListening() {
  if (listening) return;
  if (Date.now() < cooldownUntil) { setStatus(cooldownMessage()); return; }
  if (!navigator.mediaDevices?.getUserMedia) {
    setStatus('浏览器未提供麦克风接口。请使用 Safari 打开 HTTPS 地址。');
    return;
  }
  try {
    startButton.disabled = true;
    setStatus('正在请求手机麦克风权限…');
    stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, channelCount: 1 } });
    audioContext = new (window.AudioContext || window.webkitAudioContext)();
    await audioContext.resume();
    source = audioContext.createMediaStreamSource(stream);
    processor = audioContext.createScriptProcessor(4096, 1, 1);
    processor.onaudioprocess = processAudio;
    source.connect(processor);
    processor.connect(audioContext.destination);
    listening = true;
    startButton.disabled = true; stopButton.disabled = false;
    setStatus('正在收音');
    live.textContent = '等待讲话…';
    micLevel.textContent = '麦克风：等待声音…';
    try { wakeLock = await navigator.wakeLock?.request('screen'); } catch { /* user can keep screen awake */ }
  } catch (error) {
    const reason = error?.name === 'NotAllowedError'
      ? '手机麦克风权限被拒绝，请在浏览器设置中允许麦克风'
      : `${error?.name || '错误'}：${error?.message || '未知原因'}`;
    await stopListening(`无法收音：${reason}`);
  }
}

async function stopListening(message = '') {
  if (utterance) flushChunk(true);
  listening = false;
  if (processor) { processor.disconnect(); processor.onaudioprocess = null; processor = null; }
  if (source) { source.disconnect(); source = null; }
  if (stream) { stream.getTracks().forEach((track) => track.stop()); stream = null; }
  if (audioContext) { await audioContext.close(); audioContext = null; }
  if (wakeLock) { try { await wakeLock.release(); } catch {} wakeLock = null; }
  micLevel.textContent = '麦克风：已停止';
  startButton.disabled = false; stopButton.disabled = true;
  setStatus(message || (queued ? `已停止收音；正在处理 ${queued} 段` : '已停止'));
}

startButton.addEventListener('click', startListening);
stopButton.addEventListener('click', () => stopListening());
async function submitKorean(korean) {
  if (!korean) return;
  const item = makeCard();
  item.output.textContent = '正在生成回答…';
  try {
    const answer = await jsonPost('/api/answer', JSON.stringify({ korean, partial: false }), 'application/json');
    item.output.textContent = answer.output || '识别失败';
  } catch (error) {
    item.output.textContent = `处理失败：${error.message}`;
  } finally { item.card.classList.remove('pending'); }
}
el('submit').addEventListener('click', () => submitKorean(el('manual').value.trim()));
quickTestButton.addEventListener('click', () => submitKorean('이 연구의 핵심 질문은 무엇입니까?'));
document.addEventListener('visibilitychange', () => {
  if (document.hidden && listening) setStatus('页面进入后台；iPhone 可能暂停收音');
});
updateSensitivity();
