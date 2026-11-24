/// <reference types="vite/client" />

interface ImportMetaEnv {
    /** Optional override; by default the app uses ws(s)://<its own host>/ws */
    readonly VITE_WS_URL?: string;
}

interface ImportMeta {
    readonly env: ImportMetaEnv;
}
