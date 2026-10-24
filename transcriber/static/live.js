/* 实时字幕前端(Task 7):双形态 UI(半透明字幕条 ↔ 完整面板)+ /ws/live 客户端 +
   流式降采样 + 断线缓冲。独立页面,不依赖 recorder.js。

   与服务端实现(app.py ws_live / live_session.py / live_pass2.py)的对齐点:
   - started 应答无 seq;partial/pass1_final(字段 t0_ms/t1_ms)带单调 seq;
   - resume 无 resumed 应答:服务端直接重放 seq > last_seq 的 backlog 再继续实时流,
     故重连后按 seq 去重即可,首个下行消息可能就是重放事件;
   - audio_flush_done 上行消息当前被服务端忽略(前向兼容,补发完毕后照发);
   - stop → stop_ack →(引擎尾部冲刷的 pass1_final)→ stopped,随后服务端主动断开;
     stopped 自 Task 9 起携带 job_id(停止即建 job,可在工作台查看完整文稿);
   - start 阶段失败:服务端回 {'type':'error','message':'无法开始实时会话:…'} 并断开,
     前端收到后停止自动重连、显示错误(onError),不再无限重试;
   - pass2 / pass2_skip / llm 事件字段名是 t0 / t1(与 live_pass2.py 一致)。

   文案硬约束:停止按钮的文案就是「停止」二字,不用任何替代表述。 */
(function(){
'use strict';

var TARGET_RATE = 16000;
var CHUNK_SAMPLES = 3200;                    // 200ms/包 @16k mono s16le
var BUFFER_MAX_SEC = 5 * 60;                 // 断线本地缓冲上限:5 分钟(超出丢最旧)
var BUFFER_MAX_BYTES = BUFFER_MAX_SEC * TARGET_RATE * 2;
var RECONNECT_MS = 3000;                     // 断线 3s 后自动重连 resume
var SPEAKER_COLORS = ['#4C9AFF', '#FFB02E', '#98A2B3'];   // A / B / 其余灰

var $ = function(id){ return document.getElementById(id); };

var state = {
  phase: 'idle',            // idle → connecting → live → stopping → ended
  ws: null, wsOpen: false,
  sessionId: null, lastSeq: 0,
  userStop: false,          // 已按「停止」;断线时挂起,重连后补发 stop
  reconnectTimer: 0,
  doc: [],                  // {id, kind:'f1'|'p2'|'llm'|'skip', t0, t1, speaker, text, translation, el}
  partial: '',
  paused: false,            // 暂停显示:仅冻结 DOM,事件照收、录音照常
  startedAt: 0, timerIv: 0, entryId: 0,
  capture: null, capRate: TARGET_RATE, resampler: null,
  batch: [], batchN: 0,     // 发送侧 200ms 攒包(16k Int16)
  pending: [], pendingBytes: 0, droppedSec: 0,   // 断线缓冲(16k Int16)
  mode: 'bar',
  title: '', speakers: 0, lang: 'zh>en',
  autoResume: false,        // 正在自动恢复上次的会话(撞上死会话时回表单而非报错卡)
};

/* ---------- 流式降采样:16k AudioContext 构造失败时按实际捕获率逐块线性重采样 ---------- */

function resampleTo16k(fromRate){
  /* 返回 null 表示无需重采样;否则返回 chunk(Int16 @fromRate) → Int16Array(@16k) 的
     流式变换:线性插值,携带最近两个历史样本与全局输出位置,恒定内存、无跨块接缝。
     必须带两个样本:块末输出位置可落在上一块倒数第二样本上(步长非整数时必然出现),
     只带一个会在块边界取到 undefined。 */
  if (!fromRate || Math.abs(fromRate - TARGET_RATE) < 1) return null;
  var step = fromRate / TARGET_RATE;
  var pos = 0;                    // 下一个输出样本的输入侧浮点位置
  var fed = 0;                    // 已喂入的样本总数(全局索引基)
  var k0 = -1, c0 = 0;            // 历史 carry:全局索引 k0 的样本
  var k1 = -1, c1 = 0;            // 全局索引 k1 的样本(k0/k1=-1 表示尚无历史)
  return function(chunk){
    var n = chunk.length;
    if (!n) return new Int16Array(0);
    var start = fed;              // 本块首样本的全局索引
    fed += n;
    var last = fed - 1;           // 当前可用的最大全局索引
    var out = new Int16Array(Math.max(0, Math.floor((last - pos) / step) + 2));
    var w = 0;
    var at = function(i){ return i === k0 ? c0 : (i === k1 ? c1 : chunk[i - start]); };
    while (Math.floor(pos) + 1 <= last){
      var i = Math.floor(pos), f = pos - i;
      var s0 = at(i), s1 = at(i + 1);
      out[w++] = Math.round(s0 + (s1 - s0) * f);
      pos += step;
    }
    if (w < out.length) out = out.subarray(0, w);
    if (n >= 2){ k0 = last - 1; c0 = chunk[n - 2]; k1 = last; c1 = chunk[n - 1]; }
    else { k0 = k1; c0 = c1; k1 = last; c1 = chunk[0]; }   // 单样本块:与旧 carry 拼成末两个
    return out;
  };
}

/* ---------- WebSocket 客户端 ---------- */

function wsUrl(){
  if (location.protocol === 'http:' || location.protocol === 'https:')
    return (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws/live';
  return 'ws://127.0.0.1:8765/ws/live';   // file:// 等非 http 场景固定本机服务
}

function connect(){
  var resume = !!state.sessionId;
  var ws;
  try { ws = new WebSocket(wsUrl()); }
  catch (e){ scheduleReconnect(); return; }
  ws.binaryType = 'arraybuffer';
  state.ws = ws; state.wsOpen = false;
  ws.onopen = function(){
    state.wsOpen = true;
    $('connChip').hidden = true;
    barChip(null);
    if (state.userStop){                          // 停止请求在断线期挂起:新连接 session=None,
      if (state.sessionId)                        // 必须先 resume 挂回会话,否则 stop 落空、会话悬死
        ws.send(JSON.stringify({type: 'resume', session_id: state.sessionId,
                                last_seq: state.lastSeq}));
      flushPending();                             // 补发断线音频(此刻服务端已挂回会话)
      try { ws.send(JSON.stringify({type: 'stop'})); } catch (e){}
      return;
    }
    if (resume){
      ws.send(JSON.stringify({type: 'resume', session_id: state.sessionId,
                              last_seq: state.lastSeq}));
      flushPending();                             // 补发断线期间音频 → audio_flush_done
    } else {
      ws.send(JSON.stringify({type: 'start', title: state.title,
                              speakers: state.speakers, language: state.lang}));
    }
  };
  ws.onmessage = function(ev){
    if (typeof ev.data !== 'string') return;
    var msg;
    try { msg = JSON.parse(ev.data); } catch (e){ return; }
    handleEvent(msg);
  };
  ws.onclose = function(){
    state.wsOpen = false;
    // ended:正常收尾;idle:尚未开始(含自动恢复失败回表单)——都无需重连,
    // 否则 idle 下重连会误发 start,凭空开出一个无人供音的幽灵会话
    if(state.phase === 'ended' || state.phase === 'idle') return;
    scheduleReconnect();
  };
  ws.onerror = function(){ /* onclose 随后必至 */ };
}

function scheduleReconnect(){
  $('connChip').hidden = false;
  barChip('连接中断 · 重连中');
  clearTimeout(state.reconnectTimer);
  state.reconnectTimer = setTimeout(connect, RECONNECT_MS);
}

function barChip(text){                       // 状态在字幕条形态下的镜像(字幕条自身无徽标区)
  var el = $('barChip');
  if (!text){ if (el) el.remove(); return; }
  if (!el){
    el = document.createElement('span');
    el.className = 'chip'; el.id = 'barChip';
    $('barState').appendChild(el);
  }
  el.textContent = text;
}

function handleEvent(ev){
  if (typeof ev.seq === 'number'){
    if (ev.seq <= state.lastSeq) return;          // resume 重放边界去重(seq 单调)
    state.lastSeq = ev.seq;
    if (state.phase === 'connecting') setPhase('live');   // resume 无 started 应答:首条事件即转 live
  }
  switch (ev.type){
    case 'started':
      state.sessionId = ev.session_id;
      state.autoResume = false;                     // 已真正挂上会话,此后失效走错误卡
      state.startedAt = Date.now();
      localStorage.setItem('live.sessionId', state.sessionId);
      localStorage.setItem('live.startedAt', String(state.startedAt));
      setPhase('live');
      // 翻译目标非同语种但服务端无 LLM 密钥(env.rtf 缺失):显式告知,而非静默无翻译
      if (ev.llm === false){
        var p = (state.lang || '').split('>');
        if (p[1] && p[1] !== p[0])
          setChip('stopChip', '未配置翻译密钥(.env),本场仅识别、不修正不翻译', 9000);
      }
      break;
    case 'partial':
      state.partial = ev.text || '';
      badge('bdgAsr');
      renderActive(); renderBar();
      break;
    case 'pass1_final':
      insertEntry({kind: 'f1', t0: ev.t0_ms, t1: ev.t1_ms, text: ev.text || ''});
      badge('bdgAsr');
      break;
    case 'pass2':
      replaceWindow(ev.t0, ev.t1,
        {kind: 'p2', speaker: ev.speaker, text: ev.text || '', translation: ''});
      badge('bdgFix');
      break;
    case 'pass2_skip':
      replaceWindow(ev.t0, ev.t1, {kind: 'skip', text: ''});
      badge('bdgFix');
      break;
    case 'llm':
      llmReplace(ev.t0, ev.t1, ev.text || '', ev.translation || '');
      badge('bdgTr');
      break;
    case 'stop_ack':
      setChip('stopChip', '正在收尾…');
      break;
    case 'stopped':
      onStopped(ev);
      break;
    case 'error':
      onError(ev);
      break;
  }
}

function onError(ev){
  var msg = ev.message || '未知错误';
  if (msg.indexOf('会话不存在') >= 0){
    if (state.autoResume){
      /* 打开页面时的自动恢复撞上死会话(服务被重启过):不是用户会话的失败,
         清掉陈旧记录、回到开始表单,可直接开新会话——而不是死路错误卡 */
      state.autoResume = false;
      state.sessionId = null;                      // 不清的话重连会再 resume 死 ID,二次落入错误卡
      try { if (state.ws) state.ws.close(); } catch (e){}
      setPhase('idle');
      stopCapture();
      stopTimer();
      clearSessionStorage();
      clearTimeout(state.reconnectTimer);
      $('connChip').hidden = true;
      barChip(null);
      $('setup').hidden = false;
      $('endwrap').hidden = true;                  // 防御:恢复过程中错误卡若已亮,一并收起
      $('resumeNote').textContent = '上次会话已因服务重启结束,可直接开始新会话。';
      $('resumeNote').hidden = false;
      return;
    }
    setPhase('ended');
    stopCapture();
    stopTimer();
    clearSessionStorage();
    clearTimeout(state.reconnectTimer);
    $('connChip').hidden = true;
    barChip(null);
    $('endTitle').textContent = '会话已失效';
    $('endSub').textContent = '服务可能已重启,刚才的会话无法恢复。已录音频仍在服务端 live 目录中。';
    $('endLink').hidden = true;
    $('endwrap').hidden = false;
  } else if (msg.indexOf('无法开始实时会话') >= 0){
    /* start 阶段失败(模型加载等):服务端发完 error 即断开。必须停掉自动重连
       (否则 onclose 又会 scheduleReconnect,无限重试同一失败),收尾采集并显示原因。 */
    setPhase('ended');
    stopCapture();
    stopTimer();
    clearTimeout(state.reconnectTimer);
    clearSessionStorage();
    $('connChip').hidden = true;
    $('stopChip').hidden = true;
    barChip(null);
    try { if (state.ws) state.ws.close(); } catch (e){}
    $('endTitle').textContent = '无法开始实时会话';
    $('endSub').textContent = msg + '。可刷新页面重试;若持续失败请重启转写服务。';
    $('endLink').hidden = true;
    $('endwrap').hidden = false;
  } else {
    setChip('stopChip', msg, 6000);
  }
}

function onStopped(ev){
  setPhase('ended');
  stopCapture();
  stopTimer();
  clearTimeout(state.reconnectTimer);
  clearSessionStorage();
  try { if (state.ws) state.ws.close(); } catch (e){}   // 正常路径服务端先断;此处兜底
  $('stopChip').hidden = true;
  $('connChip').hidden = true;
  barChip(null);
  $('endTitle').textContent = '录音已保存 · 正在生成完整文稿';
  $('endSub').textContent = '完整文稿生成后会出现在工作台历史记录中(带「实时」标记)。';
  if (ev.job_id){                                 // Task 9 起停止即建 job,可回工作台看完整文稿
    $('endLink').hidden = false;
    $('endLink').href = '/#' + ev.job_id;         // hash 深链:工作台启动读 hash 直接打开该任务
    $('endLink').onclick = function(e){           // 悬浮窗内:聚焦主窗跳转并隐藏悬浮窗(使命已完成)
      if (window.desk && window.desk.openMainJob){
        e.preventDefault();
        window.desk.openMainJob(ev.job_id);
        if (window.desk.closeLiveCaption) window.desk.closeLiveCaption();
      }
    };
  } else {
    $('endLink').hidden = true;
  }
  $('endwrap').hidden = false;
}

/* ---------- 音频上行:采集 → 降采样 → 200ms 攒包 / 断线缓冲 → 补发 ---------- */

function push16k(int16){
  if (!int16 || !int16.length) return;
  if (state.wsOpen && state.ws && state.ws.readyState === 1 && !state.userStop){
    state.batch.push(int16); state.batchN += int16.length;
    flushBatch();
  } else {
    state.pending.push(int16); state.pendingBytes += int16.length * 2;
    capPending();
  }
}

function drainBatch(){                       // 组出尽可能多的整 3200 样本帧,余量保留
  var frames = [];
  while (state.batchN >= CHUNK_SAMPLES){
    var frame = new Int16Array(CHUNK_SAMPLES), need = CHUNK_SAMPLES;
    while (need > 0){
      var head = state.batch[0];
      var take = Math.min(need, head.length);
      frame.set(take === head.length ? head : head.subarray(0, take), CHUNK_SAMPLES - need);
      if (take === head.length) state.batch.shift();
      else state.batch[0] = head.subarray(take);
      need -= take;
    }
    state.batchN -= CHUNK_SAMPLES;
    frames.push(frame);
  }
  return frames;
}

function flushBatch(){
  var fs = drainBatch();
  for (var i = 0; i < fs.length; i++) sendFrame(fs[i]);
}

function mergeBatch(){                       // 余量合并为单个尾包
  if (!state.batchN) return null;
  var out = new Int16Array(state.batchN), off = 0;
  for (var i = 0; i < state.batch.length; i++){ out.set(state.batch[i], off); off += state.batch[i].length; }
  state.batch = []; state.batchN = 0;
  return out;
}

function sendFrame(frame){
  if (!state.ws || state.ws.readyState !== 1){
    state.pending.push(frame); state.pendingBytes += frame.length * 2; capPending();
    return;
  }
  try { state.ws.send(frame.buffer); }
  catch (e){
    state.pending.unshift(frame); state.pendingBytes += frame.length * 2;
    capPending();
    scheduleReconnect();
  }
}

function capPending(){
  while (state.pendingBytes > BUFFER_MAX_BYTES){
    var drop = state.pending.shift();
    if (!drop) break;
    state.pendingBytes -= drop.length * 2;
    state.droppedSec += drop.length / TARGET_RATE;
    var msg = '断线缓冲超 5 分钟 · 已丢最旧 ' + fmtDur(state.droppedSec) + ' 音频';
    $('bufChip').textContent = msg;
    $('bufChip').hidden = false;
    barChip(msg);
  }
}

function flushPending(){                     // 重连成功后补发断线期间音频(停止挂起时同样需要)
  if (!state.wsOpen) return;
  if (state.pending.length){
    for (var i = 0; i < state.pending.length; i++){
      state.batch.push(state.pending[i]); state.batchN += state.pending[i].length;
    }
    state.pending = []; state.pendingBytes = 0;
  }
  flushBatch();
  var tail = mergeBatch();                   // 不足一帧的尾包也发出,避免音频滞留
  if (tail) sendFrame(tail);
  try { state.ws.send(JSON.stringify({type: 'audio_flush_done'})); } catch (e){}
  state.droppedSec = 0;
  $('bufChip').hidden = true;
  if (state.wsOpen) barChip(null);
}

/* ---------- 麦克风采集(独立实现;AudioWorklet 优先,ScriptProcessor 兜底) ---------- */

var WORKLET_SRC = [
  'class LivePCM extends AudioWorkletProcessor{',
  '  constructor(){ super(); this._buf = new Float32Array(1024); this._n = 0; }',
  '  process(inputs){',
  '    var input = inputs[0];',
  '    if(input && input.length){',
  '      var frames = input[0].length;',
  '      for(var i = 0; i < frames; i++){',
  '        var sum = 0;',
  '        for(var c = 0; c < input.length; c++) sum += input[c][i];',
  '        this._buf[this._n++] = sum / input.length;',
  '        if(this._n === 1024){ this.port.postMessage(this._buf.slice(0)); this._n = 0; }',
  '      }',
  '    }',
  '    return true;',
  '  }',
  '}',
  'registerProcessor("live-pcm", LivePCM);'
].join('\n');

function onChunkF32(f32){
  if (!f32 || !f32.length) return;
  var raw = new Int16Array(f32.length);
  for (var i = 0; i < f32.length; i++){
    var x = f32[i]; x = x > 1 ? 1 : (x < -1 ? -1 : x);
    raw[i] = Math.round(x < 0 ? x * 32768 : x * 32767);
  }
  var pcm16k = state.resampler ? state.resampler(raw) : raw;
  push16k(pcm16k);
}

function startCapture(){
  return new Promise(function(resolve, reject){
    var md = navigator.mediaDevices;
    if (!md || !md.getUserMedia){ reject(new Error('当前浏览器不支持麦克风采集')); return; }
    md.getUserMedia({audio: {echoCancellation: false, noiseSuppression: false,
                             autoGainControl: false}}).then(function(stream){
      var AC = window.AudioContext || window.webkitAudioContext;
      if (!AC){ reject(new Error('当前浏览器不支持 Web Audio,无法录音')); return; }
      var ctx;
      try { ctx = new AC({sampleRate: TARGET_RATE}); }
      catch (e){ ctx = new AC(); }
      state.capRate = ctx.sampleRate || TARGET_RATE;
      state.resampler = resampleTo16k(state.capRate);
      var src = ctx.createMediaStreamSource(stream);
      var mute = ctx.createGain(); mute.gain.value = 0; mute.connect(ctx.destination);
      var node = null;
      var wire = function(){
        if (ctx.audioWorklet && ctx.audioWorklet.addModule){
          var url = URL.createObjectURL(new Blob([WORKLET_SRC], {type: 'text/javascript'}));
          return ctx.audioWorklet.addModule(url).then(function(){
            URL.revokeObjectURL(url);
            var n = new AudioWorkletNode(ctx, 'live-pcm');
            n.port.onmessage = function(ev){ onChunkF32(ev.data); };
            src.connect(n); n.connect(mute);
            return n;
          });
        }
        return Promise.reject(new Error('no-worklet'));
      };
      wire().catch(function(){
        var spn = ctx.createScriptProcessor(2048, 1, 1);
        spn.onaudioprocess = function(ev){ onChunkF32(ev.inputBuffer.getChannelData(0)); };
        src.connect(spn); spn.connect(mute);
        return spn;
      }).then(function(n){
        node = n;
        if (ctx.state === 'suspended') return ctx.resume();
      }).then(function(){
        state.capture = {ctx: ctx, src: src, node: node, mute: mute, stream: stream};
        resolve();
      }, function(err){
        stream.getTracks().forEach(function(t){ t.stop(); });
        try { ctx.close(); } catch (e){}
        if (err && (err.name === 'NotAllowedError' || err.name === 'SecurityError'))
          reject(new Error('麦克风权限被拒绝,请在系统或浏览器设置中允许后重试'));
        else
          reject(new Error('无法开始录音:' + ((err && err.message) || '未知错误')));
      });
    }, function(e){
      if (e && (e.name === 'NotAllowedError' || e.name === 'SecurityError' ||
                e.name === 'PermissionDeniedError'))
        reject(new Error('麦克风权限被拒绝,请在系统或浏览器设置中允许后重试'));
      else
        reject(new Error('无法打开麦克风:' + ((e && e.message) || '未知错误')));
    });
  });
}

function stopCapture(){
  var c = state.capture;
  state.capture = null;
  if (!c) return;
  try {
    if (c.node && c.node.port) c.node.port.onmessage = null;
    if (c.node && c.node.onaudioprocess !== undefined) c.node.onaudioprocess = null;
    if (c.node) c.node.disconnect();
    if (c.src) c.src.disconnect();
    if (c.mute) c.mute.disconnect();
  } catch (e){}
  if (c.stream) c.stream.getTracks().forEach(function(t){ t.stop(); });
  try { c.ctx.close(); } catch (e){}
}

/* ---------- 三级渲染:partial 覆盖活动行 / pass1_final 追加 / pass2 整窗替换染色 ---------- */

function speakerColor(speaker){
  return SPEAKER_COLORS[speaker != null && speaker < SPEAKER_COLORS.length - 1 ? speaker
                        : SPEAKER_COLORS.length - 1];
}
function speakerName(speaker){
  return speaker == null ? '' : '说话人' + (speaker + 1);
}

function intersects(e, t0, t1){ return e.kind !== 'skip' && e.t0 < t1 && e.t1 > t0; }

function insertEntry(entry){
  if (entry.kind === 'f1'){                       // 已被 pass2/llm 覆盖的窗口不再收留旧 final
    for (var k = 0; k < state.doc.length; k++){
      var d = state.doc[k];
      if ((d.kind === 'p2' || d.kind === 'llm') && d.t0 <= entry.t0 && entry.t1 <= d.t1) return;
    }
  }
  entry.id = ++state.entryId;
  var i = state.doc.length;
  while (i > 0 && state.doc[i - 1].t0 > entry.t0) i--;
  state.doc.splice(i, 0, entry);
  if (!state.paused){
    entry.el = makeEntryEl(entry);
    var next = state.doc[i + 1];
    $('plist').insertBefore(entry.el, next && next.el ? next.el : $('activeLine'));
    $('plistEmpty').hidden = true;
    entry.bel = makeEntryEl(entry);               // 字幕条历史区的同源条目(紧凑样式由 .blog 作用域)
    $('barLog').insertBefore(entry.bel, next && next.bel ? next.bel : null);
    $('barLog').hidden = false;
    renderBar(); autoScroll($('plist')); autoScroll($('barLog'));
  }
}

function removeEntry(entry){
  var i = state.doc.indexOf(entry);
  if (i >= 0) state.doc.splice(i, 1);
  if (entry.el) entry.el.remove();
  if (entry.bel) entry.bel.remove();
}

function replaceWindow(t0, t1, base){
  var victims = state.doc.filter(function(e){ return intersects(e, t0, t1); });
  victims.forEach(removeEntry);
  insertEntry(Object.assign({t0: t0, t1: t1, speaker: null, translation: ''}, base));
}

function llmReplace(t0, t1, text, translation){   // 软替换:替换文本并附译文,保留位置与说话人
  var victims = state.doc.filter(function(e){ return intersects(e, t0, t1); });
  if (victims.length === 1){
    var e = victims[0];
    e.kind = 'llm'; e.text = text; e.translation = translation;
    if (e.el){ var fresh = makeEntryEl(e); e.el.replaceWith(fresh); e.el = fresh; }
    if (e.bel){ var bfresh = makeEntryEl(e); e.bel.replaceWith(bfresh); e.bel = bfresh; }
    autoScroll($('plist')); autoScroll($('barLog'));
    renderBar();
    return;
  }
  victims.forEach(removeEntry);
  var sp = victims.length ? victims[victims.length - 1].speaker : null;
  insertEntry({kind: 'llm', t0: t0, t1: t1, speaker: sp, text: text, translation: translation});
}

function makeEntryEl(entry){
  var div = document.createElement('div');
  div.className = 'e ' + entry.kind;
  if (entry.kind === 'skip'){
    div.innerHTML = '<span class="skiptxt">· 静音段已略过 ·</span>';
    return div;
  }
  var row = document.createElement('div'); row.className = 'row';
  var color = speakerColor(entry.speaker);
  var dot = document.createElement('i'); dot.className = 'dot';
  dot.style.background = color; row.appendChild(dot);
  var name = speakerName(entry.speaker);
  if (name){
    var who = document.createElement('b'); who.className = 'who';
    who.textContent = name; row.appendChild(who);
  }
  var txt = document.createElement('span'); txt.className = 'txt';
  txt.textContent = entry.text || '';
  if (entry.kind === 'p2' || entry.kind === 'llm') txt.style.color = color;
  row.appendChild(txt);
  div.appendChild(row);
  if (entry.translation){
    var tr = document.createElement('span'); tr.className = 'tr';
    tr.textContent = entry.translation; div.appendChild(tr);
  }
  return div;
}

function renderActive(){                          // partial 覆盖活动行(灰字+光标)
  if (state.paused) return;
  var has = !!state.partial;
  $('activeLine').hidden = !has;
  if (has){
    $('activeText').textContent = state.partial;
    $('plistEmpty').hidden = true;
  }
  autoScroll();
}

function renderBar(){                             // 字幕条 = 最近 1 条定稿 + 译文 + partial 灰行
  if (state.paused) return;
  var main = null, i = state.doc.length;
  while (i-- > 0){
    var k = state.doc[i].kind;
    if (k === 'p2' || k === 'llm'){ main = state.doc[i]; break; }
  }
  var fallback = null;
  if (!main) for (i = 0; i < state.doc.length; i++)
    if (state.doc[i].kind === 'f1') fallback = state.doc[i];
  var show = main || fallback;
  var bmain = $('barMain');
  bmain.classList.toggle('fallback', !main && !!fallback);
  if (show){
    $('barText').textContent = show.text || '…';
    var sp = main ? show.speaker : null;
    $('barDot').style.background = speakerColor(sp);
    var n = speakerName(sp);
    $('barWho').textContent = n; $('barWho').hidden = !n;
    var tr = show.translation;
    $('barTrans').textContent = tr || '';
    $('barTrans').hidden = !tr;
  } else {
    $('barText').textContent = '等待语音…';
    $('barDot').style.background = speakerColor(null);
    $('barWho').hidden = true;
    $('barTrans').hidden = true;
  }
  $('barPartial').textContent = state.partial;
  syncBarLog(show);                               // 历史区不重复展示当前主行
}

/* 历史区可见性:正在作为主行展示的条目在 log 里隐藏,避免同句上下重复 */
function syncBarLog(show){
  for (var i = 0; i < state.doc.length; i++){
    var e = state.doc[i];
    if (e.bel) e.bel.style.display = (e === show) ? 'none' : '';
  }
}

function fullRender(){                            // 暂停恢复/形态切换时整体重建
  state.doc.forEach(function(e){
    if (e.el) e.el.remove();
    if (e.bel) e.bel.remove();
    e.el = makeEntryEl(e);
    $('plist').insertBefore(e.el, $('activeLine'));
    e.bel = makeEntryEl(e);
    $('barLog').appendChild(e.bel);
  });
  $('barLog').hidden = state.doc.length === 0;
  $('plistEmpty').hidden = state.doc.length > 0;
  renderActive(); renderBar();
  autoScroll($('plist')); autoScroll($('barLog'));
}

function autoScroll(box){                         // 距底 ≤90px 时跟随新内容;用户上滚时不打扰
  box = box || $('plist');
  if (box.scrollHeight - box.scrollTop - box.clientHeight < 90)
    box.scrollTop = box.scrollHeight;
}

/* ---------- UI:计时/徽标/形态/拖动/暂停 ---------- */

function setPhase(p){
  state.phase = p;
  var stopping = p === 'stopping' || p === 'ended';
  $('panelStop').disabled = stopping;
  $('barStop').disabled = stopping;
}

function fmtDur(sec){
  sec = Math.max(0, Math.round(sec));
  var m = Math.floor(sec / 60), s = sec % 60;
  return m > 0 ? m + 'm' + (s < 10 ? '0' : '') + s + 's' : s + 's';
}

function startTimer(){
  stopTimer();
  var tick = function(){
    var t = Math.max(0, Math.floor((Date.now() - state.startedAt) / 1000));
    var h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), s = t % 60;
    var mm = (m < 10 ? '0' : '') + m, ss = (s < 10 ? '0' : '') + s;
    $('timer').textContent = h > 0 ? h + ':' + mm + ':' + ss : mm + ':' + ss;
  };
  tick();
  state.timerIv = setInterval(tick, 500);
}
function stopTimer(){ clearInterval(state.timerIv); state.timerIv = 0; }

var badgeTimers = {};
function badge(id){
  var el = $(id);
  el.classList.add('on');
  clearTimeout(badgeTimers[id]);
  badgeTimers[id] = setTimeout(function(){ el.classList.remove('on'); }, 2400);
}

function setChip(id, text, ttl){
  var el = $(id);
  el.textContent = text; el.hidden = false;
  barChip(text);
  clearTimeout(badgeTimers[id]);
  if (ttl) badgeTimers[id] = setTimeout(function(){ el.hidden = true; barChip(null); }, ttl);
}

function setMode(m){
  state.mode = m;
  $('bar').hidden = m !== 'bar';
  $('panel').hidden = m !== 'panel';
  localStorage.setItem('live.mode', m);
  if (!state.paused) fullRender();
}

function togglePause(){
  state.paused = !state.paused;
  $('barPause').textContent = state.paused ? '恢复显示' : '暂停显示';
  if (state.paused){
    var chip = document.createElement('span');
    chip.className = 'chip'; chip.id = 'pauseChip';
    chip.textContent = '字幕已暂停(仍在记录)';
    $('barState').appendChild(chip);
  } else {
    var pc = $('pauseChip'); if (pc) pc.remove();
    fullRender();
  }
}

function makeDraggable(handle, target, storeKey, defaultPos){
  var restore = localStorage.getItem(storeKey);
  var apply = function(x, y){
    var w = target.offsetWidth, h = target.offsetHeight;
    x = Math.min(Math.max(0, x), Math.max(0, window.innerWidth - w));
    y = Math.min(Math.max(0, y), Math.max(0, window.innerHeight - h));
    target.style.left = x + 'px'; target.style.top = y + 'px';
    localStorage.setItem(storeKey, Math.round(x) + ',' + Math.round(y));
  };
  if (restore){
    var p = restore.split(',');
    apply(parseFloat(p[0]), parseFloat(p[1]));
  } else {
    var d = defaultPos();
    target.style.left = d.x + 'px'; target.style.top = d.y + 'px';
  }
  handle.addEventListener('pointerdown', function(ev){
    if (ev.button !== 0) return;
    ev.preventDefault();
    var sx = ev.clientX, sy = ev.clientY;
    var r = target.getBoundingClientRect();
    var ox = r.left, oy = r.top;
    handle.setPointerCapture(ev.pointerId);
    var move = function(e2){ apply(ox + e2.clientX - sx, oy + e2.clientY - sy); };
    var up = function(e3){
      handle.releasePointerCapture(e3.pointerId);
      handle.removeEventListener('pointermove', move);
      handle.removeEventListener('pointerup', up);
    };
    handle.addEventListener('pointermove', move);
    handle.addEventListener('pointerup', up);
  });
}

/* ---------- 语言选择(识别语言 + 翻译目标,spec 2026-10-24) ---------- */

var LANG_OK = {zh: 1, en: 1, ja: 1};
var LANG_NAME = {zh: '中文', en: '英文', ja: '日文'};

function langFromStorage(){
  /* 存储仍是 'src>tgt' 对字符串(兼容旧值 zh>en / zh>zh 等):
     tgt==src 即「不翻译」;非法值回落默认 zh>en。 */
  var p = String(localStorage.getItem('live.lang') || 'zh>en').split('>');
  var src = LANG_OK[p[0]] ? p[0] : 'zh';
  var tgt = LANG_OK[p[1]] ? p[1] : 'en';
  return {src: src, tgt: tgt === src ? '' : tgt};
}

function restoreLangSelects(){
  var l = langFromStorage();
  $('fSrc').value = l.src;
  $('fTgt').value = l.tgt;
}

function currentLangSpec(){
  var src = $('fSrc').value || 'zh';
  var tgt = $('fTgt').value || src;   // 「不翻译」序列化为 tgt==src
  return src + '>' + tgt;
}

function langLabel(pairStr){
  var p = String(pairStr || '').split('>');
  var s = LANG_NAME[p[0]] || p[0], t = LANG_NAME[p[1]] || p[1];
  return p[1] === p[0] ? s + '(不翻译)' : s + ' → ' + t;
}

/* ---------- 停止(文案硬约束:按钮就叫「停止」) ---------- */

function requestStop(){
  if (state.phase !== 'live' && state.phase !== 'connecting') return;
  state.userStop = true;
  stopCapture();
  if (!state.sessionId){                          // 会话尚未建立:本地取消即可
    setPhase('ended');
    stopTimer();
    clearSessionStorage();
    $('endTitle').textContent = '已停止';
    $('endSub').textContent = '尚未建立会话,没有产生录音。';
    $('endLink').hidden = true;
    $('endwrap').hidden = false;
    return;
  }
  setPhase('stopping');
  flushBatch();
  var tail = mergeBatch();
  if (tail) sendFrame(tail);
  if (state.wsOpen && state.ws && state.ws.readyState === 1){
    try { state.ws.send(JSON.stringify({type: 'stop'})); } catch (e){}
    setChip('stopChip', '已按「停止」,正在收尾…');
  } else {
    setChip('stopChip', '连接中断,将在重连后停止');
    scheduleReconnect();
  }
}

function clearSessionStorage(){
  localStorage.removeItem('live.sessionId');
  localStorage.removeItem('live.startedAt');
}

/* ---------- 启动:表单 or 自动恢复上次会话 ---------- */

function enterLiveUI(){
  $('setup').hidden = true;
  $('stage').hidden = false;
  $('panelTitle').textContent = state.title || '实时会议';
  setMode(state.mode);
  startTimer();
}

function beginSession(){
  state.autoResume = false;
  state.title = ($('fTitle').value || '').trim();
  state.speakers = parseInt($('fSpeakers').value, 10) || 0;
  state.lang = currentLangSpec();
  localStorage.setItem('live.title', state.title);
  localStorage.setItem('live.speakers', String(state.speakers));
  localStorage.setItem('live.lang', state.lang);
  state.startedAt = Date.now();
  localStorage.setItem('live.startedAt', String(state.startedAt));
  setPhase('connecting');
  startCapture().then(function(){
    enterLiveUI();
    connect();
  }, function(err){
    $('setupErr').textContent = err.message || '无法开始';
    $('setupErr').hidden = false;
    $('goBtn').disabled = false;
    setPhase('idle');
  });
}

function tryAutoResume(){
  var saved = localStorage.getItem('live.sessionId');
  if (!saved) return false;
  $('setup').hidden = true;                       // 恢复期间先藏起表单,失败再放回
  state.sessionId = saved;
  state.autoResume = true;
  state.title = localStorage.getItem('live.title') || '实时会议';
  state.speakers = parseInt(localStorage.getItem('live.speakers') || '0', 10) || 0;
  state.lang = localStorage.getItem('live.lang') || 'zh>en';
  var sa = parseInt(localStorage.getItem('live.startedAt') || '0', 10);
  state.startedAt = sa > 0 ? sa : Date.now();
  setPhase('connecting');
  startCapture().then(function(){
    enterLiveUI();
    connect();                          // resume:重放 backlog(last_seq=0 → 全量重建历史)
  }, function(err){
    clearSessionStorage();
    $('setup').hidden = false;
    $('setupErr').textContent = '恢复上次会话失败:' + (err.message || '无法采集麦克风');
    $('setupErr').hidden = false;
    setPhase('idle');
  });
  return true;
}

function init(){
  var modeParam = new URLSearchParams(location.search).get('mode');
  state.mode = modeParam === 'panel' || modeParam === 'bar'
    ? modeParam
    : (localStorage.getItem('live.mode') === 'panel' ? 'panel' : 'bar');
  $('fTitle').value = localStorage.getItem('live.title') || '';
  restoreLangSelects();
  var sp = localStorage.getItem('live.speakers');
  if (sp != null && $('fSpeakers').querySelector('option[value="' + sp + '"]'))
    $('fSpeakers').value = sp;

  makeDraggable($('barGrab'), $('bar'), 'live.barPos', function(){
    var bar = $('bar');
    return {x: Math.max(8, (window.innerWidth - (bar.offsetWidth || 640)) / 2),
            y: window.innerHeight - (bar.offsetHeight || 90) - 84};
  });
  makeDraggable($('panelHead'), $('panel'), 'live.panelPos', function(){
    var p = $('panel');
    return {x: Math.max(8, (window.innerWidth - (p.offsetWidth || 800)) / 2),
            y: Math.max(8, (window.innerHeight - (p.offsetHeight || 560)) / 2)};
  });

  $('setupForm').addEventListener('submit', function(ev){
    ev.preventDefault();
    $('goBtn').disabled = true;
    $('setupErr').hidden = true;
    beginSession();
  });
  $('barExpand').addEventListener('click', function(){ setMode('panel'); });
  $('panelCollapse').addEventListener('click', function(){ setMode('bar'); });
  $('barPause').addEventListener('click', togglePause);
  $('barStop').addEventListener('click', requestStop);
  $('panelStop').addEventListener('click', requestStop);
  $('panelSettings').addEventListener('click', function(){
    var pop = $('settingsPop');
    if (pop.hidden){
      $('spTitle').textContent = state.title || '实时会议';
      $('spSpeakers').textContent = state.speakers > 0 ? state.speakers + ' 人' : '自动';
      $('spLang').textContent = langLabel(state.lang);
      $('spSession').textContent = state.sessionId || '尚未建立';
      $('spConn').textContent = state.phase === 'live'
        ? (state.wsOpen ? '已连接' : '重连中') : state.phase;
    }
    pop.hidden = !pop.hidden;
  });
  $('endNew').addEventListener('click', function(){ location.href = location.pathname; });
  // 关闭:桌面端隐藏悬浮窗(会话已结束,重开入口仍在主界面/Dock);网页端回工作台
  $('endClose').addEventListener('click', function(){
    if (window.desk && window.desk.closeLiveCaption){
      try{ window.desk.closeLiveCaption(); return; }catch(e){}
    }
    location.href = '/';
  });

  if (!tryAutoResume()){ $('setup').hidden = false; }
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
else init();
})();
