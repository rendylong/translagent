'use strict';
const {contextBridge, ipcRenderer} = require('electron');

contextBridge.exposeInMainWorld('desk', {
  isDesktop: true,
  platform: process.platform,
  electronVersion: process.versions.electron,
  getScreenPermission: () => ipcRenderer.invoke('screen-permission'),
  diag: msg => ipcRenderer.invoke('rec-diag', msg),
  openPermissionGuide: () => ipcRenderer.invoke('open-permission-guide'),
  startDragApp: () => ipcRenderer.invoke('start-drag-app'),
  relaunchApp: () => ipcRenderer.invoke('relaunch-app'),
  openAppFolder: () => ipcRenderer.invoke('open-app-folder'),
  // 实时字幕:主界面入口拉起/聚焦悬浮窗(主窗口用);
  // 悬浮窗内页面用:resizeLive 自绘手柄缩放、openMainJob 结束卡回工作台
  openLiveCaption: () => ipcRenderer.invoke('open-live-caption'),
  resizeLive: (w, h) => ipcRenderer.invoke('resize-live', w, h),
  openMainJob: id => ipcRenderer.invoke('open-main-job', id),
  closeLiveCaption: () => ipcRenderer.invoke('hide-live')
});
