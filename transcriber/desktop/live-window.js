'use strict';
/* 实时字幕悬浮条窗口:加载 /live?mode=bar 的无边框置顶半透明小窗。
 * 设计约束:
 *  - 关闭只隐藏(hide),字幕会话(麦克风采集 + WS)不中断;仅 before-quit / 主窗关闭时销毁。
 *  - 页面(live.js)在窗内自采音频,隐藏时必须不节流,否则音频图停摆(与 main.js 录音窗同一硬要求)。
 *  - 页面自带的字幕条拖动只移动 DOM 元素,移不动无边框窗口——由注入的
 *    -webkit-app-region 接管为原生窗口拖动,窗口位置由主进程持久化。
 *  - 页面各阶段(setup 表单 / bar 字幕条 / panel 完整面板 / end 结束卡)高度差异大,
 *    用注入的 MutationObserver → document.title('live:<phase>') 通知主进程调窗尺寸,
 *    不改 static/ 一行。
 */
const {app, BrowserWindow, screen} = require('electron');
const fs = require('fs');
const path = require('path');

let liveWin = null;

/* 与 main.js 同款运行日志(launchd 拉起时 stdout 会丢) */
function log(){
  const line = new Date().toISOString().slice(11, 19) + ' [live-win] '
    + Array.from(arguments).join(' ');
  console.log('[desktop]', Array.from(arguments).join(' '));
  try{ fs.appendFileSync(path.join(app.getPath('userData'), 'main.log'), line + '\n'); }catch(e){}
}

/* ---------- 窗口位置记忆(userData/live-window.json,只记位置;尺寸随页面阶段走) ---------- */

function statePath(){
  return path.join(app.getPath('userData'), 'live-window.json');
}

function loadPos(){
  try{
    const p = JSON.parse(fs.readFileSync(statePath(), 'utf8'));
    const onScreen = screen.getAllDisplays().some(d =>
      p.x >= d.workArea.x && p.x < d.workArea.x + d.workArea.width &&
      p.y >= d.workArea.y && p.y < d.workArea.y + d.workArea.height);
    if(Number.isFinite(p.x) && Number.isFinite(p.y) && onScreen) return p;
  }catch(e){}
  return null;
}

function savePos(){
  if(!liveWin || liveWin.isDestroyed()) return;
  try{
    const [x, y] = liveWin.getPosition();
    fs.writeFileSync(statePath(), JSON.stringify({x, y}));
  }catch(e){}
}

/* ---------- 页面阶段 → 窗口尺寸档位 ---------- */

const PHASE_SIZE = {
  setup: [560, 500],   // 开始表单卡片
  bar:   [560, 150],   // 字幕条:操作按钮悬停在条上方 top:-34px,150 高才不裁
  panel: [920, 700],   // 完整面板(页面 .panel 名义 880×640)
  end:   [560, 450]    // 结束卡
};

function resizeForPhase(phase){
  if(!liveWin || liveWin.isDestroyed() || !PHASE_SIZE[phase]) return;
  const [w, h] = PHASE_SIZE[phase];
  const wa = screen.getDisplayMatching(liveWin.getBounds()).workArea;
  let [x, y] = liveWin.getPosition();
  if(y + h > wa.y + wa.height) y = Math.max(wa.y, wa.y + wa.height - h);  // 长大时别伸出屏幕底
  if(x + w > wa.x + wa.width) x = Math.max(wa.x, wa.x + wa.width - w);
  liveWin.setBounds({x, y, width: w, height: h});
}

/* ---------- 注入页面:悬浮形态 + 原生窗口拖动 ---------- */

const PAGE_CSS = `
  body{background:transparent !important}
  #bar{left:50% !important; top:auto !important; bottom:8px !important;
       transform:translateX(-50%) !important; -webkit-app-region:drag}
  #barActions, #barActions *{-webkit-app-region:no-drag}
  .phead{-webkit-app-region:drag}
  .phead *{-webkit-app-region:no-drag}
`;

/* 页面无主进程事件通道,借 document.title 桥接阶段变化 */
const PAGE_PHASE_WATCH = `
(function(){
  var prev = null;
  function phase(){
    var s = document.querySelector('#setup'),
        st = document.querySelector('#stage'),
        e  = document.querySelector('#endwrap');
    if (e && !e.hidden) return 'end';
    if (s && !s.hidden) return 'setup';
    if (st && !st.hidden) return document.querySelector('#bar').hidden ? 'panel' : 'bar';
    return 'setup';
  }
  function report(){
    var p = phase();
    if (p !== prev){ prev = p; document.title = 'live:' + p; }
  }
  new MutationObserver(report).observe(document.documentElement,
    {subtree: true, attributes: true, attributeFilter: ['hidden']});
  report();
})()`;

/* ---------- 窗口生命周期 ---------- */

function createLiveCaptionWindow(serverUrl, mainWin){
  if(liveWin && !liveWin.isDestroyed()) return liveWin;
  const wa = screen.getPrimaryDisplay().workArea;
  const saved = loadPos();
  liveWin = new BrowserWindow({
    x: saved ? saved.x : Math.round(wa.x + (wa.width - 560) / 2),
    y: saved ? saved.y : Math.round(wa.y + wa.height - 150),
    width: 560,
    height: 150,
    frame: false,
    alwaysOnTop: 'screen-saver',
    transparent: true,
    hasShadow: false,
    resizable: true,
    skipTaskbar: true,
    show: false,
    title: '实时字幕',
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      // 字幕条隐藏时页面仍持续采音,后台节流会停摆音频图 → 字幕静默断流
      backgroundThrottling: false
    }
  });
  liveWin.on('closed', () => { liveWin = null; });
  // 关闭 = 隐藏,会话不中断;真正销毁只走 destroyLiveWindow
  liveWin.on('close', e => {
    if(liveWin.__forceClose || liveWin.isDestroyed()) return;
    e.preventDefault();
    liveWin.hide();
  });
  liveWin.on('moved', savePos);
  liveWin.once('ready-to-show', () => liveWin.show());

  const wc = liveWin.webContents;
  wc.on('did-finish-load', () => {
    wc.insertCSS(PAGE_CSS).catch(e => log('insertCSS 失败', e && e.message));
    wc.executeJavaScript(PAGE_PHASE_WATCH, true)
      .catch(e => log('阶段监视注入失败', e && e.message));
  });
  wc.on('did-fail-load', (e, code, desc, url, isMain) => {
    if(!isMain) return;
    log('live 页面加载失败', code, String(desc), '—— 3 秒后重试');
    setTimeout(() => {
      if(liveWin && !liveWin.isDestroyed()) liveWin.webContents.reload();
    }, 3000);
  });
  liveWin.on('page-title-updated', (e, title) => {
    if(title.startsWith('live:')) resizeForPhase(title.slice(5));
  });

  liveWin.loadURL(serverUrl + '/live?mode=bar')
    .catch(e => log('loadURL 失败', e && e.message));

  // 主窗关闭即应用退出:销毁悬浮窗,否则隐藏窗口会卡住 window-all-closed(Dock 残留)
  if(mainWin && !mainWin.isDestroyed() && !mainWin.__liveWinHooked){
    mainWin.__liveWinHooked = true;
    mainWin.once('closed', () => destroyLiveWindow());
  }
  return liveWin;
}

function toggleLiveCaptionWindow(serverUrl, mainWin){
  if(!liveWin || liveWin.isDestroyed()){
    createLiveCaptionWindow(serverUrl, mainWin);
    return;
  }
  if(liveWin.isVisible()) liveWin.hide();
  else { liveWin.show(); liveWin.focus(); }
}

function destroyLiveWindow(){
  if(liveWin && !liveWin.isDestroyed()){
    liveWin.__forceClose = true;
    liveWin.destroy();
  }
  liveWin = null;
}

/* Dock 菜单入口:右键 Dock 图标 → 实时字幕悬浮条(main.js 原无菜单结构,这是最小侵入的挂载点) */
function installDockMenu(serverUrl, getMainWin){
  try{
    const {Menu} = require('electron');
    app.dock.setMenu(Menu.buildFromTemplate([
      {label: '实时字幕悬浮条', click: () => toggleLiveCaptionWindow(serverUrl, getMainWin())}
    ]));
    return true;
  }catch(e){
    log('Dock 菜单注册失败', e && e.message);
    return false;
  }
}

app.on('before-quit', destroyLiveWindow);

module.exports = {createLiveCaptionWindow, toggleLiveCaptionWindow, destroyLiveWindow, installDockMenu};
