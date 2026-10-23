/* 现场录音:把浏览器麦克风/系统声音采成 16kHz 单声道 PCM16 WAV,并上传本地转录服务。
   自包含、零依赖,不引用页面任何全局;唯一出口 window.MeetRecorder = {Recorder, uploadRecording}。
   Recorder 状态机:idle → arming → recording → stopping → idle(产出 Blob)或 error(中文 .error)。
   一段录音 = { source: 'mic'|'system'|'system+mic', chunks: Int16Array[], sampleRate, startedAt }。 */
(function(){
'use strict';

var TARGET_RATE = 16000;
var SOURCES = ['mic', 'system', 'system+mic'];

/* AudioWorklet 处理器源码,经 Blob URL 内联加载;任意声道平均为单声道,每 1024 帧回传 Float32Array。 */
var WORKLET_SRC = [
'class PCMCollector extends AudioWorkletProcessor{',
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
'registerProcessor("pcm-collector", PCMCollector);'
].join('\n');

function errMsg(e, fallback){
  return e && e.message ? e.message : fallback;
}

/* 44 字节标准头、format tag 1 的单声道 16kHz PCM16 WAV */
function buildWav(chunks){
  var frames = 0;
  for(var i = 0; i < chunks.length; i++) frames += chunks[i].length;
  var buf = new ArrayBuffer(44 + frames * 2);
  var v = new DataView(buf);
  var wstr = function(off, s){ for(var i = 0; i < s.length; i++) v.setUint8(off + i, s.charCodeAt(i)); };
  wstr(0, 'RIFF'); v.setUint32(4, 36 + frames * 2, true); wstr(8, 'WAVE');
  wstr(12, 'fmt '); v.setUint32(16, 16, true); v.setUint16(20, 1, true);
  v.setUint16(22, 1, true); v.setUint32(24, TARGET_RATE, true);
  v.setUint32(28, TARGET_RATE * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true);
  wstr(36, 'data'); v.setUint32(40, frames * 2, true);
  var pcm = new Int16Array(buf, 44, frames);
  var o = 0;
  for(var c = 0; c < chunks.length; c++){ pcm.set(chunks[c], o); o += chunks[c].length; }
  return new Blob([buf], {type: 'audio/wav'});
}

/* 线性插值降采样:单遍流式、恒定内存。chunks 为捕获率 PCM16,输出 16kHz。 */
function resampleTo16k(chunks, from){
  var total = 0;
  for(var i = 0; i < chunks.length; i++) total += chunks[i].length;
  if(total < 2) return chunks;
  var step = from / TARGET_RATE;
  var outLen = Math.floor((total - 1) / step) + 1;
  var out = new Int16Array(outLen);
  out[0] = chunks[0][0];
  var prev = out[0], j = 1, gi = -1;
  for(i = 0; i < chunks.length && j < outLen; i++){
    var c = chunks[i];
    for(var k = 0; k < c.length && j < outLen; k++){
      var s = c[k];
      gi++;                      // s 的全局采样索引(0 基),插值区间为 [gi-1, gi]
      while(j < outLen && j * step <= gi){
        out[j] = Math.round(prev + (s - prev) * (j * step - (gi - 1)));
        j++;
      }
      prev = s;
    }
  }
  return [out];
}

class Recorder{
  constructor(opt){
    opt = opt || {};
    this.source = opt.source || 'mic';
    if(SOURCES.indexOf(this.source) < 0) throw new Error('未知的录音源:' + this.source);
    this.onLevel = typeof opt.onLevel === 'function' ? opt.onLevel : null;
    this._state = 'idle';
    this._error = null;
    this._chunks = null;      // Int16Array[],null 表示不在采集
    this._rate = TARGET_RATE; // 实际捕获率,stop 时非 16k 则降采样
    this._ctx = null;
    this._streams = [];
    this._src = null; this._node = null; this._mute = null;
    this._startedAt = 0;
    this._lvl = 0; this._lvlT = 0;
  }

  get state(){ return this._state; }
  get error(){ return this._error; }
  get elapsed(){
    if(!this._startedAt || (this._state !== 'recording' && this._state !== 'stopping')) return 0;
    return Date.now() - this._startedAt;
  }

  async start(){
    if(this._state === 'arming' || this._state === 'recording') throw new Error('录音已在进行');
    this._state = 'arming';
    this._error = null;
    this._chunks = [];
    try{
      const AC = window.AudioContext || window.webkitAudioContext;
      if(!AC) throw new Error('当前浏览器不支持 Web Audio,无法录音');
      let ctx;
      try{ ctx = new AC({sampleRate: TARGET_RATE}); }
      catch(e){ ctx = new AC(); }
      this._ctx = ctx;
      this._rate = ctx.sampleRate || TARGET_RATE;

      const parts = this._streams;   // 部分成功取流也要能被 _release 清理
      const needMic = this.source === 'mic' || this.source === 'system+mic';
      const needSys = this.source === 'system' || this.source === 'system+mic';
      if(needMic) parts.push(await this._micStream());
      if(needSys) parts.push(await this._systemStream());

      let tap = parts[0];
      if(parts.length > 1){
        const mix = ctx.createMediaStreamDestination();
        for(let i = 0; i < parts.length; i++) ctx.createMediaStreamSource(parts[i]).connect(mix);
        tap = mix.stream;
      }
      const src = ctx.createMediaStreamSource(tap);
      this._src = src;
      const mute = ctx.createGain();
      mute.gain.value = 0;            // 静音拉流终点,防回授
      mute.connect(ctx.destination);
      this._mute = mute;

      let wired = false;
      if(ctx.audioWorklet && ctx.audioWorklet.addModule){
        try{
          const url = URL.createObjectURL(new Blob([WORKLET_SRC], {type: 'text/javascript'}));
          await ctx.audioWorklet.addModule(url);
          URL.revokeObjectURL(url);
          const node = new AudioWorkletNode(ctx, 'pcm-collector');
          node.port.onmessage = ev => this._ingest(ev.data);
          src.connect(node);
          node.connect(mute);
          this._node = node;
          wired = true;
        }catch(e){ /* 回退 ScriptProcessorNode */ }
      }
      if(!wired){
        const spn = ctx.createScriptProcessor(2048, 1, 1);
        if(!spn) throw new Error('当前浏览器无法采集音频');
        spn.onaudioprocess = ev => this._ingest(ev.inputBuffer.getChannelData(0));
        src.connect(spn);
        spn.connect(mute);
        this._node = spn;
      }
      if(ctx.state === 'suspended') await ctx.resume();
      this._startedAt = Date.now();
      this._state = 'recording';
    }catch(e){
      const m = errMsg(e, '无法开始录音');
      await this._release();
      this._state = 'error';
      this._error = m;
      throw new Error(m);
    }
  }

  async _micStream(){
    const md = navigator.mediaDevices;
    if(!md || !md.getUserMedia) throw new Error('当前浏览器不支持麦克风采集');
    try{
      return await md.getUserMedia({audio: {echoCancellation: false, noiseSuppression: false, autoGainControl: false}});
    }catch(e){
      if(e && (e.name === 'NotAllowedError' || e.name === 'SecurityError' || e.name === 'PermissionDeniedError'))
        throw new Error('麦克风权限被拒绝,请在系统或浏览器设置中允许后重试');
      throw new Error('无法打开麦克风:' + errMsg(e, '未知错误'));
    }
  }

  async _systemStream(){
    const md = navigator.mediaDevices;
    if(!md || !md.getDisplayMedia) throw new Error('当前环境不支持系统声音采集');
    let s;
    try{
      // 纯音频请求:走 Core Audio Tap,只碰「系统声音录制」权限,不经屏幕录制
      s = await md.getDisplayMedia({audio: true, video: false});
    }catch(e){
      if(e && (e.name === 'NotAllowedError' || e.name === 'SecurityError' || e.name === 'PermissionDeniedError'))
        throw new Error('未获得系统声音权限,或已取消采集');
      throw new Error('无法获取系统声音:' + errMsg(e, '未知错误'));
    }
    const vids = s.getVideoTracks();
    for(let i = 0; i < vids.length; i++) vids[i].stop();   // 只留音频轨
    if(!s.getAudioTracks().length){
      s.getTracks().forEach(t => t.stop());
      throw new Error('未捕获到系统声音,请重试或在系统设置中允许「系统声音录制」');
    }
    s.getAudioTracks()[0].addEventListener('ended', () => {
      if(this._state === 'recording'){
        this._state = 'error';
        this._error = '系统声音共享已结束,录音中断';
      }
    });
    return s;
  }

  _ingest(f32){
    if(!this._chunks || !f32 || !f32.length) return;
    const out = new Int16Array(f32.length);
    let sq = 0;
    for(let i = 0; i < f32.length; i++){
      let x = f32[i];
      if(x > 1) x = 1; else if(x < -1) x = -1;
      out[i] = Math.round(x < 0 ? x * 32768 : x * 32767);
      sq += x * x;
    }
    this._chunks.push(out);
    if(this.onLevel){
      const rms = Math.sqrt(sq / f32.length);
      this._lvl = this._lvl * 0.6 + rms * 0.4;
      const now = performance.now();
      if(now - this._lvlT >= 90){
        this._lvlT = now;
        this.onLevel(Math.min(1, this._lvl * 3.5));
      }
    }
  }

  async stop(){
    if(this._state !== 'recording') throw new Error('当前没有正在进行的录音');
    this._state = 'stopping';
    const chunks = this._chunks;
    this._chunks = null;
    await this._release();
    try{
      const fixed = this._rate !== TARGET_RATE ? resampleTo16k(chunks, this._rate) : chunks;
      const blob = buildWav(fixed);
      this._state = 'idle';
      return blob;
    }catch(e){
      this._state = 'error';
      this._error = '生成录音文件失败:' + errMsg(e, '未知错误');
      throw new Error(this._error);
    }
  }

  abort(){
    if(this._state === 'idle') return;
    this._chunks = null;
    this._startedAt = 0;
    this._state = 'idle';
    this._error = null;
    this._release();
  }

  async _release(){
    const node = this._node;
    this._node = null;
    if(node){
      if(node.port) node.port.onmessage = null;
      else node.onaudioprocess = null;
      try{ node.disconnect(); }catch(e){}
    }
    if(this._src){ try{ this._src.disconnect(); }catch(e){} this._src = null; }
    if(this._mute){ try{ this._mute.disconnect(); }catch(e){} this._mute = null; }
    const streams = this._streams;
    this._streams = [];
    for(let i = 0; i < streams.length; i++){
      const ts = streams[i].getTracks();
      for(let k = 0; k < ts.length; k++){ try{ ts[k].stop(); }catch(e){} }
    }
    const ctx = this._ctx;
    this._ctx = null;
    if(ctx){ try{ await ctx.close(); }catch(e){} }
  }
}

async function uploadRecording(blob, opt){
  opt = opt || {};
  const fd = new FormData();
  fd.append('file', blob, 'recording.wav');
  fd.append('title', opt.title || '');
  fd.append('speakers', opt.speakers == null ? '0' : String(opt.speakers));
  fd.append('language', opt.language || 'zh');
  fd.append('voiceprint', opt.voiceprint == null || opt.voiceprint === '1' ? '1' : '0');
  let r;
  try{
    r = await fetch('/api/jobs', {method: 'POST', body: fd});
  }catch(e){
    throw new Error('无法连接本地转录服务:' + errMsg(e, '网络错误'));
  }
  let j = null;
  try{ j = await r.json(); }catch(e){}
  if(!r.ok || !j || !j.id){
    throw new Error(j && j.error ? j.error : '录音上传失败(HTTP ' + r.status + ')');
  }
  return j;
}

window.MeetRecorder = {Recorder: Recorder, uploadRecording: uploadRecording};
})();
