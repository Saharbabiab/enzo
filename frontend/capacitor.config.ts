import type { CapacitorConfig } from '@capacitor/cli';

// The Android app is a thin shell around the Enzo web app: it loads the live site, so web updates
// reach the phone without a new APK. The native layer adds what a browser can't: speech recognition,
// contacts and calling. webDir is only the offline fallback.
const config: CapacitorConfig = {
  appId: 'com.enzo.assistant',
  appName: 'Enzo',
  webDir: 'dist',
  server: {
    url: 'https://enzo.example.com:4444',   // your Enzo server's HTTPS address
    androidScheme: 'https',
    cleartext: false,
  },
  android: {
    allowMixedContent: false,
    backgroundColor: '#0a0a0f',
  },
};

export default config;
