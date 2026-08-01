'use strict';
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('vivieenIntro', {
  choose: (value) => ipcRenderer.send('vivieen:intro-choice', Number(value)),
});
