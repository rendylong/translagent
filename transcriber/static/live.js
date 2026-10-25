/* 实时字幕前端(Task 7):双形态 UI(半透明字幕条 ↔ 完整面板)+ /ws/live 客户端 +
   流式降采样 + 断线缓冲。独立页面,不依赖 recorder.js。

   与服务端实现(app.py ws_live / live_session.py / live_pass2.py)的对齐点:
   - started 应答无 seq;partial/pass1_final(字段 t0_ms/t1_ms)带单调 seq;
   - resume 无 resumed 应答:服务端直接重放 seq > last_seq 的 backlog 再继续实时流,
     故重连后按 seq 去重即可,首个下行消息可能就是重放事件;
   - audio_flush_done 上行消息当前被服务端忽略(前向兼容,补发完毕后照发);
   - stop → stop_ack →(引擎尾部冲刷的 pass1_final)→ stopped,随后服务端主动断开;
     stopped 自 Task 9 起携带 job_id(停止即建 job,可在工作台查看完整文稿);
     WP6 起 stopped 带 reason 分态:成功只有 job_id;reason='empty' 空会话(无音频);
     reason='finalize_failed' 文稿生成失败(另带 error 简要)——失败态经
     POST /api/live/recover_now 运行期寻回(返回 {recovered:[job_id,…]}),不必重启服务;
   - start 阶段失败:服务端回 {'type':'error','message':'无法开始实时会话:…'} 并断开,
     前端收到后停止自动重连、显示错误(onError),不再无限重试;
   - resume 冲突(另一窗口 resume 同一会话):服务端接管语义——旧连接先收
     {'type':'session_taken_over'} 即终态,前端停麦、清 session 存储、回表单态,
     绝不再自动重连(与 userStop 同级,防 A/B 双页 3 秒互踢死循环);
   - pass2 / pass2_skip / llm 事件字段名是 t0 / t1(与 live_pass2.py 一致)。

   文案硬约束:停止按钮的文案就是「停止」二字,不用任何替代表述。 */
(function(){
'use strict';

var TARGET_RATE = 16000;
var CHUNK_SAMPLES = 3200;                    // 200ms/包 @16k mono s16le
var BUFFER_MAX_SEC = 5 * 60;                 // 断线本地缓冲上限:5 分钟(超出丢最旧)
var BUFFER_MAX_BYTES = BUFFER_MAX_SEC * TARGET_RATE * 2;
var RECONNECT_MS = 3000;                     // 断线 3s 后自动重连 resume
var RECONNECT_MAX_TRIES = 10;                // 7.14:自动重连额度,超过即进终态等手动重试
var SPEAKER_COLORS = ['#4C9AFF', '#FFB02E', '#98A2B3'];   // A / B / 其余灰

var $ = function(id){ return document.getElementById(id); };

var state = {
  phase: 'idle',            // idle → connecting → live → stopping → ended
  ws: null, wsOpen: false,
  sessionId: null, lastSeq: 0,
  userStop: false,          // 已按「停止」;断线时挂起,重连后补发 stop
  reconnectTimer: 0,
  reconnectTries: 0,        // 7.14 自动重连计数:连接成功清零,>RECONNECT_MAX_TRIES 进终态
  reconnectFinal: false,    // 7.14 重连终态:已停止自动重连,等 chip 手动重试
  lock: null,               // WP3 单实例锁句柄('live-session';无 Web Locks 环境为空操作句柄)
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
  recoverBusy: false,       // WP6「立即重试转正」进行中(防连点,配合按钮 disabled)
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
    state.reconnectTries = 0;                     // 7.14:连上即清零计数,重连终态一并解除
    disarmReconnectChip();
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
  state.reconnectTries++;
  if (state.reconnectTries > RECONNECT_MAX_TRIES){   // 7.14:额度用尽,停止自动重连转终态
    enterReconnectFinal();
    return;
  }
  disarmReconnectChip();                    // 可能从终态返回(如停止补发重置计数后恢复自动重连)
  $('connChip').hidden = false;
  $('connChip').textContent = '连接中断 · 重连中';
  barChip('连接中断 · 重连中');
  clearTimeout(state.reconnectTimer);
  state.reconnectTimer = setTimeout(connect, RECONNECT_MS);
}

function enterReconnectFinal(){
  /* 7.14 重连终态:自动重连额度用尽(>10 次 ≈30s),停止打扰式重试,把恢复动作交给
     用户——connChip 升级为可点按钮(role=button + 键盘可达),点击手动 connect() 并
     清零计数;字幕条形态的 barChip 镜像同款可点(web 叠加态默认只见字幕条,不给它
     留死路)。会话未终了:断线缓冲与 sessionId 仍在,服务恢复后点 chip 即挂回。 */
  clearTimeout(state.reconnectTimer);
  if (state.reconnectFinal) return;         // 幂等:onclose 与 sendFrame 失败可能先后流入
  state.reconnectFinal = true;
  var chip = $('connChip');
  chip.hidden = false;
  chip.textContent = '无法连接转写服务,请确认服务已启动 · 点击重试';
  armChipButton(chip);
  var bchip = barChip('连接中断 · 已停止自动重连 · 点击重试');   // bar 形态镜像;同款可点(web 叠加态默认只见 bar)
  if (bchip) armChipButton(bchip);
}

function armChipButton(el){
  /* chip 升级按钮语义:role/tabindex + Enter/Space 激活。监听器只挂一次(__armed),
     role/class 可重复设置(disarm 后再进终态须恢复);manualReconnect 以
     reconnectFinal 旗标守卫,终态外点击是空操作。 */
  el.setAttribute('role', 'button');
  el.setAttribute('tabindex', '0');
  el.classList.add('clickable');
  if (el.__armed) return;
  el.__armed = true;
  el.addEventListener('click', manualReconnect);
  el.addEventListener('keydown', function(e){
    if (e.key === 'Enter' || e.key === ' '){ e.preventDefault(); manualReconnect(); }
  });
}

function disarmReconnectChip(){
  /* 退出重连终态:还原 chip 纯展示形态(重连成功/会话终态时调用),字幕条镜像
     一并退场。监听器不摘——以 reconnectFinal 旗标守卫,残留监听是空操作。 */
  state.reconnectFinal = false;
  var ids = ['connChip', 'barChip'];
  for (var i = 0; i < ids.length; i++){
    var el = $(ids[i]);
    if (!el) continue;
    el.classList.remove('clickable');
    el.removeAttribute('role');
    el.removeAttribute('tabindex');
  }
}

function manualReconnect(){
  /* 7.14 手动重试:仅重连终态且会话未终了(ended/idle)时生效;清零计数立即重连。
     终态后 stopped/taken_over/userStop 的收尾路径到此一律空操作(状态互斥)。 */
  if (!state.reconnectFinal || state.phase === 'ended' || state.phase === 'idle') return;
  state.reconnectFinal = false;
  state.reconnectTries = 0;
  $('connChip').textContent = '连接中断 · 重连中';   // 即时反馈:终态文案让位于重连进行时
  barChip('连接中断 · 重连中');
  connect();
}

function barChip(text){                       // 状态在字幕条形态下的镜像(字幕条自身无徽标区)
  var el = $('barChip');
  if (!text){ if (el) el.remove(); return null; }
  if (!el){
    el = document.createElement('span');
    el.className = 'chip'; el.id = 'barChip';
    $('barState').appendChild(el);
  }
  el.textContent = text;
  return el;
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
      // 7.8:无修正会话(无密钥或本就未选翻译)pass1 句即定稿——开始判一次,会话中途不改
      applyLlmOff(ev.llm === false || langPairIsNoTranslate(state.lang));
      break;
    case 'partial':
      state.partial = ev.text || '';
      badge('bdgAsr');
      renderActive();
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
    case 'session_taken_over':                // WP3:会话被另一窗口 resume 接管,本页即终态
      onTakenOver();
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
      releaseLiveLock();                           // 会话已死,锁立即放行其他窗口
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
    releaseLiveLock();
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
    releaseLiveLock();
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
  releaseLiveLock();
  try { if (state.ws) state.ws.close(); } catch (e){}   // 正常路径服务端先断;此处兜底
  disarmReconnectChip();
  $('stopChip').hidden = true;
  $('connChip').hidden = true;
  barChip(null);
  /* WP6 三分态:成功带 job_id(无 reason);空会话 reason='empty';其余(含
     finalize_failed)落失败态。旧服务端 stopped 无 reason 字段、两键皆无——
     保守按失败态渲染给寻回文案,绝不误报「没有录到语音」。 */
  if (ev.job_id){
    showEndSuccess(ev.job_id);
  } else if (ev.reason === 'empty'){
    $('endRetry').hidden = true;
    $('endRetryNote').hidden = true;
    $('endTitle').textContent = '本场没有录到语音';
    $('endSub').textContent = '会话已结束,未生成文稿。';
    $('endLink').hidden = true;
    $('endwrap').hidden = false;
  } else {
    showEndFailure(ev.error);
  }
}

function showEndSuccess(jobId){
  /* 成功态(onStopped 成功分支与「立即重试转正」寻回成功共用,WP6):标题「文稿已生成」,
     endLink 升主按钮(.elink.primary,复用 .ego 主按钮视觉),「开始新会话」降 ghost。
     深链:web 开新标签(本页停在结束卡,/live 内导航会开出双工作台);桌面悬浮窗内
     聚焦主窗跳转并隐藏悬浮窗(路径不变)。 */
  $('endRetry').hidden = true;
  $('endRetryNote').hidden = true;
  $('endTitle').textContent = '文稿已生成';
  $('endSub').textContent = '文稿已存入工作台历史记录(带「实时」标记)。';
  var link = $('endLink');
  link.hidden = false;
  link.classList.add('primary');
  link.href = '/#' + jobId;
  link.onclick = function(e){
    e.preventDefault();
    if (window.desk && window.desk.openMainJob){    // 悬浮窗内:聚焦主窗跳转(使命已完成)
      window.desk.openMainJob(jobId);
      if (window.desk.closeLiveCaption) window.desk.closeLiveCaption();
      return;
    }
    window.open('/#' + jobId, '_blank');            // web:新标签打开工作台文稿
  };
  $('endNew').classList.add('ghost');
  $('endwrap').hidden = false;
}

function showEndFailure(errorBrief){
  /* 失败态(finalize_failed,或旧服务端无 reason 无 job_id 的兜底):error 为服务端
     打码简要,缺省只给寻回文案。「立即重试转正」在此态亮出,详见 retryFinalize。 */
  $('endRetry').hidden = false;
  $('endRetry').disabled = false;
  $('endRetry').textContent = '立即重试转正';
  $('endRetryNote').hidden = true;
  $('endTitle').textContent = '文稿生成失败,录音已保留';
  $('endSub').textContent = (errorBrief ? '原因:' + errorBrief + '。' : '')
    + '录音仍在服务端,重启转写服务后会自动转为任务(历史记录带「实时」标记)。';
  $('endLink').hidden = true;
  $('endwrap').hidden = false;
}

function retryFinalize(){
  /* 「立即重试转正」(WP6-4):运行期调 /api/live/recover_now 寻回失败录音,不必
     重启服务。有新 job → 原位转成功态(showEndSuccess,深链可用);空数组/请求失败
     → 就地提示(endRetryNote),保持失败态,不弹整卡错误。recoverBusy + disabled
     双保险防连点(recover_now 会扫全部死目录,连点必然重复等待)。 */
  if (state.recoverBusy) return;
  state.recoverBusy = true;
  var btn = $('endRetry');
  btn.disabled = true;
  btn.textContent = '正在寻回…';
  $('endRetryNote').hidden = true;
  fetch('/api/live/recover_now', {method: 'POST'}).then(function(r){
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  }).then(function(data){
    var ids = (data && data.recovered) || [];
    if (!ids.length){
      var err = new Error('暂未寻回到可转为任务的录音,可稍后重试或重启转写服务。');
      err.plain = true;                             // 可读话术直接上屏,不加「重试失败:」前缀
      throw err;
    }
    showEndSuccess(ids[0]);
  }).catch(function(e){
    btn.disabled = false;                           // 保持失败态,按钮还原可再点
    btn.textContent = '立即重试转正';
    $('endRetryNote').textContent = (e && e.plain)
      ? e.message
      : '重试失败:' + ((e && e.message) || '未知错误');
    $('endRetryNote').hidden = false;
  }).then(function(){
    state.recoverBusy = false;                      // 成功/失败都清旗标(catch 后必经)
  });
}

function onTakenOver(){
  /* WP3 终态(与 userStop 同级):本页会话已被另一窗口的 resume 接管。服务端发完
     本消息立即关连接,onclose 随后必至——先把相位置 ended(onclose 的重连分支
     短路),再 clearTimeout 兜底已排队的重连,杜绝 A/B 双页 3 秒互踢死循环。
     session 存储必须清:localStorage 同源双页共享,留着只会让下次打开又去 resume
     抢回;锁同步释放,本页与新窗口各归其位。 */
  setPhase('ended');
  state.sessionId = null;                 // 复位页内单会话状态(对照 autoResume 死会话分支):
  state.userStop = false;                 // goBtn 已重启允许不刷新直接重开——sessionId 残留会让
  state.lastSeq = 0;                      // 重开误发 resume 死会话,stopping 相位被接管还叠加
  state.reconnectTries = 0;               // 误发 resume+stop,lastSeq 不清新会话小 seq 被去重全吞;
  stopCapture();                          // 重连状态机(计数+终态旗标)随会话一并归零
  stopTimer();
  clearTimeout(state.reconnectTimer);
  clearSessionStorage();
  releaseLiveLock();
  try { if (state.ws) state.ws.close(); } catch (e){}   // 服务端马上会断;此处先断防中间态事件
  disarmReconnectChip();
  $('stopChip').hidden = true;
  $('connChip').hidden = true;
  barChip(null);
  $('stage').hidden = true;
  $('endwrap').hidden = true;
  $('setupErr').hidden = true;
  $('goBtn').disabled = false;                  // 回表单须可直接再开始(goBtn 在 submit 时被禁用)
  $('setup').hidden = false;
  $('resumeNote').textContent = '会话已在其他窗口打开。';
  $('resumeNote').hidden = false;
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

/* ---------- 三级渲染:partial 覆盖活动行 / pass1_final 追加 / pass2 整窗替换染色 ----------
   字幕条侧 bel 是「句子账本」:一句一行、译文跟句、partial 灰行常驻最底(面板 el 维持窗口条目式)。 */

function speakerColor(speaker){
  return SPEAKER_COLORS[speaker != null && speaker < SPEAKER_COLORS.length - 1 ? speaker
                        : SPEAKER_COLORS.length - 1];
}
function speakerName(speaker){
  return speaker == null ? '' : '说话人' + (speaker + 1);
}

function intersects(e, t0, t1){ return e.kind !== 'skip' && e.t0 < t1 && e.t1 > t0; }

/* 断句:原文与译文共用。协议里没有句边界,只能客户端按终符切;原则是宁长勿错——
   小数、缩写、小写续写都不切(切错比不切伤害大),超长无终句靠 CSS 换行兜底。 */
function splitSentences(text){
  text = String(text == null ? '' : text)
    .replace(/[ \t\r　]+/g, ' ')            // 空白串折叠为单空格(\n 是终符,留待切分)
    .replace(/^\s+|\s+$/g, '');
  if (!text) return [];
  // 终符集码点:U+3002 。 U+002E . U+0021 ! U+FF01 ! U+003F ? U+FF1F ? U+2026 …
  //             U+003B ; U+FF1B ; U+000A 换行(中日文句读与 ASCII 全半角成对,防再漏)
  var END = '。.!！?？…;；\n';
  var ABBR = {Mr:1, Dr:1, Ms:1, Mrs:1, Jr:1, Sr:1, vs:1, etc:1,
              'e.g':1, 'i.e':1, Inc:1, Ltd:1};
  var out = [], start = 0;
  for (var i = 0; i < text.length; i++){
    var ch = text.charAt(i);
    if (END.indexOf(ch) < 0) continue;
    if (ch === '.'){                          // ASCII 句点三重守卫:命中则当普通字符继续累积
      var prev = text.charAt(i - 1), next = text.charAt(i + 1);
      if (prev >= '0' && prev <= '9' && next >= '0' && next <= '9') continue;   // 3.2 / 12.50
      var stem = /([A-Za-z.]+)$/.exec(text.slice(0, i));
      if (stem && ABBR[stem[1]]) continue;    // Mr. / e.g. 等缩写词尾
      if (next >= 'a' && next <= 'z') continue;   // vs. the:句点后小写续写多半非句界
    }
    var j = i;                                // 连续终符(好!!!/。。。/……)并入前一句,不产孤标点行
    while (j + 1 < text.length && END.indexOf(text.charAt(j + 1)) >= 0) j++;
    var sen = text.slice(start, j + 1).replace(/^\s+|\s+$/g, '');
    if (sen) out.push(sen);
    i = j; start = j + 1;
  }
  if (start < text.length){
    var tail = text.slice(start).replace(/^\s+|\s+$/g, '');   // 无终符尾巴自成一行(pass1 裸文本即此类)
    if (tail) out.push(tail);
  }
  return out;
}

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
    entry.bel = makeBarEl(entry);               // 账本条目同源镜像(一句一行,样式由 .blog 作用域)
    $('barLog').insertBefore(entry.bel, next && next.bel ? next.bel : $('barEmpty'));
    $('barEmpty').hidden = true;                // 首个 entry 到达,空态退场(partial 到达在 renderActive 里退)
    autoScroll($('plist')); autoScroll($('barLog'));
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
    if (e.bel){
      var bfresh = makeBarEl(e);               // 句数/译文变化自然重排
      bfresh.classList.add('no-anim');         // 重建不眨眼:历史句行不集体重播入场动画
      e.bel.replaceWith(bfresh); e.bel = bfresh;
    }
    autoScroll($('plist')); autoScroll($('barLog'));
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

/* 账本条目的说话人标记:仅切换首句出圆点+名。pass1 无 speaker 从不标记;
   参照是紧邻前条——遇 skip 即重置(下一窗口必出点),无前驱视为切换(会话第一条)。 */
function barSpeakerMark(entry){
  if (entry.speaker == null) return false;
  var i = state.doc.indexOf(entry);
  while (i-- > 0){
    var d = state.doc[i];
    if (d.kind === 'skip') return true;
    return d.speaker !== entry.speaker;
  }
  return true;
}

function makeBarEl(entry){                       // 字幕条侧条目容器:句行(srow)+ 整块译文/静音行
  var div = document.createElement('div');
  div.className = 'e ' + entry.kind;
  if (entry.kind === 'skip'){
    var sk = document.createElement('div');
    sk.className = 'skipline';
    sk.textContent = '· 静音段已略过 ·';
    div.appendChild(sk);
    return div;
  }
  var sents = splitSentences(entry.text);
  if (!sents.length) sents = [''];               // 空文本条目保一行,容器在位重排才稳定
  var trs = entry.translation ? splitSentences(entry.translation) : [];
  var per = trs.length === sents.length;        // 句数一致才逐句跟,不猜对齐(sents 已兜底非空)
  var mark = barSpeakerMark(entry);
  var color = speakerColor(entry.speaker);
  for (var i = 0; i < sents.length; i++){
    var row = document.createElement('div');
    row.className = 'srow ' + entry.kind;
    var dot = document.createElement('i');
    if (i === 0 && mark){ dot.className = 'sdot'; dot.style.background = color; }
    else { dot.className = 'sdot sdot-none'; }  // 其余句透明占位,正文缩进不跳
    row.appendChild(dot);
    var stx = document.createElement('div');
    stx.className = 'stx';
    if (i === 0 && mark){
      var who = document.createElement('b');     // 名字与圆点同条件同现
      who.className = 'swho';
      who.textContent = speakerName(entry.speaker);
      stx.appendChild(who);
      stx.appendChild(document.createTextNode(' '));
    }
    stx.appendChild(document.createTextNode(sents[i]));
    if (per){
      var str = document.createElement('span');  // 跟句小字译文
      str.className = 'str';
      str.textContent = trs[i];
      stx.appendChild(str);
    }
    row.appendChild(stx);
    div.appendChild(row);
  }
  if (!per && trs.length){                       // 句数不等:整窗译文整块回落(混排两形态是预期行为)
    var blk = document.createElement('div');
    blk.className = 'trblk';
    blk.textContent = entry.translation;
    div.appendChild(blk);
  }
  return div;
}

function renderActive(){                          // partial 行(面板灰字 + 账本常驻最底灰行)+ 空态显隐
  if (state.paused) return;
  var has = !!state.partial;
  $('activeLine').hidden = !has;
  if (has){
    $('activeText').textContent = state.partial;
    $('plistEmpty').hidden = true;
  }
  $('barPartial').textContent = state.partial;   // #barPartialRow 行常驻最底不隐藏(caret 常闪表识别进行中),只换文本
  $('barEmpty').hidden = has || state.doc.length > 0;   // doc 与 partial 均空才显示「等待语音…」
  autoScroll(); autoScroll($('barLog'));
}

function fullRender(){                            // 暂停恢复/形态切换时整体重建
  state.doc.forEach(function(e){
    if (e.el) e.el.remove();
    if (e.bel) e.bel.remove();
    e.el = makeEntryEl(e);
    $('plist').insertBefore(e.el, $('activeLine'));
    e.bel = makeBarEl(e);
    e.bel.classList.add('no-anim');               // 全量重建非新增条目,不重播入场动画
    $('barLog').insertBefore(e.bel, $('barEmpty'));   // 静态行(bspace/空态/partial)永不 remove
  });
  $('plistEmpty').hidden = state.doc.length > 0;
  renderActive();                                 // 空态恢复/最新 partial 文本由此补齐
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
  /* 存储仍是 'src>tgt' 对字符串(兼容旧值 zh>zh 等):tgt==src 即「不翻译」。
     旧默认 'zh>en' 在此一次性迁移清除(WP4,见 migrateLegacyLang);恰为旧默认的
     返回值不得沿用(迁移的立意就是让旧默认失效),与无值/非法一样按新默认
     「不翻译」回落(原回落 'en' 已废);其余合法值不动。 */
  var raw = migrateLegacyLang();
  var p = String(raw === LEGACY_DEFAULT_LANG ? '' : (raw || '')).split('>');
  var src = LANG_OK[p[0]] ? p[0] : 'zh';
  var tgt = LANG_OK[p[1]] ? p[1] : src;
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

/* ---------- WP4 隐私:note 动态化 + 旧默认迁移 + 7.8 llm-off ---------- */

var LEGACY_DEFAULT_LANG = 'zh>en';   // 旧版翻译默认;恰为此值即视为未显式选择(见迁移)
var NOTE_LOCAL = '识别与说话人分离即时可用,全程在本机完成;修正与翻译事件到达时自动上屏。';
var NOTE_REMOTE = '识别与说话人分离在本机完成;修正与翻译会把窗口文本(不含音频、路径、人名)发送至外部 LLM 服务。';

function updateLangNote(){
  /* tgt≠src 才外发:note 即时切换外发告知。fSrc 变化同样会翻转 tgt==src 语义
     (如 tgt=en 时把 src 改成 en),两处 change 都监听。 */
  var src = $('fSrc').value || 'zh';
  var tgt = $('fTgt').value;
  $('langNote').textContent = (tgt && tgt !== src) ? NOTE_REMOTE : NOTE_LOCAL;
}

function migrateLegacyLang(){
  /* 旧默认一次性迁移(WP4):存量机器的 live.lang 恰为 'zh>en'(旧默认,无法区分
     是否显式选过)时清除该键,让新默认「不翻译」生效——显式选过 zh>en 的用户被
     重置一次,隐私收益优先;其余合法值不动。返回清除前读到的原始值:resume 路径
     用它保持本次会话的展示语言不被迁移改写(真实会话语言在服务端,resume 不发 start)。 */
  var raw = localStorage.getItem('live.lang');
  if (raw === LEGACY_DEFAULT_LANG) localStorage.removeItem('live.lang');
  return raw;
}

function langPairIsNoTranslate(pairStr){
  var p = String(pairStr || '').split('>');
  return p.length === 2 && p[0] !== '' && p[0] === p[1];
}

function applyLlmOff(off){
  /* 7.8 f1 升白:无修正会话(服务端无密钥,或本就未选翻译)里 pass1 句上屏即定稿,
     f1 灰层不再承担「待修正」语义 → 根节点 llm-off 让 f1 直接升定稿色(译文层不动)。
     开始时判一次;页面单会话生命周期(endNew 走整页刷新),无需对称卸除。 */
  document.documentElement.classList.toggle('llm-off', !!off);
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
    releaseLiveLock();                            // 从未挂上会话:锁立即放行其他窗口
    clearTimeout(state.reconnectTimer);           // 重连终态下按停止:终态连同 chip 一并收掉
    disarmReconnectChip();
    $('connChip').hidden = true;
    barChip(null);
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
    state.reconnectTries = 0;                     // 停止补发优先:重置计数,不被旧额度干扰(7.14 互斥)
    scheduleReconnect();
  }
}

function clearSessionStorage(){
  localStorage.removeItem('live.sessionId');
  localStorage.removeItem('live.startedAt');
}

/* ---------- WP3 单实例锁:同一浏览器同时只允许一个 /live 会话页 ---------- */

var LIVE_LOCK_NAME = 'live-session';
var LOCK_BUSY_MSG = '字幕会话已在其他窗口进行';

function acquireLiveLock(){
  /* 返回 Promise:resolve 锁句柄 {release()},或 null(锁被占/请求异常)。
     Web Locks 的 request 是回调式持锁——callback 返回的 promise settle 之前锁被
     持有,故把 settle 权交进句柄:会话终态(ended/出错)时 release() 放锁;页面
     隐藏、断线重连中不算结束,不放锁。'locks' in navigator 为 false 的环境降级
     无锁(句柄为空操作;双开场景由服务端 resume 接管守卫兜底,旧页会收到
     session_taken_over 终态)。 */
  return new Promise(function(resolve){
    if (!('locks' in navigator)){
      resolve({release: function(){}});          // 降级无锁:服务端守卫兜底
      return;
    }
    var granted = false;
    navigator.locks.request(LIVE_LOCK_NAME, {ifAvailable: true}, function(){
      granted = true;
      return new Promise(function(holdLock){     // holdLock 调用前锁保持
        resolve({release: function(){ holdLock(); }});
      });
    }).then(function(){
      if (!granted) resolve(null);               // ifAvailable:锁被占 → callback 不执行,请求照常 resolve
    }, function(){
      if (!granted) resolve(null);               // request 本身异常:保守按锁不可得处理
    });
  });
}

function releaseLiveLock(){
  var h = state.lock;
  state.lock = null;                             // 先清再放:重复调用安全
  if (h) h.release();
}

function lockBusyToForm(fromResume){
  /* 锁不可得:回表单态提示(与 autoResume 失败提示同槽位),绝不触发 getUserMedia
     ——麦克风不该让两页互抢。resume 侧只清本地 state.sessionId、绝不动 localStorage:
     会话属于持锁窗口,清了存储反而让第三个窗口丢掉 resume 线索。 */
  setPhase('idle');
  state.autoResume = false;
  if (fromResume){
    state.sessionId = null;                      // 不清则表单再点「开始」会误发 resume 抢别人的会话
    $('setup').hidden = false;
  }
  $('goBtn').disabled = false;
  var note = fromResume ? $('resumeNote') : $('setupErr');
  note.textContent = LOCK_BUSY_MSG;
  note.hidden = false;
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
  state.reconnectTries = 0;                       // 页内重开:上一场残留的重连计数/终态清零(7.14)
  disarmReconnectChip();
  setPhase('connecting');
  acquireLiveLock().then(function(handle){        // WP3:先过锁闸,占用路径不碰 getUserMedia
    if (!handle){
      lockBusyToForm(false);
      return;
    }
    state.lock = handle;
    // 共享键在锁到手后才写:占用路径不得污染 localStorage(第三窗口 autoResume
    // 会取到一次失败尝试的 startedAt)
    state.startedAt = Date.now();
    localStorage.setItem('live.title', state.title);
    localStorage.setItem('live.speakers', String(state.speakers));
    localStorage.setItem('live.lang', state.lang);
    localStorage.setItem('live.startedAt', String(state.startedAt));
    startCapture().then(function(){
      enterLiveUI();
      connect();
    }, function(err){
      releaseLiveLock();                         // 采集失败会话未成立:立即放锁,页内重试不被自己挡
      $('setupErr').textContent = err.message || '无法开始';
      $('setupErr').hidden = false;
      $('goBtn').disabled = false;
      setPhase('idle');
    });
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
  var savedLang = migrateLegacyLang();     // resume 路径同样过迁移(键清除);原值仍用于本次会话
  state.lang = savedLang || 'zh>zh';       // resume 无 start 上行:真实语言在服务端,此处仅供
                                           // 设置弹层展示与 llm-off 判断(旧回落 'zh>en' 已废)
  var sa = parseInt(localStorage.getItem('live.startedAt') || '0', 10);
  state.startedAt = sa > 0 ? sa : Date.now();
  // resume 无 started 应答(llm 旗标拿不到):按恢复出的语言对判——不翻译会话升白;
  // 曾选翻译的维持灰→白渐进(原始 llm 状态未持久化,backlog 重放也不携带,无从恢复)
  applyLlmOff(langPairIsNoTranslate(state.lang));
  setPhase('connecting');
  acquireLiveLock().then(function(handle){       // WP3:另一窗口恢复/直播中 → 提示回表单
    if (!handle){
      lockBusyToForm(true);
      return;
    }
    state.lock = handle;
    startCapture().then(function(){
      enterLiveUI();
      connect();                          // resume:重放 backlog(last_seq=0 → 全量重建历史)
    }, function(err){
      releaseLiveLock();
      clearSessionStorage();
      $('setup').hidden = false;
      $('setupErr').textContent = '恢复上次会话失败:' + (err.message || '无法采集麦克风');
      $('setupErr').hidden = false;
      setPhase('idle');
    });
  });
  return true;
}

function init(){
  var modeParam = new URLSearchParams(location.search).get('mode');
  state.mode = modeParam === 'panel' || modeParam === 'bar'
    ? modeParam
    : (localStorage.getItem('live.mode') === 'panel' ? 'panel' : 'bar');

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
  $('fTgt').addEventListener('change', updateLangNote);
  $('fSrc').addEventListener('change', updateLangNote);   // src 变化同样翻转 tgt==src 语义
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
  $('endRetry').addEventListener('click', retryFinalize);   // WP6 失败态寻回(态机见 retryFinalize)
  // 关闭:桌面端隐藏悬浮窗(会话已结束,重开入口仍在主界面/Dock);网页端回工作台
  $('endClose').addEventListener('click', function(){
    if (window.desk && window.desk.closeLiveCaption){
      try{ window.desk.closeLiveCaption(); return; }catch(e){}
    }
    location.href = '/';
  });

  // 7.3:进行中(含连接中/收尾中)关标签页须确认;ended/idle 不拦。桌面悬浮窗形态
  //(window.desk)跳过——主进程已有关窗拦截,双拦会弹两次原生确认。
  window.addEventListener('beforeunload', function(e){
    if (window.desk) return;
    if (state.phase !== 'connecting' && state.phase !== 'live' && state.phase !== 'stopping') return;
    e.preventDefault();
    e.returnValue = '';
  });

  // 先判 resume 再恢复表单:tryAutoResume 必须在表单路径迁移之前拿到 live.lang 原始值
  //(langFromStorage 的迁移会清掉旧默认键,顺序反了会改写本次会话的展示语言)
  var resumed = tryAutoResume();
  $('fTitle').value = localStorage.getItem('live.title') || '';
  restoreLangSelects();               // 表单路径迁移在此发生;resume 失败回表单时已是新默认
  var sp = localStorage.getItem('live.speakers');
  if (sp != null && $('fSpeakers').querySelector('option[value="' + sp + '"]'))
    $('fSpeakers').value = sp;
  updateLangNote();                   // 初始 note 与恢复出的语言对一致(默认不翻译→无外发字样)
  if (!resumed){ $('setup').hidden = false; }
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
else init();
})();
