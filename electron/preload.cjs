'use strict';

const { contextBridge, ipcRenderer } = require('electron');

const listeners = new Map();

function subscribe(channel, callback) {
  if (typeof callback !== 'function') return () => {};
  const wrapped = (_event, value) => callback(value);
  let channelListeners = listeners.get(channel);
  if (!channelListeners) {
    channelListeners = new Map();
    listeners.set(channel, channelListeners);
  }
  channelListeners.set(callback, wrapped);
  ipcRenderer.on(channel, wrapped);
  return () => {
    const active = channelListeners.get(callback);
    if (active) ipcRenderer.removeListener(channel, active);
    channelListeners.delete(callback);
    if (channelListeners.size === 0) listeners.delete(channel);
  };
}

contextBridge.exposeInMainWorld('vivieen', Object.freeze({
  isElectron: true,
  getState: () => ipcRenderer.invoke('vivieen:get-state'),
  getEnconvoSamples: (afterSequence = 0) => (
    ipcRenderer.invoke('vivieen:get-enconvo-samples', Number(afterSequence) || 0)
  ),
  openSettings: () => ipcRenderer.invoke('vivieen:open-settings'),
  openAppearance: () => ipcRenderer.invoke('vivieen:open-appearance'),
  showAvatar: () => ipcRenderer.invoke('vivieen:show-main'),
  hideAvatar: () => ipcRenderer.invoke('vivieen:hide-main'),
  minimize: () => ipcRenderer.invoke('vivieen:minimize'),
  toggleAlwaysOnTop: () => ipcRenderer.invoke('vivieen:toggle-top'),
  showPetMenu: () => ipcRenderer.invoke('vivieen:pet-menu'),
  setPetView: (value) => ipcRenderer.invoke('vivieen:set-pet-view', String(value || '')),
  setPetOpacity: (value) => ipcRenderer.invoke('vivieen:set-pet-opacity', Number(value)),
  setPetZoom: (value) => ipcRenderer.invoke('vivieen:set-pet-zoom', Number(value)),
  setPetClickThrough: (value) => ipcRenderer.invoke('vivieen:set-pet-click-through', Boolean(value)),
  setPetLock: (value) => ipcRenderer.invoke('vivieen:set-pet-lock', Boolean(value)),
  setPetRoam: (value) => ipcRenderer.invoke('vivieen:set-pet-roam', Boolean(value)),
  setPetMotionReady: (value) => ipcRenderer.send('vivieen:pet-motion-ready', value),
  triggerEnconvoVoiceCommand: () => ipcRenderer.invoke('vivieen:trigger-enconvo-voice'),
  showSpeechBubble: (value) => ipcRenderer.send('vivieen:show-speech-bubble', String(value || '')),
  setPetEngaged: (value) => ipcRenderer.send('vivieen:pet-engaged', Boolean(value)),
  setPetHit: (value) => ipcRenderer.send('vivieen:pet-hit', Boolean(value)),
  beginPetDrag: (point) => ipcRenderer.send('vivieen:drag-start', point),
  movePetDrag: (point) => ipcRenderer.send('vivieen:drag-move', point),
  endPetDrag: () => ipcRenderer.send('vivieen:drag-end'),
  setEnconvoMonitor: (enabled) => ipcRenderer.invoke('vivieen:set-enconvo-monitor', Boolean(enabled)),
  avatarChanged: () => ipcRenderer.invoke('vivieen:avatar-changed'),
  restartBackend: () => ipcRenderer.invoke('vivieen:restart-backend'),
  saveMotionAsset: (asset) => ipcRenderer.invoke('vivieen:save-motion-asset', asset),
  onState: (callback) => subscribe('vivieen:state', callback),
  onEnconvoMonitor: (callback) => subscribe('vivieen:monitor-state', callback),
  onEnconvoAudio: (callback) => subscribe('vivieen:monitor-sample', callback),
  onPetChat: (callback) => subscribe('vivieen:pet-chat', callback),
  onPetPointer: (callback) => subscribe('vivieen:pet-pointer', callback),
  onPetRoamMotion: (callback) => subscribe('vivieen:pet-roam-motion', callback),
}));
