'use strict';

const { contextBridge, ipcRenderer } = require('electron');

const listeners = new Map();

contextBridge.exposeInMainWorld('vivieen', Object.freeze({
  isElectron: true,
  getState: () => ipcRenderer.invoke('vivieen:get-state'),
  openSettings: () => ipcRenderer.invoke('vivieen:open-settings'),
  showAvatar: () => ipcRenderer.invoke('vivieen:show-main'),
  hideAvatar: () => ipcRenderer.invoke('vivieen:hide-main'),
  minimize: () => ipcRenderer.invoke('vivieen:minimize'),
  toggleAlwaysOnTop: () => ipcRenderer.invoke('vivieen:toggle-top'),
  avatarChanged: () => ipcRenderer.invoke('vivieen:avatar-changed'),
  restartBackend: () => ipcRenderer.invoke('vivieen:restart-backend'),
  onState: (callback) => {
    if (typeof callback !== 'function') return () => {};
    const wrapped = (_event, value) => callback(value);
    listeners.set(callback, wrapped);
    ipcRenderer.on('vivieen:state', wrapped);
    return () => {
      const active = listeners.get(callback);
      if (active) ipcRenderer.removeListener('vivieen:state', active);
      listeners.delete(callback);
    };
  },
}));
