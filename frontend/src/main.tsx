import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import './index.css'
import { isNative, checkForAppUpdate, askNotificationPermission, registerPush } from './native'
import { getToken } from './auth'

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
)

checkForAppUpdate().finally(() => void askNotificationPermission());
void registerPush(getToken());   // notifications as real pushes (signed-in phone app only)

// Installable app (home screen). Only in the built app — in dev it would cache Vite's files.
if ('serviceWorker' in navigator && import.meta.env.PROD && !isNative) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js').catch((e) => console.warn('Service worker failed:', e));
  });
}
